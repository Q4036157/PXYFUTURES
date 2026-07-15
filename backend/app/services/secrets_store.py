"""本地私有凭据存储。

文件仅写入项目 data/user_data，且不回传密码给前端。当前实现与 DAA 的
本地 secrets 约定保持一致；部署时应限制该目录的操作系统访问权限。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.config import settings


def _path() -> Path:
    path = settings.data_dir / "user_data" / "secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load() -> dict[str, str]:
    path = _path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, str]) -> None:
    path = _path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_tq_credentials(username: str, password: str) -> None:
    data = _load()
    data["tq_user_name"] = username.strip()
    data["tq_password"] = password
    _save(data)


def get_tq_credentials() -> tuple[str, str]:
    data = _load()
    return data.get("tq_user_name", ""), data.get("tq_password", "")


def has_tq_credentials() -> bool:
    username, password = get_tq_credentials()
    return bool(username and password) or bool(settings.tq_user_name and settings.tq_password)
