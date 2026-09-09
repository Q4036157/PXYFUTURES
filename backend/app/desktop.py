"""Windows 客户版入口：静默启动本地服务、托盘菜单和浏览器。"""
from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, TextIO

_DEFAULT_PORT = 31220
_LAST_PORT = 31229
_APP_DATA_FOLDER = "PXYFUTURES"
_output_stream: TextIO | None = None


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _default_data_dir() -> Path:
    if not getattr(sys, "frozen", False):
        return _runtime_dir() / "data"
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / _APP_DATA_FOLDER / "data"
    return Path.home() / "AppData" / "Local" / _APP_DATA_FOLDER / "data"


def _configure_output(data_dir: Path) -> None:
    """有控制台时配置中文；无控制台时把启动输出写入本地日志。"""
    global _output_stream

    has_console = os.name != "nt" or bool(ctypes.windll.kernel32.GetConsoleWindow())
    if has_console:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleTitleW("智能期货 - 实时运行日志")
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        return

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _output_stream = (log_dir / "desktop-bootstrap.log").open(
        "a", encoding="utf-8", buffering=1
    )
    sys.stdout = _output_stream
    sys.stderr = _output_stream


def _show_error_dialog(error: BaseException) -> None:
    message = (
        "智能期货启动失败。\n\n"
        f"{error}\n\n"
        "请查看本机 AppData\\Local\\PXYFUTURES\\data\\logs 中的日志。"
    )
    if os.name == "nt" and os.getenv("PXYFUTURES_NO_DIALOG") != "1":
        ctypes.windll.user32.MessageBoxW(None, message, "智能期货", 0x10)
    else:
        print(message)


def _port_candidates() -> tuple[int, ...]:
    configured = os.getenv("PXYFUTURES_DESKTOP_PORT", "").strip()
    if not configured:
        return tuple(range(_DEFAULT_PORT, _LAST_PORT + 1))
    try:
        port = int(configured)
    except ValueError as exc:
        raise RuntimeError("PXYFUTURES_DESKTOP_PORT 必须是有效端口") from exc
    if not 1024 <= port <= 65535:
        raise RuntimeError("PXYFUTURES_DESKTOP_PORT 必须在 1024-65535 之间")
    return (port,)


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.75) as response:
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


def _find_existing_instance(ports: tuple[int, ...]) -> int | None:
    return next((port for port in ports if _health(port)), None)


def _find_available_port(ports: tuple[int, ...]) -> int:
    port = next((candidate for candidate in ports if _port_available(candidate)), None)
    if port is None:
        if len(ports) == 1:
            raise RuntimeError(f"端口 {ports[0]} 已被占用，请关闭占用程序后重试")
        raise RuntimeError(f"端口 {ports[0]}-{ports[-1]} 均被占用，请关闭其他程序后重试")
    return port


def _open_browser(port: int) -> None:
    if os.getenv("PXYFUTURES_NO_BROWSER") != "1":
        webbrowser.open(f"http://127.0.0.1:{port}/")


def _open_browser_when_ready(port: int) -> None:
    for _ in range(120):
        if _health(port):
            _open_browser(port)
            return
        time.sleep(0.25)
    print("服务启动超时，请通过托盘菜单重新打开界面并检查日志。")


def _tray_image() -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (12, 41, 43, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 59, 59), radius=13, outline=(185, 240, 220, 255), width=3)
    draw.line((13, 43, 24, 31, 34, 37, 50, 18), fill=(255, 138, 91, 255), width=5)
    draw.ellipse((45, 13, 55, 23), fill=(185, 240, 220, 255))
    return image


def _start_tray(server: Any, port: int) -> Any | None:
    if os.getenv("PXYFUTURES_NO_TRAY") == "1":
        return None

    import pystray

    def open_dashboard(_icon: Any, _item: Any) -> None:
        _open_browser(port)

    def exit_application(icon: Any, _item: Any) -> None:
        server.should_exit = True
        icon.stop()

    icon = pystray.Icon(
        "pxyfutures",
        _tray_image(),
        "智能期货",
        menu=pystray.Menu(
            pystray.MenuItem("打开智能期货", open_dashboard, default=True),
            pystray.MenuItem("退出软件", exit_application),
        ),
    )
    icon.run_detached()
    return icon


def main() -> int:
    multiprocessing.freeze_support()
    runtime_dir = _runtime_dir()
    data_dir = Path(os.getenv("DATA_DIR", "").strip() or _default_data_dir()).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    _configure_output(data_dir)
    os.chdir(runtime_dir)
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["PXYFUTURES_DESKTOP"] = "1"

    print("=" * 64)
    print("智能期货客户版正在启动")
    print(f"本地数据：{data_dir}")
    print("=" * 64)

    try:
        ports = _port_candidates()
        existing_port = _find_existing_instance(ports)
        if existing_port is not None:
            print(f"智能期货已经运行，正在打开：http://127.0.0.1:{existing_port}/")
            _open_browser(existing_port)
            return 0

        port = _find_available_port(ports)
        os.environ["PORT"] = str(port)

        # 环境与数据目录准备完成后再导入应用，确保配置使用客户版本地目录。
        import uvicorn

        from app.main import app

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="info",
                access_log=True,
            )
        )
        tray = _start_tray(server, port)
        print(f"软件地址：http://127.0.0.1:{port}/")
        print("浏览器将在服务就绪后自动打开，可从系统托盘退出软件。")
        threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()
        try:
            server.run()
        finally:
            if tray is not None:
                tray.stop()
        return 0
    except KeyboardInterrupt:
        print("智能期货已停止。")
        return 0
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _show_error_dialog(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
