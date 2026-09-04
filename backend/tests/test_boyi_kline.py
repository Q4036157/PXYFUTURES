from app.market.boyi_kline import rebuild_boyi_bars
from app.market.ma_engine import Bar

MINUTE_NS = 60 * 1_000_000_000


def _five_minute_group(start_minute: int, count: int, first_close: float) -> list[Bar]:
    return [
        Bar(timestamp_ns=(start_minute + index * 5) * MINUTE_NS, close=first_close + index)
        for index in range(count)
    ]


def _pvc_day_and_night_bars() -> list[Bar]:
    # PVC 日盘：09:00-10:15、10:30-11:30、13:30-15:00，共 45 根 5 分钟线；
    # 夜盘：21:00-23:00，共 24 根。时间值只需保持真实的间隔关系。
    day = (
        _five_minute_group(9 * 60, 15, 1.0)
        + _five_minute_group(10 * 60 + 30, 12, 16.0)
        + _five_minute_group(13 * 60 + 30, 18, 28.0)
    )
    night = _five_minute_group(21 * 60, 24, 46.0)
    return day + night


def test_120_minute_bars_accumulate_trading_time_across_lunch() -> None:
    rebuilt = rebuild_boyi_bars(_pvc_day_and_night_bars(), 7_200, 300)

    # 日盘 225 个有效交易分钟：一根完整 120 分钟线和一根收市未满线；
    # 夜盘 120 分钟单独结算，不能与日盘剩余 105 分钟拼接。
    assert [(bar.timestamp_ns // MINUTE_NS, bar.close) for bar in rebuilt] == [
        (9 * 60, 24.0),
        (11 * 60 + 15, 45.0),
        (21 * 60, 69.0),
    ]


def test_240_minute_bars_keep_day_and_night_sessions_separate() -> None:
    rebuilt = rebuild_boyi_bars(_pvc_day_and_night_bars(), 14_400, 300)

    # 日盘 225 分钟、夜盘 120 分钟都不足 240 分钟，分别保留为未满 K 线。
    assert [(bar.timestamp_ns // MINUTE_NS, bar.close) for bar in rebuilt] == [
        (9 * 60, 45.0),
        (21 * 60, 69.0),
    ]


def test_current_partial_bar_is_included_for_realtime_ma() -> None:
    current = _five_minute_group(21 * 60, 2, 4_590.0)

    rebuilt = rebuild_boyi_bars(current, 7_200, 300)

    assert [(bar.timestamp_ns // MINUTE_NS, bar.close) for bar in rebuilt] == [(21 * 60, 4_591.0)]


def test_pvc_screenshot_ma5_uses_boyi_session_sequence() -> None:
    """截图时博易的 MA5=4549，必须由日盘/夜盘分组后的序列得到。"""
    day_one = _pvc_day_and_night_bars()[:45]
    night_one = _pvc_day_and_night_bars()[45:]
    day_two = [
        Bar(timestamp_ns=bar.timestamp_ns + 24 * 60 * MINUTE_NS, close=bar.close)
        for bar in _pvc_day_and_night_bars()[:45]
    ]
    current_night = _five_minute_group(45 * 60, 2, 4_590.0)

    # 120 分钟日盘完整线、日盘未满线、夜盘线的收盘价，来自截图时的实际序列。
    day_one[23] = Bar(day_one[23].timestamp_ns, 4_520.0)
    day_one[44] = Bar(day_one[44].timestamp_ns, 4_488.0)
    night_one[-1] = Bar(night_one[-1].timestamp_ns, 4_510.0)
    day_two[23] = Bar(day_two[23].timestamp_ns, 4_573.0)
    day_two[44] = Bar(day_two[44].timestamp_ns, 4_579.0)
    current_night[-1] = Bar(current_night[-1].timestamp_ns, 4_596.0)

    rebuilt = rebuild_boyi_bars(day_one + night_one + day_two + current_night, 7_200, 300)

    assert [bar.close for bar in rebuilt[-6:]] == [4_520.0, 4_488.0, 4_510.0, 4_573.0, 4_579.0, 4_596.0]
    assert sum(bar.close for bar in rebuilt[-5:]) / 5 == 4_549.2
