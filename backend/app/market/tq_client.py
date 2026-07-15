"""天勤 K 线读取适配器。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from threading import Lock
from time import monotonic, time, time_ns

from app.config import settings
from app.market.boyi_kline import (
    base_count_for,
    base_duration_for,
    rebuild_boyi_bars,
    should_rebuild_for_boyi,
)
from app.market.contract_catalog import to_tq_symbol
from app.market.ma_engine import Bar
from app.services.repository import ConfigRepository, KlineSyncState
from app.services.secrets_store import get_tq_credentials

logger = logging.getLogger(__name__)
_CHINA_TZ = timezone(timedelta(hours=8))
_MAX_TQ_KLINE_BARS = 10_000
_MAX_CACHED_BASE_BARS = 50_000


class MarketDataUnavailable(RuntimeError):
    """天勤未配置或行情暂不可用。"""


class ContractUnavailable(ValueError):
    """合约不存在、已下市或不属于期货合约。"""


@dataclass(frozen=True)
class MarketBars:
    """行情读取结果，明确标识实时或本地缓存来源。"""

    bars: list[Bar]
    source: str
    updated_at_ns: int | None


class TqClient:
    """以单个 TqApi 实例读取行情，所有调用由锁串行化。"""

    def __init__(self) -> None:
        self._api: object | None = None
        self._lock = Lock()
        self._last_live_update_monotonic = 0.0
        self._remote_retry_after_monotonic = 0.0
        self._history_shortage_notices: set[tuple[str, int, int, int]] = set()
        self._active_contract_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}

    def _get_api(self) -> object:
        if self._api is not None:
            return self._api
        username, password = get_tq_credentials()
        username = username or settings.tq_user_name
        password = password or settings.tq_password
        if not username or not password:
            raise MarketDataUnavailable("未配置天勤账号，请先在设置中保存天勤账号和密码")
        try:
            from tqsdk import TqApi, TqAuth
        except ModuleNotFoundError as exc:
            logger.error("未安装天勤 SDK: %s", exc)
            raise MarketDataUnavailable(
                "当前后端环境未安装天勤 SDK，请运行 QIHUOBAT\\安装依赖.bat，或将 tqsdk 安装到当前 Python 环境"
            ) from exc

        try:
            self._api = TqApi(auth=TqAuth(username, password))
            return self._api
        except Exception as exc:  # noqa: BLE001
            logger.exception("天勤连接失败")
            raise MarketDataUnavailable("天勤连接失败，请检查账号、密码和网络") from exc

    def fetch_bars(
        self,
        symbol: str,
        duration_seconds: int,
        count: int,
        repository: ConfigRepository,
    ) -> MarketBars:
        """优先增量同步天勤 K 线，维护或断网时回退到本地缓存。"""
        if should_rebuild_for_boyi(duration_seconds):
            return self._fetch_boyi_bars(symbol, duration_seconds, count, repository)
        return self._fetch_native_bars(symbol, duration_seconds, count, repository)

    def _fetch_boyi_bars(
        self,
        symbol: str,
        duration_seconds: int,
        count: int,
        repository: ConfigRepository,
    ) -> MarketBars:
        """使用缓存的分钟基础线重建博易兼容盘中 K 线。"""
        base_duration_seconds = base_duration_for(duration_seconds)
        required_base_count = base_count_for(duration_seconds, count, base_duration_seconds)
        # 天勤免费行情单次最多返回 10,000 根 K 线。缓存会在后续盘中持续增长。
        base_count = min(_MAX_TQ_KLINE_BARS, required_base_count)
        cached_base_count = min(_MAX_CACHED_BASE_BARS, required_base_count)
        base_result = self._fetch_native_bars(
            symbol,
            base_duration_seconds,
            base_count,
            repository,
            cache_count=cached_base_count,
        )
        rebuilt = rebuild_boyi_bars(
            base_result.bars,
            duration_seconds,
            base_duration_seconds,
        )
        if len(rebuilt) < count:
            notice_key = (to_tq_symbol(symbol), duration_seconds, count, len(rebuilt))
            if notice_key not in self._history_shortage_notices:
                self._history_shortage_notices.add(notice_key)
                logger.info(
                    "博易兼容 K 线历史少于状态回溯窗口: symbol=%s target=%s base=%s requested=%d received=%d",
                    symbol,
                    duration_seconds,
                    base_duration_seconds,
                    count,
                    len(rebuilt),
                )
        return MarketBars(rebuilt[-count:], base_result.source, base_result.updated_at_ns)

    def _fetch_native_bars(
        self,
        symbol: str,
        duration_seconds: int,
        count: int,
        repository: ConfigRepository,
        cache_count: int | None = None,
    ) -> MarketBars:
        """读取天勤原生周期并写入本地缓存。"""
        with self._lock:
            tq_symbol = to_tq_symbol(symbol)
            sync_state = repository.get_kline_sync_state(symbol, duration_seconds)
            cached = repository.load_kline_bars(symbol, duration_seconds, cache_count or count)
            if self._should_use_cache_only():
                return self._cached_result(symbol, duration_seconds, cached, sync_state)

            request_count = self._request_count(duration_seconds, count, sync_state)
            try:
                api = self._get_api()
                frame = api.get_kline_serial(
                    tq_symbol,
                    duration_seconds=duration_seconds,
                    data_length=request_count,
                )
                # get_kline_serial 会同步等待首批历史 K 线初始化完成。一个监控批次中
                # 只需推进一次行情循环，避免十合约多周期场景把短等待累积成排队延迟。
                if monotonic() - self._last_live_update_monotonic >= 0.2:
                    api.wait_update(deadline=time() + 0.25)
                    self._last_live_update_monotonic = monotonic()
                remote_bars = [
                    (int(row.datetime), float(row.close))
                    for row in frame.itertuples(index=False)
                    if float(row.close) > 0 and int(row.datetime) > 0
                ]
                synced_at_ns = time_ns()
                repository.save_kline_bars(
                    symbol,
                    duration_seconds,
                    remote_bars,
                    request_count,
                    synced_at_ns,
                )
                cached = repository.load_kline_bars(symbol, duration_seconds, cache_count or count)
                if request_count == count and len(remote_bars) < count:
                    notice_key = (tq_symbol, duration_seconds, count, len(remote_bars))
                    if notice_key not in self._history_shortage_notices:
                        self._history_shortage_notices.add(notice_key)
                        logger.info(
                            "天勤可用历史少于状态回溯窗口: symbol=%s tq_symbol=%s requested=%d received=%d",
                            symbol, tq_symbol, count, len(remote_bars),
                        )
                return MarketBars(self._bars_from_rows(cached), "live", synced_at_ns)
            except Exception as exc:  # noqa: BLE001
                self._remote_retry_after_monotonic = monotonic() + 30
                if cached:
                    logger.warning(
                        "天勤 K 线读取失败，改用本地缓存: symbol=%s duration=%s error=%s",
                        symbol,
                        duration_seconds,
                        exc,
                    )
                    return self._cached_result(symbol, duration_seconds, cached, sync_state)
                logger.exception("读取天勤 K 线失败: %s (%s) %s", symbol, tq_symbol, duration_seconds)
                raise MarketDataUnavailable(
                    f"读取天勤 K 线失败：{tq_symbol}，且本地没有可用缓存；请检查天勤连接后重试"
                ) from exc

    @staticmethod
    def _bars_from_rows(rows: list[tuple[int, float]]) -> list[Bar]:
        return [Bar(timestamp_ns=timestamp_ns, close=close) for timestamp_ns, close in rows]

    def _should_use_cache_only(self) -> bool:
        now = datetime.now(_CHINA_TZ)
        in_maintenance = now.hour == 19 and 0 <= now.minute <= 30
        return in_maintenance or monotonic() < self._remote_retry_after_monotonic

    @staticmethod
    def _request_count(duration_seconds: int, count: int, sync_state: KlineSyncState | None) -> int:
        if sync_state is None or sync_state.max_requested_bars < count:
            return count
        elapsed_seconds = max(0, (time_ns() - sync_state.last_synced_at_ns) / 1_000_000_000)
        estimated_missing = ceil(elapsed_seconds / duration_seconds) + 5
        return min(count, max(10, estimated_missing))

    def _cached_result(
        self,
        symbol: str,
        duration_seconds: int,
        cached: list[tuple[int, float]],
        sync_state: KlineSyncState | None,
    ) -> MarketBars:
        if cached:
            return MarketBars(
                self._bars_from_rows(cached),
                "cache",
                sync_state.last_synced_at_ns if sync_state else None,
            )
        raise MarketDataUnavailable(
            f"天勤行情暂不可用，且本地没有 {symbol} {duration_seconds} 秒 K 线缓存"
        )

    def require_active_future(self, symbol: str) -> str:
        """确认天勤当前存在指定的未下市期货合约，并返回天勤代码。"""
        with self._lock:
            api = self._get_api()
            tq_symbol = to_tq_symbol(symbol)
            try:
                quote = api.get_quote(tq_symbol)
            except Exception as exc:  # noqa: BLE001
                logger.info("天勤合约验证失败: symbol=%s tq_symbol=%s", symbol, tq_symbol, exc_info=True)
                raise MarketDataUnavailable(
                    f"暂时无法验证 {symbol} 是否可用，请检查天勤连接后重试"
                ) from exc

            if getattr(quote, "ins_class", "") != "FUTURE" or bool(getattr(quote, "expired", False)):
                raise ContractUnavailable(f"{symbol} 当前不存在、已下市，或不是可添加的期货合约")
            return tq_symbol

    def list_active_futures(self, exchange: str, product: str) -> list[str]:
        """返回指定品种当前未下市的期货合约，结果短时缓存以减少合约服务请求。"""
        normalized_exchange = exchange.upper()
        normalized_product = product.upper()
        cache_key = (normalized_exchange, normalized_product)
        now = monotonic()
        with self._lock:
            cached = self._active_contract_cache.get(cache_key)
            if cached and now - cached[0] < 300:
                return list(cached[1])

            api = self._get_api()
            try:
                symbols = api.query_quotes(
                    ins_class="FUTURE",
                    exchange_id=normalized_exchange,
                    product_id=normalized_product.lower(),
                    expired=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "天勤合约列表查询失败: exchange=%s product=%s",
                    normalized_exchange,
                    normalized_product,
                    exc_info=True,
                )
                raise MarketDataUnavailable("暂时无法读取天勤可用合约，请稍后重试") from exc

            prefix = f"{normalized_exchange}."
            active = tuple(sorted({symbol for symbol in symbols if symbol.upper().startswith(prefix)}))
            self._active_contract_cache[cache_key] = (now, active)
            return list(active)

    def close(self) -> None:
        with self._lock:
            if self._api is not None:
                try:
                    self._api.close()
                except Exception:  # noqa: BLE001
                    logger.warning("关闭天勤连接失败", exc_info=True)
                finally:
                    self._api = None
