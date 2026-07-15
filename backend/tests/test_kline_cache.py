from app.config import settings
from app.services.repository import ConfigRepository, PeriodConfig


def test_kline_cache_persists_history_and_updates_the_current_bar(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()

    repository.save_kline_bars(
        "DCE.V2609",
        86400,
        [(1, 100.0), (2, 101.0), (3, 102.0)],
        requested_count=300,
        synced_at_ns=1000,
    )
    repository.save_kline_bars(
        "DCE.V2609",
        86400,
        [(3, 103.5), (4, 104.0)],
        requested_count=10,
        synced_at_ns=2000,
    )

    assert repository.load_kline_bars("DCE.V2609", 86400, 10) == [
        (1, 100.0),
        (2, 101.0),
        (3, 103.5),
        (4, 104.0),
    ]
    state = repository.get_kline_sync_state("DCE.V2609", 86400)
    assert state is not None
    assert state.max_requested_bars == 300
    assert state.last_synced_at_ns == 2000


def test_updating_contract_symbol_preserves_all_periods(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()
    contract = repository.create_contract("local", "DCE.PP2609", "PP2609")
    repository.save_period(
        "local",
        contract.id,
        PeriodConfig(id=None, label="日线", duration_seconds=86_400, m4=180, m3=60, m2=21, m1=4),
    )
    repository.save_period(
        "local",
        contract.id,
        PeriodConfig(id=None, label="2小时", duration_seconds=7_200, m4=120, m3=60, m2=20, m1=5),
    )

    updated = repository.update_contract("local", contract.id, "DCE.PP2701", "PP2701")

    assert updated is not None
    assert updated.id == contract.id
    assert updated.symbol == "DCE.PP2701"
    assert [(period.label, period.m4, period.m3, period.m2, period.m1) for period in updated.periods] == [
        ("日线", 180, 60, 21, 4),
        ("2小时", 120, 60, 20, 5),
    ]

    assert repository.save_period_note("local", contract.id, 86_400, "等待突破") is True
    reloaded = ConfigRepository().get_contract("local", contract.id)
    assert reloaded is not None
    assert reloaded.periods[0].note == "等待突破"
    assert reloaded.periods[1].note == ""
