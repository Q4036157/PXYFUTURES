"""中国期货常用品种代码与交易所映射。"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 以常用品种为主。按最长前缀匹配，避免例如 A 与 AO、P 与 PP/PX 混淆。
PRODUCT_EXCHANGES: dict[str, str] = {
    # 郑商所
    "AP": "CZCE", "CF": "CZCE", "CJ": "CZCE", "FG": "CZCE", "MA": "CZCE",
    "OI": "CZCE", "PF": "CZCE", "PK": "CZCE", "PM": "CZCE", "PR": "CZCE",
    "PX": "CZCE", "RI": "CZCE", "RM": "CZCE", "RS": "CZCE", "SA": "CZCE",
    "SF": "CZCE", "SH": "CZCE", "SM": "CZCE", "SR": "CZCE", "TA": "CZCE",
    "UR": "CZCE", "WH": "CZCE", "ZC": "CZCE", "LR": "CZCE",
    # 大商所
    "A": "DCE", "B": "DCE", "BB": "DCE", "C": "DCE", "CS": "DCE", "EB": "DCE",
    "EG": "DCE", "FB": "DCE", "I": "DCE", "J": "DCE", "JD": "DCE", "JM": "DCE",
    "L": "DCE", "LH": "DCE", "M": "DCE", "P": "DCE", "PG": "DCE", "PP": "DCE",
    "RR": "DCE", "V": "DCE", "Y": "DCE",
    # 上期所
    "AG": "SHFE", "AL": "SHFE", "AO": "SHFE", "AU": "SHFE", "BR": "SHFE", "BU": "SHFE",
    "CU": "SHFE", "HC": "SHFE", "NI": "SHFE", "PB": "SHFE", "RB": "SHFE", "RU": "SHFE",
    "SN": "SHFE", "SP": "SHFE", "SS": "SHFE", "WR": "SHFE", "ZN": "SHFE",
    # 上海国际能源交易中心
    "BC": "INE", "EC": "INE", "LU": "INE", "NR": "INE", "SC": "INE",
    # 中金所
    "IC": "CFFEX", "IF": "CFFEX", "IH": "CFFEX", "IM": "CFFEX", "T": "CFFEX",
    "TF": "CFFEX", "TL": "CFFEX", "TS": "CFFEX",
    # 广期所
    "LC": "GFEX", "PS": "GFEX", "PT": "GFEX", "SI": "GFEX",
}

# 常见中文品种简称。数据源仍使用右侧的标准品种代码。
PRODUCT_ALIASES: dict[str, str] = {
    "PVC": "V",
    "PV": "V",  # 用户常以 PV 简写 PVC，统一转为大商所 V
}

_FULL_CONTRACT = re.compile(r"^([A-Z]+)(\d{3,4})$")


@dataclass(frozen=True)
class ContractCode:
    code: str
    product: str | None
    exchange: str | None
    complete: bool


def to_tq_symbol(symbol: str) -> str:
    """转换为天勤使用的合约格式：交易所大写，品种代码小写。"""
    exchange, separator, code = symbol.partition(".")
    if not separator or not exchange or not code:
        return symbol
    return f"{exchange.upper()}.{code.lower()}"


def normalize_contract_code(value: str) -> ContractCode:
    """规范化大小写和空格，并按最长产品前缀推断交易所。"""
    code = re.sub(r"\s+", "", value).upper()
    match = re.match(r"^([A-Z]+)", code)
    letters = match.group(1) if match else ""
    # 字母部分必须完整等于品种代码；例如 PV 不能误判为 P，避免落到错误交易所。
    product = PRODUCT_ALIASES.get(letters, letters if letters in PRODUCT_EXCHANGES else None)
    suffix = code[len(letters):]
    standard_code = f"{product}{suffix}" if product else code
    return ContractCode(
        code=standard_code,
        product=product,
        exchange=PRODUCT_EXCHANGES.get(product) if product else None,
        complete=bool(_FULL_CONTRACT.fullmatch(code) and product),
    )
