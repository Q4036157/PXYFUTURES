"""本地天勤账号配置接口。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.market.tq_client import MarketDataUnavailable, TqCredentialsInvalid
from app.services.auth import current_user_id
from app.services.secrets_store import get_tq_credentials, has_tq_credentials, save_tq_credentials
from app.services.tq_auth_state import load_tq_auth_state, record_tq_auth_state

router = APIRouter(prefix="/api/settings", tags=["设置"])
logger = logging.getLogger(__name__)


class TqCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.get("/tq-credentials")
async def tq_credentials_status(request: Request, _: str = Depends(current_user_id)) -> dict[str, object]:
    state = load_tq_auth_state()
    payload = {"configured": has_tq_credentials(), **state.__dict__}
    if state.status == "valid":
        payload["message"] = "账号此前已通过认证；当前行情状态见下方连接提示"
    return {**payload, **request.app.state.tq_client.connection_state()}


@router.put("/tq-credentials")
async def save_credentials(
    body: TqCredentials, request: Request, _: str = Depends(current_user_id)
) -> dict[str, object]:
    # 多个页面同时保存时串行验证，避免旧请求晚完成覆盖新账号。
    if not hasattr(request.app.state, "tq_credentials_save_lock"):
        request.app.state.tq_credentials_save_lock = asyncio.Lock()
    async with request.app.state.tq_credentials_save_lock:
        return await _save_credentials(body, request)


async def _save_credentials(body: TqCredentials, request: Request) -> dict[str, object]:
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

    previous = await asyncio.to_thread(get_tq_credentials)
    await asyncio.to_thread(save_tq_credentials, body.username, body.password)
    state = record_tq_auth_state("valid", "天勤账号真实认证成功并保存", "save_validation")
    logger.info("天勤账号保存验证成功")
    # 只有真实认证成功后才替换旧连接；失败输入不会覆盖原可用凭据。
    if previous != (body.username.strip(), body.password) or getattr(client, "credentials_blocked", False):
        client.request_credentials_change()
    return {"configured": True, **state.__dict__}
