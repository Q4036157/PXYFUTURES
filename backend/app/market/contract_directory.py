"""本地活跃期货目录，前台读取不依赖行情连接和行情锁。"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import time

logger = logging.getLogger(__name__)
REFRESH_SECONDS = 3600
VALIDATION_MAX_AGE_SECONDS = 6 * 3600
SUGGESTION_MAX_AGE_SECONDS = 7 * 86400
_MAX_CONTRACTS = 10_000
_MAX_FILE_BYTES = 5_000_000
SUPPORTED_EXCHANGES = frozenset({"CZCE", "DCE", "SHFE", "INE", "CFFEX", "GFEX"})


@dataclass(frozen=True)
class DirectoryContract:
    symbol: str
    name: str
    exchange: str
    product: str
    delivery_year: int
    delivery_month: int
    expires_at: float

    @classmethod
    def from_metadata(cls, row: dict) -> DirectoryContract:
        symbol = str(row["instrument_id"])
        exchange = str(row["exchange_id"]).upper()
        product = str(row["product_id"]).upper()
        year, month = float(row["delivery_year"]), float(row["delivery_month"])
        expiry = float(row["expire_datetime"])
        code_match = re.fullmatch(r"([A-Za-z]+)(\d{3,4})([A-Za-z]*)", symbol.partition(".")[2])
        if (
            row.get("ins_class") != "FUTURE" or row.get("expired", True)
            or not code_match or exchange not in SUPPORTED_EXCHANGES
            or symbol.partition(".")[0] != exchange
            or code_match[1].upper() != product
            or not math.isfinite(year) or not year.is_integer() or not 2000 <= year <= 2200
            or not math.isfinite(month) or not month.is_integer() or not 1 <= month <= 12
            or not math.isfinite(expiry) or expiry <= 0
        ):
            raise ValueError("合约目录元数据不完整")
        return cls(symbol, str(row["instrument_name"])[:100], exchange, product, int(year), int(month), expiry)

    @property
    def full_code(self) -> str:
        suffix = re.search(r"\d([A-Za-z]*)$", self.symbol)[1].upper()
        return f"{self.product}{self.delivery_year % 100:02d}{self.delivery_month:02d}{suffix}"


class LocalContractDirectory:
    """完整快照原子落盘，更新失败保留上一份可用目录。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self._records: dict[str, DirectoryContract] = {}
        self._products: dict[tuple[str, str], tuple[DirectoryContract, ...]] = {}
        self._updated_at = 0.0
        self._refresh_requested = False
        try:
            if path.exists():
                if path.stat().st_size > _MAX_FILE_BYTES:
                    raise ValueError("合约目录文件过大")
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload["version"] != 1:
                    raise ValueError("合约目录版本不支持")
                rows = payload["contracts"]
                if not isinstance(rows, list) or not 0 < len(rows) <= _MAX_CONTRACTS:
                    raise ValueError("合约目录数量异常")
                records = [DirectoryContract.from_metadata({
                    "instrument_id": row["symbol"], "instrument_name": row["name"],
                    "exchange_id": row["exchange"], "product_id": row["product"],
                    "delivery_year": row["delivery_year"], "delivery_month": row["delivery_month"],
                    "expire_datetime": row["expires_at"], "ins_class": "FUTURE", "expired": False,
                }) for row in rows]
                self._install(records, float(payload["updated_at"]))
        except (OSError, ValueError, TypeError, KeyError):
            logger.warning("本地合约目录不可读，等待后台重新同步")

    @staticmethod
    def _validate(records: list[DirectoryContract], updated_at: float) -> None:
        if not records or len(records) > _MAX_CONTRACTS:
            raise ValueError("合约目录数量异常")
        if not math.isfinite(updated_at) or not 0 < updated_at <= time() + 60:
            raise ValueError("合约目录同步时间异常")
        for record in records:
            DirectoryContract.from_metadata({
                "instrument_id": record.symbol, "instrument_name": record.name,
                "exchange_id": record.exchange, "product_id": record.product,
                "delivery_year": record.delivery_year, "delivery_month": record.delivery_month,
                "expire_datetime": record.expires_at, "ins_class": "FUTURE", "expired": False,
            })
        if len({record.symbol.upper() for record in records}) != len(records):
            raise ValueError("合约目录存在重复合约")

    def _install(self, records: list[DirectoryContract], updated_at: float) -> None:
        self._validate(records, updated_at)
        products: dict[tuple[str, str], list[DirectoryContract]] = {}
        for record in records:
            products.setdefault((record.exchange, record.product), []).append(record)
        with self._lock:
            self._records = {record.symbol.upper(): record for record in records}
            self._products = {key: tuple(sorted(rows, key=lambda r: r.symbol)) for key, rows in products.items()}
            self._updated_at = updated_at
            self._refresh_requested = False

    def replace(self, records: list[DirectoryContract]) -> None:
        updated_at = time()
        self._validate(records, updated_at)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump({"version": 1, "updated_at": updated_at, "contracts": [asdict(r) for r in records]}, stream, ensure_ascii=False, allow_nan=False)
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        self._install(records, updated_at)

    def needs_refresh(self) -> bool:
        with self._lock:
            return self._refresh_requested or time() - self._updated_at >= REFRESH_SECONDS

    def request_refresh(self) -> None:
        with self._lock:
            self._refresh_requested = True

    def validation_ready(self) -> bool:
        with self._lock:
            return bool(self._records) and time() - self._updated_at <= VALIDATION_MAX_AGE_SECONDS

    def candidates(self, exchange: str, product: str) -> list[str] | None:
        with self._lock:
            now = time()
            if not self._records or now - self._updated_at > SUGGESTION_MAX_AGE_SECONDS:
                return None
            return [
                r.symbol for r in self._products.get((exchange.upper(), product.upper()), ())
                if r.expires_at > now and r.symbol[-1].isdigit()
            ]

    def get(self, symbol: str) -> DirectoryContract | None:
        with self._lock:
            return self._records.get(symbol.upper())
