"""本地天勤账号配置接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.services.auth import current_user_id
from app.services.secrets_store import has_tq_credentials, save_tq_credentials

router = APIRouter(prefix="/api/settings", tags=["设置"])


class TqCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.get("/tq-credentials")
async def tq_credentials_status(_: str = Depends(current_user_id)) -> dict[str, bool]:
    return {"configured": has_tq_credentials()}


@router.put("/tq-credentials")
async def save_credentials(
    body: TqCredentials, request: Request, _: str = Depends(current_user_id)
) -> dict[str, bool]:
    save_tq_credentials(body.username, body.password)
    # 凭据变更后强制下一次查询重新认证，不能沿用旧连接。
    request.app.state.tq_client.close()
    return {"configured": True}
