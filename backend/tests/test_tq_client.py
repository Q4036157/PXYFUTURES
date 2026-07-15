from types import SimpleNamespace

import pandas as pd
import pytest

from app.market.tq_client import ContractUnavailable, TqClient
from app.services.repository import KlineSyncState


class FakeKlineApi:
    def __init__(self) -> None:
        self.wait_update_calls = 0
        self.wait_update_deadline: float | None = None
        self.request: tuple[str, int, int] | None = None

    def get_kline_serial(self, symbol: str, duration_seconds: int, data_length: int) -> pd.DataFrame:
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

    def query_quotes(self, **kwargs: object) -> list[str]:
        self.calls += 1
        assert kwargs == {
            "ins_class": "FUTURE",
            "exchange_id": "DCE",
            "product_id": "v",
            "expired": False,
        }
        return ["DCE.v2609", "DCE.v2610", "SHFE.au2609"]


class FakeCzceSymbolApi:
    def query_quotes(self, **kwargs: object) -> list[str]:
        assert kwargs == {
            "ins_class": "FUTURE",
            "exchange_id": "CZCE",
            "product_id": "OI",
            "expired": False,
        }
        return ["CZCE.OI609", "CZCE.OI611", "DCE.v2609"]


class FakeKlineCache:
    def __init__(self) -> None:
        self.rows: list[tuple[int, float]] = []
        self.state: KlineSyncState | None = None
        self.saved_request_count: int | None = None

    def load_kline_bars(self, symbol: str, duration_seconds: int, count: int) -> list[tuple[int, float]]:
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
    assert [(bar.timestamp_ns, bar.close) for bar in result.bars] == [(1, 100.0), (2, 101.0), (3, 102.0)]
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
    assert [(bar.timestamp_ns, bar.close) for bar in result.bars] == [(1, 100.0), (2, 101.0), (3, 102.0)]


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
def test_active_future_validation_rejects_unavailable_contract(ins_class: str, expired: bool) -> None:
    client = TqClient()
    client._api = FakeQuoteApi(ins_class=ins_class, expired=expired)

    with pytest.raises(ContractUnavailable):
        client.require_active_future("DCE.V2609")


def test_active_future_suggestions_are_cached_and_limited_to_the_exchange() -> None:
    api = FakeSymbolApi()
    client = TqClient()
    client._api = api

    assert client.list_active_futures("dce", "v") == ["DCE.v2609", "DCE.v2610"]
    assert client.list_active_futures("DCE", "V") == ["DCE.v2609", "DCE.v2610"]
    assert api.calls == 1


def test_active_future_suggestions_use_czce_product_case() -> None:
    client = TqClient()
    client._api = FakeCzceSymbolApi()

    assert client.list_active_futures("czce", "oi") == ["CZCE.OI609", "CZCE.OI611"]
