"""仅使用 PXYLH 主平台签发的短时应用票据认证。"""
from __future__ import annotations

from fastapi import HTTPException, Request
from jose import JWTError, jwt

from app.config import settings

_APP_ID = "futures"
_SESSION_COOKIE = "pxy_futures_session"

def auth_mode() -> str:
    """返回固定的主平台认证模式，便于健康检查确认配置。"""
    return "app_session"


def _extract_token(request: Request) -> str | None:
    prefix, _, value = request.headers.get("Authorization", "").partition(" ")
    if prefix.lower() == "bearer" and value.strip():
        return value.strip()
    cookie = request.cookies.get(_SESSION_COOKIE, "").strip()
    return cookie or None


def _decode_app_session(token: str) -> str | None:
    secret = settings.app_session_secret.strip()
    if not secret:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError:
        return None
    if payload.get("type") != "app_session":
        return None
    if payload.get("app_id") != _APP_ID:
        return None
    user_id = payload.get("sub")
    return str(user_id) if user_id else None


async def current_user_id(request: Request) -> str:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="缺少登录凭据")
    user_id = _decode_app_session(token)
    if user_id:
        return user_id
    if not settings.app_session_secret.strip():
        raise HTTPException(status_code=503, detail="期货应用未配置主平台会话密钥")
    raise HTTPException(status_code=401, detail="主平台登录已失效")
