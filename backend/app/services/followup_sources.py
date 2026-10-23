"""二级监控左侧固定条件的最新已收盘状态与确认检查。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.repository import ConfigRepository, ContractConfig, PeriodConfig

SourceScanner = Callable[
    [ConfigRepository, list[tuple[ContractConfig, PeriodConfig]]],
    tuple[list[dict[str, Any]], int],
]


def source_identity(source: dict) -> tuple:
    """根数、时间推进不构成状态改变。"""
    return (source.get("duration_seconds"), source.get("condition"), source.get("state"))


class FollowupSourceMonitor:
    def __init__(self, repository: ConfigRepository, scanner: SourceScanner) -> None:
        self.repository = repository
        self.scanner = scanner
        self.current: dict[int, dict] = {}

    def refresh(self, rules: list[dict], contract_status: Callable[[str], str] | None) -> None:
        jobs: dict[tuple[int, int], tuple[ContractConfig, PeriodConfig]] = {}
        rule_keys: dict[int, tuple[int, int]] = {}
        contracts: dict[tuple[str, int], ContractConfig | None] = {}
        for rule in rules:
            self.current.pop(rule["id"], None)
            owner = (rule["user_id"], rule["contract_id"])
            if owner not in contracts:
                contracts[owner] = self.repository.get_contract(*owner)
            contract = contracts[owner]
            if (contract is None or contract.symbol != rule["symbol"]
                    or (contract_status and contract_status(contract.symbol) == "expired")):
                continue
            duration = rule["source"].get("duration_seconds")
            period = next((p for p in contract.periods if p.duration_seconds == duration), None)
            if period is None:
                continue
            key = (contract.id, period.duration_seconds)
            jobs[key] = (contract, period)
            rule_keys[rule["id"]] = key
        if not jobs:
            return
        snapshots, _failed = self.scanner(self.repository, list(jobs.values()))
        latest: dict[tuple[int, int], dict] = {}
        for signal in snapshots:
            key = (signal["contract_id"], signal["period"]["duration_seconds"])
            if (key not in jobs or signal.get("symbol") != jobs[key][0].symbol
                    or signal.get("data_status", "current") != "current"
                    or not signal.get("cross_type") or not signal.get("confirmed_bar_time")):
                continue
            if key in latest and signal["confirmed_bar_time"] <= latest[key]["confirmed_bar_time"]:
                continue
            latest[key] = signal
        for rule_id, key in rule_keys.items():
            signal = latest.get(key)
            if signal is not None:
                self.current[rule_id] = {
                    "duration_seconds": key[1], "period_label": jobs[key][1].label,
                    "condition": f"{signal['trend']['m4']}{signal['trend']['m3']}{signal['cross_type']}",
                    "state": signal["label"], "confirmed_bar_time": signal["confirmed_bar_time"],
                }

    def retain(self, ids: set[int]) -> None:
        self.current = {key: value for key, value in self.current.items() if key in ids}
