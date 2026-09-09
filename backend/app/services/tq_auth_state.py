"""持久化天勤认证状态，不记录用户名、密码或访问令牌。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Literal

from app.config import settings
from app.services.secrets_store import has_tq_credentials

TqAuthStateName = Literal["unconfigured", "unknown", "valid", "invalid", "unavailable"]
_CHINA_TZ = timezone(timedelta(hours=8))
_lock = Lock()


@dataclass(frozen=True)
class TqAuthState:
    status: TqAuthStateName
    checked_at: str | None
    message: str
    source: str


def _path() -> Path:
    path = settings.data_dir / "user_data" / "tq_auth_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def default_tq_auth_state() -> TqAuthState:
    if has_tq_credentials():
        return TqAuthState("unknown", None, "账号已保存，等待真实认证", "startup")
    return TqAuthState("unconfigured", None, "尚未保存天勤账号", "startup")


def load_tq_auth_state() -> TqAuthState:
    path = _path()
    if not path.exists():
        return default_tq_auth_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        status = str(payload.get("status", ""))
        if status not in {"unconfigured", "unknown", "valid", "invalid", "unavailable"}:
            return default_tq_auth_state()
        return TqAuthState(
            status=status,  # type: ignore[arg-type]
            checked_at=payload.get("checked_at"),
            message=str(payload.get("message", "")),
            source=str(payload.get("source", "runtime")),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default_tq_auth_state()


def record_tq_auth_state(
    status: TqAuthStateName,
    message: str,
    source: str,
) -> TqAuthState:
    """原子更新脱敏认证状态，供页面与客户日志排查使用。"""
    state = TqAuthState(
        status=status,
        checked_at=datetime.now(_CHINA_TZ).isoformat(),
        message=message,
        source=source,
    )
    path = _path()
    temporary = Path(f"{path}.tmp")
    with _lock:
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    return state
