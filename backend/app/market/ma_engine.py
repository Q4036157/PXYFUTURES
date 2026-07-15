"""四均线状态计算。

所有输入必须按时间升序排列，并包含最新进行中 K 线，
以保持和盘中行情软件的实时均线结果一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence


class Trend(StrEnum):
    BULLISH = "多"
    BEARISH = "空"


class CrossType(StrEnum):
    GOLDEN = "金叉"
    DEATH = "死叉"


@dataclass(frozen=True)
class Bar:
    """用于均线计算的一根有效 K 线，最后一根可为盘中 K。"""

    timestamp_ns: int
    close: float


@dataclass(frozen=True)
class MovingAverageConfig:
    m4: int
    m3: int
    m2: int
    m1: int

    def __post_init__(self) -> None:
        if any(value <= 0 for value in (self.m1, self.m2, self.m3, self.m4)):
            raise ValueError("均线周期必须是正整数")
        if self.m1 == self.m2:
            raise ValueError("M1 和 M2 的周期不能相同，否则无法判断交叉")

    @property
    def required_bars(self) -> int:
        # 博易短线 M1 还需要 MA(M1*4)。
        return max(self.m1 * 4, self.m2, self.m3, self.m4) + 1


@dataclass(frozen=True)
class SignalState:
    trend_m3: Trend
    trend_m4: Trend
    cross_type: CrossType | None
    label: str
    state_since_ns: int | None
    ma_values: dict[str, float]


def _sma_series(closes: Sequence[float], period: int) -> list[float | None]:
    values: list[float | None] = [None] * len(closes)
    total = 0.0
    for index, close in enumerate(closes):
        total += close
        if index >= period:
            total -= closes[index - period]
        if index >= period - 1:
            values[index] = total / period
    return values


def _ema_series(closes: Sequence[float], period: int) -> list[float]:
    """按博易 EMA 递推公式计算。"""
    if not closes:
        return []
    values = [float(closes[0])]
    for close in closes[1:]:
        values.append((2 * close + (period - 1) * values[-1]) / (period + 1))
    return values


def _short_series(closes: Sequence[float], period: int) -> list[float | None]:
    """短线=(EMA(CLOSE,M1)+MA(CLOSE,M1*2)+MA(CLOSE,M1*4))/3。"""
    ema = _ema_series(closes, period)
    double = _sma_series(closes, period * 2)
    quadruple = _sma_series(closes, period * 4)
    return [
        (ema_value + float(double_value) + float(quadruple_value)) / 3
        if double_value is not None and quadruple_value is not None
        else None
        for ema_value, double_value, quadruple_value in zip(ema, double, quadruple, strict=True)
    ]


def _classify(cross_type: CrossType, price: float, m3: float, m4: float) -> str:
    lower, upper = sorted((m3, m4))
    if price < lower:
        return "反弹" if cross_type == CrossType.GOLDEN else "下跌"
    if price > upper:
        return "上涨" if cross_type == CrossType.GOLDEN else "回调"
    return "震荡"


def calculate_signal(bars: Sequence[Bar], config: MovingAverageConfig) -> SignalState:
    """从 K 线历史恢复当前状态。

    交叉发生时根据该根 K 线收盘价相对 M3/M4 的位置确定分类，随后保持，
    直到反向交叉。因此无需把状态作为唯一事实持久化。
    """
    if len(bars) < config.required_bars:
        raise ValueError(f"K 线数量不足，至少需要 {config.required_bars} 根有效 K 线")

    closes = [bar.close for bar in bars]
    series = {
        "M4": _sma_series(closes, config.m4),
        "M3": _sma_series(closes, config.m3),
        "M2": _sma_series(closes, config.m2),
        "M1": _short_series(closes, config.m1),
    }
    latest = len(bars) - 1
    previous = latest - 1
    ma_values = {name: float(values[latest]) for name, values in series.items() if values[latest] is not None}
    if len(ma_values) != 4:
        raise ValueError("均线数据不完整")

    trend_m3 = Trend.BULLISH if series["M3"][latest] > series["M3"][previous] else Trend.BEARISH
    trend_m4 = Trend.BULLISH if series["M4"][latest] > series["M4"][previous] else Trend.BEARISH

    latest_cross: CrossType | None = None
    latest_label = "等待交叉"
    state_since_ns: int | None = None
    for index in range(1, len(bars)):
        m1_prev, m2_prev = series["M1"][index - 1], series["M2"][index - 1]
        m1_now, m2_now = series["M1"][index], series["M2"][index]
        m3_now, m4_now = series["M3"][index], series["M4"][index]
        if None in (m1_prev, m2_prev, m1_now, m2_now, m3_now, m4_now):
            continue
        cross_type: CrossType | None = None
        if m1_prev <= m2_prev and m1_now > m2_now:
            cross_type = CrossType.GOLDEN
        elif m1_prev >= m2_prev and m1_now < m2_now:
            cross_type = CrossType.DEATH
        if cross_type is not None:
            latest_cross = cross_type
            latest_label = _classify(cross_type, bars[index].close, float(m3_now), float(m4_now))
            state_since_ns = bars[index].timestamp_ns

    return SignalState(
        trend_m3=trend_m3,
        trend_m4=trend_m4,
        cross_type=latest_cross,
        label=latest_label,
        state_since_ns=state_since_ns,
        ma_values=ma_values,
    )
