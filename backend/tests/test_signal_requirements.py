import pytest

from app.api.signals import _snapshot
from app.market.ma_engine import Bar
from app.market.tq_client import MarketBars
from app.services.repository import PeriodConfig


class StubClient:
    def fetch_bars(self, symbol: str, duration_seconds: int, count: int, repository: object) -> MarketBars:
        return MarketBars([Bar(index, 100.0) for index in range(197)], "cache", 1)


def test_insufficient_history_explains_required_period() -> None:
    period = PeriodConfig(id=1, label="日线", duration_seconds=86400, m4=120, m3=200, m2=30, m1=10)
    with pytest.raises(ValueError, match="当前只有 197 根有效 K 线.*至少需要 201 根"):
        _snapshot(StubClient(), object(), "DCE.V2609", period)


class CrossingClient:
    def fetch_bars(self, symbol: str, duration_seconds: int, count: int, repository: object) -> MarketBars:
        closes = [10, 9, 8, 7, 6, 7, 8]
        return MarketBars(
            [Bar(index * 1_000_000_000, float(close)) for index, close in enumerate(closes)],
            "cache",
            1,
        )


def test_snapshot_exposes_cross_and_confirmed_bar_times() -> None:
    period = PeriodConfig(id=1, label="30分钟", duration_seconds=1800, m4=5, m3=4, m2=3, m1=1)

    snapshot = _snapshot(CrossingClient(), object(), "CZCE.SR701", period)

    assert snapshot["cross_time"] == "1970-01-01T08:00:05+08:00"
    assert snapshot["confirmed_bar_time"] == "1970-01-01T08:00:05+08:00"
