"""合约和各周期均线配置接口。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from app.market.contract_catalog import normalize_contract_code, to_tq_symbol
from app.market.tq_client import ContractUnavailable, MarketDataUnavailable, TqClient
from app.services.auth import current_user_id
from app.services.repository import ConfigRepository, ContractConfig, ContractGroup, PeriodConfig

router = APIRouter(prefix="/api/contracts", tags=["合约配置"])


class ContractCreate(BaseModel):
    exchange: str | None = Field(default=None, min_length=2, max_length=10)
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(default="", max_length=50)
    group_id: int | None = Field(default=None, gt=0)

    @field_validator("exchange", "code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class PeriodBody(BaseModel):
    label: str = Field(min_length=1, max_length=20)
    duration_seconds: int = Field(gt=0, le=604800)
    m4: int = Field(gt=0, le=5000)
    m3: int = Field(gt=0, le=5000)
    m2: int = Field(gt=0, le=5000)
    m1: int = Field(gt=0, le=5000)

    @field_validator("m1")
    @classmethod
    def m1_must_differ_from_m2(cls, value: int, info: object) -> int:
        data = getattr(info, "data", {})
        if value == data.get("m2"):
            raise ValueError("M1 和 M2 不能相同")
        return value


class PeriodNoteBody(BaseModel):
    note: str = Field(default="", max_length=500)


class GroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=30)


class ContractOrderBody(BaseModel):
    contract_ids: list[int] = Field(default_factory=list)


def _repo(request: Request) -> ConfigRepository:
    return request.app.state.config_repository


def _serialize(contract: ContractConfig, client: TqClient | None = None) -> dict[str, object]:
    exchange, _, code = contract.symbol.partition(".")
    return {
        "id": contract.id,
        "symbol": contract.symbol,
        "exchange": exchange,
        "code": code or contract.symbol,
        "name": contract.name,
        "group_id": contract.group_id,
        "sort_order": contract.sort_order,
        "market_status": client.contract_status(contract.symbol) if client else "unknown",
        "periods": [period.__dict__ for period in contract.periods],
    }


def _serialize_group(group: ContractGroup, client: TqClient | None = None) -> dict[str, object]:
    return {
        "id": group.id,
        "name": group.name,
        "sort_order": group.sort_order,
        "contracts": [_serialize(contract, client) for contract in group.contracts],
    }


@router.get("")
async def list_contracts(request: Request, user_id: str = Depends(current_user_id)) -> list[dict[str, object]]:
    client = getattr(request.app.state, "tq_client", None)
    return [_serialize(contract, client) for contract in _repo(request).list_contracts(user_id)]


@router.get("/groups")
async def list_contract_groups(request: Request, user_id: str = Depends(current_user_id)) -> list[dict[str, object]]:
    client = getattr(request.app.state, "tq_client", None)
    return [_serialize_group(group, client) for group in _repo(request).list_contract_groups(user_id)]


@router.post("/groups", status_code=201)
async def create_contract_group(
    body: GroupBody, request: Request, user_id: str = Depends(current_user_id)
) -> dict[str, object]:
    try:
        return _serialize_group(_repo(request).create_group(user_id, body.name))
    except Exception as exc:  # sqlite unique constraint
        raise HTTPException(status_code=409, detail="该分组名称已存在") from exc


@router.put("/groups/{group_id}")
async def rename_contract_group(
    group_id: int,
    body: GroupBody,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> dict[str, object]:
    try:
        group = _repo(request).rename_group(user_id, group_id, body.name)
    except Exception as exc:  # sqlite unique constraint
        raise HTTPException(status_code=409, detail="该分组名称已存在") from exc
    if group is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    return _serialize_group(group)


@router.get("/suggestions")
async def list_contract_suggestions(
    request: Request,
    query: str = Query(min_length=1, max_length=30),
    user_id: str = Depends(current_user_id),
) -> list[dict[str, str]]:
    del user_id
    normalized = normalize_contract_code(query)
    if not normalized.product or not normalized.exchange:
        return []
    client: TqClient = request.app.state.tq_client
    try:
        symbols = await asyncio.to_thread(client.list_active_futures, normalized.exchange, normalized.product)
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    prefix = to_tq_symbol(f"{normalized.exchange}.{normalized.code}").partition(".")[2].upper()
    suggestions: list[dict[str, str]] = []
    for symbol in symbols:
        exchange, _, code = symbol.partition(".")
        code = code.upper()
        if code.startswith(prefix):
            suggestions.append({"value": code, "exchange": exchange.upper(), "symbol": f"{exchange.upper()}.{code}"})
    return suggestions[:20]


@router.post("", status_code=201)
async def create_contract(
    body: ContractCreate, request: Request, user_id: str = Depends(current_user_id)
) -> dict[str, object]:
    normalized = normalize_contract_code(body.code)
    if not normalized.complete:
        raise HTTPException(
            status_code=422,
            detail="请输入完整合约代码，例如 FG609、RB2609；无法识别的品种请确认代码后重试",
        )
    exchange = normalized.exchange or (body.exchange or "").upper()
    if not exchange:
        raise HTTPException(status_code=422, detail="无法识别交易所，请手动选择交易所")
    symbol = f"{exchange}.{normalized.code}"
    client: TqClient = request.app.state.tq_client
    try:
        symbol = (await asyncio.to_thread(client.require_active_future, symbol)).upper()
    except ContractUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        contract = _repo(request).create_contract(
            user_id,
            symbol,
            body.name or body.code.strip().upper(),
            body.group_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # sqlite unique constraint
        raise HTTPException(status_code=409, detail="该合约已添加") from exc
    return _serialize(contract)


class ContractGroupBody(BaseModel):
    group_id: int = Field(gt=0)


@router.put("/{contract_id}/group")
async def move_contract_group(
    contract_id: int,
    body: ContractGroupBody,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> dict[str, object]:
    moved = _repo(request).move_contract(user_id, contract_id, body.group_id)
    if moved is None:
        raise HTTPException(status_code=404, detail="合约或分组不存在")
    return _serialize(moved)


@router.put("/groups/{group_id}/order")
async def reorder_contract_group(
    group_id: int,
    body: ContractOrderBody,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> dict[str, bool]:
    if not _repo(request).reorder_contracts(user_id, group_id, body.contract_ids):
        raise HTTPException(status_code=400, detail="排序中的合约必须完整属于当前分组")
    return {"ok": True}


@router.put("/{contract_id}")
async def update_contract(
    contract_id: int,
    body: ContractCreate,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> dict[str, object]:
    repository = _repo(request)
    current = repository.get_contract(user_id, contract_id)
    if current is None:
        raise HTTPException(status_code=404, detail="合约不存在")

    normalized = normalize_contract_code(body.code)
    if not normalized.complete:
        raise HTTPException(
            status_code=422,
            detail="请输入完整合约代码，例如 PP2701、RB2610",
        )
    exchange = normalized.exchange or (body.exchange or "").upper()
    if not exchange:
        raise HTTPException(status_code=422, detail="无法识别交易所，请手动选择交易所")
    symbol = f"{exchange}.{normalized.code}"
    if symbol == current.symbol:
        return _serialize(current)

    client: TqClient = request.app.state.tq_client
    try:
        symbol = (await asyncio.to_thread(client.require_active_future, symbol)).upper()
    except ContractUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if symbol == current.symbol:
        return _serialize(current)

    try:
        updated = repository.update_contract(
            user_id,
            contract_id,
            symbol,
            body.name or normalized.code,
        )
    except Exception as exc:  # sqlite unique constraint
        raise HTTPException(status_code=409, detail="目标合约已经添加，请先处理现有合约") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="合约不存在")
    return _serialize(updated)


@router.delete("/{contract_id}", status_code=204)
async def delete_contract(
    contract_id: int, request: Request, user_id: str = Depends(current_user_id)
) -> Response:
    if not _repo(request).delete_contract(user_id, contract_id):
        raise HTTPException(status_code=404, detail="合约不存在")
    return Response(status_code=204)


@router.put("/{contract_id}/periods")
async def save_period(
    contract_id: int, body: PeriodBody, request: Request, user_id: str = Depends(current_user_id)
) -> dict[str, object]:
    saved = _repo(request).save_period(
        user_id,
        contract_id,
        PeriodConfig(id=None, **body.model_dump()),
    )
    if saved is None:
        raise HTTPException(status_code=404, detail="合约不存在")
    return saved.__dict__


@router.delete("/{contract_id}/periods/{duration_seconds}", status_code=204)
async def delete_period(
    contract_id: int, duration_seconds: int, request: Request, user_id: str = Depends(current_user_id)
) -> Response:
    if not _repo(request).delete_period(user_id, contract_id, duration_seconds):
        raise HTTPException(status_code=404, detail="周期不存在")
    return Response(status_code=204)


@router.put("/{contract_id}/periods/{duration_seconds}/note")
async def save_period_note(
    contract_id: int,
    duration_seconds: int,
    body: PeriodNoteBody,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> dict[str, str]:
    if not _repo(request).save_period_note(user_id, contract_id, duration_seconds, body.note):
        raise HTTPException(status_code=404, detail="周期不存在")
    return {"note": body.note}
