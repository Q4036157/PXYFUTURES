from app.market.contract_catalog import normalize_contract_code, to_tq_symbol


def test_lowercase_contract_is_normalized_and_recognized() -> None:
    contract = normalize_contract_code(" fg609 ")
    assert contract.code == "FG609"
    assert contract.exchange == "CZCE"
    assert contract.complete is True


def test_longest_prefix_avoids_single_letter_collision() -> None:
    contract = normalize_contract_code("rb2609")
    assert contract.exchange == "SHFE"
    assert contract.complete is True


def test_current_futures_products_are_recognized() -> None:
    cases = {
        "cy609": ("CY609", "CZCE"),
        "jr609": ("JR609", "CZCE"),
        "pl609": ("PL609", "CZCE"),
        "bz2609": ("BZ2609", "DCE"),
        "lg2609": ("LG2609", "DCE"),
        "ad2609": ("AD2609", "SHFE"),
        "fu2609": ("FU2609", "SHFE"),
        "op2609": ("OP2609", "SHFE"),
        "pd2610": ("PD2610", "GFEX"),
    }

    for value, (code, exchange) in cases.items():
        contract = normalize_contract_code(value)
        assert contract.code == code
        assert contract.exchange == exchange
        assert contract.complete is True


def test_product_prefix_without_delivery_month_is_not_addable() -> None:
    contract = normalize_contract_code("v")
    assert contract.exchange == "DCE"
    assert contract.complete is False


def test_unknown_product_is_not_guessed() -> None:
    contract = normalize_contract_code("unknown2609")
    assert contract.exchange is None
    assert contract.complete is False


def test_pvc_alias_uses_dce_standard_code() -> None:
    contract = normalize_contract_code("pvc2609")
    assert contract.code == "V2609"
    assert contract.exchange == "DCE"
    assert contract.complete is True


def test_tq_symbol_uses_exchange_specific_product_case() -> None:
    assert to_tq_symbol("DCE.V2609") == "DCE.v2609"
    assert to_tq_symbol("CZCE.FG609") == "CZCE.FG609"
    assert to_tq_symbol("CFFEX.IF2609") == "CFFEX.IF2609"
