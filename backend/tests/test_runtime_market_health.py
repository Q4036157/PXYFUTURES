"""线上超时与历史行情误标的确定性回归。"""
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.api import signals
from app.api.contracts import _serialize
from app.config import settings
from app.market.ma_engine import Bar
from app.market.tq_client import (
    MarketBars,
    MarketDataUnavailable,
    TqClient,
    _LiveKlineSeries,
)
from app.services.market_sync import MarketSyncJob
from app.services.repository import ContractConfig, KlineSyncState, PeriodConfig


def period():
    return PeriodConfig(id=1, label="日线", duration_seconds=86400, m4=5, m3=4, m2=3, m1=1)


class Cache:
    def __init__(self):
        self.rows = [(1, 100.0), (2, 101.0), (3, 102.0)]
        self.saved = 0

    def get_kline_sync_state(self, *_args):
        return KlineSyncState(500, 123)

    def load_kline_bars(self, *_args):
        return self.rows

    def save_kline_bars(self, *_args):
        self.saved += 1


def test_expired_contract_keeps_history_without_remote_kline_requests(monkeypatch):
    client = TqClient()
    calls = []
    client._api = SimpleNamespace(
        get_quote=lambda _symbol: SimpleNamespace(ins_class="FUTURE", expired=True),
        get_kline_serial=lambda *_args, **_kwargs: calls.append("unexpected"),
        wait_update=lambda **_kwargs: None,
    )
    cache = Cache()
    monkeypatch.setattr(client, "_should_use_cache_only", lambda: False)
    result = client.fetch_bars("DCE.V2609", 86400, 3, cache)
    assert result.source == "historical"
    assert result.updated_at_ns == 123
    assert len(result.bars) == 3 and cache.saved == 0 and calls == []
    rollover = client.wait_for_bar_rollovers([MarketSyncJob("DCE.V2609", 60, 10)], cache, 0.01)
    assert rollover.changed_sources == {} and calls == []
    assert client.contract_status("DCE.V2609") == "expired"
    assert _serialize(ContractConfig(1, "DCE.V2609", "历史", []), client)["market_status"] == "expired"


def test_contract_metadata_cache_revalidates_and_is_bounded(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app.market.tq_client.monotonic", lambda: now[0])
    quote = SimpleNamespace(ins_class="FUTURE", expired=False)
    calls = []
    api = SimpleNamespace(get_quote=lambda symbol: (calls.append(symbol), quote)[1])
    client = TqClient()
    assert not client._contract_expired(api, "DCE.V2609")
    quote.expired = True
    assert not client._contract_expired(api, "DCE.V2609")
    now[0] += 301
    assert client._contract_expired(api, "DCE.V2609")
    assert len(calls) == 2
    for index in range(600):
        client._remember_contract_status(str(index), "active")
    assert len(client._contract_states) == 512


def test_proxy_failures_cool_down_without_losing_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.market.tq_client.get_tq_credentials", lambda: ("u", "p"))
    now = [100.0]
    monkeypatch.setattr("app.market.tq_client.monotonic", lambda: now[0])
    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(TqApi=type("Api", (), {}), TqAuth=object))
    client = TqClient()
    attempts = []

    def unavailable(*_args):
        attempts.append(now[0])
        raise MarketDataUnavailable("代理连接不可用")

    monkeypatch.setattr(client, "validate_credentials", unavailable)
    for delay in [3, 6, 12, 24, 48, 60, 60]:
        with pytest.raises(MarketDataUnavailable):
            client._get_api()
        assert client._connect_retry_after == now[0] + delay
        for _ in range(5):
            with pytest.raises(MarketDataUnavailable):
                client._get_api()
        now[0] += delay + 0.01
    assert len(attempts) == 7 and client._credential_error is None
    client.credentials_changed()
    assert client._connect_failures == 0 and client._connect_retry_after == 0


def test_frontend_requests_reuse_ready_live_frame(monkeypatch):
    client = TqClient()
    frame = pd.DataFrame([{"datetime": 3, "close": 102.0}])
    client._live_kline_series[("DCE.V2701", 86400)] = _LiveKlineSeries(frame, 10, 500, 3)
    client._remember_contract_status("DCE.V2701", "active")
    requests = []
    client._api = SimpleNamespace(
        get_kline_serial=lambda *_args, **_kwargs: requests.append("unexpected"),
        wait_update=lambda **_kwargs: None,
    )
    monkeypatch.setattr(client, "_should_use_cache_only", lambda: False)
    cache = Cache()
    result = client.fetch_bars("DCE.V2701", 86400, 3, cache)
    assert result.source == "live" and cache.saved == 1 and requests == []


def test_connection_recovers_after_cooldown_without_editing_credentials(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.market.tq_client.get_tq_credentials", lambda: ("u", "p"))
    now = [100.0]
    monkeypatch.setattr("app.market.tq_client.monotonic", lambda: now[0])

    class Api:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(TqApi=Api, TqAuth=lambda *_args: object()))
    client = TqClient()
    attempts = []

    def validate(*_args):
        attempts.append(now[0])
        if len(attempts) == 1:
            raise MarketDataUnavailable("代理连接不可用")

    monkeypatch.setattr(client, "validate_credentials", validate)
    with pytest.raises(MarketDataUnavailable):
        client._get_api()
    with pytest.raises(MarketDataUnavailable):
        client._get_api()
    now[0] += 3.1
    with caplog.at_level("INFO", logger="app.market.tq_client"):
        api = client._get_api()
        assert client._get_api() is api
    assert len(attempts) == 2
    assert client._connect_failures == 0 and client._connect_retry_after == 0
    assert sum("天勤连接已恢复" in record.message for record in caplog.records) == 1


def test_empty_remote_bars_preserve_cache_and_sync_timestamp(monkeypatch):
    client = TqClient()
    client._remember_contract_status("DCE.V2701", "active")
    client._api = SimpleNamespace(
        get_kline_serial=lambda *_args, **_kwargs: pd.DataFrame(),
        wait_update=lambda **_kwargs: None,
    )
    monkeypatch.setattr(client, "_should_use_cache_only", lambda: False)
    cache = Cache()
    result = client.fetch_bars("DCE.V2701", 86400, 3, cache)
    assert result.source == "cache" and result.updated_at_ns == 123
    assert cache.saved == 0 and len(result.bars) == 3


def test_unknown_contract_status_does_not_exclude_market_subscription():
    client = TqClient()

    def unavailable(_symbol):
        raise TimeoutError("元数据暂不可用")

    assert not client._contract_expired(SimpleNamespace(get_quote=unavailable), "CZCE.AP701")
    assert client.contract_status("CZCE.AP701") == "unknown"


@pytest.mark.parametrize("source,age,status", [("live", 30, "historical"), ("historical", 1, "expired"), ("cache", 1, "current")])
def test_snapshot_uses_actual_bar_age_not_recent_sync_time(source, age, status):
    now = datetime.now(UTC)
    start = now - timedelta(days=age, minutes=8)
    bars = [Bar(int((start + timedelta(minutes=i)).timestamp() * 1e9), 100.0 + i) for i in range(8)]
    result = signals._snapshot_from_market_bars("DCE.V2609", period(), MarketBars(bars, source, int(now.timestamp() * 1e9)))
    assert result["data_status"] == status
    assert result["data_updated_at"] != result["bar_time"]


def test_expired_alerts_do_not_read_history_or_change_saved_contracts():
    contracts = [ContractConfig(1, "DCE.V2609", "历史", [period()])]
    repository = SimpleNamespace(list_contracts=lambda _user: contracts)
    client = TqClient()
    client._remember_contract_status("DCE.V2609", "expired")
    result = signals.scan_cached_alerts_for_user(repository, "user", frozenset([86400]), client=client)
    assert result == {"signals": [], "requested": 0, "failed": 0, "excluded_contracts": 1}
    assert len(contracts) == 1 and contracts[0].symbol == "DCE.V2609"


def test_historical_cache_does_not_generate_real_time_alerts():
    repository = SimpleNamespace(
        load_kline_bars=lambda *_args: [(index * 1_000_000_000, 100.0 + index) for index in range(8)],
        get_kline_sync_state=lambda *_args: KlineSyncState(500, 123),
    )
    snapshots, failed = signals._scan_cached_alert_jobs(repository, [(ContractConfig(1, "DCE.V2609", "历史", []), period())])
    assert snapshots == [] and failed == 0


def test_alert_state_count_expands_history_to_show_true_500_bars():
    # 原始交叉在索引 52：连同信号 K 共 500 根收盘 K，最后另留一根盘中 K。
    closes = [100 - index for index in range(30)]
    closes += [70 + index for index in range(20)]
    closes += [90.0] * (553 - len(closes))
    start_ns = int(datetime.now(UTC).timestamp() * 1_000_000_000) - 553 * 86_400_000_000_000
    rows = [
        (start_ns + index * 86_400_000_000_000, close)
        for index, close in enumerate(closes)
    ]
    requested_counts = []

    def load_bars(_symbol, _duration, count):
        requested_counts.append(count)
        return rows[-count:]

    repository = SimpleNamespace(
        load_kline_bars=load_bars,
        get_kline_sync_state=lambda *_args: KlineSyncState(1_000, rows[-1][0]),
    )
    snapshots, failed = signals._scan_cached_alert_jobs(
        repository,
        [(ContractConfig(1, "DCE.V2609", "累计", []), period())],
    )

    assert failed == 0
    assert requested_counts == [306, 612]
    assert len(snapshots) == 1
    assert snapshots[0]["state_bar_count"] == 500
    assert snapshots[0]["state_since"] == snapshots[0]["cross_time"]
    assert snapshots[0]["cross_time"] == signals._iso_time(rows[52][0])


def test_alert_history_expansion_stops_when_cache_is_exhausted():
    start_ns = int(datetime.now(UTC).timestamp() * 1_000_000_000) - 400 * 86_400_000_000_000
    rows = [
        (start_ns + index * 86_400_000_000_000, 100.0)
        for index in range(400)
    ]
    requested_counts = []

    def load_bars(_symbol, _duration, count):
        requested_counts.append(count)
        return rows[-count:]

    repository = SimpleNamespace(
        load_kline_bars=load_bars,
        get_kline_sync_state=lambda *_args: KlineSyncState(1_000, rows[-1][0]),
    )
    snapshots, failed = signals._scan_cached_alert_jobs(
        repository,
        [(ContractConfig(1, "DCE.V2609", "无交叉", []), period())],
    )

    assert snapshots == [] and failed == 0
    assert requested_counts == [306, 612]
