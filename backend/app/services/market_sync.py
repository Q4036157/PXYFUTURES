"""所有已配置合约的后台行情缓存同步。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

from app.market.tq_client import TqClient
from app.services.repository import ConfigRepository, ContractConfig

logger = logging.getLogger(__name__)
_STATE_LOOKBACK_BARS = 300
_SYNC_INTERVAL_SECONDS = 60.0


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


def build_market_sync_jobs(contracts: list[ContractConfig]) -> list[MarketSyncJob]:
    """合并重复的合约周期，并保留最大的历史深度。"""
    requested_counts: dict[tuple[str, int], int] = {}
    for contract in contracts:
        for period in contract.periods:
            required_bars = max(period.m1 * 4, period.m2, period.m3, period.m4) + 1
            key = (contract.symbol, period.duration_seconds)
            requested_counts[key] = max(
                requested_counts.get(key, 0),
                required_bars + _STATE_LOOKBACK_BARS,
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
) -> None:
    """启动后立即同步，随后按固定间隔持续更新。"""
    logger.info("后台行情同步已启动，间隔 %.0f 秒", interval_seconds)
    while not stop_event.is_set():
        started_at = monotonic()
        result = await sync_all_market_data(client, repository, stop_event)
        logger.info(
            "后台行情同步完成: 任务=%d 实时=%d 本地缓存=%d 失败=%d 耗时=%.1f秒",
            result.jobs,
            result.live,
            result.cache,
            result.failed,
            monotonic() - started_at,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
    logger.info("后台行情同步已停止")
