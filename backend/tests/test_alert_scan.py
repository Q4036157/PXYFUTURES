import asyncio
from types import SimpleNamespace

import pytest

from app.api import signals
from app.services.repository import ContractConfig, PeriodConfig


def _period(duration_seconds: int, label: str) -> PeriodConfig:
    return PeriodConfig(
        id=duration_seconds,
        label=label,
        duration_seconds=duration_seconds,
        m4=5,
        m3=4,
        m2=3,
        m1=1,
    )


class StubRepository:
    requested_user_id: str | None = None

    def list_contracts(self, user_id: str) -> list[ContractConfig]:
        self.requested_user_id = user_id
        return [
            ContractConfig(
                id=7,
                symbol="CZCE.SR701",
                name="SR701",
                periods=[_period(1_800, "30分钟"), _period(900, "15分钟")],
            )
        ]


def test_alert_scan_uses_current_user_and_selected_configured_periods(monkeypatch) -> None:
    repository = StubRepository()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config_repository=repository)))

    def fake_scan(repo, jobs):
        assert repo is repository
        assert [(contract.symbol, period.duration_seconds) for contract, period in jobs] == [
            ("CZCE.SR701", 1_800)
        ]
        return [
            {
                "contract_id": 7,
                "symbol": "CZCE.SR701",
                "name": "SR701",
                "period": _period(1_800, "30分钟").__dict__,
            }
        ], 0

    monkeypatch.setattr(signals, "_scan_cached_alert_jobs", fake_scan)

    result = asyncio.run(
        signals.scan_alerts(
            signals.AlertScanBody(durations=[1_800, 3_600]),
            request,
            user_id="pxylh-user-42",
        )
    )

    assert repository.requested_user_id == "pxylh-user-42"
    assert result["requested"] == 1
    assert result["failed"] == 0
    assert result["signals"] == [
        {
            "contract_id": 7,
            "symbol": "CZCE.SR701",
            "name": "SR701",
            "period": _period(1_800, "30分钟").__dict__,
        }
    ]


def test_one_minute_is_an_accepted_alert_period() -> None:
    body = signals.AlertScanBody(durations=[60])

    assert body.durations == [60]


@pytest.mark.parametrize(("label", "position", "expected"), [
    ("上涨", "above", "顺多"),
    ("反弹", "above", "顺多"),
    ("多震荡", "above", "顺多"),
    ("下跌", "above", "逆空"),
    ("回调", "above", "逆空"),
    ("空震荡", "above", "逆空"),
    ("上涨", "below", "逆多"),
    ("反弹", "below", "逆多"),
    ("多震荡", "below", "逆多"),
    ("下跌", "below", "顺空"),
    ("回调", "below", "顺空"),
    ("空震荡", "below", "顺空"),
    ("上涨", None, None),
])
def test_daily_m4_alignment_maps_raw_state_to_direction(
    label: str, position: str | None, expected: str | None,
) -> None:
    assert signals.alignment_state(label, position) == expected
