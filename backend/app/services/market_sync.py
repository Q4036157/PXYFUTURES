"""所有已配置合约的后台行情缓存同步。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from app.market.boyi_kline import base_count_for, base_duration_for, should_rebuild_for_boyi
from app.market.tq_client import MarketDataUnavailable, TqClient, TqCredentialsInvalid
from app.services.repository import ConfigRepository, ContractConfig
from app.services.runtime_notices import RuntimeNoticeLimiter

logger = logging.getLogger(__name__)
_STATE_LOOKBACK_BARS = 300
_SYNC_INTERVAL_SECONDS = 60.0
_MAX_CACHED_BASE_BARS = 50_000
_MARKET_EVENT_WAIT_SECONDS = 0.75


@dataclass(frozen=True)
class MarketSyncJob:
    symbol: str
    duration_seconds: int
    count: int


@dataclass(frozen=True)
class MarketSyncResult:
    jobs: int
    live: int
    cache: int
    failed: int


@dataclass(frozen=True)
class MarketSyncEvent:
    """天勤确认出现新基础K线后产生的事件。"""

    changed_sources: dict[tuple[str, int], int]
    received_at_ns: int


def build_market_sync_jobs(contracts: list[ContractConfig]) -> list[MarketSyncJob]:
    """把派生周期合并为最少的天勤基础K线订阅。"""
    requested_counts: dict[tuple[str, int], int] = {}
    for contract in contracts:
        for period in contract.periods:
            target_count = (
                max(period.m1 * 4, period.m2, period.m3, period.m4)
                + 1
                + _STATE_LOOKBACK_BARS
            )
            source_duration = period.duration_seconds
            source_count = target_count
            if should_rebuild_for_boyi(period.duration_seconds):
                source_duration = base_duration_for(period.duration_seconds)
                source_count = min(
                    _MAX_CACHED_BASE_BARS,
                    base_count_for(
                        period.duration_seconds,
                        target_count,
                        source_duration,
                    ),
                )
            key = (contract.symbol, source_duration)
            requested_counts[key] = max(
                requested_counts.get(key, 0),
                source_count,
            )

    return [
        MarketSyncJob(symbol=symbol, duration_seconds=duration_seconds, count=count)
        for (symbol, duration_seconds), count in sorted(requested_counts.items())
    ]


async def sync_all_market_data(
    client: TqClient,
    repository: ConfigRepository,
    stop_event: asyncio.Event | None = None,
) -> MarketSyncResult:
    """串行同步一轮；单个合约失败不影响后续合约。"""
    jobs = build_market_sync_jobs(await asyncio.to_thread(repository.list_all_contracts))
    live = 0
    cache = 0
    failed = 0
    failures: list[str] = []

    for job in jobs:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            result = await asyncio.to_thread(
                client.fetch_bars,
                job.symbol,
                job.duration_seconds,
                job.count,
                repository,
            )
            if result.source == "live":
                live += 1
            else:
                cache += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if len(failures) < 3:
                failures.append(f"{job.symbol}/{job.duration_seconds}s: {exc}")

    if failures:
        logger.warning("后台行情同步失败示例: %s", "；".join(failures))
    return MarketSyncResult(jobs=len(jobs), live=live, cache=cache, failed=failed)


async def run_market_sync_loop(
    client: TqClient,
    repository: ConfigRepository,
    stop_event: asyncio.Event,
    interval_seconds: float = _SYNC_INTERVAL_SECONDS,
    on_rollover: Callable[[MarketSyncEvent], Awaitable[None]] | None = None,
) -> None:
    """持续推进天勤事件循环，并在新K线出现时立即通知预警层。"""
    logger.info("后台行情事件同步已启动，订阅刷新间隔 %.0f 秒", interval_seconds)
    jobs: list[MarketSyncJob] = []
    refresh_deadline = 0.0
    notices = RuntimeNoticeLimiter(logger)
    while not stop_event.is_set():
        now = monotonic()
        if now >= refresh_deadline:
            contracts = await asyncio.to_thread(repository.list_all_contracts)
            jobs = build_market_sync_jobs(contracts)
            refresh_deadline = now + interval_seconds
            logger.info("后台行情订阅已刷新: 候选基础订阅=%d", len(jobs))

        if not jobs:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=min(interval_seconds, 1.0))
            except TimeoutError:
                continue
            break

        try:
            event = await asyncio.to_thread(
                client.wait_for_bar_rollovers,
                jobs,
                repository,
                _MARKET_EVENT_WAIT_SECONDS,
            )
        except TqCredentialsInvalid as exc:
            blocked_revision = client.credential_revision
            logger.warning("后台行情认证已暂停: %s；等待重新保存天勤账号", exc)
            while (
                not stop_event.is_set()
                and client.credential_revision == blocked_revision
            ):
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                except TimeoutError:
                    pass
            continue
        except Exception as exc:  # noqa: BLE001
            notices.warning(
                "market-sync", "天勤事件同步暂时失败，将按连接冷却时间恢复: 类型=%s",
                type(exc).__name__,
                exc_info=not isinstance(exc, MarketDataUnavailable),
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3.0)
            except TimeoutError:
                pass
            continue
        notices.recover("market-sync", "天勤事件同步已恢复")
        if event.changed_sources and on_rollover is not None:
            logger.info(
                "后台行情检测到收K: 基础序列=%d",
                len(event.changed_sources),
            )
            await on_rollover(
                MarketSyncEvent(
                    changed_sources=event.changed_sources,
                    received_at_ns=event.received_at_ns,
                )
            )
    logger.info("后台行情事件同步已停止")
