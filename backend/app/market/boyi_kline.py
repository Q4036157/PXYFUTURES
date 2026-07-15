"""按博易交易时段重建盘中周期 K 线。

天勤直接请求 7200、14400 秒 K 线时按自然时钟切分。期货行情软件的
120 分钟、240 分钟图则按有效交易时间累计：午休不计入周期，日盘和
夜盘分别结算，交易时段结束时保留最后一根未满周期 K 线。
"""
from __future__ import annotations

from collections.abc import Sequence

from app.market.ma_engine import Bar

_NANOSECONDS_PER_SECOND = 1_000_000_000
# 午休最长约两小时；日盘到夜盘、夜盘到次日日盘均超过三小时。
_SESSION_GROUP_GAP_SECONDS = 3 * 60 * 60


def should_rebuild_for_boyi(duration_seconds: int) -> bool:
    """仅重建盘中分钟/小时周期，日线及更长周期保留交易日 K 线。"""
    return 60 < duration_seconds < 86_400


def base_duration_for(duration_seconds: int) -> int:
    """返回可精确覆盖目标周期的最粗基础 K 线周期。"""
    if duration_seconds % 300 == 0:
        return 300
    return 60


def base_count_for(target_duration_seconds: int, target_count: int, base_duration_seconds: int) -> int:
    """估算重建指定根数目标 K 线需要的基础 K 线数量。"""
    return (target_duration_seconds * target_count + base_duration_seconds - 1) // base_duration_seconds


def rebuild_boyi_bars(
    base_bars: Sequence[Bar],
    target_duration_seconds: int,
    base_duration_seconds: int,
) -> list[Bar]:
    """由连续基础 K 线重建博易兼容的盘中 K 线。

    一组交易时段可以跨午休，但日盘和夜盘之间的间隔会结束当前分组。
    每组内按有效交易时间累计；收市时即使不足目标周期，也输出最后一根
    进行中/未满周期 K 线，使盘中均线与行情软件显示方式一致。
    """
    if target_duration_seconds <= base_duration_seconds:
        return list(base_bars)
    if target_duration_seconds % base_duration_seconds:
        raise ValueError("目标周期必须是基础 K 线周期的整数倍")
    if not base_bars:
        return []

    bars_per_target = target_duration_seconds // base_duration_seconds
    groups: list[list[Bar]] = []
    current_group: list[Bar] = []
    previous_timestamp_ns: int | None = None
    for bar in base_bars:
        if previous_timestamp_ns is not None:
            gap_seconds = (bar.timestamp_ns - previous_timestamp_ns) / _NANOSECONDS_PER_SECOND
            if gap_seconds > _SESSION_GROUP_GAP_SECONDS:
                if current_group:
                    groups.append(current_group)
                current_group = []
        current_group.append(bar)
        previous_timestamp_ns = bar.timestamp_ns
    if current_group:
        groups.append(current_group)

    rebuilt: list[Bar] = []
    for group in groups:
        for start in range(0, len(group), bars_per_target):
            chunk = group[start:start + bars_per_target]
            rebuilt.append(Bar(timestamp_ns=chunk[0].timestamp_ns, close=chunk[-1].close))
    return rebuilt
