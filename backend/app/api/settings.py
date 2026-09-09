"""本地天勤账号配置接口。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.market.tq_client import MarketDataUnavailable, TqCredentialsInvalid
from app.services.auth import current_user_id
from app.services.secrets_store import has_tq_credentials, save_tq_credentials
from app.services.tq_auth_state import load_tq_auth_state, record_tq_auth_state

router = APIRouter(prefix="/api/settings", tags=["设置"])
logger = logging.getLogger(__name__)


class TqCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.get("/tq-credentials")
async def tq_credentials_status(_: str = Depends(current_user_id)) -> dict[str, object]:
    state = load_tq_auth_state()
    return {"configured": has_tq_credentials(), **state.__dict__}


@router.put("/tq-credentials")
async def save_credentials(
    body: TqCredentials, request: Request, _: str = Depends(current_user_id)
) -> dict[str, object]:
    client = request.app.state.tq_client
    try:
        await asyncio.to_thread(
            client.validate_credentials,
            body.username,
            body.password,
        )
    except TqCredentialsInvalid as exc:
        state = record_tq_auth_state("invalid", str(exc), "save_validation")
        logger.warning("天勤账号保存验证失败: result=invalid_credentials")
        raise HTTPException(status_code=422, detail=state.message) from exc
    except MarketDataUnavailable as exc:
        state = record_tq_auth_state("unavailable", str(exc), "save_validation")
        logger.warning("天勤账号保存验证失败: result=auth_service_unavailable")
        raise HTTPException(status_code=503, detail=state.message) from exc

    save_tq_credentials(body.username, body.password)
    state = record_tq_auth_state("valid", "天勤账号真实认证成功", "save_validation")
    logger.info("天勤账号保存验证成功")
    # 只有真实认证成功后才替换旧连接；失败输入不会覆盖原可用凭据。
    client.credentials_changed()
    return {"configured": True, **state.__dict__}
