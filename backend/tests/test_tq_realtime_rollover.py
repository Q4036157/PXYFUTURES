from dataclasses import dataclass
from time import time_ns

from app.market.tq_client import TqClient
from app.services.market_sync import MarketSyncJob
from app.services.repository import KlineSyncState


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
        self.sync_state: KlineSyncState | None = None

    def get_kline_sync_state(
        self,
        _symbol: str,
        _duration_seconds: int,
    ) -> KlineSyncState | None:
        return self.sync_state

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
    assert repository.saved[-1][2] == [(100, 10.0), (200, 11.0), (300, 12.0)]


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


def test_live_rollover_reuses_short_incremental_window_after_history_is_cached() -> None:
    frame = Frame([Row(100, 10.0), Row(200, 11.0)])
    requested_lengths: list[int] = []

    class Api:
        @staticmethod
        def get_kline_serial(
            _symbol: str,
            *,
            duration_seconds: int,
            data_length: int,
        ) -> Frame:
            assert duration_seconds == 60
            requested_lengths.append(data_length)
            return frame

        @staticmethod
        def wait_update(*_args: object, **_kwargs: object) -> bool:
            frame.rows.append(Row(300, 12.0))
            return True

    client = TqClient()
    client._api = Api()
    repository = Repository()
    repository.sync_state = KlineSyncState(
        max_requested_bars=1503,
        last_synced_at_ns=time_ns(),
    )

    rollover = client.wait_for_bar_rollovers(
        [MarketSyncJob("CZCE.UR701", 60, 1503)],
        repository,  # type: ignore[arg-type]
        0.01,
    )

    assert requested_lengths == [10]
    assert rollover.changed_sources == {("CZCE.UR701", 60): 300}
    assert repository.saved[0][3] == 10


def test_cold_start_backfills_history_then_listens_on_short_window() -> None:
    history_frame = Frame([Row(100, 10.0), Row(200, 11.0)])
    live_frame = Frame([Row(100, 10.0), Row(200, 11.0)])
    requested_lengths: list[int] = []

    class Api:
        @staticmethod
        def get_kline_serial(
            _symbol: str,
            *,
            duration_seconds: int,
            data_length: int,
        ) -> Frame:
            assert duration_seconds == 60
            requested_lengths.append(data_length)
            return live_frame if data_length == 10 else history_frame

        @staticmethod
        def wait_update(*_args: object, **_kwargs: object) -> bool:
            live_frame.rows.append(Row(300, 12.0))
            return True

    client = TqClient()
    client._api = Api()
    repository = Repository()

    rollover = client.wait_for_bar_rollovers(
        [MarketSyncJob("CZCE.UR701", 60, 1503)],
        repository,  # type: ignore[arg-type]
        0.01,
    )

    assert requested_lengths == [1503, 10]
    assert rollover.changed_sources == {("CZCE.UR701", 60): 300}
    assert repository.saved[0][3] == 1503


def test_credentials_change_rebuilds_live_series_on_new_api() -> None:
    first_frame = Frame([Row(100, 10.0), Row(200, 11.0)])
    second_frame = Frame([Row(100, 10.0), Row(200, 11.0)])

    class FirstApi:
        closed = False

        @staticmethod
        def get_kline_serial(*_args: object, **_kwargs: object) -> Frame:
            return first_frame

        @staticmethod
        def wait_update(*_args: object, **_kwargs: object) -> bool:
            return False

        @classmethod
        def close(cls) -> None:
            cls.closed = True

    class SecondApi:
        requested = 0

        @classmethod
        def get_kline_serial(cls, *_args: object, **_kwargs: object) -> Frame:
            cls.requested += 1
            return second_frame

        @staticmethod
        def wait_update(*_args: object, **_kwargs: object) -> bool:
            second_frame.rows.append(Row(300, 12.0))
            return True

    client = TqClient()
    client._api = FirstApi()
    repository = Repository()
    job = MarketSyncJob("CZCE.UR701", 60, 10)
    client.wait_for_bar_rollovers([job], repository, 0.01)  # type: ignore[arg-type]

    client.credentials_changed()
    client._api = SecondApi()
    rollover = client.wait_for_bar_rollovers(
        [job],
        repository,  # type: ignore[arg-type]
        0.01,
    )

    assert FirstApi.closed
    assert SecondApi.requested == 1
    assert rollover.changed_sources == {("CZCE.UR701", 60): 300}
