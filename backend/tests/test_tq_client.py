import asyncio
from time import monotonic
from types import SimpleNamespace

import pandas as pd
import pytest

from app.market import tq_client
from app.market.tq_client import (
    ContractUnavailable,
    MarketDataUnavailable,
    TqClient,
    _TqConnectionNoticeLimiter,
)
from app.services.repository import KlineSyncState


class FakeKlineApi:
    def __init__(self) -> None:
        self.wait_update_calls = 0
        self.wait_update_deadline: float | None = None
        self.request: tuple[str, int, int] | None = None

    def get_kline_serial(
        self, symbol: str, duration_seconds: int, data_length: int
    ) -> pd.DataFrame:
        self.request = (symbol, duration_seconds, data_length)
        return pd.DataFrame(
            [
                {"datetime": 1, "close": 100.0},
                {"datetime": 2, "close": 101.0},
                {"datetime": 3, "close": 102.0},
            ]
        )

    def wait_update(self, deadline: float | None = None) -> bool:
        self.wait_update_calls += 1
        self.wait_update_deadline = deadline
        return False


class FakeQuoteApi:
    def __init__(self, ins_class: str = "FUTURE", expired: bool = False) -> None:
        self.quote = SimpleNamespace(ins_class=ins_class, expired=expired)
        self.symbol: str | None = None

    def get_quote(self, symbol: str) -> SimpleNamespace:
        self.symbol = symbol
        return self.quote


class FakeSymbolApi:
    def __init__(self) -> None:
        self.calls = 0
        self.loop = asyncio.new_event_loop()

    def create_task(self, coroutine):
        return self.loop.create_task(coroutine)

    def wait_update(self, **_kwargs):
        self.loop.run_until_complete(asyncio.sleep(0))
        return True

    def close(self):
        if self.loop.is_closed():
            return
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def query_quotes(self, **kwargs: object) -> list[str]:
        self.calls += 1
        assert kwargs == {
            "ins_class": "FUTURE",
            "exchange_id": "DCE",
            "product_id": "v",
            "expired": False,
        }
        return ["DCE.v2609", "DCE.v2610", "SHFE.au2609"]


class FakeCzceSymbolApi(FakeSymbolApi):
    async def query_quotes(self, **kwargs: object) -> list[str]:
        assert kwargs == {
            "ins_class": "FUTURE",
            "exchange_id": "CZCE",
            "product_id": "OI",
            "expired": False,
        }
        return ["CZCE.OI609", "CZCE.OI611", "DCE.v2609"]


@pytest.fixture
def symbol_api():
    instances = []

    def create(api_type=FakeSymbolApi):
        api = api_type()
        instances.append(api)
        return api

    yield create
    for api in instances:
        api.close()


class FakeKlineCache:
    def __init__(self) -> None:
        self.rows: list[tuple[int, float]] = []
        self.state: KlineSyncState | None = None
        self.saved_request_count: int | None = None

    def load_kline_bars(
        self, symbol: str, duration_seconds: int, count: int
    ) -> list[tuple[int, float]]:
        return self.rows[-count:]

    def get_kline_sync_state(self, symbol: str, duration_seconds: int) -> KlineSyncState | None:
        return self.state

    def save_kline_bars(
        self,
        symbol: str,
        duration_seconds: int,
        bars: list[tuple[int, float]],
        requested_count: int,
        synced_at_ns: int,
    ) -> None:
        merged = {timestamp_ns: close for timestamp_ns, close in self.rows}
        merged.update(dict(bars))
        self.rows = sorted(merged.items())
        self.state = KlineSyncState(requested_count, synced_at_ns)
        self.saved_request_count = requested_count


def test_tq_connection_notices_are_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    now = [0.0]
    limiter = _TqConnectionNoticeLimiter(interval_seconds=900, clock=lambda: now[0])
    url = "wss://free-api.shinnytech.com/t/nfmd/front/mobile"

    with caplog.at_level("INFO", logger="app.market.tq_client"):
        assert limiter.handle(f"通知 : 开始与 {url} 重新建立网络连接", "WARNING")
        assert limiter.handle(
            f"通知 : 与 {url} 的网络连接断开，请检查客户端及网络是否正常", "WARNING"
        )
        assert limiter.handle(
            f"通知 : 与 {url} 的网络连接断开，请检查客户端及网络是否正常", "WARNING"
        )
        assert limiter.handle(f"通知 : 与 {url} 的网络连接已恢复", "WARNING")
        assert limiter.handle(f"通知 : 与 {url} 的网络连接已恢复", "WARNING")

        now[0] = 901
        assert limiter.handle(
            f"通知 : 与 {url} 的网络连接断开，请检查客户端及网络是否正常", "WARNING"
        )

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        f"天勤行情连接断开: {url}",
        f"天勤行情连接恢复: {url}",
        f"天勤行情连接断开: {url}，已抑制 1 条重复提示",
    ]


def test_fetch_bars_includes_latest_bar_and_persists_it(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeKlineApi()
    client = TqClient()
    client._api = api
    monkeypatch.setattr(client, "_should_use_cache_only", lambda: False)
    cache = FakeKlineCache()

    result = client.fetch_bars("DCE.V2609", 86400, 3, cache)

    assert api.request == ("DCE.v2609", 86400, 3)
    assert api.wait_update_calls == 1
    assert api.wait_update_deadline is not None
    assert result.source == "live"
    assert [(bar.timestamp_ns, bar.close) for bar in result.bars] == [
        (1, 100.0),
        (2, 101.0),
        (3, 102.0),
    ]
    assert cache.rows == [(1, 100.0), (2, 101.0), (3, 102.0)]


def test_fetch_bars_uses_cache_without_calling_tianqin_when_remote_is_unavailable() -> None:
    api = FakeKlineApi()
    client = TqClient()
    client._api = api
    client._remote_retry_after_monotonic = float("inf")
    cache = FakeKlineCache()
    cache.rows = [(1, 100.0), (2, 101.0), (3, 102.0)]
    cache.state = KlineSyncState(max_requested_bars=3, last_synced_at_ns=123)

    result = client.fetch_bars("DCE.V2609", 86400, 3, cache)

    assert result.source == "cache"
    assert result.updated_at_ns == 123
    assert api.request is None
    assert [(bar.timestamp_ns, bar.close) for bar in result.bars] == [
        (1, 100.0),
        (2, 101.0),
        (3, 102.0),
    ]


def test_active_future_validation_accepts_current_future() -> None:
    api = FakeQuoteApi()
    client = TqClient()
    client._api = api

    assert client.require_active_future("DCE.V2609") == "DCE.v2609"
    assert api.symbol == "DCE.v2609"


def test_active_future_validation_preserves_czce_product_case() -> None:
    api = FakeQuoteApi()
    client = TqClient()
    client._api = api

    assert client.require_active_future("CZCE.OI609") == "CZCE.OI609"
    assert api.symbol == "CZCE.OI609"


@pytest.mark.parametrize("ins_class,expired", [("", False), ("FUTURE", True), ("OPTION", False)])
def test_active_future_validation_rejects_unavailable_contract(
    ins_class: str, expired: bool
) -> None:
    client = TqClient()
    client._api = FakeQuoteApi(ins_class=ins_class, expired=expired)

    with pytest.raises(ContractUnavailable):
        client.require_active_future("DCE.V2609")


def test_active_future_suggestions_are_cached_and_limited_to_the_exchange(symbol_api) -> None:
    api = symbol_api()
    client = TqClient()
    client._api = api

    assert client.list_active_futures("dce", "v") == ["DCE.v2609", "DCE.v2610"]
    assert client.list_active_futures("DCE", "V") == ["DCE.v2609", "DCE.v2610"]
    assert api.calls == 1


def test_active_future_suggestions_use_czce_product_case(symbol_api) -> None:
    client = TqClient()
    client._api = symbol_api(FakeCzceSymbolApi)

    assert client.list_active_futures("czce", "oi") == ["CZCE.OI609", "CZCE.OI611"]


def test_cached_suggestions_do_not_wait_for_market_lock(symbol_api) -> None:
    client = TqClient()
    client._api = symbol_api()
    expected = client.list_active_futures("DCE", "V")
    assert client._lock.acquire(blocking=False)
    try:
        assert client.list_active_futures("DCE", "V") == expected
    finally:
        client._lock.release()


def test_uncached_suggestions_fail_promptly_when_market_lock_is_busy(monkeypatch) -> None:
    client = TqClient()
    monkeypatch.setattr(tq_client, "_CONTRACT_QUERY_LOCK_TIMEOUT_SECONDS", 0)
    with client._lock, pytest.raises(MarketDataUnavailable, match="行情同步繁忙"):
        client.list_active_futures("CZCE", "SH")


def test_catalog_timeout_cancels_query_and_releases_market_lock(symbol_api, monkeypatch) -> None:
    class SlowApi(FakeSymbolApi):
        async def query_quotes(self, **_kwargs):
            self.calls += 1
            await asyncio.Event().wait()

    api = symbol_api(SlowApi)
    client = TqClient()
    client._api = api
    monkeypatch.setattr(tq_client, "_CONTRACT_QUERY_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(MarketDataUnavailable, match="查询超时"):
        client.list_active_futures("CZCE", "SH")
    api.wait_update()
    assert not asyncio.all_tasks(api.loop)
    assert not client._active_contract_cache
    assert client._lock.acquire(blocking=False)
    client._lock.release()


def test_expired_catalog_is_not_reused_and_cache_is_bounded(symbol_api) -> None:
    api = symbol_api()
    client = TqClient()
    client._api = api
    client._active_contract_cache[("DCE", "V")] = (monotonic() - 301, ("DCE.v1901",))
    for index in range(128):
        client._active_contract_cache[("OTHER", str(index))] = (monotonic(), ())
    assert client.list_active_futures("DCE", "V") == ["DCE.v2609", "DCE.v2610"]
    assert len(client._active_contract_cache) == 128
    assert api.calls == 1
    client.close()
    assert not client._active_contract_cache


def test_unready_catalog_query_does_not_start_a_connection(monkeypatch) -> None:
    client = TqClient()

    def unexpected_connection():
        pytest.fail("候选查询不应阻塞于新连接的认证和初始化")

    monkeypatch.setattr(client, "_get_api", unexpected_connection)
    with pytest.raises(MarketDataUnavailable, match="连接尚未就绪"):
        client.list_active_futures("CZCE", "SH")


def test_failed_catalog_query_recovers_without_caching_failure(symbol_api) -> None:
    class FlakyApi(FakeSymbolApi):
        def query_quotes(self, **kwargs):
            if not self.calls:
                self.calls += 1
                raise RuntimeError("模拟合约服务不可用")
            return super().query_quotes(**kwargs)

    client = TqClient()
    client._api = symbol_api(FlakyApi)
    with pytest.raises(MarketDataUnavailable):
        client.list_active_futures("DCE", "V")
    assert not client._active_contract_cache
    assert client.list_active_futures("DCE", "V") == ["DCE.v2609", "DCE.v2610"]
