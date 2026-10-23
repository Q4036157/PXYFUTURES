"""天勤 K 线读取适配器。"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from inspect import isawaitable
from math import ceil
from threading import Lock
from time import monotonic, time, time_ns
from typing import Protocol

from app.config import settings
from app.market.boyi_kline import (
    base_count_for,
    base_duration_for,
    rebuild_boyi_bars,
    should_rebuild_for_boyi,
)
from app.market.contract_catalog import to_tq_product, to_tq_symbol
from app.market.contract_directory import (
    SUPPORTED_EXCHANGES,
    DirectoryContract,
    LocalContractDirectory,
)
from app.market.ma_engine import Bar
from app.services.repository import ConfigRepository, KlineSyncState
from app.services.runtime_notices import RuntimeNoticeLimiter
from app.services.secrets_store import get_tq_credentials

logger = logging.getLogger(__name__)
_CHINA_TZ = timezone(timedelta(hours=8))
_MAX_TQ_KLINE_BARS = 10_000
_MAX_CACHED_BASE_BARS = 50_000
_LIVE_KLINE_BARS = 10
_CONNECTION_NOTICE_INTERVAL_SECONDS = 15 * 60
_CONTRACT_QUERY_TIMEOUT_SECONDS = 2.0
_CONTRACT_QUERY_LOCK_TIMEOUT_SECONDS = 0.5


def _is_explicit_credential_rejection(error: BaseException) -> bool:
    error_text = str(error)
    return "invalid_grant" in error_text or "Invalid user credentials" in error_text


def _connection_failure_message(error: BaseException) -> str:
    """只输出故障类别，不把代理地址中的凭据或认证参数写入日志。"""
    text = f"{type(error).__name__} {error}".lower()
    if "proxy" in text or "代理" in text:
        return "代理连接不可用，请检查代理程序及代理端口"
    if "timeout" in text or "timed out" in text:
        return "天勤连接超时，请检查网络或代理服务"
    return "天勤连接暂时不可用，请检查网络或代理服务"


class MarketDataUnavailable(RuntimeError):
    """天勤未配置或行情暂不可用。"""


class TqCredentialsInvalid(MarketDataUnavailable):
    """天勤认证服务明确拒绝了当前账号凭据。"""


class ContractUnavailable(ValueError):
    """合约不存在、已下市或不属于期货合约。"""


@dataclass(frozen=True)
class MarketBars:
    """行情读取结果，明确标识实时或本地缓存来源。"""

    bars: list[Bar]
    source: str
    updated_at_ns: int | None


class MarketSourceJob(Protocol):
    symbol: str
    duration_seconds: int
    count: int


@dataclass(frozen=True)
class MarketRollover:
    """一批基础K线发生切换的时间信息。"""

    changed_sources: dict[tuple[str, int], int]
    received_at_ns: int


@dataclass
class _LiveKlineSeries:
    frame: object
    data_length: int
    cache_count: int
    latest_timestamp_ns: int | None


class _TqConnectionNoticeLimiter:
    """压缩 TqSdk 连接抖动提示，同时保留首次状态变化。"""

    def __init__(
        self,
        interval_seconds: float = _CONNECTION_NOTICE_INTERVAL_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._last_emitted: dict[tuple[str, str], float] = {}
        self._suppressed: dict[tuple[str, str], int] = {}

    def handle(self, message: str, _level: str | int = "INFO") -> bool:
        if "重新建立网络连接" in message:
            return True
        if "网络连接断开" in message:
            event = "disconnected"
            label = "断开"
            log_level = logging.WARNING
        elif "网络连接已恢复" in message:
            event = "recovered"
            label = "恢复"
            log_level = logging.INFO
        elif "网络连接已建立" in message:
            event = "established"
            label = "建立"
            log_level = logging.INFO
        else:
            return False

        match = re.search(r"(wss?://\S+?)(?:\s+的网络连接|\s+重新建立网络连接)", message)
        url = match.group(1) if match else "unknown"
        key = (url, event)
        now = self._clock()
        last_emitted = self._last_emitted.get(key)
        if last_emitted is not None and now - last_emitted < self._interval_seconds:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return True

        suppressed = self._suppressed.pop(key, 0)
        suffix = f"，已抑制 {suppressed} 条重复提示" if suppressed else ""
        logger.log(log_level, "天勤行情连接%s: %s%s", label, url, suffix)
        self._last_emitted[key] = now
        return True


class TqClient:
    """以单个 TqApi 实例读取行情，所有调用由锁串行化。"""

    def __init__(self, contract_directory: LocalContractDirectory | None = None) -> None:
        self._api: object | None = None
        self._contract_directory = contract_directory
        self._directory_retry_after = 0.0
        self._lock = Lock()
        self._last_live_update_monotonic = 0.0
        self._remote_retry_after_monotonic = 0.0
        self._history_shortage_notices: set[tuple[str, int, int, int]] = set()
        self._active_contract_cache: OrderedDict[
            tuple[str, str], tuple[float, tuple[str, ...]]
        ] = OrderedDict()
        self._active_contract_cache_lock = Lock()
        self._connection_notice_limiter = _TqConnectionNoticeLimiter()
        self._live_kline_series: dict[tuple[str, int], _LiveKlineSeries] = {}
        self._credential_error: str | None = None
        self._credential_revision = 0
        self._connect_retry_after = 0.0
        self._connect_failures = 0
        self._last_connect_error = "天勤连接暂时不可用"
        self._notices = RuntimeNoticeLimiter(logger)
        self._status_lock = Lock()
        self._contract_states: dict[str, tuple[float, str]] = {}
        self._excluded_symbols: frozenset[str] = frozenset()

    @property
    def credential_revision(self) -> int:
        return self._credential_revision

    @staticmethod
    def validate_credentials(username: str, password: str) -> None:
        """只调用天勤认证接口校验凭据，不创建行情任务或记录敏感内容。"""
        try:
            from tqsdk import TqAuth
        except ModuleNotFoundError as exc:
            raise MarketDataUnavailable("当前环境未安装天勤 SDK") from exc

        try:
            TqAuth(username.strip(), password).login()
        except Exception as exc:
            if _is_explicit_credential_rejection(exc):
                raise TqCredentialsInvalid("天勤账号或密码错误，请重新输入") from exc
            raise MarketDataUnavailable(
                f"天勤认证服务暂时不可用：{_connection_failure_message(exc)}"
            ) from exc

    def credentials_changed(self) -> None:
        """凭据成功更新后清除永久阻断，并关闭旧行情连接。"""
        self.close()
        self._credential_error = None
        self._remote_retry_after_monotonic = 0.0
        self._credential_revision += 1
        self._connect_retry_after = 0.0
        self._connect_failures = 0

    def _get_api(self) -> object:
        if self._api is not None:
            return self._api
        if self._credential_error is not None:
            raise TqCredentialsInvalid(self._credential_error)
        if monotonic() < self._connect_retry_after:
            raise MarketDataUnavailable(self._last_connect_error)
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

        from app.services.tq_auth_state import record_tq_auth_state

        try:
            self.validate_credentials(username, password)
        except TqCredentialsInvalid as exc:
            self._credential_error = str(exc)
            record_tq_auth_state("invalid", str(exc), "runtime")
            logger.error("天勤认证被拒绝，已停止自动重试；重新保存账号后恢复")
            raise
        except MarketDataUnavailable as exc:
            record_tq_auth_state("unavailable", str(exc), "runtime")
            self._defer_connection(str(exc))
            raise

        try:
            notice_limiter = self._connection_notice_limiter

            class FilteredTqApi(TqApi):
                def _setup_connection(self) -> None:
                    try:
                        super()._setup_connection()
                    except Exception:
                        # SDK 在此处失败时尚未进入自身的初始化清理范围。
                        try:
                            self.close()
                        except Exception as cleanup_error:  # noqa: BLE001
                            logger.error(
                                "天勤连接初始化失败后的资源释放异常: 类型=%s",
                                type(cleanup_error).__name__,
                            )
                        raise

                def _print(self, msg: str = "", level: str | int = "INFO") -> None:
                    if notice_limiter.handle(msg, level):
                        return
                    super()._print(msg, level)

            self._api = FilteredTqApi(auth=TqAuth(username, password))
            record_tq_auth_state("valid", "天勤账号认证成功，行情连接已建立", "runtime")
            logger.info("天勤账号认证成功，行情连接已建立")
            self._connect_failures = 0
            self._connect_retry_after = 0.0
            self._notices.recover("connection", "天勤连接已恢复")
            return self._api
        except Exception as exc:
            if _is_explicit_credential_rejection(exc):
                self._credential_error = "天勤账号或密码错误，请重新输入"
                record_tq_auth_state("invalid", self._credential_error, "runtime")
                logger.error("天勤认证被拒绝，已停止自动重试；重新保存账号后恢复")
                raise TqCredentialsInvalid(self._credential_error) from exc
            record_tq_auth_state(
                "unavailable",
                "天勤账号已通过认证，但行情连接建立失败",
                "runtime",
            )
            message = _connection_failure_message(exc)
            self._defer_connection(message)
            raise MarketDataUnavailable(message) from exc

    def _defer_connection(self, message: str) -> None:
        self._connect_failures = min(self._connect_failures + 1, 6)
        delay = min(60.0, 3.0 * 2 ** (self._connect_failures - 1))
        self._connect_retry_after = monotonic() + delay
        self._last_connect_error = message
        self._notices.warning("connection", "%s；%.0f秒后重试", message, delay)

    def contract_status(self, symbol: str) -> str:
        """仅读取已核实的生命周期状态，不在HTTP或预警快路径请求天勤。"""
        with self._status_lock:
            state = self._contract_states.get(symbol)
            return state[1] if state else "unknown"

    def _remember_contract_status(self, symbol: str, status: str) -> None:
        with self._status_lock:
            self._contract_states.pop(symbol, None)
            self._contract_states[symbol] = (monotonic(), status)
            while len(self._contract_states) > 512:
                self._contract_states.pop(next(iter(self._contract_states)))

    def _contract_expired(self, api: object, symbol: str) -> bool:
        with self._status_lock:
            cached = self._contract_states.get(symbol)
        if cached and monotonic() - cached[0] < 300:
            return cached[1] == "expired"
        get_quote = getattr(api, "get_quote", None)
        if not callable(get_quote):
            return False
        try:
            quote = get_quote(to_tq_symbol(symbol))
            status = ("expired" if bool(getattr(quote, "expired", False)) else
                      "active" if getattr(quote, "ins_class", "") == "FUTURE" else "unknown")
            self._remember_contract_status(symbol, status)
            return status == "expired"
        except Exception as exc:  # noqa: BLE001
            self._notices.warning(
                "contract-status", "合约状态暂未核实，保留原有行情流程: 类型=%s", type(exc).__name__,
            )
            self._remember_contract_status(symbol, "unknown")
            return False

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

    def wait_for_bar_rollovers(
        self,
        jobs: Sequence[MarketSourceJob],
        repository: ConfigRepository,
        timeout_seconds: float,
    ) -> MarketRollover:
        """复用天勤内存序列，行情到达后只落库发生切换的基础K线。"""
        with self._lock:
            api = self._get_api()
            active_keys: list[tuple[str, int]] = []
            initialized = 0
            history_backfills = 0
            excluded: set[str] = set()
            for job in jobs:
                if self._contract_expired(api, job.symbol):
                    excluded.add(job.symbol)
                    continue
                key = (job.symbol, job.duration_seconds)
                active_keys.append(key)
                sync_state = repository.get_kline_sync_state(
                    job.symbol,
                    job.duration_seconds,
                )
                history_length = min(
                    _MAX_TQ_KLINE_BARS,
                    max(
                        _LIVE_KLINE_BARS,
                        self._request_count(
                            job.duration_seconds,
                            job.count,
                            sync_state,
                        ),
                    ),
                )
                series = self._live_kline_series.get(key)
                if series is not None and series.data_length == _LIVE_KLINE_BARS:
                    series.cache_count = max(series.cache_count, job.count)
                    continue

                # 冷启动时先用长窗口一次性补齐均线历史，再明确切换到10根的
                # 实时窗口。天勤会把不同 data_length 当成不同序列；页面增量
                # 请求也使用10根，后台必须监听同一个短序列才能稳定收到收K。
                history_frame = api.get_kline_serial(
                    to_tq_symbol(job.symbol),
                    duration_seconds=job.duration_seconds,
                    data_length=history_length,
                )
                history_bars = self._remote_bars(history_frame)
                synced_at_ns = time_ns()
                if history_bars:
                    repository.save_kline_bars(
                        job.symbol,
                        job.duration_seconds,
                        history_bars,
                        history_length,
                        synced_at_ns,
                    )
                live_frame = history_frame
                live_bars = history_bars
                if history_length != _LIVE_KLINE_BARS:
                    live_frame = api.get_kline_serial(
                        to_tq_symbol(job.symbol),
                        duration_seconds=job.duration_seconds,
                        data_length=_LIVE_KLINE_BARS,
                    )
                    live_bars = self._remote_bars(live_frame)
                    if live_bars:
                        repository.save_kline_bars(
                            job.symbol,
                            job.duration_seconds,
                            live_bars,
                            _LIVE_KLINE_BARS,
                            synced_at_ns,
                        )
                    history_backfills += 1
                self._live_kline_series[key] = _LiveKlineSeries(
                    frame=live_frame,
                    data_length=_LIVE_KLINE_BARS,
                    cache_count=job.count,
                    latest_timestamp_ns=live_bars[-1][0] if live_bars else None,
                )
                initialized += 1

            if frozenset(excluded) != self._excluded_symbols:
                self._excluded_symbols = frozenset(excluded)
                logger.info(
                    "到期合约保留历史并退出实时订阅: 到期合约=%d 活跃基础订阅=%d",
                    len(excluded), len(active_keys),
                )
            active_set = set(active_keys)
            self._live_kline_series = {
                key: value for key, value in self._live_kline_series.items() if key in active_set
            }

            if initialized:
                logger.info(
                    "实时行情短序列已就绪: 新增=%d 历史回填=%d 总数=%d",
                    initialized,
                    history_backfills,
                    len(self._live_kline_series),
                )

            received_at_ns = time_ns()
            changed = self._collect_bar_rollovers(active_keys, repository, received_at_ns)
            if changed:
                return MarketRollover(changed, received_at_ns)

            api.wait_update(deadline=time() + max(0.05, timeout_seconds))
            self._last_live_update_monotonic = monotonic()
            received_at_ns = time_ns()
            return MarketRollover(
                self._collect_bar_rollovers(active_keys, repository, received_at_ns),
                received_at_ns,
            )

    def _collect_bar_rollovers(
        self,
        active_keys: Sequence[tuple[str, int]],
        repository: ConfigRepository,
        synced_at_ns: int,
    ) -> dict[tuple[str, int], int]:
        changed: dict[tuple[str, int], int] = {}
        batches: list[tuple[str, int, list[tuple[int, float]], int]] = []
        for key in active_keys:
            series = self._live_kline_series[key]
            latest_rows = self._remote_bars(series.frame, tail=10)
            if not latest_rows:
                continue
            latest_timestamp_ns = latest_rows[-1][0]
            if series.latest_timestamp_ns is None:
                repository.save_kline_bars(
                    key[0],
                    key[1],
                    latest_rows,
                    series.cache_count,
                    synced_at_ns,
                )
                series.latest_timestamp_ns = latest_timestamp_ns
                continue
            if latest_timestamp_ns <= series.latest_timestamp_ns:
                continue
            batches.append((key[0], key[1], latest_rows, series.cache_count))
            series.latest_timestamp_ns = latest_timestamp_ns
            changed[key] = latest_timestamp_ns
        if batches:
            repository.save_kline_bar_batches(batches, synced_at_ns)
        return changed

    @staticmethod
    def _remote_bars(frame: object, tail: int | None = None) -> list[tuple[int, float]]:
        source = frame.tail(tail) if tail is not None else frame
        return [
            (int(row.datetime), float(row.close))
            for row in source.itertuples(index=False)
            if float(row.close) > 0 and int(row.datetime) > 0
        ]

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
            if self.contract_status(symbol) == "expired":
                return self._cached_result(symbol, duration_seconds, cached, sync_state)
            if self._should_use_cache_only():
                return self._cached_result(symbol, duration_seconds, cached, sync_state)

            request_count = self._request_count(duration_seconds, count, sync_state)
            try:
                api = self._get_api()
                if self._contract_expired(api, symbol):
                    return self._cached_result(symbol, duration_seconds, cached, sync_state)
                series = self._live_kline_series.get((symbol, duration_seconds))
                if series is not None and sync_state and sync_state.max_requested_bars >= count:
                    frame = series.frame
                    request_count = series.data_length
                else:
                    frame = api.get_kline_serial(
                        tq_symbol, duration_seconds=duration_seconds, data_length=request_count,
                    )
                # get_kline_serial 会同步等待首批历史 K 线初始化完成。一个监控批次中
                # 只需推进一次行情循环，避免十合约多周期场景把短等待累积成排队延迟。
                if monotonic() - self._last_live_update_monotonic >= 0.2:
                    api.wait_update(deadline=time() + 0.25)
                    self._last_live_update_monotonic = monotonic()
                remote_bars = self._remote_bars(frame)
                if not remote_bars:
                    raise MarketDataUnavailable("天勤未返回有效K线，保留本地历史时间")
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
                            symbol,
                            tq_symbol,
                            count,
                            len(remote_bars),
                        )
                self._notices.recover("kline-fetch", "天勤K线读取已恢复")
                return MarketBars(self._bars_from_rows(cached), "live", synced_at_ns)
            except TqCredentialsInvalid:
                if cached:
                    return self._cached_result(symbol, duration_seconds, cached, sync_state)
                raise
            except Exception as exc:
                self._remote_retry_after_monotonic = monotonic() + 30
                if cached:
                    self._notices.warning(
                        "kline-fetch",
                        "天勤K线读取失败，改用本地缓存: symbol=%s duration=%s 类型=%s",
                        symbol,
                        duration_seconds,
                        type(exc).__name__,
                    )
                    return self._cached_result(symbol, duration_seconds, cached, sync_state)
                logger.exception(
                    "读取天勤 K 线失败: %s (%s) %s", symbol, tq_symbol, duration_seconds
                )
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
                "historical" if self.contract_status(symbol) == "expired" else "cache",
                sync_state.last_synced_at_ns if sync_state else None,
            )
        raise MarketDataUnavailable(
            f"天勤行情暂不可用，且本地没有 {symbol} {duration_seconds} 秒 K 线缓存"
        )

    def require_active_future(self, symbol: str) -> str:
        """确认天勤当前存在指定的未下市期货合约，并返回天勤代码。"""
        if self._contract_directory is not None:
            directory = self._contract_directory
            if not directory.validation_ready():
                directory.request_refresh()
                raise MarketDataUnavailable("本地合约目录正在同步，请稍后重试验证")
            record = directory.get(to_tq_symbol(symbol))
            if record is None or record.expires_at <= time():
                directory.request_refresh()
                raise ContractUnavailable(f"{symbol} 不在当前可用合约目录中或已下市；后台将刷新目录，请核对代码")
            match = re.fullmatch(r"CZCE\.[A-Z]+(\d{2})(\d{2})", symbol.upper())
            if match and (record.delivery_year != 2000 + int(match[1]) or record.delivery_month != int(match[2])):
                raise ContractUnavailable(f"{symbol} 的交割年月与当前可用合约不符，请核对年份和月份")
            self._remember_contract_status(symbol, "active")
            self._remember_contract_status(record.symbol, "active")
            return record.symbol
        if not self._lock.acquire(timeout=_CONTRACT_QUERY_LOCK_TIMEOUT_SECONDS):
            raise MarketDataUnavailable("行情同步繁忙，请稍后重试验证")
        try:
            api = self._get_api()
            tq_symbol = to_tq_symbol(symbol)
            try:
                async def query_quote():
                    quote = api.get_quote(tq_symbol)
                    if isawaitable(quote):
                        await quote
                    return quote

                quote = self._wait_contract_query(api, query_quote)
            except MarketDataUnavailable:
                raise
            except Exception as exc:
                logger.info(
                    "天勤合约验证失败: symbol=%s tq_symbol=%s", symbol, tq_symbol, exc_info=True
                )
                raise MarketDataUnavailable(
                    f"暂时无法验证 {symbol} 是否可用，请检查天勤连接后重试"
                ) from exc

            if getattr(quote, "ins_class", "") != "FUTURE" or bool(
                getattr(quote, "expired", False)
            ):
                if bool(getattr(quote, "expired", False)):
                    self._remember_contract_status(symbol, "expired")
                raise ContractUnavailable(f"{symbol} 当前不存在、已下市，或不是可添加的期货合约")
            match = re.fullmatch(r"CZCE\.[A-Z]+(\d{2})(\d{2})", symbol.upper())
            if match:
                year, month = getattr(quote, "delivery_year", 0), getattr(quote, "delivery_month", 0)
                if not year or not month:
                    raise MarketDataUnavailable(f"暂时无法确认 {symbol} 的交割年月，请稍后重试")
                if year != 2000 + int(match[1]) or month != int(match[2]):
                    raise ContractUnavailable(f"{symbol} 的交割年月与当前可用合约不符，请核对年份和月份")
            self._remember_contract_status(symbol, "active")
            self._remember_contract_status(tq_symbol, "active")
            return tq_symbol
        finally:
            self._lock.release()

    @staticmethod
    def _wait_contract_query(api: object, query: Callable) -> object:
        """在SDK协程内发起查询，统一限制等待时间，避免同步接口等待30秒。"""
        task = api.create_task(query())
        try:
            deadline = monotonic() + _CONTRACT_QUERY_TIMEOUT_SECONDS
            while not task.done():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise MarketDataUnavailable("合约查询超时，请稍后重试")
                api.wait_update(deadline=time() + min(0.05, remaining))
            return task.result()
        finally:
            if not task.done():
                task.cancel()

    def list_active_futures(self, exchange: str, product: str) -> list[str]:
        """返回指定品种当前未下市的期货合约，结果短时缓存以减少合约服务请求。"""
        if self._contract_directory is not None:
            symbols = self._contract_directory.candidates(exchange, product)
            if symbols is None:
                self._contract_directory.request_refresh()
                raise MarketDataUnavailable("本地合约目录正在同步，请稍后重试候选查询")
            return symbols
        normalized_exchange = exchange.upper()
        normalized_product = product.upper()
        cache_key = (normalized_exchange, normalized_product)

        def read_cache() -> list[str] | None:
            with self._active_contract_cache_lock:
                cached = self._active_contract_cache.get(cache_key)
                if cached and monotonic() - cached[0] < 300:
                    return list(cached[1])
            return None

        # 候选缓存不排队等待行情同步；只在缓存未命中时限时获取 SDK 锁。
        cached_symbols = read_cache()
        if cached_symbols is not None:
            return cached_symbols
        if not self._lock.acquire(timeout=_CONTRACT_QUERY_LOCK_TIMEOUT_SECONDS):
            self._notices.warning("contract-suggestions", "可用合约查询暂不可用：行情锁等待超时")
            raise MarketDataUnavailable("行情同步繁忙，可稍后重试候选查询，或输入完整合约后验证")
        try:
            cached_symbols = read_cache()
            if cached_symbols is not None:
                return cached_symbols
            if self._api is None:
                raise MarketDataUnavailable("行情连接尚未就绪，请稍后重试候选查询")
            api = self._api

            async def query_symbols() -> list[str]:
                result = api.query_quotes(
                    ins_class="FUTURE",
                    exchange_id=normalized_exchange,
                    product_id=to_tq_product(normalized_exchange, normalized_product),
                    expired=False,
                )
                return list(await result if isawaitable(result) else result)

            # 在 SDK 协程中查询，避免同步接口内置的 30 秒等待阻塞行情刷新。
            symbols = self._wait_contract_query(api, query_symbols)
            prefix = f"{normalized_exchange}."
            active = tuple(
                sorted({symbol for symbol in symbols if symbol.upper().startswith(prefix)})
            )
            with self._active_contract_cache_lock:
                self._active_contract_cache[cache_key] = (monotonic(), active)
                self._active_contract_cache.move_to_end(cache_key)
                while len(self._active_contract_cache) > 128:
                    self._active_contract_cache.popitem(last=False)
            self._notices.recover("contract-suggestions", "可用合约查询已恢复")
            return list(active)
        except Exception as exc:
            self._notices.warning(
                "contract-suggestions", "可用合约查询未完成：exchange=%s product=%s 类型=%s",
                normalized_exchange, normalized_product, type(exc).__name__,
            )
            if isinstance(exc, MarketDataUnavailable):
                raise
            raise MarketDataUnavailable("暂时无法读取天勤可用合约，请稍后重试") from exc
        finally:
            self._lock.release()

    def contract_full_code(self, symbol: str) -> str:
        """从目录交割年月生成完整输入代码，用于不完整前缀的候选匹配。"""
        record = self._contract_directory.get(symbol) if self._contract_directory else None
        return record.full_code if record else symbol.partition(".")[2].upper()

    def refresh_contract_directory(self) -> None:
        """由行情后台批量更新目录，前台不发起SDK请求。"""
        directory = self._contract_directory
        if directory is None or not directory.needs_refresh() or monotonic() < self._directory_retry_after:
            return
        self._directory_retry_after = monotonic() + 60
        if not self._lock.acquire(timeout=_CONTRACT_QUERY_LOCK_TIMEOUT_SECONDS):
            return
        started = monotonic()
        stage = "连接"
        try:
            api = self._get_api()
            stage = "批量查询与校验"

            async def query_directory():
                result = api.query_quotes(ins_class="FUTURE", expired=False)
                symbols = list(await result if isawaitable(result) else result)
                # 数据源还会返回海外连续合约；本系统只接入国内六个交易所的实月合约。
                symbols = [s for s in symbols if s.partition(".")[0] in SUPPORTED_EXCHANGES]
                if not symbols or len(symbols) > 10_000:
                    raise ValueError("活跃合约列表数量异常")
                result = api.query_symbol_info(symbols)
                frame = await result if isawaitable(result) else result
                records = [DirectoryContract.from_metadata(row) for row in frame.to_dict("records")]
                if {r.symbol.upper() for r in records} != {s.upper() for s in symbols}:
                    raise ValueError("合约目录元数据未完整返回")
                return records

            records = self._wait_contract_query(api, query_directory)
            stage = "本地持久化"
            directory.replace(records)
            self._notices.recover("contract-directory", "本地合约目录同步已恢复")
            logger.info("本地合约目录已更新: 合约=%d 耗时=%.3f秒", len(records), monotonic() - started)
        except Exception as exc:  # noqa: BLE001
            self._notices.warning(
                "contract-directory", "合约目录同步未完成，保留上次快照并稍后重试: 阶段=%s 类型=%s",
                stage, type(exc).__name__,
            )
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            if self._api is not None:
                try:
                    self._api.close()
                except Exception:
                    logger.warning("关闭天勤连接失败", exc_info=True)
                finally:
                    self._api = None
            # K线 DataFrame 绑定创建它的 TqApi。连接关闭或凭据切换后继续
            # 复用这些对象不会再收到行情更新，必须让新连接重新订阅。
            self._live_kline_series.clear()
            with self._active_contract_cache_lock:
                self._active_contract_cache.clear()
