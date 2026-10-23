"""合约目录落盘、有效性与行情锁隔离的回归测试。"""
import asyncio
import json
from time import perf_counter, time
from types import SimpleNamespace

import pandas as pd
import pytest

from app.api.contracts import list_contract_suggestions
from app.market.contract_directory import (
    SUGGESTION_MAX_AGE_SECONDS,
    VALIDATION_MAX_AGE_SECONDS,
    DirectoryContract,
    LocalContractDirectory,
)
from app.market.tq_client import ContractUnavailable, MarketDataUnavailable, TqClient
from app.services.market_sync import run_market_sync_loop


def contract(**changes):
    return DirectoryContract(**{
        "symbol": "CZCE.SH701", "name": "烧碱2701", "exchange": "CZCE", "product": "SH",
        "delivery_year": 2027, "delivery_month": 1, "expires_at": time() + 86400, **changes,
    })


def test_persisted_directory_serves_queries_and_validation_while_market_locked(tmp_path):
    path = tmp_path / "contracts.json"
    directory = LocalContractDirectory(path)
    directory.replace([contract()])
    client = TqClient(LocalContractDirectory(path))
    with client._lock:
        started = perf_counter()
        for _ in range(100):
            assert client.list_active_futures("czce", "sh") == ["CZCE.SH701"]
            assert client.require_active_future("CZCE.SH2701") == "CZCE.SH701"
        assert perf_counter() - started < 0.2
    assert client._api is None
    client.close()
    assert LocalContractDirectory(path).validation_ready()


def test_directory_rejects_wrong_decade_and_expired_contract(tmp_path):
    directory = LocalContractDirectory(tmp_path / "contracts.json")
    directory.replace([contract(), contract(symbol="CZCE.SH702", delivery_month=2, expires_at=time() - 1)])
    client = TqClient(directory)
    with pytest.raises(ContractUnavailable, match="交割年月"):
        client.require_active_future("CZCE.SH3701")
    with pytest.raises(ContractUnavailable):
        client.require_active_future("CZCE.SH2702")
    with pytest.raises(ContractUnavailable):
        client.require_active_future("CZCE.SH2703")
    assert directory.needs_refresh()
    assert client.list_active_futures("CZCE", "SH") == ["CZCE.SH701"]


def test_stale_directory_allows_candidates_but_requires_refresh_before_adding(tmp_path):
    path = tmp_path / "contracts.json"
    directory = LocalContractDirectory(path)
    directory.replace([contract()])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = time() - VALIDATION_MAX_AGE_SECONDS - 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    client = TqClient(LocalContractDirectory(path))
    assert client.list_active_futures("CZCE", "SH") == ["CZCE.SH701"]
    with pytest.raises(MarketDataUnavailable, match="正在同步"):
        client.require_active_future("CZCE.SH2701")
    payload["updated_at"] = time() - SUGGESTION_MAX_AGE_SECONDS - 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MarketDataUnavailable):
        TqClient(LocalContractDirectory(path)).list_active_futures("CZCE", "SH")


@pytest.mark.parametrize("payload", ["invalid", '{"version": 2}', '{"version": 1, "contracts": []}'])
def test_corrupt_directory_requests_refresh_without_connecting(tmp_path, payload):
    path = tmp_path / "contracts.json"
    path.write_text(payload, encoding="utf-8")
    directory = LocalContractDirectory(path)
    client = TqClient(directory)
    assert directory.needs_refresh()
    with pytest.raises(MarketDataUnavailable):
        client.list_active_futures("CZCE", "SH")
    assert client._api is None


def test_failed_atomic_write_preserves_memory_and_previous_disk_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "contracts.json"
    directory = LocalContractDirectory(path)
    directory.replace([contract()])
    def fail(*args):
        raise OSError("模拟替换失败")
    monkeypatch.setattr("app.market.contract_directory.os.replace", fail)
    with pytest.raises(OSError):
        directory.replace([contract(symbol="CZCE.SH702", delivery_month=2)])
    for snapshot in (directory, LocalContractDirectory(path)):
        assert snapshot.candidates("CZCE", "SH") == ["CZCE.SH701"]
    assert list(tmp_path.iterdir()) == [path]


def test_partial_four_digit_prefix_suggests_full_year_without_accepting_partial_input(tmp_path):
    directory = LocalContractDirectory(tmp_path / "contracts.json")
    directory.replace([contract()])
    client = TqClient(directory)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tq_client=client)))
    result = asyncio.run(list_contract_suggestions(request, "sh270", "owner"))
    assert result == [{"value": "SH701", "exchange": "CZCE", "symbol": "CZCE.SH701"}]


def test_background_refresh_is_batched_and_failed_refresh_keeps_snapshot(tmp_path, monkeypatch):
    directory = LocalContractDirectory(tmp_path / "contracts.json")
    client = TqClient(directory)
    calls = []
    class Api:
        def query_quotes(self, **kwargs):
            calls.append(kwargs)
            return ["CZCE.SH701", "KQD.m@CME.ES"]

        async def query_symbol_info(self, symbols):
            calls.append(symbols)
            return pd.DataFrame([{
                "instrument_id": "CZCE.SH701", "instrument_name": "烧碱2701",
                "exchange_id": "CZCE", "product_id": "SH", "delivery_year": 2027.0,
                "delivery_month": 1.0, "expire_datetime": time() + 86400,
                "ins_class": "FUTURE", "expired": False,
            }])

    monkeypatch.setattr(client, "_get_api", lambda: Api())
    monkeypatch.setattr(client, "_wait_contract_query", lambda api, query: asyncio.run(query()))
    client.refresh_contract_directory()
    assert calls == [{"ins_class": "FUTURE", "expired": False}, ["CZCE.SH701"]]
    assert client.require_active_future("CZCE.SH2701") == "CZCE.SH701"
    client.refresh_contract_directory()
    assert len(calls) == 2
    directory.request_refresh()
    client._directory_retry_after = 0
    def fail():
        calls.append("failed")
        raise RuntimeError("模拟断线")
    monkeypatch.setattr(client, "_get_api", fail)
    client.refresh_contract_directory()
    client.refresh_contract_directory()
    assert calls[-1] == "failed" and len(calls) == 3
    assert client.require_active_future("CZCE.SH2701") == "CZCE.SH701"


@pytest.mark.parametrize("records", [[], [contract()] * 10_001, [contract(), contract()], [contract(expires_at=float("nan"))]])
def test_invalid_snapshot_never_overwrites_previous_directory(tmp_path, records):
    directory = LocalContractDirectory(tmp_path / "contracts.json")
    directory.replace([contract()])
    with pytest.raises(ValueError):
        directory.replace(records)
    assert directory.validation_ready()
    assert directory.candidates("CZCE", "SH") == ["CZCE.SH701"]


def test_sdk_metadata_preserves_delivery_suffix_in_complete_directory(tmp_path):
    record = DirectoryContract.from_metadata({
        "instrument_id": "DCE.l2701F", "instrument_name": "测试期货", "exchange_id": "DCE",
        "product_id": "l", "delivery_year": 2027.0, "delivery_month": 1.0,
        "expire_datetime": time() + 86400, "ins_class": "FUTURE", "expired": False,
    })
    directory = LocalContractDirectory(tmp_path / "contracts.json")
    directory.replace([record])
    restored = LocalContractDirectory(directory.path)
    assert restored.get("DCE.L2701F").full_code == "L2701F"
    assert restored.candidates("DCE", "L") == []


def test_directory_refresh_runs_even_when_no_contract_is_configured():
    stop = asyncio.Event()
    calls = []
    class Client:
        def refresh_contract_directory(self):
            calls.append("refresh")
            stop.set()
    class Repository:
        def list_all_contracts(self):
            return []
        def list_followup_alerts(self):
            return []
    asyncio.run(run_market_sync_loop(Client(), Repository(), stop))
    assert calls == ["refresh"]


def test_incomplete_metadata_refresh_is_not_published(tmp_path, monkeypatch):
    directory = LocalContractDirectory(tmp_path / "contracts.json")
    directory.replace([contract()])
    directory.request_refresh()
    client = TqClient(directory)
    class Api:
        def query_quotes(self, **kwargs):
            return ["CZCE.SH701", "CZCE.SH702"]
        def query_symbol_info(self, symbols):
            return pd.DataFrame([{
                "instrument_id": "CZCE.SH701", "instrument_name": "烧碱2701",
                "exchange_id": "CZCE", "product_id": "SH", "delivery_year": 2027.0,
                "delivery_month": 1.0, "expire_datetime": time() + 86400,
                "ins_class": "FUTURE", "expired": False,
            }])
    monkeypatch.setattr(client, "_get_api", lambda: Api())
    monkeypatch.setattr(client, "_wait_contract_query", lambda api, query: asyncio.run(query()))
    client.refresh_contract_directory()
    assert directory.needs_refresh()
    assert LocalContractDirectory(directory.path).candidates("CZCE", "SH") == ["CZCE.SH701"]
