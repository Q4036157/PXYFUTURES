"""合约与周期均线配置的 SQLite 持久化。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from time import time_ns
from typing import Sequence

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


@dataclass(frozen=True)
class ContractConfig:
    id: int
    symbol: str
    name: str
    periods: list[PeriodConfig]


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
                CREATE TABLE IF NOT EXISTS period_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL CHECK(duration_seconds > 0),
                    m4 INTEGER NOT NULL CHECK(m4 > 0),
                    m3 INTEGER NOT NULL CHECK(m3 > 0),
                    m2 INTEGER NOT NULL CHECK(m2 > 0),
                    m1 INTEGER NOT NULL CHECK(m1 > 0),
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
                """
            )

    def list_contracts(self, user_id: str) -> list[ContractConfig]:
        with self._connect() as conn:
            contracts = conn.execute(
                "SELECT id, symbol, name FROM contracts WHERE user_id = ? ORDER BY symbol", (user_id,)
            ).fetchall()
            return [self._contract_from_row(conn, row) for row in contracts]

    def get_contract(self, user_id: str, contract_id: int) -> ContractConfig | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, symbol, name FROM contracts WHERE id = ? AND user_id = ?", (contract_id, user_id)
            ).fetchone()
            return self._contract_from_row(conn, row) if row else None

    def create_contract(self, user_id: str, symbol: str, name: str) -> ContractConfig:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO contracts(user_id, symbol, name) VALUES (?, ?, ?)",
                (user_id, symbol.strip(), name.strip()),
            )
            row = conn.execute("SELECT id, symbol, name FROM contracts WHERE id = ?", (cursor.lastrowid,)).fetchone()
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
        if not bars:
            return
        synced_at_ns = synced_at_ns or time_ns()
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO kline_bars(symbol, duration_seconds, timestamp_ns, close, updated_at_ns)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, duration_seconds, timestamp_ns) DO UPDATE SET
                     close = excluded.close, updated_at_ns = excluded.updated_at_ns""",
                [(symbol, duration_seconds, timestamp_ns, close, synced_at_ns) for timestamp_ns, close in bars],
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
        return ContractConfig(
            id=row["id"], symbol=row["symbol"], name=row["name"],
            periods=[self._period_from_row(period) for period in periods],
        )

    @staticmethod
    def _period_from_row(row: sqlite3.Row) -> PeriodConfig:
        return PeriodConfig(
            id=row["id"], label=row["label"], duration_seconds=row["duration_seconds"],
            m4=row["m4"], m3=row["m3"], m2=row["m2"], m1=row["m1"],
        )
