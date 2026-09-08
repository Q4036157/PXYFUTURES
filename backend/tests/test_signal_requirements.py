import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.api import signals
from app.api.signals import _snapshot, _snapshot_from_market_bars
from app.market.ma_engine import Bar
from app.market.tq_client import MarketBars
from app.services.repository import ContractConfig, PeriodConfig


class StubClient:
    def fetch_bars(
        self, symbol: str, duration_seconds: int, count: int, repository: object
    ) -> MarketBars:
        return MarketBars([Bar(index, 100.0) for index in range(197)], "cache", 1)


def test_insufficient_history_explains_required_period() -> None:
    period = PeriodConfig(id=1, label="日线", duration_seconds=86400, m4=120, m3=200, m2=30, m1=10)
    with pytest.raises(ValueError, match="当前只有 197 根有效 K 线.*至少需要 201 根"):
        _snapshot(StubClient(), object(), "DCE.V2609", period)


class CrossingClient:
    def fetch_bars(
        self, symbol: str, duration_seconds: int, count: int, repository: object
    ) -> MarketBars:
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
    assert snapshot["confirmed_at"] == "1970-01-01T08:00:06+08:00"


def test_alert_snapshot_ignores_entire_in_progress_bar() -> None:
    period = PeriodConfig(id=1, label="3分钟", duration_seconds=180, m4=5, m3=4, m2=3, m1=1)
    confirmed_closes = [10, 9, 8, 7, 6, 7, 8]

    low_live = MarketBars(
        [Bar(index * 1_000_000_000, float(close)) for index, close in enumerate([*confirmed_closes, 1])],
        "cache",
        1,
    )
    high_live = MarketBars(
        [Bar(index * 1_000_000_000, float(close)) for index, close in enumerate([*confirmed_closes, 100])],
        "cache",
        1,
    )

    low_snapshot = _snapshot_from_market_bars("DCE.B2701", period, low_live, confirmed_only=True)
    high_snapshot = _snapshot_from_market_bars("DCE.B2701", period, high_live, confirmed_only=True)

    stable_fields = (
        "trend",
        "cross_type",
        "cross_time",
        "label",
        "state_since",
        "ma_values",
        "bar_close",
        "bar_time",
        "confirmed_bar_time",
        "confirmed_at",
    )
    assert {field: low_snapshot[field] for field in stable_fields} == {
        field: high_snapshot[field] for field in stable_fields
    }
    assert low_snapshot["bar_close"] == 8
    assert low_snapshot["bar_time"] == "1970-01-01T08:00:06+08:00"
    assert low_snapshot["confirmed_bar_time"] == low_snapshot["bar_time"]
    assert low_snapshot["confirmed_at"] == "1970-01-01T08:00:07+08:00"


class MixedHistoryClient:
    def fetch_bars(
        self,
        symbol: str,
        duration_seconds: int,
        count: int,
        repository: object,
    ) -> MarketBars:
        bar_count = 59 if duration_seconds == 86_400 else 20
        return MarketBars([Bar(index, 100.0) for index in range(bar_count)], "cache", 1)


class SingleContractRepository:
    contract = ContractConfig(
        id=88,
        symbol="SHFE.AU2609",
        name="AU2609",
        periods=[
            PeriodConfig(
                id=1,
                label="日线",
                duration_seconds=86_400,
                m4=120,
                m3=60,
                m2=21,
                m1=4,
            ),
            PeriodConfig(
                id=2,
                label="5分钟",
                duration_seconds=300,
                m4=5,
                m3=4,
                m2=3,
                m1=1,
            ),
        ],
    )

    def get_contract(self, user_id: str, contract_id: int) -> ContractConfig | None:
        return self.contract if contract_id == self.contract.id else None


def test_signal_endpoint_returns_available_periods_and_logs_shortage_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    signals._signal_requirement_notices.clear()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config_repository=SingleContractRepository(),
                tq_client=MixedHistoryClient(),
            )
        )
    )

    with caplog.at_level(logging.WARNING, logger="app.api.signals"):
        first = asyncio.run(signals.get_signals(88, request, user_id="user-1"))
        second = asyncio.run(signals.get_signals(88, request, user_id="user-1"))

    assert len(first["signals"]) == 1
    assert len(first["warnings"]) == 1
    assert "当前只有 59 根有效 K 线" in first["warnings"][0]
    assert second["warnings"] == first["warnings"]
    warning_records = [
        record for record in caplog.records if "均线计算条件不满足" in record.getMessage()
    ]
    assert len(warning_records) == 1
