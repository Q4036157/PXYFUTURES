"""实时均线状态接口。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.market.ma_engine import MovingAverageConfig, calculate_signal
from app.market.tq_client import MarketDataUnavailable, TqClient
from app.services.auth import current_user_id
from app.services.repository import ConfigRepository, PeriodConfig

router = APIRouter(prefix="/api/contracts", tags=["均线信号"])
_CHINA_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


def _snapshot(
    client: TqClient,
    repository: ConfigRepository,
    symbol: str,
    period: PeriodConfig,
) -> dict[str, object]:
    config = MovingAverageConfig(m4=period.m4, m3=period.m3, m2=period.m2, m1=period.m1)
    market_bars = client.fetch_bars(symbol, period.duration_seconds, config.required_bars + 300, repository)
    bars = market_bars.bars
    if len(bars) < config.required_bars:
        raise ValueError(
            f"{symbol} 的 {period.label} 当前只有 {len(bars)} 根有效 K 线，"
            f"当前均线最大周期为 {max(period.m1 * 4, period.m2, period.m3, period.m4)}，"
            f"至少需要 {config.required_bars} 根；请降低均线周期或更换历史更长的合约"
        )
    # 天勤序列最后一根可能仍在形成；状态只用此前已确认收盘的 K 线。
    signal = calculate_signal(bars, config, confirmed_bar_count=len(bars) - 1)
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
        "label": signal.label,
        "state_since": (
            datetime.fromtimestamp(signal.state_since_ns / 1_000_000_000, tz=_CHINA_TZ).isoformat()
            if signal.state_since_ns
            else None
        ),
        "ma_values": {name: round(value, 4) for name, value in signal.ma_values.items()},
        "bar_close": bars[-1].close,
        "bar_time": datetime.fromtimestamp(bars[-1].timestamp_ns / 1_000_000_000, tz=_CHINA_TZ).isoformat(),
        "data_source": market_bars.source,
        "data_updated_at": (
            datetime.fromtimestamp(market_bars.updated_at_ns / 1_000_000_000, tz=_CHINA_TZ).isoformat()
            if market_bars.updated_at_ns
            else None
        ),
    }


@router.get("/{contract_id}/signals")
async def get_signals(
    contract_id: int, request: Request, user_id: str = Depends(current_user_id)
) -> dict[str, object]:
    repository: ConfigRepository = request.app.state.config_repository
    contract = repository.get_contract(user_id, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="合约不存在")
    if not contract.periods:
        return {"contract_id": contract.id, "symbol": contract.symbol, "signals": []}
    client: TqClient = request.app.state.tq_client
    try:
        signals = await asyncio.gather(
            *(
                asyncio.to_thread(_snapshot, client, repository, contract.symbol, period)
                for period in contract.periods
            )
        )
    except MarketDataUnavailable as exc:
        logger.error("均线数据获取失败: contract_id=%s symbol=%s error=%s", contract_id, contract.symbol, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        logger.warning("均线计算条件不满足: contract_id=%s symbol=%s error=%s", contract_id, contract.symbol, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"contract_id": contract.id, "symbol": contract.symbol, "signals": signals}
