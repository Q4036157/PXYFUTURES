"""实时均线状态接口。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.market.boyi_kline import (
    base_count_for,
    base_duration_for,
    rebuild_boyi_bars,
    should_rebuild_for_boyi,
)
from app.market.ma_engine import Bar, MovingAverageConfig, calculate_signal
from app.market.tq_client import MarketBars, MarketDataUnavailable, TqClient
from app.services.auth import current_user_id
from app.services.repository import ConfigRepository, ContractConfig, PeriodConfig

router = APIRouter(prefix="/api/contracts", tags=["均线信号"])
_CHINA_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)
_SUPPORTED_ALERT_DURATIONS = frozenset({180, 300, 600, 900, 1800, 3600, 7200, 14400, 86400})
_ALERT_STATE_LOOKBACK_BARS = 300
_MAX_CACHED_BASE_BARS = 50_000
_signal_requirement_notices: set[tuple[int, str, str]] = set()
_signal_requirement_notice_lock = Lock()


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
    durations: list[int] = Field(default_factory=list, max_length=9)

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
        "ma_values": {name: round(value, 4) for name, value in signal.ma_values.items()},
        "bar_close": calculation_bars[-1].close,
        "bar_time": _iso_time(calculation_bars[-1].timestamp_ns),
        "confirmed_bar_time": _iso_time(bars[-2].timestamp_ns),
        # 下一根 K 线出现时，上一根才具备确定的收盘确认；该时刻用于拦截迟到旧提醒。
        "confirmed_at": _iso_time(bars[-1].timestamp_ns),
        "data_source": market_bars.source,
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
) -> tuple[list[dict[str, object]], int]:
    """按合约和基础周期合并数据库读取，再恢复各预警周期状态。"""
    source_counts: dict[tuple[str, int], int] = {}
    for contract, period in jobs:
        base_duration_seconds, base_count = _cached_source_request(period)
        key = (contract.symbol, base_duration_seconds)
        source_counts[key] = max(source_counts.get(key, 0), base_count)

    sources: dict[tuple[str, int], MarketBars] = {}
    for (symbol, duration_seconds), count in source_counts.items():
        rows = repository.load_kline_bars(symbol, duration_seconds, count)
        sync_state = repository.get_kline_sync_state(symbol, duration_seconds)
        sources[(symbol, duration_seconds)] = MarketBars(
            bars=[Bar(timestamp_ns=timestamp_ns, close=close) for timestamp_ns, close in rows],
            source="cache",
            updated_at_ns=sync_state.last_synced_at_ns if sync_state else None,
        )

    snapshots: list[dict[str, object]] = []
    failed = 0
    for contract, period in jobs:
        base_duration_seconds, _base_count = _cached_source_request(period)
        source = sources[(contract.symbol, base_duration_seconds)]
        config = MovingAverageConfig(m4=period.m4, m3=period.m3, m2=period.m2, m1=period.m1)
        target_count = config.required_bars + _ALERT_STATE_LOOKBACK_BARS
        bars = source.bars
        if should_rebuild_for_boyi(period.duration_seconds):
            bars = rebuild_boyi_bars(bars, period.duration_seconds, base_duration_seconds)
        market_bars = MarketBars(
            bars=bars[-target_count:],
            source="cache",
            updated_at_ns=source.updated_at_ns,
        )
        try:
            snapshot = _snapshot_from_market_bars(
                contract.symbol,
                period,
                market_bars,
                confirmed_only=True,
            )
        except (MarketDataUnavailable, ValueError) as exc:
            failed += 1
            logger.debug("预警缓存计算跳过: symbol=%s error=%s", contract.symbol, exc)
            continue
        snapshots.append(
            {
                "contract_id": contract.id,
                "symbol": contract.symbol,
                "name": contract.name,
                **snapshot,
            }
        )
    return snapshots, failed


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
    return {
        "contract_id": contract.id,
        "symbol": contract.symbol,
        "signals": signals,
        "warnings": warnings,
    }


@router.post("/alerts/scan")
async def scan_alerts(
    body: AlertScanBody,
    request: Request,
    user_id: str = Depends(current_user_id),
) -> dict[str, object]:
    """批量计算当前用户自选合约中已配置的预警周期。"""
    if not body.durations:
        return {"signals": [], "requested": 0, "failed": 0}

    repository: ConfigRepository = request.app.state.config_repository
    selected_durations = set(body.durations)
    jobs = [
        (contract, period)
        for contract in repository.list_contracts(user_id)
        for period in contract.periods
        if period.duration_seconds in selected_durations
    ]
    snapshots, failed = await asyncio.to_thread(
        _scan_cached_alert_jobs,
        repository,
        jobs,
    )
    return {"signals": snapshots, "requested": len(jobs), "failed": failed}
