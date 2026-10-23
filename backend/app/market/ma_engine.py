"""四均线状态计算。

所有输入必须按时间升序排列，并包含最新进行中 K 线，
以保持和盘中行情软件的实时均线结果一致。
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum


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
    cross_time_ns: int | None
    label: str
    state_since_ns: int | None
    state_bar_count: int
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


def _classify(cross_type: CrossType, m2: float, m3: float, m4: float) -> str:
    """根据最近交叉方向和当前 M2 相对长均线的位置判断阶段。"""
    lower, upper = sorted((m3, m4))
    if m2 < lower:
        return "反弹" if cross_type == CrossType.GOLDEN else "下跌"
    if m2 > upper:
        return "上涨" if cross_type == CrossType.GOLDEN else "回调"
    if m3 > m4:
        return "多震荡"
    if m4 > m3:
        return "空震荡"
    return "震荡"


def calculate_signal(
    bars: Sequence[Bar],
    config: MovingAverageConfig,
    *,
    confirmed_bar_count: int | None = None,
) -> SignalState:
    """从 K 线历史恢复当前状态。

    M1/M2 最近一次交叉确定金叉或死叉阶段；之后每根已收盘 K 线根据
    M2 相对 M3/M4 的位置动态更新反弹、震荡、上涨、回调或下跌。
    计数包含原始交叉信号 K 线，之后每根收盘 K 线加一，方向或阶段变化不重置。
    最新进行中 K 线仍参与实时均线值和方向计算，但不提前改变状态。
    """
    if len(bars) < config.required_bars:
        raise ValueError(f"K 线数量不足，至少需要 {config.required_bars} 根有效 K 线")

    confirmed_count = len(bars) if confirmed_bar_count is None else confirmed_bar_count
    confirmed_count = min(len(bars), max(0, confirmed_count))
    confirmed = None
    emit_from = max(config.required_bars - 1, confirmed_count - 1)
    for count, current in enumerate(
        iter_signal_history(bars, config, emit_from=emit_from), start=emit_from + 1,
    ):
        if count == confirmed_count:
            confirmed = current
    return SignalState(
        trend_m3=current.trend_m3, trend_m4=current.trend_m4,
        cross_type=confirmed.cross_type if confirmed else None,
        cross_time_ns=confirmed.cross_time_ns if confirmed else None,
        label=confirmed.label if confirmed else "等待交叉",
        state_since_ns=confirmed.state_since_ns if confirmed else None,
        state_bar_count=confirmed.state_bar_count if confirmed else 0,
        ma_values=current.ma_values,
    )


def iter_signal_history(
    bars: Sequence[Bar], config: MovingAverageConfig, *, emit_from: int = 0,
) -> Iterator[SignalState]:
    """一次计算全部历史状态，从满足均线长度的第一根开始逐根返回。"""
    closes = [bar.close for bar in bars]
    series = {
        "M4": _sma_series(closes, config.m4),
        "M3": _sma_series(closes, config.m3),
        "M2": _sma_series(closes, config.m2),
        "M1": _short_series(closes, config.m1),
    }
    latest_cross: CrossType | None = None
    cross_time_ns: int | None = None
    cross_index: int | None = None
    latest_label = "等待交叉"
    for index in range(config.required_bars - 1, len(bars)):
        m1_prev, m2_prev = series["M1"][index - 1], series["M2"][index - 1]
        m1_now, m2_now = series["M1"][index], series["M2"][index]
        m3_prev, m4_prev = series["M3"][index - 1], series["M4"][index - 1]
        m3_now, m4_now = series["M3"][index], series["M4"][index]
        if None in (m1_prev, m2_prev, m1_now, m2_now, m3_prev, m4_prev, m3_now, m4_now):
            continue
        if m1_prev <= m2_prev and m1_now > m2_now:
            latest_cross = CrossType.GOLDEN
            cross_time_ns = bars[index].timestamp_ns
            cross_index = index
        elif m1_prev >= m2_prev and m1_now < m2_now:
            latest_cross = CrossType.DEATH
            cross_time_ns = bars[index].timestamp_ns
            cross_index = index
        if latest_cross is not None:
            latest_label = _classify(latest_cross, float(m2_now), float(m3_now), float(m4_now))
        if index < emit_from:
            continue
        yield SignalState(
            trend_m3=Trend.BULLISH if m3_now > m3_prev else Trend.BEARISH,
            trend_m4=Trend.BULLISH if m4_now > m4_prev else Trend.BEARISH,
            cross_type=latest_cross, cross_time_ns=cross_time_ns, label=latest_label,
            state_since_ns=cross_time_ns,
            state_bar_count=index - cross_index + 1 if cross_index is not None else 0,
            ma_values={name: float(values[index]) for name, values in series.items()},
        )
