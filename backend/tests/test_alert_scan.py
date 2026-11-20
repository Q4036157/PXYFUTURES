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


@pytest.mark.parametrize(("group_id", "expected"), [(None, [1, 2]), (21, [1]), (22, [2]), (99, [])])
def test_alert_scan_limits_calculation_to_the_selected_user_group(monkeypatch, group_id, expected):
    def contracts(user_id):
        assert user_id == "current-user"
        return [
            ContractConfig(1, "DCE.I2701", "铁矿", [_period(1_800, "30分钟")], group_id=21),
            ContractConfig(2, "CZCE.AP705", "苹果", [_period(1_800, "30分钟")], group_id=22),
        ]

    def scan(_repository, jobs):
        assert [contract.id for contract, _period_config in jobs] == expected
        return [{"contract_id": contract.id} for contract, _period_config in jobs], 0

    repository = SimpleNamespace(list_contracts=contracts)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config_repository=repository)))
    monkeypatch.setattr(signals, "_scan_cached_alert_jobs", scan)
    result = asyncio.run(signals.scan_alerts(
        signals.AlertScanBody(durations=[1_800], group_id=group_id), request, "current-user",
    ))
    assert result == {"signals": [{"contract_id": item} for item in expected], "requested": len(expected), "failed": 0}


def test_confirmed_contract_preview_uses_all_configured_periods_without_live_market(monkeypatch):
    periods = [_period(86_400, "日线"), _period(14_400, "4小时"), _period(600, "10分钟")]
    contract = ContractConfig(7, "CZCE.CF705", "棉花", periods)

    def get_contract(user_id, contract_id):
        assert (user_id, contract_id) == ("current-user", contract.id)
        return contract

    def scan(_repository, jobs, *, include_unclassified):
        assert include_unclassified
        assert jobs == [(contract, period) for period in periods]
        return [{"period": periods[1].__dict__, "cross_type": "金叉", "label": "反弹"}], 2

    repository = SimpleNamespace(get_contract=get_contract)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        config_repository=repository, tq_client=object(),
    )))
    monkeypatch.setattr(signals, "_scan_cached_alert_jobs", scan)
    result = asyncio.run(signals.get_signals(7, request, "current-user", confirmed_only=True))
    assert result["symbol"] == "CZCE.CF705"
    assert result["signals"] == [{"period": periods[1].__dict__, "cross_type": "金叉", "label": "反弹"}]
    assert result["warnings"] == ["部分配置周期暂无可用的收盘状态"]


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
