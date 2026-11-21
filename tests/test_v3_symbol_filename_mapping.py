"""Source-only tests for exact-symbol V3 JSONL discovery.

These are synthetic fixtures; not broker-authenticated market evidence.
"""
import json
from pathlib import Path

import pytest

from abutron.feeds.native_market_v3 import broker_symbol_from_v3_file, SCHEMA


def _write_v3(tmp_path: Path, symbol: str, stem: str | None = None) -> Path:
    safe = symbol.replace(" ", "_") if stem is None else stem
    path = tmp_path / f"ABUTRON_NATIVE_MARKET_V3_{safe}_20261121.jsonl"
    path.write_text(json.dumps({
        "schema": SCHEMA,
        "symbol": symbol,
        "broker_server": "Deriv-Demo",
        "account_login": 123456,
        "environment": "DEMO",
        "record_type": "STATUS",
        "time_msc": 1795260000000,
    }) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("symbol", (
    "EURUSD.r", "Volatility 50 Index.0", "Crash 1000 Index.0",
    "Boom 150 Index.0", "Volatility_50_Index.0",
))
def test_returns_exact_json_broker_symbol(tmp_path, symbol):
    assert broker_symbol_from_v3_file(_write_v3(tmp_path, symbol)) == symbol


def test_rejects_header_symbol_filename_mismatch(tmp_path):
    path = _write_v3(tmp_path, "Volatility 50 Index.0", stem="Volatility_25_Index.0")
    with pytest.raises(ValueError, match="SYMBOL_FILENAME_MISMATCH"):
        broker_symbol_from_v3_file(path)


def test_rejects_invalid_json(tmp_path):
    p = tmp_path / "ABUTRON_NATIVE_MARKET_V3_EURUSD.r_20261121.jsonl"
    p.write_text("{broken\n")
    p.chmod(0o600)
    with pytest.raises(ValueError, match="HEADER_INVALID"):
        broker_symbol_from_v3_file(p)


def test_rejects_symlink(tmp_path):
    p = _write_v3(tmp_path, "EURUSD.r")
    alias = tmp_path / "ABUTRON_NATIVE_MARKET_V3_EURUSD.r_20261122.jsonl"
    alias.symlink_to(p)
    with pytest.raises(OSError):
        broker_symbol_from_v3_file(alias)
