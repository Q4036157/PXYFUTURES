from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import desktop


def test_frozen_client_uses_local_app_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Client\AppData\Local")

    assert desktop._default_data_dir() == Path(
        r"C:\Users\Client\AppData\Local\PXYFUTURES\data"
    )


def test_client_uses_dedicated_default_port_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PXYFUTURES_DESKTOP_PORT", raising=False)

    assert desktop._port_candidates() == tuple(range(31220, 31230))


def test_existing_instance_reports_its_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        desktop,
        "_health_info",
        lambda port: {
            "status": "ok",
            "service": "pxyfutures",
            "version": "2026.09.10.1",
        }
        if port == 31221
        else None,
    )

    assert desktop._find_existing_instance((31220, 31221)) == (
        31221,
        "2026.09.10.1",
    )
