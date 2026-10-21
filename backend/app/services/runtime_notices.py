"""线程安全的有界运行告警汇总，不参与业务重试。"""
from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from time import monotonic


class RuntimeNoticeLimiter:
    def __init__(
        self, logger: logging.Logger, interval_seconds: float = 120.0,
        max_keys: int = 32, clock: Callable[[], float] = monotonic,
    ) -> None:
        if interval_seconds <= 0 or max_keys < 1:
            raise ValueError("告警窗口和键数量必须为正数")
        self._logger = logger
        self._interval = interval_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._states: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = Lock()

    def warning(
        self, key: str, message: str, *args: object, exc_info: bool = False,
    ) -> None:
        now = self._clock()
        with self._lock:
            previous = self._states.get(key)
            if previous is not None and now - previous[0] < self._interval:
                self._states[key] = (previous[0], previous[1] + 1)
                return
            suppressed = previous[1] if previous else 0
            self._states[key] = (now, 0)
            self._states.move_to_end(key)
            while len(self._states) > self._max_keys:
                self._states.popitem(last=False)
        suffix = f"；已抑制 {suppressed} 条重复告警" if suppressed else ""
        try:
            self._logger.warning(message + suffix, *args, exc_info=exc_info)
        except Exception:  # noqa: BLE001, S110
            # 日志设备故障不能阻止行情继续同步或恢复。
            pass

    def recover(self, key: str, message: str, *args: object) -> None:
        with self._lock:
            previous = self._states.pop(key, None)
        if previous is not None:
            suffix = f"；恢复前已抑制 {previous[1]} 条重复告警"
            try:
                self._logger.info(message + suffix, *args)
            except Exception:  # noqa: BLE001, S110
                # 不向故障日志设备递归记录诊断。
                pass
