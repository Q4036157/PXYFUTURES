"""按账号保存的二级条件单与已收盘K线监控。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from threading import RLock
from typing import Any

from app.services.market_sync import MarketSyncEvent
from app.services.repository import ConfigRepository, ContractConfig, PeriodConfig
from app.services.runtime_notices import RuntimeNoticeLimiter

logger = logging.getLogger(__name__)
Rule = dict[str, Any]
FollowupScanner = Callable[
    [ConfigRepository, list[tuple[ContractConfig, PeriodConfig]], dict[tuple[int, int], str | None]],
    tuple[list[dict[str, Any]], int],
]
CONDITIONS = frozenset(
    f"{m4}{m3}{cross}" for m4 in ("多", "空") for m3 in ("多", "空")
    for cross in ("金叉", "死叉")
)
STATES = frozenset({"上涨", "回调", "多震荡", "下跌", "反弹", "空震荡"})


class FollowupAlertService:
    """独立于主列表筛选和网页连接，按收盘顺序推进每条条件单。"""

    def __init__(
        self, repository: ConfigRepository, scanner: FollowupScanner,
        contract_status: Callable[[str], str] | None = None,
    ) -> None:
        self.repository = repository
        self._scanner = scanner
        self._contract_status = contract_status
        self._lock = RLock()
        self._wake = asyncio.Event()
        self._history_checked: set[int] = set()

    async def publish_rollovers(self, _event: MarketSyncEvent) -> None:
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        notices = RuntimeNoticeLimiter(logger)
        while not stop.is_set():
            self._wake.clear()
            try:
                await asyncio.to_thread(self.evaluate)
                notices.recover("followup-scan", "二级条件单扫描已恢复")
            except Exception:  # noqa: BLE001
                notices.warning("followup-scan", "二级条件单扫描失败，将自动重试", exc_info=True)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    @staticmethod
    def matches(rule: Rule, signal: Rule) -> bool:
        condition = (
            f"{signal['trend']['m4']}{signal['trend']['m3']}{signal['cross_type']}"
            if signal.get("cross_type") else None
        )
        return (
            signal.get("cross_type") is not None
            and (not rule["conditions"] or condition in rule["conditions"])
            and (not rule["states"] or signal["label"] in rule["states"])
        )

    def create_rule(
        self, user_id: str, contract_id: int, source: Rule,
        duration_seconds: int, conditions: list[str], states: list[str],
    ) -> int:
        with self._lock:
            contract = self.repository.get_contract(user_id, contract_id)
            if contract is None:
                raise LookupError("合约不存在")
            source_period = next(
                (p for p in contract.periods if p.duration_seconds == source["duration_seconds"]), None,
            )
            if source_period is None or not any(
                p.duration_seconds == duration_seconds for p in contract.periods
            ):
                raise ValueError("请先为该合约配置所选周期的均线参数")
            if len(self.repository.list_followup_alerts(user_id)) >= 200:
                raise ValueError("当前账号最多保存200条条件单，请先删除不需要的条件单")
            source = {**source, "period_label": source_period.label}
            rule_id = self.repository.create_followup_alert(
                user_id, contract.id, contract.symbol, source, duration_seconds, conditions, states,
            )
            # 保存后立即检查已确认状态，已满足的旧信号也允许首次提醒。
            try:
                self.evaluate()
            except Exception:
                logger.exception("二级条件单已保存，首次行情检查失败，后台将自动重试: rule=%s", rule_id)
            return rule_id

    def evaluate(self) -> None:
        with self._lock:
            rules = self.repository.list_followup_alerts()
            self._history_checked.intersection_update(rule["id"] for rule in rules)
            if not rules:
                return
            contracts: dict[tuple[str, int], ContractConfig | None] = {}
            jobs: dict[tuple[int, int], tuple[ContractConfig, PeriodConfig]] = {}
            cursors: dict[tuple[int, int], str | None] = {}
            active_rules: dict[tuple[int, int], list[Rule]] = {}
            for rule in rules:
                owner = (rule["user_id"], rule["contract_id"])
                if owner not in contracts:
                    contracts[owner] = self.repository.get_contract(*owner)
                contract = contracts[owner]
                if contract is None or contract.symbol != rule["symbol"]:
                    continue
                if self._contract_status and self._contract_status(contract.symbol) == "expired":
                    continue
                period = next(
                    (item for item in contract.periods
                     if item.duration_seconds == rule["duration_seconds"]), None,
                )
                if period is None:
                    continue
                key = (contract.id, period.duration_seconds)
                jobs[key] = (contract, period)
                cursor = rule["last_bar_time"]
                # 新规则与重启后仍满足的规则先回溯区间；其他规则重放未处理K线。
                if rule["id"] not in self._history_checked and rule["matched"]:
                    cursor = None
                if key not in cursors or cursor is None or (
                    cursors[key] is not None and cursor < cursors[key]
                ):
                    cursors[key] = cursor
                active_rules.setdefault(key, []).append(rule)
            if not jobs:
                return
            snapshots, _failed = self._scanner(self.repository, list(jobs.values()), cursors)
            latest: dict[tuple[int, int], str] = {}
            for signal in snapshots:
                key = (signal["contract_id"], signal["period"]["duration_seconds"])
                latest[key] = max(latest.get(key, ""), signal["confirmed_bar_time"])
            ordered = sorted(snapshots, key=lambda item: item["confirmed_bar_time"])
            histories: dict[tuple[int, int], list[Rule]] = {}
            for signal in ordered:
                key = (signal["contract_id"], signal["period"]["duration_seconds"])
                histories.setdefault(key, []).append(signal)
            initial_origins: dict[int, str | None] = {}
            for key, items in active_rules.items():
                history = histories.get(key, [])
                for rule in items:
                    if not history or rule["id"] in self._history_checked:
                        continue
                    if rule["last_bar_time"] is None:
                        initial_origins[rule["id"]] = self._matching_origin(rule, history)
                    elif rule["matched"]:
                        prior = [s for s in history if s["confirmed_bar_time"] <= rule["last_bar_time"]]
                        origin = self._matching_origin(rule, prior)
                        if (prior and prior[-1]["confirmed_bar_time"] == rule["last_bar_time"]
                                and origin and origin != rule["last_triggered_at"]
                                and self.repository.correct_followup_origin(
                                    rule["id"], rule["symbol"], rule["last_bar_time"],
                                    rule["last_triggered_at"], origin,
                                )):
                            logger.info(
                                "二级条件起点已校正，不重复通知: rule=%s symbol=%s previous=%s closed_at=%s",
                                rule["id"], rule["symbol"], rule["last_triggered_at"], origin,
                            )
                    self._history_checked.add(rule["id"])
            for signal in ordered:
                if signal.get("cross_type") is None:
                    continue
                key = (signal["contract_id"], signal["period"]["duration_seconds"])
                for rule in active_rules.get(key, []):
                    if rule["last_bar_time"] and signal["confirmed_bar_time"] <= rule["last_bar_time"]:
                        continue
                    if rule["last_bar_time"] is None and signal["confirmed_bar_time"] != latest[key]:
                        continue
                    closed_at = (
                        initial_origins.get(rule["id"])
                        if rule["last_bar_time"] is None else signal["closed_at"]
                    )
                    if self.repository.advance_followup_alert(
                        rule["id"], rule["symbol"], signal["confirmed_bar_time"],
                        self.matches(rule, signal), closed_at,
                    ):
                        logger.info(
                            "二级条件已触发: rule=%s symbol=%s period=%ss closed_at=%s",
                            rule["id"], rule["symbol"], rule["duration_seconds"], closed_at,
                        )

    @classmethod
    def _matching_origin(cls, rule: Rule, history: list[Rule]) -> str | None:
        """只接受有前一根不满足状态作证的区间起点，历史不足时不猜时间。"""
        if not history or not cls.matches(rule, history[-1]):
            return None
        for index in range(len(history) - 2, -1, -1):
            if history[index].get("cross_type") is None:
                return None
            if not cls.matches(rule, history[index]):
                return history[index + 1]["closed_at"]
        return None

    def list_rules(self, user_id: str) -> list[Rule]:
        contracts = {item.id: item for item in self.repository.list_contracts(user_id)}
        results: list[Rule] = []
        for rule in self.repository.list_followup_alerts(user_id):
            contract = contracts.get(rule["contract_id"])
            if contract is None or contract.symbol != rule["symbol"]:
                status = "合约已更换，等待删除"
            elif self._contract_status and self._contract_status(contract.symbol) == "expired":
                status = "合约已到期，监控暂停"
            elif not any(p.duration_seconds == rule["duration_seconds"] for p in contract.periods):
                status = "目标周期已移除，等待恢复配置"
            elif rule["last_bar_time"] is None:
                status = "等待收盘行情"
            else:
                status = "条件已满足" if rule["matched"] else "监控中"
            results.append({
                key: value for key, value in {**rule, "status": status}.items()
                if key not in {"user_id", "notified_count"}
            })
        return results
