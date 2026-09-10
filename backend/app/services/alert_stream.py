"""把收盘K线快照实时推送给当前打开的用户页面。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.services.market_sync import MarketSyncEvent
from app.services.repository import ConfigRepository

logger = logging.getLogger(__name__)
_CHINA_TZ = timezone(timedelta(hours=8))
_ROLLOVER_COALESCE_SECONDS = 0.15

AlertScanner = Callable[[ConfigRepository, str, frozenset[int]], dict[str, object]]


@dataclass
class AlertSubscriber:
    id: str
    user_id: str
    durations: frozenset[int] = field(default_factory=frozenset)
    queue: asyncio.Queue[dict[str, object]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1)
    )


class AlertStreamService:
    """维护网页订阅，并将同一轮多品种收K合并成一条消息。"""

    def __init__(self, repository: ConfigRepository, scanner: AlertScanner) -> None:
        self._repository = repository
        self._scanner = scanner
        self._subscribers: dict[str, AlertSubscriber] = {}
        self._pending_sources: dict[tuple[str, int], int] = {}
        self._pending_received_at_ns = 0
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    def register(self, user_id: str) -> AlertSubscriber:
        subscriber = AlertSubscriber(id=uuid4().hex, user_id=user_id)
        self._subscribers[subscriber.id] = subscriber
        logger.info("预警实时连接已建立: user=%s connections=%d", user_id, len(self._subscribers))
        return subscriber

    async def unregister(self, subscriber_id: str) -> None:
        subscriber = self._subscribers.pop(subscriber_id, None)
        if subscriber is not None:
            logger.info(
                "预警实时连接已关闭: user=%s connections=%d",
                subscriber.user_id,
                len(self._subscribers),
            )

    async def update(self, subscriber_id: str, durations: frozenset[int]) -> None:
        subscriber = self._subscribers.get(subscriber_id)
        if subscriber is None:
            return
        subscriber.durations = durations
        payload = await asyncio.to_thread(
            self._scanner,
            self._repository,
            subscriber.user_id,
            durations,
        )
        logger.info(
            "预警订阅已更新: user=%s periods=%s requested=%s signals=%s failed=%s",
            subscriber.user_id,
            ",".join(str(value) for value in sorted(durations)) or "none",
            payload.get("requested", 0),
            len(payload.get("signals", [])),
            payload.get("failed", 0),
        )
        self._offer(subscriber, self._message(payload, "initial", None))

    def offer_control(self, subscriber_id: str, event_type: str) -> None:
        subscriber = self._subscribers.get(subscriber_id)
        if subscriber is not None:
            self._offer(subscriber, {"type": event_type})

    async def publish_rollovers(self, event: MarketSyncEvent) -> None:
        """聚合同一收盘时刻分批到达的多个品种，避免重复扫描。"""
        if self._closed or not self._subscribers:
            return
        self._pending_sources.update(event.changed_sources)
        self._pending_received_at_ns = max(
            self._pending_received_at_ns,
            event.received_at_ns,
        )
        if self._flush_task is None or self._flush_task.done():
            self._start_flush()

    def _start_flush(self) -> None:
        self._flush_task = asyncio.create_task(self._flush_after_delay())
        self._flush_task.add_done_callback(self._flush_finished)

    def _flush_finished(self, task: asyncio.Task[None]) -> None:
        if self._flush_task is task:
            self._flush_task = None
        if not task.cancelled() and task.exception() is not None:
            logger.error("收盘预警推送任务异常: %s", task.exception())
        if self._pending_sources and not self._closed:
            self._start_flush()

    async def close(self) -> None:
        self._closed = True
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        self._subscribers.clear()

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(_ROLLOVER_COALESCE_SECONDS)
        changed_sources = dict(self._pending_sources)
        received_at_ns = self._pending_received_at_ns
        self._pending_sources.clear()
        self._pending_received_at_ns = 0

        groups: dict[tuple[str, frozenset[int]], list[str]] = {}
        for subscriber in self._subscribers.values():
            if subscriber.durations:
                groups.setdefault(
                    (subscriber.user_id, subscriber.durations),
                    [],
                ).append(subscriber.id)
        if not groups:
            return

        keys = list(groups)
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self._scanner,
                    self._repository,
                    user_id,
                    durations,
                )
                for user_id, durations in keys
            ),
            return_exceptions=True,
        )
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "收盘预警快照计算失败: user=%s error=%s",
                    key[0],
                    result,
                )
                continue
            message = self._message(result, "bar_closed", received_at_ns)
            for subscriber_id in groups[key]:
                subscriber = self._subscribers.get(subscriber_id)
                if subscriber is not None and subscriber.durations == key[1]:
                    self._offer(subscriber, message)
        logger.info(
            "收盘预警已推送: 基础序列=%d 用户组合=%d",
            len(changed_sources),
            len(groups),
        )

    @staticmethod
    def _message(
        payload: dict[str, object],
        trigger: str,
        received_at_ns: int | None,
    ) -> dict[str, object]:
        result = {
            "type": "snapshot",
            "trigger": trigger,
            **payload,
            "server_sent_at": datetime.now(_CHINA_TZ).isoformat(),
        }
        if received_at_ns is not None:
            result["source_received_at"] = datetime.fromtimestamp(
                received_at_ns / 1_000_000_000,
                tz=_CHINA_TZ,
            ).isoformat()
        return result

    @staticmethod
    def _offer(subscriber: AlertSubscriber, message: dict[str, object]) -> None:
        if subscriber.queue.full():
            try:
                subscriber.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        subscriber.queue.put_nowait(message)
