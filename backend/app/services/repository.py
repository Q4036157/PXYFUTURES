"""合约与周期均线配置的 SQLite 持久化。"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from time import time_ns

from app.config import settings


@dataclass(frozen=True)
class PeriodConfig:
    id: int | None
    label: str
    duration_seconds: int
    m4: int
    m3: int
    m2: int
    m1: int
    note: str = ""


DEFAULT_PERIOD_CONFIGS: tuple[PeriodConfig, ...] = (
    PeriodConfig(id=None, label="日线", duration_seconds=86_400, m4=120, m3=60, m2=21, m1=4),
    PeriodConfig(id=None, label="2小时", duration_seconds=7_200, m4=180, m3=60, m2=21, m1=4),
    PeriodConfig(id=None, label="1小时", duration_seconds=3_600, m4=240, m3=80, m2=21, m1=4),
    PeriodConfig(id=None, label="30分钟", duration_seconds=1_800, m4=240, m3=80, m2=21, m1=4),
    PeriodConfig(id=None, label="10分钟", duration_seconds=600, m4=240, m3=60, m2=21, m1=4),
    PeriodConfig(id=None, label="5分钟", duration_seconds=300, m4=240, m3=60, m2=21, m1=4),
    PeriodConfig(id=None, label="3分钟", duration_seconds=180, m4=200, m3=100, m2=21, m1=4),
)


@dataclass(frozen=True)
class ContractConfig:
    id: int
    symbol: str
    name: str
    periods: list[PeriodConfig]
    group_id: int | None = None
    sort_order: int = 0


@dataclass(frozen=True)
class ContractGroup:
    id: int
    name: str
    sort_order: int
    contracts: list[ContractConfig]


@dataclass(frozen=True)
class KlineSyncState:
    max_requested_bars: int
    last_synced_at_ns: int


class ConfigRepository:
    def __init__(self) -> None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = settings.data_dir / "futures.db"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    UNIQUE(user_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS contract_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, name)
                );
                CREATE TABLE IF NOT EXISTS period_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL CHECK(duration_seconds > 0),
                    m4 INTEGER NOT NULL CHECK(m4 > 0),
                    m3 INTEGER NOT NULL CHECK(m3 > 0),
                    m2 INTEGER NOT NULL CHECK(m2 > 0),
                    m1 INTEGER NOT NULL CHECK(m1 > 0),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(contract_id, duration_seconds)
                );
                CREATE TABLE IF NOT EXISTS kline_bars (
                    symbol TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL CHECK(duration_seconds > 0),
                    timestamp_ns INTEGER NOT NULL,
                    close REAL NOT NULL CHECK(close > 0),
                    updated_at_ns INTEGER NOT NULL,
                    PRIMARY KEY(symbol, duration_seconds, timestamp_ns)
                );
                CREATE INDEX IF NOT EXISTS idx_kline_bars_lookup
                    ON kline_bars(symbol, duration_seconds, timestamp_ns DESC);
                CREATE TABLE IF NOT EXISTS kline_sync_states (
                    symbol TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL CHECK(duration_seconds > 0),
                    max_requested_bars INTEGER NOT NULL CHECK(max_requested_bars > 0),
                    last_synced_at_ns INTEGER NOT NULL,
                    PRIMARY KEY(symbol, duration_seconds)
                );
                CREATE TABLE IF NOT EXISTS followup_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    conditions_json TEXT NOT NULL,
                    states_json TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    last_bar_time TEXT,
                    matched INTEGER NOT NULL DEFAULT 0,
                    last_triggered_at TEXT,
                    trigger_count INTEGER NOT NULL DEFAULT 0,
                    notified_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_followup_alerts_user
                    ON followup_alerts(user_id, id);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(period_configs)")}
            if "note" not in columns:
                conn.execute("ALTER TABLE period_configs ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            contract_columns = {row["name"] for row in conn.execute("PRAGMA table_info(contracts)")}
            if "group_id" not in contract_columns:
                conn.execute("ALTER TABLE contracts ADD COLUMN group_id INTEGER REFERENCES contract_groups(id)")
            if "sort_order" not in contract_columns:
                conn.execute("ALTER TABLE contracts ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")

    def list_contracts(self, user_id: str) -> list[ContractConfig]:
        with self._connect() as conn:
            self._ensure_user_groups(conn, user_id)
            contracts = conn.execute(
                """SELECT id, symbol, name, group_id, sort_order
                   FROM contracts WHERE user_id = ?
                   ORDER BY group_id, sort_order, id""",
                (user_id,),
            ).fetchall()
            return [self._contract_from_row(conn, row) for row in contracts]

    def list_contract_groups(self, user_id: str) -> list[ContractGroup]:
        with self._connect() as conn:
            groups = self._ensure_user_groups(conn, user_id)
            return [
                ContractGroup(
                    id=int(group["id"]),
                    name=str(group["name"]),
                    sort_order=int(group["sort_order"]),
                    contracts=[
                        self._contract_from_row(conn, row)
                        for row in conn.execute(
                            """SELECT id, symbol, name, group_id, sort_order
                               FROM contracts WHERE user_id = ? AND group_id = ?
                               ORDER BY sort_order, id""",
                            (user_id, group["id"]),
                        ).fetchall()
                    ],
                )
                for group in groups
            ]

    def create_group(self, user_id: str, name: str) -> ContractGroup:
        with self._connect() as conn:
            self._ensure_user_groups(conn, user_id)
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM contract_groups WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO contract_groups(user_id, name, sort_order) VALUES (?, ?, ?)",
                (user_id, name.strip(), next_order),
            )
            return ContractGroup(int(cursor.lastrowid), name.strip(), int(next_order), [])

    def rename_group(self, user_id: str, group_id: int, name: str) -> ContractGroup | None:
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE contract_groups SET name = ? WHERE id = ? AND user_id = ?",
                (name.strip(), group_id, user_id),
            )
            if updated.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT id, name, sort_order FROM contract_groups WHERE id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            contracts = [
                self._contract_from_row(conn, contract)
                for contract in conn.execute(
                    """SELECT id, symbol, name, group_id, sort_order
                       FROM contracts WHERE user_id = ? AND group_id = ?
                       ORDER BY sort_order, id""",
                    (user_id, group_id),
                ).fetchall()
            ]
            return ContractGroup(int(row["id"]), str(row["name"]), int(row["sort_order"]), contracts)

    def move_contract(self, user_id: str, contract_id: int, group_id: int) -> ContractConfig | None:
        with self._connect() as conn:
            self._ensure_user_groups(conn, user_id)
            if not conn.execute(
                "SELECT 1 FROM contract_groups WHERE id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone():
                return None
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM contracts WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            ).fetchone()[0]
            updated = conn.execute(
                "UPDATE contracts SET group_id = ?, sort_order = ? WHERE id = ? AND user_id = ?",
                (group_id, next_order, contract_id, user_id),
            )
            if updated.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT id, symbol, name, group_id, sort_order FROM contracts WHERE id = ? AND user_id = ?",
                (contract_id, user_id),
            ).fetchone()
            return self._contract_from_row(conn, row)

    def reorder_contracts(self, user_id: str, group_id: int, contract_ids: Sequence[int]) -> bool:
        with self._connect() as conn:
            self._ensure_user_groups(conn, user_id)
            existing = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM contracts WHERE user_id = ? AND group_id = ? ORDER BY sort_order, id",
                    (user_id, group_id),
                ).fetchall()
            ]
            requested = [int(contract_id) for contract_id in contract_ids]
            if len(requested) != len(set(requested)) or set(requested) != set(existing):
                return False
            conn.executemany(
                "UPDATE contracts SET sort_order = ? WHERE id = ? AND user_id = ? AND group_id = ?",
                [(index, contract_id, user_id, group_id) for index, contract_id in enumerate(requested)],
            )
            return True

    def list_all_contracts(self) -> list[ContractConfig]:
        """供后台行情同步读取全部用户已添加的合约。"""
        with self._connect() as conn:
            contracts = conn.execute(
                "SELECT id, symbol, name, group_id, sort_order FROM contracts ORDER BY symbol, id"
            ).fetchall()
            return [self._contract_from_row(conn, row) for row in contracts]

    def get_contract(self, user_id: str, contract_id: int) -> ContractConfig | None:
        with self._connect() as conn:
            self._ensure_user_groups(conn, user_id)
            row = conn.execute(
                """SELECT id, symbol, name, group_id, sort_order
                   FROM contracts WHERE id = ? AND user_id = ?""",
                (contract_id, user_id),
            ).fetchone()
            return self._contract_from_row(conn, row) if row else None

    def create_contract(
        self, user_id: str, symbol: str, name: str, group_id: int | None = None
    ) -> ContractConfig:
        with self._connect() as conn:
            groups = self._ensure_user_groups(conn, user_id)
            target_group_id = group_id or int(groups[0]["id"])
            if not conn.execute(
                "SELECT 1 FROM contract_groups WHERE id = ? AND user_id = ?",
                (target_group_id, user_id),
            ).fetchone():
                raise ValueError("分组不存在")
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM contracts WHERE user_id = ? AND group_id = ?",
                (user_id, target_group_id),
            ).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO contracts(user_id, symbol, name, group_id, sort_order) VALUES (?, ?, ?, ?, ?)",
                (user_id, symbol.strip(), name.strip(), target_group_id, next_order),
            )
            conn.executemany(
                """INSERT INTO period_configs(contract_id,label,duration_seconds,m4,m3,m2,m1,note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        cursor.lastrowid,
                        period.label,
                        period.duration_seconds,
                        period.m4,
                        period.m3,
                        period.m2,
                        period.m1,
                        period.note,
                    )
                    for period in DEFAULT_PERIOD_CONFIGS
                ],
            )
            row = conn.execute(
                "SELECT id, symbol, name, group_id, sort_order FROM contracts WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self._contract_from_row(conn, row)

    def update_contract(
        self,
        user_id: str,
        contract_id: int,
        symbol: str,
        name: str,
    ) -> ContractConfig | None:
        """原位更换合约代码，保留 contract_id 下的全部周期配置。"""
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE contracts SET symbol = ?, name = ? WHERE id = ? AND user_id = ?",
                (symbol.strip(), name.strip(), contract_id, user_id),
            )
            if updated.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT id, symbol, name, group_id, sort_order FROM contracts WHERE id = ? AND user_id = ?",
                (contract_id, user_id),
            ).fetchone()
            return self._contract_from_row(conn, row)

    def delete_contract(self, user_id: str, contract_id: int) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM contracts WHERE id = ? AND user_id = ?", (contract_id, user_id)).rowcount > 0

    def save_period(self, user_id: str, contract_id: int, config: PeriodConfig) -> PeriodConfig | None:
        with self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM contracts WHERE id = ? AND user_id = ?", (contract_id, user_id)
            ).fetchone()
            if not owned:
                return None
            conn.execute(
                """INSERT INTO period_configs(contract_id,label,duration_seconds,m4,m3,m2,m1)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(contract_id,duration_seconds) DO UPDATE SET
                     label=excluded.label,m4=excluded.m4,m3=excluded.m3,m2=excluded.m2,m1=excluded.m1""",
                (contract_id, config.label, config.duration_seconds, config.m4, config.m3, config.m2, config.m1),
            )
            row = conn.execute(
                "SELECT * FROM period_configs WHERE contract_id = ? AND duration_seconds = ?",
                (contract_id, config.duration_seconds),
            ).fetchone()
            return self._period_from_row(row)

    def delete_period(self, user_id: str, contract_id: int, duration_seconds: int) -> bool:
        with self._connect() as conn:
            return conn.execute(
                """DELETE FROM period_configs WHERE contract_id = ? AND duration_seconds = ?
                   AND EXISTS (SELECT 1 FROM contracts WHERE id = ? AND user_id = ?)""",
                (contract_id, duration_seconds, contract_id, user_id),
            ).rowcount > 0

    def save_period_note(
        self,
        user_id: str,
        contract_id: int,
        duration_seconds: int,
        note: str,
    ) -> bool:
        """保存单个合约周期的纯文本备注。"""
        with self._connect() as conn:
            return conn.execute(
                """UPDATE period_configs SET note = ?
                   WHERE contract_id = ? AND duration_seconds = ?
                   AND EXISTS (SELECT 1 FROM contracts WHERE id = ? AND user_id = ?)""",
                (note, contract_id, duration_seconds, contract_id, user_id),
            ).rowcount > 0

    def create_followup_alert(
        self, user_id: str, contract_id: int, symbol: str, source: dict[str, object],
        duration_seconds: int, conditions: list[str], states: list[str],
    ) -> int:
        """条件单独立保存，同一品种允许创建多条不同规则。"""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO followup_alerts(
                    user_id,contract_id,symbol,source_json,duration_seconds,
                    conditions_json,states_json,created_at_ns
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (user_id, contract_id, symbol, json.dumps(source, ensure_ascii=False),
                 duration_seconds, json.dumps(conditions, ensure_ascii=False),
                 json.dumps(states, ensure_ascii=False), time_ns()),
            )
            return int(cursor.lastrowid)

    def list_followup_alerts(self, user_id: str | None = None) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM followup_alerts"
                + (" WHERE user_id = ?" if user_id is not None else "") + " ORDER BY id",
                (user_id,) if user_id is not None else (),
            ).fetchall()
            return [self._followup_from_row(row) for row in rows]

    @staticmethod
    def _followup_from_row(row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        result["source"] = json.loads(result.pop("source_json"))
        result["conditions"] = json.loads(result.pop("conditions_json"))
        result["states"] = json.loads(result.pop("states_json"))
        result["matched"] = bool(result["matched"])
        return result

    def advance_followup_alert(
        self, rule_id: int, symbol: str, bar_time: str, matched: bool, closed_at: str | None,
    ) -> bool:
        """原子推进收盘游标；同一根或迟到旧K线不能再次触发。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM followup_alerts WHERE id = ? AND symbol = ?", (rule_id, symbol),
            ).fetchone()
            if row is None or (row["last_bar_time"] and bar_time <= row["last_bar_time"]):
                return False
            triggered = matched and not row["matched"]
            conn.execute(
                """UPDATE followup_alerts SET last_bar_time = ?, matched = ?,
                   last_triggered_at = CASE WHEN ? THEN ? ELSE last_triggered_at END,
                   trigger_count = trigger_count + ? WHERE id = ?""",
                (bar_time, int(matched), int(triggered), closed_at, int(triggered), rule_id),
            )
            return triggered

    def correct_followup_origin(
        self, rule_id: int, symbol: str, bar_time: str, previous: str | None,
        closed_at: str | None,
    ) -> bool:
        """历史回放校正当前连续区间的起点，不增加触发次数或重新通知。"""
        with self._connect() as conn:
            return conn.execute(
                """UPDATE followup_alerts SET last_triggered_at = ?
                   WHERE id = ? AND symbol = ? AND last_bar_time = ? AND matched = 1
                   AND last_triggered_at IS ?""",
                (closed_at, rule_id, symbol, bar_time, previous),
            ).rowcount > 0

    def claim_followup_notifications(self, user_id: str) -> list[dict[str, object]]:
        """同账号多个页面只领取一次；离线期间保留各条件单的最近触发。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT * FROM followup_alerts WHERE user_id = ?
                   AND trigger_count > notified_count ORDER BY id""", (user_id,),
            ).fetchall()
            conn.execute(
                """UPDATE followup_alerts SET notified_count = trigger_count
                   WHERE user_id = ? AND trigger_count > notified_count""", (user_id,),
            )
            return [self._followup_from_row(row) for row in rows]

    def delete_followup_alert(self, user_id: str, rule_id: int) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "DELETE FROM followup_alerts WHERE id = ? AND user_id = ?", (rule_id, user_id),
            ).rowcount > 0

    def load_kline_bars(self, symbol: str, duration_seconds: int, count: int) -> list[tuple[int, float]]:
        """读取最近 count 根本地 K 线，并保持时间升序。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT timestamp_ns, close FROM (
                       SELECT timestamp_ns, close
                       FROM kline_bars
                       WHERE symbol = ? AND duration_seconds = ?
                       ORDER BY timestamp_ns DESC
                       LIMIT ?
                   ) ORDER BY timestamp_ns ASC""",
                (symbol, duration_seconds, count),
            ).fetchall()
            return [(int(row["timestamp_ns"]), float(row["close"])) for row in rows]

    def get_kline_sync_state(self, symbol: str, duration_seconds: int) -> KlineSyncState | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT max_requested_bars, last_synced_at_ns
                   FROM kline_sync_states WHERE symbol = ? AND duration_seconds = ?""",
                (symbol, duration_seconds),
            ).fetchone()
            if row is None:
                return None
            return KlineSyncState(
                max_requested_bars=int(row["max_requested_bars"]),
                last_synced_at_ns=int(row["last_synced_at_ns"]),
            )

    def save_kline_bars(
        self,
        symbol: str,
        duration_seconds: int,
        bars: Sequence[tuple[int, float]],
        requested_count: int,
        synced_at_ns: int | None = None,
    ) -> None:
        """写入天勤返回的 K 线；同一根进行中 K 线以最新收盘价覆盖。"""
        self.save_kline_bar_batches(
            [(symbol, duration_seconds, bars, requested_count)],
            synced_at_ns,
        )

    def save_kline_bar_batches(
        self,
        batches: Sequence[tuple[str, int, Sequence[tuple[int, float]], int]],
        synced_at_ns: int | None = None,
    ) -> None:
        """用一个事务写入多条基础序列，缩短批量收K后的落库延迟。"""
        nonempty_batches = [batch for batch in batches if batch[2]]
        if not nonempty_batches:
            return
        synced_at_ns = synced_at_ns or time_ns()
        with self._connect() as conn:
            for symbol, duration_seconds, bars, requested_count in nonempty_batches:
                conn.executemany(
                    """INSERT INTO kline_bars(symbol, duration_seconds, timestamp_ns, close, updated_at_ns)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(symbol, duration_seconds, timestamp_ns) DO UPDATE SET
                         close = excluded.close, updated_at_ns = excluded.updated_at_ns""",
                    [
                        (symbol, duration_seconds, timestamp_ns, close, synced_at_ns)
                        for timestamp_ns, close in bars
                    ],
                )
                conn.execute(
                    """INSERT INTO kline_sync_states(symbol, duration_seconds, max_requested_bars, last_synced_at_ns)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(symbol, duration_seconds) DO UPDATE SET
                         max_requested_bars = MAX(max_requested_bars, excluded.max_requested_bars),
                         last_synced_at_ns = excluded.last_synced_at_ns""",
                    (symbol, duration_seconds, requested_count, synced_at_ns),
                )

    def _contract_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> ContractConfig:
        periods = conn.execute(
            "SELECT * FROM period_configs WHERE contract_id = ? ORDER BY duration_seconds DESC", (row["id"],)
        ).fetchall()
        columns = row.keys()
        return ContractConfig(
            id=row["id"], symbol=row["symbol"], name=row["name"],
            periods=[self._period_from_row(period) for period in periods],
            group_id=row["group_id"] if "group_id" in columns else None,
            sort_order=int(row["sort_order"]) if "sort_order" in columns else 0,
        )

    @staticmethod
    def _ensure_user_groups(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
        groups = conn.execute(
            "SELECT id, name, sort_order FROM contract_groups WHERE user_id = ? ORDER BY sort_order, id",
            (user_id,),
        ).fetchall()
        if not groups:
            conn.executemany(
                "INSERT INTO contract_groups(user_id, name, sort_order) VALUES (?, ?, ?)",
                [(user_id, f"自定义{index}", index - 1) for index in range(1, 4)],
            )
            groups = conn.execute(
                "SELECT id, name, sort_order FROM contract_groups WHERE user_id = ? ORDER BY sort_order, id",
                (user_id,),
            ).fetchall()
        first_group_id = int(groups[0]["id"])
        unassigned = conn.execute(
            "SELECT id FROM contracts WHERE user_id = ? AND group_id IS NULL ORDER BY id",
            (user_id,),
        ).fetchall()
        if unassigned:
            current_max = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM contracts WHERE user_id = ? AND group_id = ?",
                (user_id, first_group_id),
            ).fetchone()[0]
            conn.executemany(
                "UPDATE contracts SET group_id = ?, sort_order = ? WHERE id = ? AND user_id = ?",
                [
                    (first_group_id, int(current_max) + index + 1, int(row["id"]), user_id)
                    for index, row in enumerate(unassigned)
                ],
            )
        return groups

    @staticmethod
    def _period_from_row(row: sqlite3.Row) -> PeriodConfig:
        return PeriodConfig(
            id=row["id"], label=row["label"], duration_seconds=row["duration_seconds"],
            m4=row["m4"], m3=row["m3"], m2=row["m2"], m1=row["m1"], note=row["note"],
        )
