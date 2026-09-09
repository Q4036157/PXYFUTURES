"""主平台短时应用票据认证测试。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from jose import jwt

from app.config import settings
from app.services import auth


def _make_request(
    headers: dict[str, str] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 12345),
) -> object:
    from starlette.requests import Request

    header_list = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/contracts",
        "raw_path": b"/api/contracts",
        "query_string": b"",
        "headers": header_list,
        "client": client,
        "server": ("127.0.0.1", 3022),
    }
    return Request(scope)


def test_app_session_bearer_accepted(monkeypatch):
    monkeypatch.setattr(settings, "desktop_mode", False)
    secret = "test-app-session-secret"
    monkeypatch.setattr(settings, "app_session_secret", secret)
    token = jwt.encode(
        {
            "sub": "user-42",
            "app_id": "futures",
            "type": "app_session",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        secret,
        algorithm="HS256",
    )
    request = _make_request({"Authorization": f"Bearer {token}"})
    assert asyncio.run(auth.current_user_id(request)) == "user-42"
    assert auth.auth_mode() == "app_session"


def test_app_session_cookie_accepted(monkeypatch):
    monkeypatch.setattr(settings, "desktop_mode", False)
    secret = "test-app-session-secret"
    monkeypatch.setattr(settings, "app_session_secret", secret)
    token = jwt.encode(
        {
            "sub": "user-9",
            "app_id": "futures",
            "type": "app_session",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        secret,
        algorithm="HS256",
    )
    request = _make_request({"cookie": f"pxy_futures_session={token}"})
    assert asyncio.run(auth.current_user_id(request)) == "user-9"


def test_wrong_app_id_rejected(monkeypatch):
    monkeypatch.setattr(settings, "desktop_mode", False)
    secret = "test-app-session-secret"
    monkeypatch.setattr(settings, "app_session_secret", secret)
    token = jwt.encode(
        {
            "sub": "user-42",
            "app_id": "daa",
            "type": "app_session",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        secret,
        algorithm="HS256",
    )
    request = _make_request({"Authorization": f"Bearer {token}"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.current_user_id(request))
    assert exc.value.status_code == 401


def test_missing_app_session_secret_returns_service_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "desktop_mode", False)
    monkeypatch.setattr(settings, "app_session_secret", "")
    request = _make_request({"Authorization": "Bearer invalid-token"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.current_user_id(request))
    assert exc.value.status_code == 503


def test_legacy_access_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "desktop_mode", False)
    monkeypatch.setattr(settings, "app_session_secret", "shared-app-session-secret")
    token = jwt.encode(
        {
            "sub": "user-7",
            "type": "access",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "legacy-platform-secret",
        algorithm="HS256",
    )
    request = _make_request({"Authorization": f"Bearer {token}"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.current_user_id(request))
    assert exc.value.status_code == 401


def test_desktop_mode_accepts_loopback_without_platform_token(monkeypatch):
    monkeypatch.setattr(settings, "desktop_mode", True)
    monkeypatch.setattr(settings, "app_session_secret", "")

    assert asyncio.run(auth.current_user_id(_make_request())) == "desktop-local-user"
    assert auth.auth_mode() == "desktop_local"


def test_desktop_mode_rejects_non_loopback_request(monkeypatch):
    monkeypatch.setattr(settings, "desktop_mode", True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.current_user_id(_make_request(client=("192.0.2.10", 12345))))
    assert exc.value.status_code == 403


def test_desktop_mode_rejects_cross_site_browser_origin(monkeypatch):
    monkeypatch.setattr(settings, "desktop_mode", True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            auth.current_user_id(
                _make_request(headers={"origin": "https://untrusted.example"})
            )
        )
    assert exc.value.status_code == 403
