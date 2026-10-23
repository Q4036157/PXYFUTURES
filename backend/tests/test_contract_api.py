"""合约输入别名、数据源验证和持久化使用同一标准合约。"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import contracts
from app.config import settings
from app.market.tq_client import ContractUnavailable, TqClient
from app.services.repository import ConfigRepository


@pytest.fixture
def setup_contracts(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()
    validated = []

    class Client:
        def require_active_future(self, symbol):
            validated.append(symbol)
            return "CZCE.SH701"

        def list_active_futures(self, exchange, product):
            assert (exchange, product) == ("CZCE", "SH")
            return ["CZCE.SH611", "CZCE.SH701", "CZCE.SH702"]

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        config_repository=repository, tq_client=Client(),
    )))
    return repository, request, validated


def test_four_digit_alias_is_validated_then_stored_once_in_canonical_form(setup_contracts):
    repo, request, validated = setup_contracts
    group = repo.create_group("owner", "测试分组")

    async def run():
        result = await contracts.create_contract(
            contracts.ContractCreate(code="sh2701", name="烧碱", group_id=group.id), request, "owner",
        )
        assert result["symbol"] == "CZCE.SH701"
        assert result["code"] == "SH701"
        assert result["name"] == "烧碱"
        assert result["group_id"] == group.id
        assert len(result["periods"]) == 7
        with pytest.raises(HTTPException) as duplicate:
            await contracts.create_contract(contracts.ContractCreate(code="SH701"), request, "owner")
        assert duplicate.value.status_code == 409

    asyncio.run(run())
    assert validated == ["CZCE.SH2701", "CZCE.SH701"]
    assert len(repo.list_contracts("owner")) == 1


def test_change_to_alias_of_same_contract_preserves_identity_and_periods(setup_contracts):
    repo, request, validated = setup_contracts
    original = repo.create_contract("owner", "CZCE.SH701", "原名称")
    result = asyncio.run(contracts.update_contract(
        original.id, contracts.ContractCreate(code="SH2701"), request, "owner",
    ))
    assert validated == ["CZCE.SH2701"]
    assert result["id"] == original.id
    assert result["name"] == original.name
    assert result["symbol"] == original.symbol
    assert [p["id"] for p in result["periods"]] == [p.id for p in original.periods]


def test_change_to_another_contract_stores_verified_canonical_symbol(setup_contracts):
    repo, request, _ = setup_contracts
    original = repo.create_contract("owner", "CZCE.SH611", "原名称")
    result = asyncio.run(contracts.update_contract(
        original.id, contracts.ContractCreate(code="SH2701", name="新名称"), request, "owner",
    ))
    assert result["id"] == original.id
    assert result["symbol"] == "CZCE.SH701"
    assert result["name"] == "新名称"
    assert [p["id"] for p in result["periods"]] == [p.id for p in original.periods]


@pytest.mark.parametrize("query", ["sh2701", "SH701"])
def test_suggestions_match_four_and_three_digit_codes(setup_contracts, query):
    _, request, _ = setup_contracts
    result = asyncio.run(contracts.list_contract_suggestions(request, query, "owner"))
    assert result == [{"value": "SH701", "display": "SH701", "exchange": "CZCE", "symbol": "CZCE.SH701"}]


def test_invalid_partial_code_does_not_contact_market_or_create_record(setup_contracts):
    repo, request, validated = setup_contracts
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(contracts.create_contract(contracts.ContractCreate(code="SH270"), request, "owner"))
    assert invalid.value.status_code == 422
    assert validated == []
    assert repo.list_contracts("owner") == []


def test_wrong_decade_is_rejected_without_persisting_contract(setup_contracts, monkeypatch):
    repo, request, _ = setup_contracts
    client = TqClient()
    request.app.state.tq_client = client
    monkeypatch.setattr(client, "_get_api", lambda: object())
    monkeypatch.setattr(client, "_wait_contract_query", lambda *_: SimpleNamespace(
        ins_class="FUTURE", expired=False, delivery_year=2037, delivery_month=1,
    ))
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(contracts.create_contract(contracts.ContractCreate(code="SH2701"), request, "owner"))
    assert invalid.value.status_code == 422
    assert isinstance(invalid.value.__cause__, ContractUnavailable)
    assert repo.list_contracts("owner") == []
