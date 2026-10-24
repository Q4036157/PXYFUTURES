"""二级条件单的创建、领取提醒与按账号删除。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from app.api.signals import _SUPPORTED_ALERT_DURATIONS
from app.services.auth import current_user_id
from app.services.followup_alerts import CONDITIONS, STATES, FollowupAlertService, SourceConflict
from app.services.repository import DuplicateFollowupAlert

router = APIRouter(prefix="/api/followup-alerts", tags=["二级预警"])


class FollowupSource(BaseModel):
    duration_seconds: int = Field(gt=0)
    condition: str | None = Field(default=None, max_length=12)
    state: str = Field(max_length=20)


class FollowupBody(BaseModel):
    contract_id: int = Field(gt=0)
    source: FollowupSource
    duration_seconds: int
    conditions: list[str] = Field(default_factory=list, max_length=8)
    states: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("duration_seconds")
    @classmethod
    def supported_duration(cls, value: int) -> int:
        if value not in _SUPPORTED_ALERT_DURATIONS:
            raise ValueError("预警周期不受支持")
        return value

    @field_validator("conditions", "states")
    @classmethod
    def valid_choices(cls, values: list[str], info: object) -> list[str]:
        choices = CONDITIONS if getattr(info, "field_name", "") == "conditions" else STATES
        if any(value not in choices for value in values):
            raise ValueError("预警条件或状态不受支持")
        return list(dict.fromkeys(values))


class ObservedSource(FollowupSource):
    confirmed_bar_time: str = Field(min_length=1, max_length=50)


class SourceReplacement(BaseModel):
    previous: FollowupSource
    observed: ObservedSource


def _service(request: Request) -> FollowupAlertService:
    return request.app.state.followup_alerts


@router.get("")
async def list_rules(request: Request, user_id: str = Depends(current_user_id)) -> dict:
    return {"rules": await asyncio.to_thread(_service(request).list_rules, user_id)}


@router.post("", status_code=201)
async def create_rule(
    body: FollowupBody, request: Request, user_id: str = Depends(current_user_id),
) -> dict:
    service = _service(request)
    try:
        rule_id = await asyncio.to_thread(
            service.create_rule, user_id, body.contract_id, body.source.model_dump(),
            body.duration_seconds, body.conditions, body.states,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DuplicateFollowupAlert as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"created_id": rule_id, "rules": await asyncio.to_thread(service.list_rules, user_id)}


@router.post("/notifications/claim")
async def claim_notifications(request: Request, user_id: str = Depends(current_user_id)) -> dict:
    return {"notifications": await asyncio.to_thread(
        _service(request).repository.claim_followup_notifications, user_id,
    )}


@router.post("/{rule_id}/source")
async def replace_source(
    rule_id: int, body: SourceReplacement, request: Request, user_id: str = Depends(current_user_id),
) -> dict:
    service = _service(request)
    try:
        await asyncio.to_thread(
            service.replace_source, user_id, rule_id, body.previous.model_dump(), body.observed.model_dump(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (SourceConflict, DuplicateFollowupAlert) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"rules": await asyncio.to_thread(service.list_rules, user_id)}


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: int, request: Request, user_id: str = Depends(current_user_id),
) -> Response:
    deleted = await asyncio.to_thread(
        _service(request).repository.delete_followup_alert, user_id, rule_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="条件单不存在")
    return Response(status_code=204)
