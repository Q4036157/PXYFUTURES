"""实时均线状态接口。"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.market.boyi_kline import (
    base_count_for,
    base_duration_for,
    rebuild_boyi_bars,
    should_rebuild_for_boyi,
)
from app.market.ma_engine import Bar, MovingAverageConfig, calculate_signal, iter_signal_history
from app.market.tq_client import MarketBars, MarketDataUnavailable, TqClient
from app.services import auth as auth_service
from app.services.alert_stream import AlertStreamService, AlertSubscriber
from app.services.auth import current_user_id
from app.services.repository import ConfigRepository, ContractConfig, PeriodConfig

router = APIRouter(prefix="/api/contracts", tags=["均线信号"])
_CHINA_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)
_SUPPORTED_ALERT_DURATIONS = frozenset({60, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 86400})
_ALERT_STATE_LOOKBACK_BARS = 300
_MAX_CACHED_BASE_BARS = 50_000
_BULLISH_STATES = frozenset({"上涨", "反弹", "多震荡"})
_BEARISH_STATES = frozenset({"下跌", "回调", "空震荡"})
_signal_requirement_notices: set[tuple[int, str, str]] = set()
_signal_requirement_notice_lock = Lock()


def alignment_state(label: str, daily_position: str | None) -> str | None:
    """按日线收盘价相对日线M4的位置，返回顺逆方向标签。"""
    if daily_position == "above":
        if label in _BULLISH_STATES:
            return "顺多"
        if label in _BEARISH_STATES:
            return "逆空"
    elif daily_position == "below":
        if label in _BULLISH_STATES:
            return "逆多"
        if label in _BEARISH_STATES:
            return "顺空"
    return None


def _daily_positions(
    repository: ConfigRepository,
    jobs: list[tuple[ContractConfig, PeriodConfig]],
) -> dict[str, str | None]:
    """读取每个合约最近已收盘日线，比较收盘价与日线M4。"""
    positions: dict[str, str | None] = {}
    contracts = {contract.id: contract for contract, _period in jobs}
    for contract in contracts.values():
        daily = next((period for period in contract.periods if period.duration_seconds == 86_400), None)
        if daily is None:
            positions[contract.symbol] = None
            continue
        config = MovingAverageConfig(m4=daily.m4, m3=daily.m3, m2=daily.m2, m1=daily.m1)
        rows = repository.load_kline_bars(contract.symbol, 86_400, config.required_bars + 2)
        if len(rows) < config.required_bars:
            positions[contract.symbol] = None
            continue
        confirmed_rows = rows[:-1] if len(rows) > config.required_bars else rows
        confirmed = [Bar(timestamp_ns=timestamp_ns, close=close) for timestamp_ns, close in confirmed_rows]
        try:
            state = calculate_signal(confirmed, config)
        except ValueError:
            positions[contract.symbol] = None
            continue
        close = confirmed[-1].close
        m4 = state.ma_values.get("M4")
        positions[contract.symbol] = "above" if m4 is not None and close > m4 else (
            "below" if m4 is not None and close < m4 else None
        )
    return positions


def _apply_alignment_states(
    snapshots: list[dict[str, object]], daily_positions: dict[str, str | None],
) -> None:
    for snapshot in snapshots:
        snapshot["alignment_state"] = alignment_state(
            str(snapshot["label"]), daily_positions.get(str(snapshot["symbol"])),
        )


def _log_signal_requirement_once(contract_id: int, symbol: str, error: ValueError) -> None:
    key = (contract_id, symbol, str(error))
    with _signal_requirement_notice_lock:
        if key in _signal_requirement_notices:
            return
        _signal_requirement_notices.add(key)
    logger.warning(
        "均线计算条件不满足: contract_id=%s symbol=%s error=%s",
        contract_id,
        symbol,
        error,
    )


class AlertScanBody(BaseModel):
    durations: list[int] = Field(default_factory=list, max_length=10)

    @field_validator("durations")
    @classmethod
    def validate_durations(cls, values: list[int]) -> list[int]:
        unique_values = list(dict.fromkeys(values))
        if any(value not in _SUPPORTED_ALERT_DURATIONS for value in unique_values):
            raise ValueError("预警周期不受支持")
        return unique_values


def _iso_time(timestamp_ns: int | None) -> str | None:
    if timestamp_ns is None:
        return None
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=_CHINA_TZ).isoformat()


def _snapshot(
    client: TqClient,
    repository: ConfigRepository,
    symbol: str,
    period: PeriodConfig,
) -> dict[str, object]:
    config = MovingAverageConfig(m4=period.m4, m3=period.m3, m2=period.m2, m1=period.m1)
    market_bars = client.fetch_bars(
        symbol,
        period.duration_seconds,
        config.required_bars + _ALERT_STATE_LOOKBACK_BARS,
        repository,
    )
    return _snapshot_from_market_bars(symbol, period, market_bars)


def _snapshot_from_market_bars(
    symbol: str,
    period: PeriodConfig,
    market_bars: MarketBars,
    *,
    confirmed_only: bool = False,
) -> dict[str, object]:
    config = MovingAverageConfig(m4=period.m4, m3=period.m3, m2=period.m2, m1=period.m1)
    bars = market_bars.bars
    calculation_bars = bars[:-1] if confirmed_only else bars
    if len(calculation_bars) < config.required_bars:
        raise ValueError(
            f"{symbol} 的 {period.label} 当前只有 {len(calculation_bars)} 根有效 K 线，"
            f"当前均线最大周期为 {max(period.m1 * 4, period.m2, period.m3, period.m4)}，"
            f"至少需要 {config.required_bars} 根；请降低均线周期或更换历史更长的合约"
        )
    # 普通均线表保留实时均线；预警必须整套使用此前已确认收盘的 K 线。
    signal = calculate_signal(
        calculation_bars,
        config,
        confirmed_bar_count=None if confirmed_only else len(bars) - 1,
    )
    # 超过七天的数据只标为历史，不据此猜测合约到期或交易日历。
    historical = bars[-1].timestamp_ns < int(
        (datetime.now(_CHINA_TZ) - timedelta(days=7)).timestamp() * 1_000_000_000
    )
    data_status = ("expired" if market_bars.source == "historical" else
                   "historical" if historical else "current")
    logger.debug(
        "均线计算成功: symbol=%s period=%s bars=%d ma=%s cross=%s label=%s",
        symbol,
        period.label,
        len(bars),
        signal.ma_values,
        signal.cross_type,
        signal.label,
    )
    return {
        "period": period.__dict__,
        "trend": {"m3": signal.trend_m3, "m4": signal.trend_m4},
        "cross_type": signal.cross_type,
        "cross_time": _iso_time(signal.cross_time_ns),
        "label": signal.label,
        "state_since": _iso_time(signal.state_since_ns),
        "state_bar_count": signal.state_bar_count,
        "ma_values": {name: round(value, 4) for name, value in signal.ma_values.items()},
        "bar_close": calculation_bars[-1].close,
        "bar_time": _iso_time(calculation_bars[-1].timestamp_ns),
        "confirmed_bar_time": _iso_time(bars[-2].timestamp_ns),
        # 下一根 K 线出现时，上一根才具备确定的收盘确认；该时刻用于拦截迟到旧提醒。
        "confirmed_at": _iso_time(bars[-1].timestamp_ns),
        "data_source": market_bars.source,
        "data_status": data_status,
        "data_updated_at": (
            datetime.fromtimestamp(
                market_bars.updated_at_ns / 1_000_000_000, tz=_CHINA_TZ
            ).isoformat()
            if market_bars.updated_at_ns
            else None
        ),
    }


def _cached_source_request(period: PeriodConfig) -> tuple[int, int]:
    config = MovingAverageConfig(m4=period.m4, m3=period.m3, m2=period.m2, m1=period.m1)
    target_count = config.required_bars + _ALERT_STATE_LOOKBACK_BARS
    if not should_rebuild_for_boyi(period.duration_seconds):
        return period.duration_seconds, target_count
    base_duration_seconds = base_duration_for(period.duration_seconds)
    return (
        base_duration_seconds,
        min(
            _MAX_CACHED_BASE_BARS,
            base_count_for(period.duration_seconds, target_count, base_duration_seconds),
        ),
    )


def _scan_cached_alert_jobs(
    repository: ConfigRepository,
    jobs: list[tuple[ContractConfig, PeriodConfig]],
    *,
    replay_since: dict[tuple[int, int], str | None] | None = None,
) -> tuple[list[dict[str, object]], int]:
    """按合约和基础周期合并数据库读取，再恢复各预警周期状态。"""
    daily_positions = _daily_positions(repository, jobs)
    source_counts: dict[tuple[str, int], int] = {}
    for contract, period in jobs:
        base_duration_seconds, base_count = _cached_source_request(period)
        # 首次监控和重启校正需要完整可用历史，不能把最近一根当作状态起点。
        if replay_since is not None and replay_since.get((contract.id, period.duration_seconds)) is None:
            base_count = _MAX_CACHED_BASE_BARS
        key = (contract.symbol, base_duration_seconds)
        source_counts[key] = max(source_counts.get(key, 0), base_count)

    sync_states = {
        key: repository.get_kline_sync_state(*key)
        for key in source_counts
    }
    last_snapshots: dict[tuple[int, int], dict[str, object]] = {}
    last_errors: set[tuple[int, int]] = set()
    histories: dict[tuple[int, int], MarketBars] = {}

    for _attempt in range(12):
        sources: dict[tuple[str, int], MarketBars] = {}
        for (symbol, duration_seconds), count in source_counts.items():
            rows = repository.load_kline_bars(symbol, duration_seconds, count)
            sync_state = sync_states[(symbol, duration_seconds)]
            sources[(symbol, duration_seconds)] = MarketBars(
                bars=[Bar(timestamp_ns=timestamp_ns, close=close) for timestamp_ns, close in rows],
                source="cache",
                updated_at_ns=sync_state.last_synced_at_ns if sync_state else None,
            )

        expanded: set[tuple[str, int]] = set()
        last_snapshots = {}
        last_errors = set()
        for contract, period in jobs:
            base_duration_seconds, _base_count = _cached_source_request(period)
            source_key = (contract.symbol, base_duration_seconds)
            source = sources[source_key]
            config = MovingAverageConfig(m4=period.m4, m3=period.m3, m2=period.m2, m1=period.m1)
            bars = source.bars
            if should_rebuild_for_boyi(period.duration_seconds):
                bars = rebuild_boyi_bars(bars, period.duration_seconds, base_duration_seconds)
            market_bars = MarketBars(
                bars=bars,
                source="cache",
                updated_at_ns=source.updated_at_ns,
            )
            job_key = (contract.id, period.duration_seconds)
            histories[job_key] = market_bars
            try:
                snapshot = _snapshot_from_market_bars(
                    contract.symbol,
                    period,
                    market_bars,
                    confirmed_only=True,
                )
            except (MarketDataUnavailable, ValueError) as exc:
                last_errors.add(job_key)
                logger.debug("预警缓存计算跳过: symbol=%s error=%s", contract.symbol, exc)
                continue
            last_snapshots[job_key] = {
                "contract_id": contract.id,
                "symbol": contract.symbol,
                "name": contract.name,
                **snapshot,
            }

            state_since = snapshot["state_since"]
            state_index = next(
                (
                    index
                    for index, bar in enumerate(market_bars.bars)
                    if _iso_time(bar.timestamp_ns) == state_since
                ),
                -1,
            )
            warmup_boundary = config.required_bars + 1
            at_history_boundary = state_index < warmup_boundary
            no_cross_in_window = snapshot["cross_type"] is None
            requested_count = source_counts[source_key]
            source_complete = len(source.bars) < requested_count
            cursor = replay_since.get(job_key) if replay_since is not None else None
            needs_replay_history = bool(
                cursor and len(bars) > config.required_bars
                and _iso_time(bars[config.required_bars].timestamp_ns) > cursor
            )
            if (
                (at_history_boundary or no_cross_in_window or needs_replay_history)
                and not source_complete
                and requested_count < _MAX_CACHED_BASE_BARS
            ):
                expanded.add(source_key)

        if not expanded:
            break
        for source_key in expanded:
            source_counts[source_key] = min(
                _MAX_CACHED_BASE_BARS,
                source_counts[source_key] * 2,
            )

    if replay_since is not None:
        replayed: list[dict[str, object]] = []
        daily_sources: dict[str, list[int]] = {}
        for contract, period in jobs:
            key = (contract.id, period.duration_seconds)
            latest = last_snapshots.get(key)
            if latest is None or latest["data_status"] != "current":
                continue
            cursor = replay_since.get(key)
            history = histories[key]
            base_duration, _ = _cached_source_request(period)
            source = sources[(contract.symbol, base_duration)]
            source_times = [bar.timestamp_ns for bar in source.bars]
            config = MovingAverageConfig(
                m4=period.m4, m3=period.m3, m2=period.m2, m1=period.m1,
            )
            emit_from = config.required_bars - 1
            if cursor is not None:
                cursor_ns = int(datetime.fromisoformat(cursor).timestamp() * 1_000_000_000)
                emit_from = max(emit_from, bisect_right(
                    [bar.timestamp_ns for bar in history.bars], cursor_ns,
                ))
            for index, state in enumerate(
                iter_signal_history(history.bars[:-1], config, emit_from=emit_from), start=emit_from,
            ):
                if index == len(history.bars) - 2:
                    item = dict(latest)
                else:
                    item = {
                        "contract_id": contract.id, "symbol": contract.symbol, "name": contract.name,
                        "period": period.__dict__,
                        "trend": {"m3": state.trend_m3, "m4": state.trend_m4},
                        "cross_type": state.cross_type, "label": state.label,
                        "confirmed_bar_time": _iso_time(history.bars[index].timestamp_ns),
                        "confirmed_at": _iso_time(history.bars[index + 1].timestamp_ns),
                    }
                # 由实际参与聚合的最后一根基础K线取收盘时间，午休、夜盘间隔不计入。
                if period.duration_seconds < 86_400:
                    start = history.bars[index].timestamp_ns
                    end = history.bars[index + 1].timestamp_ns
                    last_base_index = bisect_left(source_times, end) - 1
                    if last_base_index >= 0 and source_times[last_base_index] >= start:
                        item["closed_at"] = _iso_time(
                            source_times[last_base_index] + base_duration * 1_000_000_000,
                        )
                else:
                    if contract.symbol not in daily_sources:
                        daily_sources[contract.symbol] = [
                            timestamp for timestamp, _close
                            in repository.load_kline_bars(contract.symbol, 300, 3000)
                        ]
                    intraday = daily_sources[contract.symbol]
                    day = datetime.fromtimestamp(
                        history.bars[index].timestamp_ns / 1_000_000_000, tz=_CHINA_TZ,
                    ).replace(hour=0, minute=0, second=0, microsecond=0)
                    # 夜盘属于下一交易日，取当日日盘范围内最后的实际基础K线。
                    begin = int((day + timedelta(hours=6)).timestamp()) * 1_000_000_000
                    end = int((day + timedelta(hours=18)).timestamp()) * 1_000_000_000
                    last_index = bisect_left(intraday, end) - 1
                    if last_index >= 0 and intraday[last_index] >= begin:
                        item["closed_at"] = _iso_time(intraday[last_index] + 300 * 1_000_000_000)
                if "closed_at" not in item:
                    continue
                replayed.append(item)
        _apply_alignment_states(replayed, daily_positions)
        return replayed, len(last_errors)

    snapshots = [
        snapshot
        for job_key, snapshot in last_snapshots.items()
        if snapshot["data_status"] == "current"
        and snapshot["cross_type"] is not None
        and snapshot["state_bar_count"] > 0
    ]
    _apply_alignment_states(snapshots, daily_positions)
    return snapshots, len(last_errors)


def scan_cached_alerts_for_user(
    repository: ConfigRepository,
    user_id: str,
    durations: frozenset[int],
    *,
    client: TqClient | None = None,
) -> dict[str, object]:
    """供HTTP扫描和WebSocket收K推送共同使用同一份确定性计算。"""
    if not durations:
        return {"signals": [], "requested": 0, "failed": 0}
    contracts = repository.list_contracts(user_id)
    excluded = {
        contract.symbol for contract in contracts
        if client is not None and client.contract_status(contract.symbol) == "expired"
    }
    jobs = [
        (contract, period)
        for contract in contracts
        if contract.symbol not in excluded
        for period in contract.periods
        if period.duration_seconds in durations
    ]
    snapshots, failed = _scan_cached_alert_jobs(repository, jobs)
    result = {"signals": snapshots, "requested": len(jobs), "failed": failed}
    if excluded:
        result["excluded_contracts"] = len(excluded)
    return result


def _websocket_user_id(websocket: WebSocket) -> str | None:
    local_user_id = auth_service.desktop_user_id(
        websocket.client.host if websocket.client else None,
        websocket.headers.get("origin"),
    )
    if local_user_id:
        return local_user_id
    token = websocket.cookies.get("pxy_futures_session", "").strip()
    return auth_service._decode_app_session(token) if token else None


async def _send_alert_messages(websocket: WebSocket, subscriber: AlertSubscriber) -> None:
    queue = subscriber.queue
    while True:
        await websocket.send_json(await queue.get())


@router.websocket("/alerts/ws")
async def stream_alerts(websocket: WebSocket) -> None:
    """通过主平台会话或本机客户版身份推送收K快照。"""
    user_id = _websocket_user_id(websocket)
    if user_id is None:
        await websocket.close(code=4401, reason="主平台登录已失效")
        return

    service: AlertStreamService = websocket.app.state.alert_stream
    await websocket.accept()
    subscriber = service.register(user_id)
    service.offer_control(subscriber.id, "connected")
    sender = asyncio.create_task(
        _send_alert_messages(websocket, subscriber),
        name=f"alert-stream-sender-{subscriber.id}",
    )
    try:
        while True:
            message = await websocket.receive_json()
            message_type = str(message.get("type", ""))
            if message_type == "ping":
                continue
            if message_type != "subscribe":
                service.offer_control(subscriber.id, "invalid_message")
                continue
            try:
                body = AlertScanBody.model_validate(message)
            except ValidationError:
                service.offer_control(subscriber.id, "invalid_subscription")
                continue
            await service.update(subscriber.id, frozenset(body.durations))
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        try:
            await sender
        except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            pass
        await service.unregister(subscriber.id)


@router.get("/{contract_id}/signals")
async def get_signals(
    contract_id: int, request: Request, user_id: str = Depends(current_user_id)
) -> dict[str, object]:
    repository: ConfigRepository = request.app.state.config_repository
    contract = repository.get_contract(user_id, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="合约不存在")
    if not contract.periods:
        return {
            "contract_id": contract.id,
            "symbol": contract.symbol,
            "signals": [],
            "warnings": [],
        }
    client: TqClient = request.app.state.tq_client
    period_results = await asyncio.gather(
        *(
            asyncio.to_thread(_snapshot, client, repository, contract.symbol, period)
            for period in contract.periods
        ),
        return_exceptions=True,
    )
    signals: list[dict[str, object]] = []
    warnings: list[str] = []
    for result in period_results:
        if isinstance(result, MarketDataUnavailable):
            logger.error(
                "均线数据获取失败: contract_id=%s symbol=%s error=%s",
                contract_id,
                contract.symbol,
                result,
            )
            raise HTTPException(status_code=503, detail=str(result)) from result
        if isinstance(result, ValueError):
            _log_signal_requirement_once(contract_id, contract.symbol, result)
            if str(result) not in warnings:
                warnings.append(str(result))
            continue
        if isinstance(result, BaseException):
            raise result
        signals.append(result)
    market_status = client.contract_status(contract.symbol) if isinstance(client, TqClient) else "unknown"
    if market_status == "expired":
        warnings.append("该合约已到期，仅展示历史行情，已退出实时订阅和预警；可使用更换合约按钮切换")
    elif any(signal.get("data_status") == "historical" for signal in signals):
        warnings.append("当前K线距今超过七天，仅供历史查看，不作为实时预警")
    return {
        "contract_id": contract.id,
        "symbol": contract.symbol,
        "signals": signals,
        "warnings": warnings,
        "market_status": market_status,
    }


@router.post("/alerts/scan")
async def scan_alerts(
    body: AlertScanBody,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> dict[str, object]:
    """批量计算当前用户自选合约中已配置的预警周期。"""
    repository: ConfigRepository = request.app.state.config_repository
    return await asyncio.to_thread(
        scan_cached_alerts_for_user,
        repository,
        user_id,
        frozenset(body.durations),
        client=getattr(request.app.state, "tq_client", None),
    )
