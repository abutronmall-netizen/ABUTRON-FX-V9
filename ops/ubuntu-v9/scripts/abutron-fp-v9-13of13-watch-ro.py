#!/usr/bin/env python3
# Migration export: private broker/CRM identity values come from runtime environment.
"""Read-only ABUTRON FP Markets V9 DEMO coverage/status monitor.

Consumes V9 inference results only; never requests, synthesizes or trades quotes.
"""
import datetime as dt
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

SOURCE = Path("/var/lib/abutron-v9-brain/decisions/fp-demo-authoritative-latest.json")
DEST = Path("/var/lib/abutron-v9-brain/monitor/fp-demo-13of13-latest.json")
CONNECTIONS = {
    "0512": __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_0512"],
    "1636": __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_1636"],
}
SYMBOLS = {
    "AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCHF", "USDJPY",
    "XAUUSD", "BTCUSD", "USOIL", "US30", "SP500", "US100", "GER40",
}
MAX_REPORT_AGE_SEC = 180
MAX_SOURCE_AGE_SEC = 180

def parse_iso(raw):
    if not isinstance(raw, str):
        raise ValueError("MISSING_TIME")
    value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("UNZONED_TIME")
    return value.timestamp()

def source_read():
    fd = os.open(SOURCE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or (st.st_mode & 0o022):
            raise ValueError("UNTRUSTED_REPORT_PERMISSIONS")
        if not (100 < st.st_size <= 1000000):
            raise ValueError("INVALID_REPORT_SIZE")
        raw = os.read(fd, 1000001)
        st2 = os.fstat(fd)
        if len(raw) != st.st_size or (st.st_ino, st.st_mtime_ns, st.st_size) != (
                st2.st_ino, st2.st_mtime_ns, st2.st_size):
            raise ValueError("REPORT_CHANGED_WHILE_READING")
        return json.loads(raw)
    finally:
        os.close(fd)

def account_status(name, raw, now):
    if not isinstance(raw, dict) or raw.get("connection_id") != CONNECTIONS[name]:
        raise ValueError("ACCOUNT_BINDING_" + name)
    if raw.get("environment") != "DEMO" or raw.get("broker") != "FP Markets":
        raise ValueError("NOT_DEMO_" + name)
    export_age = now - parse_iso(raw.get("native_export_checked_at_utc"))
    if not (-10 <= export_age <= MAX_SOURCE_AGE_SEC):
        raise ValueError("BROKER_EXPORT_STALE_" + name)
    digest = raw.get("native_source_sha256")
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        raise ValueError("NO_SOURCE_DIGEST_" + name)
    rows = raw.get("symbols")
    if not isinstance(rows, list) or len(rows) > 13:
        raise ValueError("BAD_SYMBOL_ROWS_" + name)
    names = [r.get("canonical_symbol") for r in rows if isinstance(r, dict)]
    if len(names) != len(rows) or len(set(names)) != len(rows) or not set(names) <= SYMBOLS:
        raise ValueError("BAD_SYMBOL_IDENTITIES_" + name)
    missing = sorted(SYMBOLS - set(names))
    if raw.get("symbols_current") != len(rows) or raw.get("symbols_missing") != missing:
        raise ValueError("SYMBOL_COUNTS_INCONSISTENT_" + name)
    for row in rows:
        if (row.get("trained_model") is not True
                or row.get("features_count") != 49
                or row.get("state") != "CURRENT_NATIVE_M5"
                or row.get("order_authority") is not False):
            raise ValueError("UNVERIFIED_TRAINED_ROW_" + name)
        if row.get("model_direction") not in ("BUY", "SELL", "HOLD"):
            raise ValueError("INVALID_DIRECTION_" + name)
    complete = len(rows) == 13 and not missing
    if raw.get("scope_certified") is not complete:
        raise ValueError("SCOPE_CERTIFICATION_INCONSISTENT_" + name)
    return {
        "symbols_current": len(rows),
        "symbols_required": 13,
        "missing": missing,
        "scope_certified": complete,
        "source_age_seconds": round(export_age, 1),
        "source_sha256": digest,
    }

def atomic_write(value):
    DEST.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".fp-coverage-", dir=str(DEST.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, DEST)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)

def run():
    now = time.time()
    previous = {}
    try:
        previous = json.loads(DEST.read_text())
    except (OSError, ValueError, TypeError):
        pass
    current = {
        "schema": "abutron.v9.fpmarkets.demo.13of13.monitor.v1",
        "checked_at_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        "orders_submitted_by_monitor": 0,
        "trading_configuration_modified": False,
        "accounts": {},
    }
    try:
        report = source_read()
        age = now - parse_iso(report.get("generated_utc"))
        if not (-10 <= age <= MAX_REPORT_AGE_SEC):
            raise ValueError("V9_INFERENCE_REPORT_STALE")
        if (report.get("schema") != "abutron.v9.fpmarkets.demo.native.account_trained_m5.v1"
                or report.get("trained") is not True
                or report.get("model_features") != 49
                or report.get("model_symbols") != 26
                or report.get("native_order_dispatch") != "NOT_CONNECTED"
                or report.get("orders_submitted") != 0):
            raise ValueError("UNVERIFIED_V9_REPORT")
        accounts = report.get("accounts")
        if not isinstance(accounts, dict) or set(accounts) != set(CONNECTIONS):
            raise ValueError("DEMO_ACCOUNT_SET_INCORRECT")
        current["model_sha256"] = report.get("model_sha256")
        current["inference_generated_utc"] = report["generated_utc"]
        current["inference_age_seconds"] = round(age, 1)
        for name in CONNECTIONS:
            current["accounts"][name] = account_status(name, accounts[name], now)
        counts = [x["symbols_current"] for x in current["accounts"].values()]
        if all(x["scope_certified"] for x in current["accounts"].values()):
            current["status"] = "COMPLETE_13_OF_13"
        else:
            current["status"] = "PARTIAL_INFERENCE"
        current["summary"] = ",".join(
            f"{name}:{current['accounts'][name]['symbols_current']}/13"
            for name in CONNECTIONS)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        current["status"] = "INFERENCE_UNVERIFIED"
        current["reason"] = str(exc)[:180]
        current["summary"] = "Source missing, stale or invalid"
    fingerprint = json.dumps([
        current["status"], current.get("reason"), current["summary"],
        {name: value.get("missing") for name, value in current["accounts"].items()},
    ], sort_keys=True)
    previous_logged = previous.get("last_log_epoch", 0)
    changed = previous.get("fingerprint") != fingerprint
    should_log = changed or not isinstance(previous_logged, (int, float)) or now - previous_logged >= 900
    current["fingerprint"] = fingerprint
    current["last_log_epoch"] = now if should_log else previous_logged
    atomic_write(current)
    if should_log:
        print("ABUTRON_FP_DEMO_V9_AUTO_MONITOR", current["status"], current["summary"],
              current.get("reason", ""), "READ_ONLY_NO_ORDERS", flush=True)

if __name__ == "__main__":
    run()
