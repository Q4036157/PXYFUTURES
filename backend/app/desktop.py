"""Windows 单文件交付版入口：显示日志、启动服务并打开浏览器。"""
from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

_DEFAULT_PORT = 3022
_LAST_PORT = 3032


def _configure_console() -> None:
    """确保中文日志可读，并为窗口设置明确标题。"""
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleTitleW("智能期货 - 实时运行日志")
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "ok" and payload.get("service") == "pxyfutures"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _find_existing_instance() -> int | None:
    return next((port for port in range(_DEFAULT_PORT, _LAST_PORT + 1) if _health(port)), None)


def _find_available_port() -> int:
    port = next(
        (port for port in range(_DEFAULT_PORT, _LAST_PORT + 1) if _port_available(port)),
        None,
    )
    if port is None:
        raise RuntimeError(f"端口 {_DEFAULT_PORT}-{_LAST_PORT} 均被占用，请关闭其他程序后重试")
    return port


def _open_browser_when_ready(port: int) -> None:
    if os.getenv("PXYFUTURES_NO_BROWSER") == "1":
        return
    for _ in range(120):
        if _health(port):
            webbrowser.open(f"http://127.0.0.1:{port}/")
            return
        time.sleep(0.25)
    print("服务启动超时，请查看上方日志；也可手动打开浏览器访问显示的网址。")


def _pause_after_error() -> None:
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n启动失败。请拍下本窗口日志发给技术人员，按回车键关闭窗口……")
        except (EOFError, KeyboardInterrupt):
            pass


def main() -> int:
    multiprocessing.freeze_support()
    _configure_console()
    runtime_dir = _runtime_dir()
    os.chdir(runtime_dir)
    os.environ.setdefault("DATA_DIR", str(runtime_dir / "data"))

    print("=" * 64)
    print("智能期货正在启动")
    print("本窗口显示实时日志，请勿关闭；关闭窗口将停止软件。")
    print(f"本地数据：{runtime_dir / 'data'}")
    print("=" * 64)

    existing_port = _find_existing_instance()
    if existing_port is not None:
        print(f"智能期货已经运行，正在打开：http://127.0.0.1:{existing_port}/")
        webbrowser.open(f"http://127.0.0.1:{existing_port}/")
        return 0

    try:
        port = _find_available_port()
        os.environ["PORT"] = str(port)

        # 环境与数据目录准备完成后再导入应用，确保日志写入 EXE 旁的 data。
        import uvicorn

        from app.main import app

        print(f"软件地址：http://127.0.0.1:{port}/")
        print("浏览器将在服务就绪后自动打开。按 Ctrl+C 可安全停止。\n")
        threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=True)
        return 0
    except KeyboardInterrupt:
        print("\n智能期货已停止。")
        return 0
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        _pause_after_error()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
