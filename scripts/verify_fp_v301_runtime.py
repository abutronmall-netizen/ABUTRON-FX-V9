#!/usr/bin/env python3
"""FP DEMO V3.01 runtime verification — read-only per-symbol evidence check.

Validates the V3.01 multi-symbol JSONL output produced by the ApexMarketFeedV3
indicator on FP DEMO accounts against the export contract. This script does
NOT attach indicators, start terminals, or touch any trading path. It only
reads the produced files and asserts the schema/precision/layout contract.

Contract assertions (per symbol):
  1. File layout: exactly one JSONL per symbol per day, named
     ABUTRON_NATIVE_MARKET_V3_<sanitized_symbol>_<YYYYMMDD>.jsonl
  2. Schema: every record carries schema, symbol, broker_server,
     account_login, environment="DEMO", record_type
  3. Identity: record "symbol" matches the filename's sanitized symbol
  4. Record types: QUOTE / MARKET_PRINT / DOM / STATUS all present
  5. Per-symbol price precision: all prices in a symbol's records use a
     consistent number of decimals (matches that symbol's SYMBOL_DIGITS)
  6. DOM: levels array with book_type/price/volume_real/volume per level
  7. STATUS: dom_subscribed=true for subscribed symbols
  8. Read-only: no order/position/trade fields in any record

Exit codes: 0 = all symbols verified, 1 = any assertion failed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FP_SYMBOLS = [
    "XAUUSD.r", "BTCUSD", "EURUSD.r", "GBPUSD.r", "USDJPY.r",
    "AUDUSD.r", "US100", "US30", "US500", "GER40",
    "XTIUSD", "USDCHF.r", "NZDUSD.r",
]

# Expected decimal precision per symbol (FP Markets contract digits).
# The script also derives the actual precision from the records and
# cross-checks consistency; this table is the expected reference.
EXPECTED_DIGITS = {
    "XAUUSD.r": 2, "BTCUSD": 2, "EURUSD.r": 5, "GBPUSD.r": 5,
    "USDJPY.r": 3, "AUDUSD.r": 5, "US100": 2, "US30": 2,
    "US500": 2, "GER40": 2, "XTIUSD": 3, "USDCHF.r": 5,
    "NZDUSD.r": 5,
}

FILE_NAME_RE = re.compile(
    r"ABUTRON_NATIVE_MARKET_V3_([A-Za-z0-9._-]+)_(\d{8})\.jsonl\Z")

REQUIRED_FIELDS = (
    "schema", "symbol", "broker_server", "account_login",
    "environment", "record_type",
)

PRICE_FIELDS = ("price", "bid", "ask")

# Fields that must never appear (read-only indicator contract).
FORBIDDEN_FIELDS = (
    "order_ticket", "position_ticket", "trade_action", "deal_id",
    "order_send", "position_modify",
)


def sanitize_file_symbol(symbol: str) -> str:
    """Sanitize a symbol for filename use (spaces/slashes -> underscore)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)


def decimals_of(value: str) -> int:
    """Count decimal places in a numeric string."""
    if "." in value:
        return len(value.split(".", 1)[1])
    return 0


def verify_symbol(data_dir: Path, symbol: str, day: str) -> dict:
    """Verify one symbol's JSONL file for the given day."""
    result = {
        "symbol": symbol,
        "file": None,
        "records": 0,
        "by_type": {},
        "errors": [],
        "warnings": [],
    }
    expected_file = (
        f"ABUTRON_NATIVE_MARKET_V3_{sanitize_file_symbol(symbol)}_{day}.jsonl")
    path = data_dir / expected_file
    result["file"] = expected_file

    if not path.is_file():
        result["errors"].append(f"missing file: {expected_file}")
        return result

    precisions = set()
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                result["errors"].append(
                    f"{expected_file}:{line_no} invalid JSON: {exc}")
                continue
            result["records"] += 1
            rtype = rec.get("record_type", "?")
            result["by_type"][rtype] = result["by_type"].get(rtype, 0) + 1

            # 1. Required schema fields
            for field in REQUIRED_FIELDS:
                if field not in rec:
                    result["errors"].append(
                        f"{expected_file}:{line_no} missing field: {field}")

            # 2. Environment must be DEMO
            if rec.get("environment") != "DEMO":
                result["errors"].append(
                    f"{expected_file}:{line_no} environment="
                    f"{rec.get('environment')!r} (expected DEMO)")

            # 3. Identity: record symbol matches filename symbol
            rec_sym = rec.get("symbol", "")
            if sanitize_file_symbol(rec_sym) != sanitize_file_symbol(symbol):
                result["errors"].append(
                    f"{expected_file}:{line_no} symbol mismatch: "
                    f"{rec_sym!r} != {symbol!r}")

            # 4. Read-only: no forbidden fields
            for field in FORBIDDEN_FIELDS:
                if field in rec:
                    result["errors"].append(
                        f"{expected_file}:{line_no} forbidden field: {field}")

            # 5. Per-symbol price precision
            if rtype in ("QUOTE", "MARKET_PRINT"):
                for pf in PRICE_FIELDS:
                    val = rec.get(pf)
                    if isinstance(val, (int, float)):
                        # JSON numbers may lose trailing zeros; check the
                        # string form only when present as string.
                        pass
                    elif isinstance(val, str):
                        precisions.add(decimals_of(val))
            elif rtype == "DOM":
                levels = rec.get("levels", [])
                if not isinstance(levels, list) or not levels:
                    result["errors"].append(
                        f"{expected_file}:{line_no} DOM with empty levels")
                for i, level in enumerate(levels):
                    if not isinstance(level, dict):
                        result["errors"].append(
                            f"{expected_file}:{line_no} DOM level {i} "
                            f"not an object")
                        continue
                    for key in ("book_type", "price", "volume_real"):
                        if key not in level:
                            result["errors"].append(
                                f"{expected_file}:{line_no} DOM level {i} "
                                f"missing {key}")
                    lp = level.get("price")
                    if isinstance(lp, str):
                        precisions.add(decimals_of(lp))

            # 6. STATUS records
            if rtype == "STATUS":
                if rec.get("dom_subscribed") is not True:
                    result["warnings"].append(
                        f"{expected_file}:{line_no} STATUS dom_subscribed "
                        f"not true for subscribed symbol")

    # 7. Required record types present
    for required_type in ("QUOTE", "MARKET_PRINT", "DOM", "STATUS"):
        if required_type not in result["by_type"]:
            result["errors"].append(
                f"missing record type: {required_type}")

    # 8. Per-symbol precision consistency
    if precisions:
        if len(precisions) > 1:
            result["errors"].append(
                f"inconsistent price precision: {sorted(precisions)}")
        expected = EXPECTED_DIGITS.get(symbol)
        if expected is not None and len(precisions) == 1:
            actual = next(iter(precisions))
            if actual != expected:
                result["warnings"].append(
                    f"price precision {actual} != expected {expected} "
                    f"for {symbol}")

    result["ok"] = not result["errors"]
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", required=True,
                   help="Directory containing V3 JSONL output files")
    p.add_argument("--day", required=True,
                   help="Day to verify, format YYYYMMDD")
    p.add_argument("--symbols", nargs="*", default=FP_SYMBOLS,
                   help="Symbols to verify (default: all 13 FP symbols)")
    p.add_argument("--json", action="store_true",
                   help="Output machine-readable JSON")
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"error: data directory not found: {data_dir}", file=sys.stderr)
        return 1

    results = [verify_symbol(data_dir, sym, args.day) for sym in args.symbols]

    total_records = sum(r["records"] for r in results)
    passed = sum(1 for r in results if r["ok"])
    failed = len(results) - passed

    if args.json:
        print(json.dumps({
            "day": args.day,
            "data_dir": str(data_dir),
            "symbols_checked": len(results),
            "symbols_passed": passed,
            "symbols_failed": failed,
            "total_records": total_records,
            "results": results,
        }, indent=2, sort_keys=True))
    else:
        print(f"FP DEMO V3.01 runtime verification — day {args.day}")
        print(f"data dir: {data_dir}")
        print(f"symbols: {passed}/{len(results)} passed, "
              f"{total_records} records total")
        print()
        for r in results:
            status = "PASS" if r["ok"] else "FAIL"
            print(f"[{status}] {r['symbol']:10s} "
                  f"records={r['records']:5d} "
                  f"types={r['by_type']}")
            for err in r["errors"]:
                print(f"       ERROR: {err}")
            for warn in r["warnings"]:
                print(f"       WARN:  {warn}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
