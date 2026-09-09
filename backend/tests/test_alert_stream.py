import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import WebSocket
from jose import jwt

from app.api.signals import stream_alerts
from app.config import settings
from app.services.alert_stream import AlertStreamService
from app.services.market_sync import MarketSyncEvent


def test_rollovers_are_coalesced_and_scanned_once_per_user_selection() -> None:
    calls: list[tuple[str, frozenset[int]]] = []

    def scanner(
        _repository: object,
        user_id: str,
        durations: frozenset[int],
    ) -> dict[str, object]:
        calls.append((user_id, durations))
        return {"signals": [], "requested": 1, "failed": 0}

    async def run() -> None:
        service = AlertStreamService(object(), scanner)  # type: ignore[arg-type]
        first = service.register("user-1")
        second = service.register("user-1")
        await service.update(first.id, frozenset({180}))
        await service.update(second.id, frozenset({180}))
        assert (await first.queue.get())["trigger"] == "initial"
        assert (await second.queue.get())["trigger"] == "initial"
        calls.clear()

        await service.publish_rollovers(
            MarketSyncEvent({("CZCE.UR701", 60): 100}, 200)
        )
        await service.publish_rollovers(
            MarketSyncEvent({("DCE.PP2701", 60): 101}, 201)
        )
        first_message, second_message = await asyncio.gather(
            first.queue.get(),
            second.queue.get(),
        )

        assert first_message["trigger"] == "bar_closed"
        assert second_message["trigger"] == "bar_closed"
        assert first_message["source_received_at"] == second_message["source_received_at"]
        assert calls == [("user-1", frozenset({180}))]
        await service.close()

    asyncio.run(run())


def test_websocket_authenticates_cookie_and_sends_initial_snapshot(monkeypatch) -> None:
    secret = "test-app-session-secret"
    monkeypatch.setattr(settings, "app_session_secret", secret)
    token = jwt.encode(
        {
            "sub": "user-42",
            "app_id": "futures",
            "type": "app_session",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        secret,
        algorithm="HS256",
    )
    calls: list[tuple[str, frozenset[int]]] = []

    def scanner(
        _repository: object,
        user_id: str,
        durations: frozenset[int],
    ) -> dict[str, object]:
        calls.append((user_id, durations))
        return {"signals": [], "requested": 1, "failed": 0}

    async def run() -> None:
        incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        sent: list[dict[str, object]] = []
        service = AlertStreamService(object(), scanner)  # type: ignore[arg-type]
        app = SimpleNamespace(state=SimpleNamespace(alert_stream=service))

        async def receive() -> dict[str, object]:
            return await incoming.get()

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": "/api/contracts/alerts/ws",
            "headers": [(b"cookie", f"pxy_futures_session={token}".encode())],
            "app": app,
        }
        websocket = WebSocket(scope, receive=receive, send=send)  # type: ignore[arg-type]
        await incoming.put({"type": "websocket.connect"})
        task = asyncio.create_task(stream_alerts(websocket))

        for _ in range(100):
            if len(sent) >= 2:
                break
            await asyncio.sleep(0.01)
        assert sent[0]["type"] == "websocket.accept"
        assert json.loads(str(sent[1]["text"])) == {"type": "connected"}

        await incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "subscribe", "durations": [180, 300]}),
            }
        )
        for _ in range(100):
            if len(sent) >= 3:
                break
            await asyncio.sleep(0.01)
        message = json.loads(str(sent[2]["text"]))
        assert message["type"] == "snapshot"
        assert message["trigger"] == "initial"
        assert calls == [("user-42", frozenset({180, 300}))]

        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=1)
        assert not service._subscribers

    asyncio.run(run())


def test_websocket_rejects_missing_app_session() -> None:
    async def run() -> None:
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        websocket = WebSocket(
            {"type": "websocket", "path": "/api/contracts/alerts/ws", "headers": []},
            receive=receive,  # type: ignore[arg-type]
            send=send,  # type: ignore[arg-type]
        )
        await stream_alerts(websocket)
        assert sent == [
            {
                "type": "websocket.close",
                "code": 4401,
                "reason": "主平台登录已失效",
            }
        ]

    asyncio.run(run())


def test_desktop_websocket_accepts_loopback_without_cookie(monkeypatch) -> None:
    monkeypatch.setattr(settings, "desktop_mode", True)
    calls: list[tuple[str, frozenset[int]]] = []

    def scanner(
        _repository: object,
        user_id: str,
        durations: frozenset[int],
    ) -> dict[str, object]:
        calls.append((user_id, durations))
        return {"signals": [], "requested": 1, "failed": 0}

    async def run() -> None:
        incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        sent: list[dict[str, object]] = []
        service = AlertStreamService(object(), scanner)  # type: ignore[arg-type]
        app = SimpleNamespace(state=SimpleNamespace(alert_stream=service))

        async def receive() -> dict[str, object]:
            return await incoming.get()

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        websocket = WebSocket(
            {
                "type": "websocket",
                "path": "/api/contracts/alerts/ws",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "app": app,
            },
            receive=receive,  # type: ignore[arg-type]
            send=send,  # type: ignore[arg-type]
        )
        await incoming.put({"type": "websocket.connect"})
        task = asyncio.create_task(stream_alerts(websocket))

        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
        assert sent[0]["type"] == "websocket.accept"

        await incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "subscribe", "durations": [180]}),
            }
        )
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls == [("desktop-local-user", frozenset({180}))]

        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=1)
        await service.close()

    asyncio.run(run())


def test_rollover_arriving_during_scan_is_flushed_next() -> None:
    calls = 0
    first_scan_started = threading.Event()
    release_first_scan = threading.Event()

    def scanner(
        _repository: object,
        _user_id: str,
        _durations: frozenset[int],
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            first_scan_started.set()
            assert release_first_scan.wait(timeout=2)
        return {"signals": [], "requested": calls, "failed": 0}

    async def wait_for_thread_event(event: threading.Event) -> None:
        for _ in range(100):
            if event.is_set():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("扫描线程未按时启动")

    async def run() -> None:
        service = AlertStreamService(object(), scanner)  # type: ignore[arg-type]
        subscriber = service.register("user-1")
        await service.update(subscriber.id, frozenset({180}))
        await subscriber.queue.get()

        await service.publish_rollovers(
            MarketSyncEvent({("CZCE.UR701", 60): 100}, 200)
        )
        await wait_for_thread_event(first_scan_started)
        await service.publish_rollovers(
            MarketSyncEvent({("DCE.PP2701", 60): 101}, 201)
        )
        release_first_scan.set()

        first_message = await asyncio.wait_for(subscriber.queue.get(), timeout=2)
        second_message = await asyncio.wait_for(subscriber.queue.get(), timeout=2)
        assert first_message["trigger"] == "bar_closed"
        assert second_message["trigger"] == "bar_closed"
        assert calls == 3
        await service.close()

    asyncio.run(run())
