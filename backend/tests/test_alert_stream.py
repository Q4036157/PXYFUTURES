import asyncio

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
