"""集成主平台 JWT 与独立部署本地会话认证。"""
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


def is_integrated_mode() -> bool:
    return bool(settings.jwt_secret.strip())


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
    return "jwt" if is_integrated_mode() else "local"


def _bearer_token(request: Request) -> str:
    prefix, _, value = request.headers.get("Authorization", "").partition(" ")
    if prefix.lower() != "bearer" or not value:
        raise HTTPException(status_code=401, detail="缺少登录凭据")
    return value


async def current_user_id(request: Request) -> str:
    token = _bearer_token(request)
    if is_integrated_mode():
        try:
            payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        except JWTError as exc:
            raise HTTPException(status_code=401, detail="主平台登录已失效") from exc
        if payload.get("type") not in (None, "access") or not payload.get("sub"):
            raise HTTPException(status_code=401, detail="无效的主平台令牌")
        return str(payload["sub"])

    session = _load_auth().get("sessions", {})
    expires_at = session.get(token) if isinstance(session, dict) else None
    if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
        raise HTTPException(status_code=401, detail="登录已失效")
    return _LOCAL_USER_ID
