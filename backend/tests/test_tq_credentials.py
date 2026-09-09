import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import settings as settings_api
from app.config import settings
from app.market.tq_client import (
    MarketDataUnavailable,
    TqClient,
    TqCredentialsInvalid,
    _is_explicit_credential_rejection,
)
from app.services.tq_auth_state import load_tq_auth_state, record_tq_auth_state


def test_validate_credentials_uses_auth_only_without_market_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Auth:
        def __init__(self, username: str, password: str) -> None:
            calls.append((username, password))

        @staticmethod
        def login() -> None:
            return None

    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(TqAuth=Auth))

    TqClient.validate_credentials(" user ", "secret")

    assert calls == [("user", "secret")]


@pytest.mark.parametrize(
    "error",
    [
        "用户权限认证失败 (403, {'error': 'invalid_grant'})",
        "Invalid user credentials",
    ],
)
def test_validate_credentials_classifies_explicit_rejection(
    monkeypatch: pytest.MonkeyPatch,
    error: str,
) -> None:
    class Auth:
        def __init__(self, _username: str, _password: str) -> None:
            pass

        @staticmethod
        def login() -> None:
            raise RuntimeError(error)

    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(TqAuth=Auth))

    with pytest.raises(TqCredentialsInvalid, match="账号或密码错误"):
        TqClient.validate_credentials("user", "bad")


def test_validate_credentials_preserves_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Auth:
        def __init__(self, _username: str, _password: str) -> None:
            pass

        @staticmethod
        def login() -> None:
            raise TimeoutError("timed out")

    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(TqAuth=Auth))

    with pytest.raises(MarketDataUnavailable, match="认证服务暂时不可用"):
        TqClient.validate_credentials("user", "secret")


def test_credential_rejection_classifier_does_not_mislabel_network_errors() -> None:
    assert _is_explicit_credential_rejection(
        RuntimeError("用户权限认证失败 (403, invalid_grant)")
    )
    assert not _is_explicit_credential_rejection(TimeoutError("timed out"))


def test_auth_state_is_persisted_without_sensitive_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    state = record_tq_auth_state("invalid", "天勤账号或密码错误，请重新输入", "runtime")
    payload = json.loads(
        (tmp_path / "user_data" / "tq_auth_status.json").read_text(encoding="utf-8")
    )

    assert load_tq_auth_state() == state
    assert payload["status"] == "invalid"
    assert set(payload) == {"status", "checked_at", "message", "source"}


def test_save_rejects_invalid_credentials_without_overwriting_saved_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    saved: list[tuple[str, str]] = []

    class Client:
        @staticmethod
        def validate_credentials(_username: str, _password: str) -> None:
            raise TqCredentialsInvalid("天勤账号或密码错误，请重新输入")

    monkeypatch.setattr(settings_api, "save_tq_credentials", lambda *value: saved.append(value))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tq_client=Client())))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            settings_api.save_credentials(
                settings_api.TqCredentials(username="bad", password="bad"),
                request,
                "desktop-local-user",
            )
        )

    assert caught.value.status_code == 422
    assert saved == []
    assert load_tq_auth_state().status == "invalid"


def test_save_persists_only_after_validation_and_resets_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    events: list[object] = []

    class Client:
        @staticmethod
        def validate_credentials(username: str, password: str) -> None:
            events.append(("validate", username, password))

        @staticmethod
        def credentials_changed() -> None:
            events.append("reset")

    monkeypatch.setattr(
        settings_api,
        "save_tq_credentials",
        lambda username, password: events.append(("save", username, password)),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tq_client=Client())))

    result = asyncio.run(
        settings_api.save_credentials(
            settings_api.TqCredentials(username="user", password="secret"),
            request,
            "desktop-local-user",
        )
    )

    assert result["status"] == "valid"
    assert events == [
        ("validate", "user", "secret"),
        ("save", "user", "secret"),
        "reset",
    ]


def test_runtime_rejection_blocks_retries_until_credentials_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(
        "app.market.tq_client.get_tq_credentials",
        lambda: ("saved-user", "saved-password"),
    )
    attempts: list[str] = []

    class Auth:
        def __init__(self, _username: str, _password: str) -> None:
            pass

    class Api:
        def __init__(self, *, auth: object) -> None:
            self.auth = auth

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "tqsdk",
        SimpleNamespace(TqApi=Api, TqAuth=Auth),
    )
    client = TqClient()

    def reject(_username: str, _password: str) -> None:
        attempts.append("rejected")
        raise TqCredentialsInvalid("天勤账号或密码错误，请重新输入")

    monkeypatch.setattr(client, "validate_credentials", reject)
    with pytest.raises(TqCredentialsInvalid):
        client._get_api()
    with pytest.raises(TqCredentialsInvalid):
        client._get_api()

    assert attempts == ["rejected"]
    assert load_tq_auth_state().status == "invalid"

    monkeypatch.setattr(client, "validate_credentials", lambda *_args: None)
    blocked_revision = client.credential_revision
    client.credentials_changed()

    assert client.credential_revision == blocked_revision + 1
    assert client._get_api() is not None
    assert load_tq_auth_state().status == "valid"
