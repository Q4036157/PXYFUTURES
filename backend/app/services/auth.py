"""主平台短时应用票据与本机客户版认证。"""
from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from jose import JWTError, jwt

from app.config import settings

_APP_ID = "futures"
_SESSION_COOKIE = "pxy_futures_session"
_DESKTOP_USER_ID = "desktop-local-user"

def auth_mode() -> str:
    """区分主平台会话与仅绑定本机回环地址的客户版模式。"""
    return "desktop_local" if settings.desktop_mode else "app_session"


def desktop_user_id(client_host: str | None, origin: str | None = None) -> str | None:
    """客户版只允许来自本机回环地址的请求使用本地身份。"""
    if not settings.desktop_mode or not client_host:
        return None
    try:
        if not ip_address(client_host).is_loopback:
            return None
    except ValueError:
        return None
    if origin:
        origin_host = urlparse(origin).hostname
        try:
            if not origin_host or not ip_address(origin_host).is_loopback:
                return None
        except ValueError:
            return None
    return _DESKTOP_USER_ID


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
    if settings.desktop_mode:
        local_user_id = desktop_user_id(
            request.client.host if request.client else None,
            request.headers.get("origin"),
        )
        if local_user_id:
            return local_user_id
        raise HTTPException(status_code=403, detail="客户版只允许本机访问")

    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="缺少登录凭据")
    user_id = _decode_app_session(token)
    if user_id:
        return user_id
    if not settings.app_session_secret.strip():
        raise HTTPException(status_code=503, detail="期货应用未配置主平台会话密钥")
    raise HTTPException(status_code=401, detail="主平台登录已失效")
