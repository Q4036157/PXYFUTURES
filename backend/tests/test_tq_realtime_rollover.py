from dataclasses import dataclass

from app.market.tq_client import TqClient
from app.services.market_sync import MarketSyncJob


@dataclass
class Row:
    datetime: int
    close: float


class Frame:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows

    def tail(self, count: int) -> "Frame":
        return Frame(self.rows[-count:])

    def itertuples(self, index: bool = False):
        assert index is False
        return iter(self.rows)


class Repository:
    def __init__(self) -> None:
        self.saved: list[tuple[str, int, list[tuple[int, float]], int, int]] = []
        self.batch_writes = 0

    def save_kline_bars(
        self,
        symbol: str,
        duration_seconds: int,
        bars: list[tuple[int, float]],
        requested_count: int,
        synced_at_ns: int,
    ) -> None:
        self.saved.append(
            (symbol, duration_seconds, bars, requested_count, synced_at_ns)
        )

    def save_kline_bar_batches(
        self,
        batches: list[tuple[str, int, list[tuple[int, float]], int]],
        synced_at_ns: int,
    ) -> None:
        self.batch_writes += 1
        for symbol, duration_seconds, bars, requested_count in batches:
            self.save_kline_bars(
                symbol,
                duration_seconds,
                bars,
                requested_count,
                synced_at_ns,
            )


def test_wait_update_emits_only_after_new_bar_timestamp() -> None:
    frame = Frame([Row(100, 10.0), Row(200, 11.0)])

    class Api:
        @staticmethod
        def get_kline_serial(*_args: object, **_kwargs: object) -> Frame:
            return frame

        @staticmethod
        def wait_update(*_args: object, **_kwargs: object) -> bool:
            frame.rows.append(Row(300, 12.0))
            return True

    client = TqClient()
    client._api = Api()
    repository = Repository()

    rollover = client.wait_for_bar_rollovers(
        [MarketSyncJob("CZCE.UR701", 60, 1503)],
        repository,  # type: ignore[arg-type]
        0.01,
    )

    assert rollover.changed_sources == {("CZCE.UR701", 60): 300}
    assert repository.batch_writes == 1
    assert repository.saved[0][2] == [(100, 10.0), (200, 11.0)]
    assert repository.saved[1][2] == [(100, 10.0), (200, 11.0), (300, 12.0)]


def test_wait_update_ignores_price_change_inside_same_bar() -> None:
    frame = Frame([Row(100, 10.0), Row(200, 11.0)])

    class Api:
        @staticmethod
        def get_kline_serial(*_args: object, **_kwargs: object) -> Frame:
            return frame

        @staticmethod
        def wait_update(*_args: object, **_kwargs: object) -> bool:
            frame.rows[-1] = Row(200, 12.0)
            return True

    client = TqClient()
    client._api = Api()

    rollover = client.wait_for_bar_rollovers(
        [MarketSyncJob("CZCE.UR701", 60, 1503)],
        Repository(),  # type: ignore[arg-type]
        0.01,
    )

    assert rollover.changed_sources == {}
