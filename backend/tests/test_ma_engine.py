import pytest

from app.market.ma_engine import (
    Bar,
    CrossType,
    MovingAverageConfig,
    Trend,
    _classify,
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


def test_golden_cross_advances_from_rebound_to_rise_without_reverse_cross() -> None:
    # 第 5 根形成金叉，后续 M2 上穿两条长均线后，即使没有死叉也应转为上涨。
    closes = [10, 9, 8, 7, 6, 7, 8, 9, 10, 11, 12]
    bars = [Bar(timestamp_ns=index, close=float(value)) for index, value in enumerate(closes)]
    state = calculate_signal(bars, MovingAverageConfig(m4=5, m3=4, m2=3, m1=1))

    assert state.cross_type == CrossType.GOLDEN
    assert state.cross_time_ns == 5
    assert state.label == "上涨"
    assert state.state_since_ns == 5
    assert state.state_bar_count == 6
    assert state.trend_m3 == Trend.BULLISH
    assert state.trend_m4 == Trend.BULLISH


def test_death_cross_advances_from_pullback_to_fall() -> None:
    closes = [5, 6, 7, 8, 9, 8, 7, 6, 5, 4, 3]
    bars = [Bar(timestamp_ns=index, close=float(value)) for index, value in enumerate(closes)]
    state = calculate_signal(bars, MovingAverageConfig(m4=5, m3=4, m2=3, m1=1))

    assert state.cross_type == CrossType.DEATH
    assert state.cross_time_ns == 5
    assert state.label == "下跌"
    assert state.state_since_ns == 5
    assert state.state_bar_count == 6


def test_in_progress_bar_does_not_change_confirmed_state() -> None:
    closes = [10, 9, 8, 7, 6, 7, 8, 9]
    bars = [Bar(timestamp_ns=index, close=float(value)) for index, value in enumerate(closes)]
    config = MovingAverageConfig(m4=5, m3=4, m2=3, m1=1)

    confirmed = calculate_signal(bars, config, confirmed_bar_count=len(bars) - 1)
    including_live = calculate_signal(bars, config)

    assert confirmed.label == "空震荡"
    assert including_live.label == "上涨"


def test_cross_count_survives_phase_and_long_average_direction_changes() -> None:
    closes = [10, 9, 8, 7, 6, 7, 8, 9, 10, 11, 12]
    bars = [Bar(timestamp_ns=index, close=float(close)) for index, close in enumerate(closes)]
    config = MovingAverageConfig(m4=5, m3=4, m2=3, m1=1)
    states = [calculate_signal(bars[:end], config) for end in range(6, len(bars) + 1)]

    assert [state.state_bar_count for state in states] == [1, 2, 3, 4, 5, 6]
    assert {state.state_since_ns for state in states} == {5}
    assert {state.cross_time_ns for state in states} == {5}
    assert len({state.label for state in states}) > 1
    assert len({(state.trend_m3, state.trend_m4) for state in states}) > 1


def test_count_resets_only_when_new_cross_is_confirmed() -> None:
    closes = [10, 9, 8, 7, 6, 7, 8, 9, 10, 11, 12, 1, 1, 1]
    bars = [Bar(timestamp_ns=index, close=float(close)) for index, close in enumerate(closes)]
    config = MovingAverageConfig(m4=5, m3=4, m2=3, m1=1)
    before_close = calculate_signal(bars[:12], config, confirmed_bar_count=11)
    confirmed = calculate_signal(bars[:13], config, confirmed_bar_count=12)
    next_close = calculate_signal(bars, config, confirmed_bar_count=13)

    assert before_close.cross_type == CrossType.GOLDEN
    assert before_close.state_bar_count == 6
    assert confirmed.cross_type == CrossType.DEATH
    assert confirmed.cross_time_ns == confirmed.state_since_ns == 11
    assert confirmed.state_bar_count == 1
    assert next_close.state_since_ns == 11
    assert next_close.state_bar_count == 2
    assert calculate_signal(bars, config, confirmed_bar_count=13) == next_close


def test_count_uses_actual_bars_across_session_breaks() -> None:
    closes = [10, 9, 8, 7, 6, 7, 8, 9, 10, 11, 12]
    bars = [
        Bar(timestamp_ns=(index * 180 + (5_400 if index >= 8 else 0)) * 1_000_000_000,
            close=float(close))
        for index, close in enumerate(closes)
    ]
    state = calculate_signal(bars, MovingAverageConfig(m4=5, m3=4, m2=3, m1=1))

    assert state.state_since_ns == bars[5].timestamp_ns
    assert state.state_bar_count == 6


def test_without_original_cross_count_stays_zero() -> None:
    bars = [Bar(timestamp_ns=index, close=100.0) for index in range(20)]
    state = calculate_signal(bars, MovingAverageConfig(m4=5, m3=4, m2=3, m1=1))

    assert state.cross_type is None
    assert state.state_since_ns is None
    assert state.state_bar_count == 0
