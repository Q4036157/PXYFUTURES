"""运行告警的有界限频、恢复与失效安全。"""
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.runtime_notices import RuntimeNoticeLimiter


def test_warning_window_and_recovery(caplog):
    now = [0.0]
    limiter = RuntimeNoticeLimiter(logging.getLogger("notice-test"), clock=lambda: now[0])
    with caplog.at_level(logging.INFO):
        limiter.warning("network", "故障 %s", "连接")
        for _ in range(10):
            limiter.warning("network", "故障")
        assert len(caplog.records) == 1
        now[0] = 121
        limiter.warning("network", "仍未恢复")
        assert len(caplog.records) == 2
        assert "10 条" in caplog.records[-1].getMessage()
        limiter.warning("network", "仍未恢复")
        limiter.recover("network", "连接恢复")
        limiter.recover("network", "连接恢复")
    assert len(caplog.records) == 3
    assert "1 条" in caplog.records[-1].getMessage()
    assert not limiter._states


def test_limiter_is_bounded_and_concurrent(caplog):
    limiter = RuntimeNoticeLimiter(logging.getLogger("notice-test"), max_keys=3)
    with caplog.at_level(logging.WARNING):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: limiter.warning("same", "失败"), range(100)))
        assert len(caplog.records) == 1
        assert limiter._states["same"][1] == 99
        for index in range(10):
            limiter.warning(str(index), "失败")
    assert len(limiter._states) == 3
    assert all(isinstance(state[1], int) for state in limiter._states.values())


def test_diagnostics_failure_does_not_break_business():
    class BrokenLogger:
        @staticmethod
        def warning(*_args, **_kwargs):
            raise OSError("模拟日志磁盘不可用")

        info = warning

    limiter = RuntimeNoticeLimiter(BrokenLogger())
    limiter.warning("test", "故障")
    limiter.recover("test", "恢复")
    assert not limiter._states


@pytest.mark.parametrize("interval,max_keys", [(0, 2), (-1, 2), (120, 0)])
def test_invalid_limits_are_rejected(interval, max_keys):
    with pytest.raises(ValueError):
        RuntimeNoticeLimiter(logging.getLogger(__name__), interval, max_keys)
