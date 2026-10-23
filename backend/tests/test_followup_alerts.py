"""二级预警的独立监控、收盘重放、持久化和账号边界。"""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import followup_alerts as api
from app.api.signals import _scan_cached_alert_jobs
from app.config import settings
from app.services.followup_alerts import FollowupAlertService, SourceConflict
from app.services.followup_sources import FollowupSourceMonitor
from app.services.market_sync import build_market_sync_jobs
from app.services.repository import ConfigRepository, PeriodConfig


@pytest.fixture
def setup_rule(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()
    contract = repository.create_contract("owner", "DCE.P2701", "棕榈油")
    snapshots = []
    calls = []

    def scanner(_repo, jobs, cursors):
        calls.append((jobs, dict(cursors)))
        return list(snapshots), 0

    service = FollowupAlertService(repository, scanner)

    def signal(index, label="下跌", cross="死叉", duration=1800):
        time = datetime(2026, 10, 23, 3, 30, tzinfo=UTC) + timedelta(minutes=30 * index)
        return {
            "contract_id": contract.id, "symbol": contract.symbol,
            "period": {"duration_seconds": duration}, "trend": {"m4": "空", "m3": "空"},
            "cross_type": cross, "label": label,
            "confirmed_bar_time": time.isoformat(),
            "closed_at": (time + timedelta(minutes=30)).isoformat(),
        }

    def create(conditions=None, states=None, duration=1800, user="owner"):
        return service.create_rule(
            user, contract.id, {"duration_seconds": 7200, "condition": "多空金叉", "state": "反弹"},
            duration, conditions or [], ["下跌"] if states is None else states,
        )

    return repository, contract, snapshots, calls, service, signal, create


def test_existing_matching_state_is_immediate_and_survives_restart(setup_rule):
    repo, _, snapshots, _, service, signal, create = setup_rule
    snapshots[:] = [signal(0, "反弹"), signal(1), signal(2), signal(3), signal(4)]
    create()
    rule = service.list_rules("owner")[0]
    assert rule["trigger_count"] == 1
    assert rule["last_triggered_at"] == signal(1)["closed_at"]
    assert rule["source"]["state"] == "反弹"
    assert len(repo.claim_followup_notifications("owner")) == 1
    assert repo.claim_followup_notifications("owner") == []
    service.evaluate()
    reloaded = ConfigRepository()
    restarted = FollowupAlertService(reloaded, service._scanner)
    restarted.evaluate()
    assert restarted.list_rules("owner")[0]["trigger_count"] == 1
    assert reloaded.claim_followup_notifications("owner") == []


@pytest.fixture
def setup_source(setup_rule):
    repo, _contract, snapshots, _calls, service, signal, _create = setup_rule
    source_calls = []
    def source_scanner(_repo, jobs):
        source_calls.append(jobs)
        return list(snapshots), 0
    service._sources = FollowupSourceMonitor(repo, source_scanner)
    def fixed(index, state="反弹", cross="金叉", m4="多", m3="空"):
        result = signal(index, state, cross, duration=7200)
        result["trend"] = {"m4": m4, "m3": m3}
        return result
    return (*setup_rule, fixed, source_calls)


def test_fixed_state_change_restore_and_count_progress_do_not_gate_target(setup_source):
    repo, _, snapshots, _, service, signal, create, fixed, _ = setup_source
    snapshots[:] = [fixed(0), signal(0, "反弹")]
    create()
    assert service.list_rules("owner")[0]["source_changed"] is False
    snapshots[:] = [fixed(1), signal(1, "反弹")]
    service.evaluate()
    assert service.list_rules("owner")[0]["source_changed"] is False
    snapshots[:] = [fixed(2, "下跌", "死叉", "空"), signal(2)]
    service.evaluate()
    rule = service.list_rules("owner")[0]
    assert rule["source_changed"] is True
    assert rule["source"]["state"] == "反弹"
    assert rule["source_current"]["state"] == "下跌"
    assert rule["trigger_count"] == 1
    assert len(repo.claim_followup_notifications("owner")) == 1
    triggered_at = rule["last_triggered_at"]
    snapshots[:] = [fixed(3), signal(3, "反弹")]
    service.evaluate()
    rule = service.list_rules("owner")[0]
    assert rule["source_changed"] is False
    assert rule["last_triggered_at"] == triggered_at
    snapshots[:] = [fixed(4), signal(4)]
    service.evaluate()
    assert service.list_rules("owner")[0]["trigger_count"] == 2
    assert len(repo.claim_followup_notifications("owner")) == 1


def test_replace_only_one_row_preserves_target_history_and_survives_restart(setup_source):
    repo, _, snapshots, _, service, signal, create, fixed, source_calls = setup_source
    snapshots[:] = [fixed(0), signal(0, "反弹"), signal(1)]
    first, second = create(), create()
    assert len(source_calls[-1]) == 1
    repo.claim_followup_notifications("owner")
    snapshots[:] = [fixed(2, "下跌", "死叉", "空"), signal(2)]
    service.evaluate()
    before = repo.list_followup_alerts()[0]
    viewed = service.list_rules("owner")[0]
    service.replace_source("owner", first, viewed["source"], viewed["source_current"])
    first_row, second_row = service.list_rules("owner")
    assert first_row["id"] == first and second_row["id"] == second
    assert first_row["source"]["state"] == "下跌"
    assert first_row["source_changed"] is False
    assert second_row["source"]["state"] == "反弹" and second_row["source_changed"] is True
    after = repo.list_followup_alerts()[0]
    for key in ("duration_seconds", "conditions", "states", "last_bar_time", "last_triggered_at", "trigger_count", "notified_count"):
        assert after[key] == before[key]
    assert repo.claim_followup_notifications("owner") == []
    restarted = FollowupAlertService(ConfigRepository(), service._scanner, source_scanner=service._sources.scanner)
    restarted.evaluate()
    assert restarted.list_rules("owner")[0]["source"]["state"] == "下跌"
    assert restarted.list_rules("owner")[0]["source_changed"] is False
    assert repo.claim_followup_notifications("owner") == []


def test_replace_rechecks_latest_state_and_rejects_stale_other_page_or_other_owner(setup_source):
    repo, _, snapshots, _, service, _, create, fixed, _ = setup_source
    snapshots[:] = [fixed(0, "下跌", "死叉", "空")]
    rule_id = create()
    viewed = service.list_rules("owner")[0]
    snapshots[:] = [fixed(1)]
    with pytest.raises(SourceConflict, match="已更新"):
        service.replace_source("owner", rule_id, viewed["source"], viewed["source_current"])
    assert repo.list_followup_alerts()[0]["source"] == viewed["source"]
    with pytest.raises(LookupError):
        service.replace_source("other", rule_id, viewed["source"], viewed["source_current"])
    snapshots[:] = [fixed(2, "下跌", "死叉", "空")]
    service.evaluate()
    viewed = service.list_rules("owner")[0]
    service.replace_source("owner", rule_id, viewed["source"], viewed["source_current"])
    with pytest.raises(SourceConflict, match="其他页面"):
        service.replace_source("owner", rule_id, viewed["source"], viewed["source_current"])


def test_unknown_source_and_scanner_failure_never_show_normal_or_stop_target(setup_source):
    repo, _, snapshots, _, service, signal, create, fixed, _ = setup_source
    snapshots[:] = [fixed(0), signal(0, "反弹")]
    rule_id = create()
    snapshots[:] = [signal(1)]
    service.evaluate()
    rule = service.list_rules("owner")[0]
    assert rule["source_current"] is None and rule["source_changed"] is None
    assert rule["trigger_count"] == 1
    with pytest.raises(SourceConflict, match="暂不可用"):
        service.replace_source("owner", rule_id, rule["source"], {})
    def fail(*_args):
        raise RuntimeError("模拟固定周期读取失败")
    service._sources.scanner = fail
    snapshots[:] = [fixed(2), signal(2, "反弹")]
    service.evaluate()
    snapshots[:] = [fixed(3), signal(3)]
    service.evaluate()
    rule = service.list_rules("owner")[0]
    assert rule["source_changed"] is None and rule["trigger_count"] == 2
    assert repo.claim_followup_notifications("owner")[0]["trigger_count"] == 2


def test_source_monitor_uses_latest_valid_symbol_and_removed_period_is_unknown(setup_source):
    repo, contract, snapshots, _, service, _, create, fixed, _ = setup_source
    unknown = fixed(5)
    unknown["symbol"] = "DCE.P2705"
    historical = {**fixed(6), "data_status": "historical"}
    snapshots[:] = [unknown, fixed(3), historical, fixed(1, "下跌", "死叉", "空")]
    create()
    rule = service.list_rules("owner")[0]
    assert rule["source_current"]["confirmed_bar_time"] == fixed(3)["confirmed_bar_time"]
    repo.delete_period("owner", contract.id, 7200)
    assert service.list_rules("owner")[0]["source_current"] is None
    service.evaluate()
    assert service.list_rules("owner")[0]["source_changed"] is None


def test_source_api_conflict_and_tenant_boundary(setup_source):
    _, _, snapshots, _, service, _, create, fixed, _ = setup_source
    snapshots[:] = [fixed(0, "下跌", "死叉", "空")]
    rule_id = create()
    rule = service.list_rules("owner")[0]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(followup_alerts=service)))
    body = api.SourceReplacement(previous=rule["source"], observed=rule["source_current"])
    with pytest.raises(HTTPException) as forbidden:
        asyncio.run(api.replace_source(rule_id, body, request, "other"))
    assert forbidden.value.status_code == 404
    result = asyncio.run(api.replace_source(rule_id, body, request, "owner"))
    assert result["rules"][0]["source_changed"] is False
    with pytest.raises(HTTPException) as stale:
        asyncio.run(api.replace_source(rule_id, body, request, "owner"))
    assert stale.value.status_code == 409


def test_only_nonmatching_to_matching_transition_realerts_without_new_cross(setup_rule):
    repo, _, snapshots, _, service, signal, create = setup_rule
    snapshots.append(signal(0))
    create()
    for index, label in [(1, "下跌"), (2, "空震荡"), (3, "下跌"), (4, "下跌")]:
        snapshots[:] = [signal(index, label)]
        service.evaluate()
    rule = service.list_rules("owner")[0]
    assert rule["trigger_count"] == 2
    assert rule["last_triggered_at"] == signal(3)["closed_at"]
    snapshots[:] = [signal(1, "空震荡"), signal(0)]
    service.evaluate()
    assert service.list_rules("owner")[0]["trigger_count"] == 2
    assert repo.claim_followup_notifications("owner")[0]["trigger_count"] == 2


def test_restart_corrects_legacy_latest_bar_time_without_another_notification(setup_rule):
    repo, contract, snapshots, _, service, signal, _ = setup_rule
    rule_id = repo.create_followup_alert(
        "owner", contract.id, contract.symbol, {}, 1800, [], ["下跌"],
    )
    repo.advance_followup_alert(
        rule_id, contract.symbol, signal(4)["confirmed_bar_time"], True, signal(4)["closed_at"],
    )
    repo.claim_followup_notifications("owner")
    snapshots[:] = [signal(0, "反弹"), signal(1), signal(2), signal(3), signal(4)]
    service.evaluate()
    rule = service.list_rules("owner")[0]
    assert rule["last_triggered_at"] == signal(1)["closed_at"]
    assert rule["trigger_count"] == 1
    assert repo.list_followup_alerts()[0]["notified_count"] == 1
    assert repo.claim_followup_notifications("owner") == []
    snapshots.append(signal(5))
    service.evaluate()
    assert service.list_rules("owner")[0]["last_triggered_at"] == signal(1)["closed_at"]
    assert repo.claim_followup_notifications("owner") == []


def test_origin_uses_entire_selected_predicate_not_just_cross_or_label(setup_rule):
    _, _, snapshots, _, service, signal, create = setup_rule
    snapshots[:] = [signal(0, "反弹"), signal(1), signal(2), signal(3)]
    snapshots[1]["trend"]["m3"] = "多"
    create(conditions=["空空死叉"])
    assert service.list_rules("owner")[0]["last_triggered_at"] == signal(2)["closed_at"]


def test_missing_history_does_not_invent_latest_bar_as_origin(setup_rule):
    repo, _, snapshots, _, service, signal, create = setup_rule
    snapshots.append(signal(4))
    create()
    rule = service.list_rules("owner")[0]
    assert rule["trigger_count"] == 1
    assert rule["last_triggered_at"] is None
    snapshots[:] = [signal(5, "反弹"), signal(6)]
    service.evaluate()
    assert service.list_rules("owner")[0]["last_triggered_at"] == signal(6)["closed_at"]
    repo.claim_followup_notifications("owner")
    snapshots[:] = [signal(6)]
    FollowupAlertService(repo, service._scanner).evaluate()
    assert service.list_rules("owner")[0]["last_triggered_at"] == signal(6)["closed_at"]
    assert repo.claim_followup_notifications("owner") == []


def test_unknown_cross_in_history_is_not_evidence_of_a_nonmatching_bar(setup_rule):
    _, _, snapshots, _, service, signal, create = setup_rule
    snapshots[:] = [signal(0, "等待交叉", cross=None), signal(1), signal(2)]
    create()
    rule = service.list_rules("owner")[0]
    assert rule["trigger_count"] == 1
    assert rule["last_triggered_at"] is None


def test_origin_correction_cannot_overwrite_concurrently_advanced_cursor(setup_rule):
    repo, contract, _, _, _, signal, create = setup_rule
    rule_id = create()
    repo.advance_followup_alert(
        rule_id, contract.symbol, signal(4)["confirmed_bar_time"], True, signal(4)["closed_at"],
    )
    assert not repo.correct_followup_origin(
        rule_id, contract.symbol, signal(3)["confirmed_bar_time"],
        signal(4)["closed_at"], signal(1)["closed_at"],
    )
    assert repo.list_followup_alerts()[0]["last_triggered_at"] == signal(4)["closed_at"]


@pytest.mark.parametrize(("conditions", "states", "label", "cross", "expected"), [
    (["空空死叉"], ["下跌"], "反弹", "死叉", False),
    (["空空死叉"], ["下跌"], "下跌", "金叉", False),
    (["空空死叉"], ["下跌"], "下跌", "死叉", True),
    ([], ["下跌"], "下跌", "金叉", True),
    (["空空金叉", "空空死叉"], [], "空震荡", "金叉", True),
    ([], ["反弹", "下跌"], "反弹", "金叉", True),
])
def test_choices_are_or_within_category_and_and_across_categories(
    setup_rule, conditions, states, label, cross, expected,
):
    _, _, _, _, service, signal, _ = setup_rule
    assert service.matches({"conditions": conditions, "states": states}, signal(0, label, cross)) is expected


def test_same_contract_multiple_rules_and_tenant_safe_delete(setup_rule):
    repo, contract, snapshots, _, service, signal, create = setup_rule
    snapshots[:] = [signal(0), signal(0, duration=900)]
    repo.save_period("owner", contract.id, PeriodConfig(None, "15分钟", 900, 5, 4, 3, 1))
    first = create()
    second = create(duration=900)
    assert [r["id"] for r in service.list_rules("owner")] == [first, second]
    assert service.list_rules("other") == []
    assert repo.claim_followup_notifications("other") == []
    assert not repo.delete_followup_alert("other", first)
    assert repo.delete_followup_alert("owner", first)
    assert [r["id"] for r in service.list_rules("owner")] == [second]
    assert [r["id"] for r in repo.claim_followup_notifications("owner")] == [second]
    repo.delete_contract("owner", contract.id)
    assert service.list_rules("owner") == []


def test_contract_replacement_and_missing_period_do_not_silently_retarget(setup_rule):
    repo, contract, snapshots, _, service, signal, create = setup_rule
    first = create()
    repo.update_contract("owner", contract.id, "DCE.P2705", "棕榈油")
    snapshots.append(signal(0))
    service.evaluate()
    assert service.list_rules("owner")[0]["status"] == "合约已更换，等待删除"
    assert repo.claim_followup_notifications("owner") == []
    repo.update_contract("owner", contract.id, contract.symbol, contract.name)
    repo.delete_period("owner", contract.id, 1800)
    service.evaluate()
    assert service.list_rules("owner")[0]["id"] == first
    assert service.list_rules("owner")[0]["status"] == "目标周期已移除，等待恢复配置"


def test_create_scanner_failure_preserves_saved_rule_for_retry(setup_rule):
    _, _, snapshots, _, service, signal, create = setup_rule
    normal = service._scanner

    def unavailable(*_args):
        raise RuntimeError("模拟缓存暂时不可读")

    service._scanner = unavailable
    create()
    assert service.list_rules("owner")[0]["status"] == "等待收盘行情"
    service._scanner = normal
    snapshots.append(signal(0))
    service.evaluate()
    assert service.list_rules("owner")[0]["trigger_count"] == 1


def test_batched_delayed_bars_preserve_all_state_entries_and_new_rule_uses_latest_only(setup_rule):
    _, _, snapshots, _, service, signal, create = setup_rule
    snapshots.append(signal(0, "反弹"))
    create()
    snapshots[:] = [signal(3), signal(1), signal(2, "反弹")]
    create()
    assert [r["trigger_count"] for r in service.list_rules("owner")] == [2, 1]
    service.evaluate()
    assert [r["trigger_count"] for r in service.list_rules("owner")] == [2, 1]


def test_expired_contract_is_paused_and_does_not_alert(setup_rule):
    _, _, snapshots, _, service, signal, create = setup_rule
    service._contract_status = lambda _symbol: "expired"
    snapshots.append(signal(0))
    create()
    assert service.list_rules("owner")[0]["trigger_count"] == 0
    assert service.list_rules("owner")[0]["status"] == "合约已到期，监控暂停"


def test_notification_claim_is_atomic_across_pages(setup_rule):
    from concurrent.futures import ThreadPoolExecutor

    repo, _, snapshots, _, _, signal, create = setup_rule
    snapshots.append(signal(0))
    create()
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _index: repo.claim_followup_notifications("owner"), range(4)))
    assert sum(len(items) for items in claimed) == 1


def test_replay_intermediate_bars_and_ignore_current_unclosed_bar(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repo = ConfigRepository()
    contract = repo.create_contract("owner", "DCE.P2701", "棕榈油")
    period = PeriodConfig(None, "30分钟", 1800, 5, 4, 3, 1)
    origin = datetime.now(UTC).replace(hour=1, minute=0, second=0, microsecond=0) - timedelta(days=1)
    target_closes = [20, 19, 18, 17, 16, 15, 16, 17, 18, 19, 18, 17, 16, 15, 14, 15, 16]
    rows = [
        (int((origin + timedelta(minutes=index * 30 + minute * 5)).timestamp()) * 10**9, float(close))
        for index, close in enumerate(target_closes) for minute in range(6)
    ]
    repo.save_kline_bars(contract.symbol, 300, rows, requested_count=300, synced_at_ns=rows[-1][0])
    initial, failed = _scan_cached_alert_jobs(repo, [(contract, period)], replay_since={(contract.id, 1800): None})
    assert failed == 0
    assert len(initial) > 1
    cursor = (origin + timedelta(minutes=10 * 30)).astimezone(
        timezone_china(),
    ).isoformat()
    replayed, failed = _scan_cached_alert_jobs(
        repo, [(contract, period)], replay_since={(contract.id, 1800): cursor},
    )
    assert failed == 0
    assert len(replayed) == 5
    assert [item["confirmed_bar_time"] for item in replayed] == sorted(
        item["confirmed_bar_time"] for item in replayed
    )
    assert replayed[-1]["confirmed_bar_time"] == initial[-1]["confirmed_bar_time"]
    assert replayed[-1]["closed_at"] == (origin + timedelta(minutes=16 * 30)).astimezone(
        timezone_china(),
    ).isoformat()
    assert len({item["label"] for item in replayed}) > 1
    repo.save_period("owner", contract.id, period)
    service = FollowupAlertService(
        repo, lambda r, jobs, cursors: _scan_cached_alert_jobs(r, jobs, replay_since=cursors),
        source_scanner=lambda r, jobs: _scan_cached_alert_jobs(r, jobs),
    )
    latest = initial[-1]
    baseline = {
        "duration_seconds": 1800,
        "condition": f"{latest['trend']['m4']}{latest['trend']['m3']}{latest['cross_type']}",
        "state": latest["label"],
    }
    service.create_rule("owner", contract.id, baseline, 1800, [], ["下跌"])
    before = service.list_rules("owner")[0]
    assert before["source_changed"] is False
    # 当前尚未收盘K线价格大幅变动，不得改变左侧已收盘状态。
    repo.save_kline_bars(contract.symbol, 300, [(rows[-1][0], 9999.0)], requested_count=300, synced_at_ns=rows[-1][0])
    service.evaluate()
    after = service.list_rules("owner")[0]
    assert after["source_current"] == before["source_current"]
    assert after["source_changed"] is False


@pytest.mark.parametrize("legacy", [False, True])
def test_cached_ten_minute_origin_is_first_matching_close_not_creation(setup_rule, legacy):
    repo, contract, _, _, _, _, _ = setup_rule
    period = PeriodConfig(None, "10分钟", 600, 5, 4, 3, 1)
    repo.save_period("owner", contract.id, period)
    origin = datetime.now(timezone_china()).replace(
        hour=21, minute=0, second=0, microsecond=0,
    ) - timedelta(days=1)
    closes = [30, 29, 28, 27, 26, 27, 28, 29, 30, 31, 32, 21, 20, 19, 18, 17, 16, 15, 14]
    rows = [
        (int((origin + timedelta(minutes=index * 10 + step * 5)).timestamp()) * 10**9, close)
        for index, close in enumerate(closes) for step in range(2)
    ]
    repo.save_kline_bars(contract.symbol, 300, rows, requested_count=500, synced_at_ns=rows[-1][0])
    service = FollowupAlertService(
        repo, lambda r, jobs, cursors: _scan_cached_alert_jobs(r, jobs, replay_since=cursors),
    )
    if legacy:
        rid = repo.create_followup_alert("owner", contract.id, contract.symbol, {}, 600, [], ["下跌"])
        repo.advance_followup_alert(
            rid, contract.symbol, (origin + timedelta(minutes=170)).isoformat(), True,
            (origin + timedelta(minutes=180)).isoformat(),
        )
        repo.claim_followup_notifications("owner")
        service.evaluate()
    else:
        service.create_rule("owner", contract.id, {"duration_seconds": 7200}, 600, [], ["下跌"])
    rule = service.list_rules("owner")[0]
    # 第12根下跌后曾短暂反弹，本轮从第15根重新进入下跌起算。
    assert rule["last_triggered_at"] == (origin + timedelta(minutes=150)).isoformat()
    assert rule["trigger_count"] == 1
    assert len(repo.claim_followup_notifications("owner")) == (0 if legacy else 1)
    service.evaluate()
    assert repo.claim_followup_notifications("owner") == []


def timezone_china():
    from datetime import timezone
    return timezone(timedelta(hours=8))


def test_lunch_gap_uses_actual_bar_close_not_next_session_start(setup_rule):
    repo, contract, _, _, _, _, _ = setup_rule
    day = datetime.now(timezone_china()).replace(hour=10, minute=0, second=0, microsecond=0)
    times = [day + timedelta(minutes=5 * index) for index in range(18)]
    times.append(day.replace(hour=13, minute=30))
    repo.save_kline_bars(contract.symbol, 300, [
        (int(time.timestamp()) * 10**9, float(100 + index % 5)) for index, time in enumerate(times)
    ], requested_count=500, synced_at_ns=int(times[-1].timestamp()) * 10**9)
    result, _ = _scan_cached_alert_jobs(
        repo, [(contract, PeriodConfig(None, "5分钟", 300, 5, 4, 3, 1))],
        replay_since={(contract.id, 300): None},
    )
    assert result[-1]["closed_at"] == day.replace(hour=11, minute=30).isoformat()
    assert result[-1]["confirmed_at"] == times[-1].isoformat()


def test_daily_close_uses_day_session_cache_and_adds_its_background_source(setup_rule):
    repo, contract, _, _, _, _, _ = setup_rule
    period = PeriodConfig(None, "日线", 86400, 5, 4, 3, 1)
    last_day = datetime.now(timezone_china()).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [last_day - timedelta(days=19 - index) for index in range(20)]
    repo.save_kline_bars(contract.symbol, 86400, [
        (int(day.timestamp()) * 10**9, float(100 + index % 5)) for index, day in enumerate(days)
    ], requested_count=500, synced_at_ns=int(last_day.timestamp()) * 10**9)
    actual_close = days[-2].replace(hour=15, minute=15)
    base_times = [actual_close - timedelta(minutes=5), days[-2].replace(hour=21), last_day.replace(hour=9)]
    repo.save_kline_bars(contract.symbol, 300, [
        (int(time.timestamp()) * 10**9, 100.0) for time in base_times
    ], requested_count=3000, synced_at_ns=int(last_day.timestamp()) * 10**9)
    result, _ = _scan_cached_alert_jobs(
        repo, [(contract, period)], replay_since={(contract.id, 86400): None},
    )
    assert result[0]["closed_at"] == actual_close.isoformat()
    jobs = build_market_sync_jobs([contract], [{
        "contract_id": contract.id, "symbol": contract.symbol, "duration_seconds": 86400,
    }])
    assert any(job.duration_seconds == 300 and job.count >= 3000 for job in jobs)


def test_background_monitor_works_without_websocket_subscribers(setup_rule):
    _, _, snapshots, _, service, signal, create = setup_rule
    create()
    snapshots.append(signal(0))

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(service.run(stop))
        for _ in range(100):
            if service.list_rules("owner")[0]["trigger_count"]:
                break
            await asyncio.sleep(0.01)
        assert service.list_rules("owner")[0]["trigger_count"] == 1
        stop.set()
        service.wake()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())


def test_api_validation_and_account_boundaries(setup_rule):
    _, contract, _, _, service, _, _ = setup_rule
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(followup_alerts=service)))
    body = api.FollowupBody(
        contract_id=contract.id, source=api.FollowupSource(duration_seconds=7200, state="反弹"),
        duration_seconds=1800, states=["下跌"],
    )

    async def run():
        with pytest.raises(HTTPException) as forbidden:
            await api.create_rule(body, request, user_id="other")
        assert forbidden.value.status_code == 404
        result = await api.create_rule(body, request, user_id="owner")
        assert len(result["rules"]) == 1
        assert (await api.list_rules(request, user_id="other"))["rules"] == []
        with pytest.raises(HTTPException) as hidden:
            await api.delete_rule(result["created_id"], request, user_id="other")
        assert hidden.value.status_code == 404

    asyncio.run(run())
    with pytest.raises(ValidationError):
        api.FollowupBody(**{**body.model_dump(), "duration_seconds": 99})
    with pytest.raises(ValidationError):
        api.FollowupBody(**{**body.model_dump(), "states": ["错误状态"]})
    with pytest.raises(ValidationError):
        api.FollowupBody(**{**body.model_dump(), "conditions": ["不存在的交叉"]})
