"""主平台会话认证状态接口。"""
from __future__ import annotations

from fastapi import APIRouter

from app.config import settings
from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.get("/status")
async def status() -> dict[str, object]:
    return {
        "mode": auth.auth_mode(),
        "configured": bool(settings.app_session_secret.strip()),
    }
