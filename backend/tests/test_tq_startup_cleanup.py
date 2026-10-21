"""SDK 会替换全局日志器，在独立进程中验证真实资源生命周期。"""

import subprocess
import sys
from pathlib import Path


def test_sdk_startup_cleanup_in_isolated_process():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/_tq_startup_cleanup_cases.py"],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout
