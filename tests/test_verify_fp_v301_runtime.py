"""Offline contract tests for the FP DEMO V3.01 file inspector.

These JSONL fixtures are synthetic, not broker-native verification proof.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_fp_v301_runtime.py"
SPEC = importlib.util.spec_from_file_location("verify_fp_v301_runtime", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

SCHEMA = "abutron.v9.mt5.market_microstructure.readonly.v3"
DAY = "20261121"
SYMBOL = "EURUSD.r"


def _record(kind: str, *, symbol: str = SYMBOL, digits: int = 5,
            extra: str = "", login: int = 123456) -> str:
    p = f"{1.2345:.{digits}f}"
    base = (
        '{"schema":"' + SCHEMA + '",'
        '"symbol":"' + symbol + '",'
        '"broker_server":"FPMarketsSC-Demo",'
        '"account_login":' + str(login) + ','
        '"environment":"DEMO","record_type":"' + kind + '",'
        '"time_msc":1795260000000'
    )
    if kind == "QUOTE":
        base += f',"price":{p},"bid":{p},"ask":{p},'
        base += '"volume_real":0.0,"aggressor_side":"NONE"'
    elif kind == "STATUS":
        base += ',"dom_subscribed":false'
    elif kind == "DOM":
        base += (
            ',"levels":[{"book_type":1,"price":'
            + p + ',"volume_real":1.0,"volume":1}]'
        )
    if extra:
        base += "," + extra
    return base + "}"


def _write(tmp_path: Path, *rows: str) -> None:
    name = f"ABUTRON_NATIVE_MARKET_V3_{SYMBOL}_{DAY}.jsonl"
    (tmp_path / name).write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_quote_only_feed_with_no_dom_or_market_print_is_valid(tmp_path):
    _write(tmp_path, _record("QUOTE"), _record("STATUS"))
    result = verifier.verify_symbol(tmp_path, SYMBOL, DAY)
    assert result["ok"], result["errors"]
    assert result["observed_decimal_places"] == [5]
    assert result["broker_symbol_digits_verified"] is False
    assert result["by_type"] == {"QUOTE": 1, "STATUS": 1}
    assert any("market_print" in w.lower() for w in result["warnings"])
    assert any("dom" in w.lower() for w in result["warnings"])


def test_mixed_lexical_numeric_precision_is_detected(tmp_path):
    _write(tmp_path, _record("QUOTE", digits=5), _record("QUOTE", digits=4),
           _record("STATUS"))
    result = verifier.verify_symbol(tmp_path, SYMBOL, DAY)
    assert not result["ok"]
    assert any("inconsistent price precision" in e for e in result["errors"])


def test_missing_quote_is_not_13_symbol_success(tmp_path):
    _write(tmp_path, _record("STATUS"))
    result = verifier.verify_symbol(tmp_path, SYMBOL, DAY)
    assert not result["ok"]
    assert any("missing record type: QUOTE" in e for e in result["errors"])


def test_wrong_symbol_and_changed_identity_are_errors(tmp_path):
    _write(tmp_path, _record("QUOTE", symbol="GBPUSD.r"),
           _record("STATUS", login=234567))
    result = verifier.verify_symbol(tmp_path, SYMBOL, DAY)
    assert not result["ok"]
    assert any("symbol mismatch" in e for e in result["errors"])
    assert any("identity changed" in e for e in result["errors"])


def test_forbidden_order_field_and_bad_schema_rejected(tmp_path):
    _write(tmp_path, _record("QUOTE", extra='"order_ticket":9'),
           _record("STATUS").replace(SCHEMA, "incorrect.schema"))
    result = verifier.verify_symbol(tmp_path, SYMBOL, DAY)
    assert not result["ok"]
    assert any("forbidden field" in e for e in result["errors"])
    assert any("unexpected schema" in e for e in result["errors"])


def test_dom_missing_volume_is_error_when_dom_present(tmp_path):
    dom = _record("DOM").replace(',"volume":1', "")
    _write(tmp_path, _record("QUOTE"), _record("STATUS"), dom)
    result = verifier.verify_symbol(tmp_path, SYMBOL, DAY)
    assert not result["ok"]
    assert any("missing volume" in e for e in result["errors"])


def test_status_dom_false_valid_and_malformed_dom_state_rejected(tmp_path):
    _write(tmp_path, _record("QUOTE"), _record("STATUS"))
    assert verifier.verify_symbol(tmp_path, SYMBOL, DAY)["ok"]
    _write(tmp_path, _record("QUOTE"),
           _record("STATUS").replace('"dom_subscribed":false', '"dom_subscribed":"false"'))
    result = verifier.verify_symbol(tmp_path, SYMBOL, DAY)
    assert not result["ok"]
    assert any("dom_subscribed not boolean" in e for e in result["errors"])
