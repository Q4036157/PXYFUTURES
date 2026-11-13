"""使用独立临时数据验证真实客户程序，不读取客户凭据或连接外部行情。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def verify(executable: Path, expected_version: str) -> dict[str, object]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="pxyfutures-package-") as temporary:
        env = {
            **os.environ,
            "DATA_DIR": temporary,
            "PXYFUTURES_DESKTOP_PORT": str(port),
            "PXYFUTURES_NO_BROWSER": "1",
            "PXYFUTURES_NO_TRAY": "1",
            "PXYFUTURES_NO_DIALOG": "1",
            "TQ_USER_NAME": "",
            "TQ_PASSWORD": "",
        }
        process = subprocess.Popen([str(executable)], env=env)
        base = f"http://127.0.0.1:{port}"
        # 本机请求不经过系统代理，避免把代理故障误判为程序启动失败。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def read(path: str) -> bytes:
            with opener.open(base + path, timeout=2) as response:
                assert response.status == 200
                return response.read()

        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError("客户程序在健康检查前退出")
                try:
                    health = json.loads(read("/health"))
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("客户程序启动超过60秒") from None
                    time.sleep(0.25)
            assert health["version"] == expected_version, health
            assert b"/assets/" in read("/")
            status = json.loads(read("/api/settings/tq-credentials"))
            assert status["configured"] is False
            assert status["status"] == "unconfigured"
            assert status["connection_status"] == "unknown"
            second = subprocess.run([str(executable)], env=env, timeout=20, check=False)
            assert second.returncode == 0, "重复启动未正常复用已有服务"
            assert json.loads(read("/health"))["version"] == expected_version
            bootstrap = (Path(temporary) / "logs" / "desktop-bootstrap.log").read_text(
                encoding="utf-8",
            )
            assert bootstrap.count("Started server process") == 1, "重复启动创建了第二个服务"
            return {
                "version": expected_version,
                "startup_health": True,
                "frontend_assets": True,
                "credentials_isolated": True,
                "single_instance": True,
                "exe_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            }
        finally:
            if process.poll() is None:
                # 仅结束本工具创建的临时测试进程树，包含单文件引导父进程。
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15, check=False,
                )
                process.wait(timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.exe.resolve(), args.expected_version)
    args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
