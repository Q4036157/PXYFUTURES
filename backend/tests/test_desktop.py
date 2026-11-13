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


def test_busy_existing_instance_does_not_start_second_server(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_configure_output", lambda *_args: None)
    monkeypatch.setattr(desktop, "_acquire_instance_guard", lambda: (123, True))
    monkeypatch.setattr(desktop, "_find_existing_instance", lambda *_args: None)
    monkeypatch.setattr(desktop, "_find_available_port", lambda *_args: pytest.fail("不应启动第二实例"))
    errors, released = [], []
    monkeypatch.setattr(desktop, "_show_error_dialog", lambda error: errors.append(str(error)))
    monkeypatch.setattr(desktop, "_release_instance_guard", released.append)
    # 入口会设置工作目录和运行环境；测试结束须恢复，避免污染其他用例。
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PXYFUTURES_DESKTOP", "0")
    assert desktop.main() == 1
    assert "已在后台运行" in errors[0]
    assert released == [123]


@pytest.mark.skipif(sys.platform != "win32", reason="仅验证Windows进程句柄")
def test_windows_instance_guard_detects_second_handle():
    first, _existing = desktop._acquire_instance_guard()
    try:
        second, existing = desktop._acquire_instance_guard()
        try:
            assert existing
        finally:
            desktop._release_instance_guard(second)
    finally:
        desktop._release_instance_guard(first)
