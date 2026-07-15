"""独立部署认证接口。集成主平台时不暴露本地密码登录。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["认证"])


class PasswordBody(BaseModel):
    password: str = Field(min_length=6, max_length=128)


@router.get("/status")
async def status() -> dict[str, object]:
    return {
        "mode": auth.auth_mode(),
        "configured": True if auth.is_integrated_mode() else auth.is_local_password_configured(),
    }


@router.post("/setup")
async def setup(body: PasswordBody) -> dict[str, bool]:
    if auth.is_integrated_mode():
        raise HTTPException(status_code=404, detail="当前由主平台登录认证")
    if auth.is_local_password_configured():
        raise HTTPException(status_code=409, detail="本地密码已设置")
    auth.set_local_password(body.password)
    return {"ok": True}


@router.post("/login")
async def login(body: PasswordBody) -> dict[str, str]:
    if auth.is_integrated_mode():
        raise HTTPException(status_code=404, detail="当前由主平台登录认证")
    token = auth.login_local(body.password)
    if not token:
        raise HTTPException(status_code=401, detail="密码错误或尚未初始化")
    return {"access_token": token, "token_type": "bearer"}
