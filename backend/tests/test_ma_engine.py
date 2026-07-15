import pytest

from app.market.ma_engine import (
    Bar,
    CrossType,
    MovingAverageConfig,
    Trend,
    _classify,
    _interpolate_cross,
    calculate_signal,
)


def test_cross_between_long_averages_uses_long_average_order() -> None:
    assert _classify(CrossType.GOLDEN, 90, 100, 120) == "反弹"
    assert _classify(CrossType.GOLDEN, 90, 120, 100) == "反弹"
    assert _classify(CrossType.GOLDEN, 110, 100, 120) == "空震荡"
    assert _classify(CrossType.DEATH, 110, 100, 120) == "空震荡"
    assert _classify(CrossType.GOLDEN, 110, 120, 100) == "多震荡"
    assert _classify(CrossType.DEATH, 110, 120, 100) == "多震荡"
    assert _classify(CrossType.GOLDEN, 130, 100, 120) == "上涨"
    assert _classify(CrossType.GOLDEN, 130, 120, 100) == "上涨"


def test_zone_boundaries_are_consolidation() -> None:
    assert _classify(CrossType.GOLDEN, 100, 100, 120) == "空震荡"
    assert _classify(CrossType.DEATH, 120, 100, 120) == "空震荡"


def test_interpolates_the_ma_cross_instead_of_using_bar_close() -> None:
    cross, m3, m4 = _interpolate_cross(
        9306.990267317235,
        9307.285714285714,
        9311.085827057008,
        9308.190476190477,
        9319.433333333332,
        9320.266666666666,
        9279.716666666667,
        9279.679166666667,
    )

    assert cross == pytest.approx(9307.3695, abs=0.001)
    assert m4 < cross < m3
    assert _classify(CrossType.GOLDEN, cross, m3, m4) == "多震荡"


def test_m1_uses_boyi_composite_short_formula() -> None:
    closes = [float(value) for value in range(1, 21)]
    bars = [Bar(timestamp_ns=index, close=value) for index, value in enumerate(closes)]
    state = calculate_signal(bars, MovingAverageConfig(m4=16, m3=12, m2=10, m1=4))

    ema = closes[0]
    for close in closes[1:]:
        ema = (2 * close + 3 * ema) / 5
    expected = (ema + sum(closes[-8:]) / 8 + sum(closes[-16:]) / 16) / 3

    assert state.ma_values["M1"] == pytest.approx(expected)
    assert state.ma_values["M2"] == pytest.approx(sum(closes[-10:]) / 10)


def test_last_cross_state_persists_until_reverse_cross() -> None:
    # 第 5 根形成金叉，后续价格继续上涨但没有反向交叉，状态必须保持。
    closes = [10, 9, 8, 7, 6, 7, 8, 9, 10, 11, 12]
    bars = [Bar(timestamp_ns=index, close=float(value)) for index, value in enumerate(closes)]
    state = calculate_signal(bars, MovingAverageConfig(m4=5, m3=4, m2=3, m1=1))

    assert state.cross_type == CrossType.GOLDEN
    assert state.label == "反弹"
    assert state.state_since_ns == 5
    assert state.trend_m3 == Trend.BULLISH
    assert state.trend_m4 == Trend.BULLISH


def test_reverse_cross_changes_persistent_state_to_death_cross() -> None:
    closes = [5, 6, 7, 8, 9, 8, 7, 6, 5, 4, 3]
    bars = [Bar(timestamp_ns=index, close=float(value)) for index, value in enumerate(closes)]
    state = calculate_signal(bars, MovingAverageConfig(m4=5, m3=4, m2=3, m1=1))

    assert state.cross_type == CrossType.DEATH
    assert state.label == "回调"
    assert state.state_since_ns == 5
