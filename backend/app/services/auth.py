"""集成主平台短时应用票据、遗留 JWT 与独立部署本地会话认证。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request
from jose import JWTError, jwt

from app.config import settings

_PBKDF2_ITERATIONS = 200_000
_LOCAL_USER_ID = "local-user"
_APP_ID = "futures"
_SESSION_COOKIE = "pxy_futures_session"


def _auth_path() -> Path:
    path = settings.data_dir / "user_data" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_auth() -> dict[str, object]:
    path = _auth_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_auth(value: dict[str, object]) -> None:
    path = _auth_path()
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_app_session_mode() -> bool:
    return bool(settings.app_session_secret.strip())


def is_legacy_jwt_mode() -> bool:
    return bool(settings.jwt_secret.strip())


def is_integrated_mode() -> bool:
    """是否接入主平台（短时票据或遗留 JWT）。"""
    return is_app_session_mode() or is_legacy_jwt_mode()


def is_local_password_configured() -> bool:
    return bool(_load_auth().get("password_hash"))


def set_local_password(password: str) -> None:
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    _save_auth({"password_salt": salt.hex(), "password_hash": digest.hex(), "sessions": {}})


def login_local(password: str) -> str | None:
    payload = _load_auth()
    try:
        expected = bytes.fromhex(str(payload["password_hash"]))
        salt = bytes.fromhex(str(payload["password_salt"]))
    except (KeyError, ValueError):
        return None
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    if not hmac.compare_digest(actual, expected):
        return None
    token = secrets.token_urlsafe(32)
    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), dict) else {}
    sessions[token] = time.time() + 30 * 24 * 3600
    payload["sessions"] = sessions
    _save_auth(payload)
    return token


def auth_mode() -> str:
    if is_app_session_mode():
        return "app_session"
    if is_legacy_jwt_mode():
        return "jwt"
    return "local"


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


def _decode_legacy_platform_access(token: str) -> str | None:
    secret = settings.jwt_secret.strip()
    if not secret:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError:
        return None
    if payload.get("type") not in (None, "access") or not payload.get("sub"):
        return None
    return str(payload["sub"])


async def current_user_id(request: Request) -> str:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="缺少登录凭据")

    if is_integrated_mode():
        user_id = _decode_app_session(token)
        if user_id:
            return user_id
        user_id = _decode_legacy_platform_access(token)
        if user_id:
            return user_id
        raise HTTPException(status_code=401, detail="主平台登录已失效")

    session = _load_auth().get("sessions", {})
    expires_at = session.get(token) if isinstance(session, dict) else None
    if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
        raise HTTPException(status_code=401, detail="登录已失效")
    return _LOCAL_USER_ID