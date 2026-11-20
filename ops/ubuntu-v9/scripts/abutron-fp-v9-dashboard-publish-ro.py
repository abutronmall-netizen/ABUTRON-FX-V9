#!/usr/bin/env python3
# Migration export: private broker/CRM identity values come from runtime environment.
"""Publish limited FP DEMO V9 coverage for the authenticated APEX operator UI.
No order, broker API, CRM, risk or terminal write paths.
"""
from __future__ import annotations
import datetime as dt
import grp
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from abutron_fp_v9_alert_logic import enrich, safe_previous

MONITOR = Path("/var/lib/abutron-v9-brain/monitor/fp-demo-13of13-latest.json")
COLLECTOR = Path("/var/lib/abutron-fpmarkets-feed/summary.json")
OUTPUT = Path("/var/lib/apex/operator-live/fp-v9-demo-readiness.json")
ACCOUNTS = {
    "0512": __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_0512"],
    "1636": __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_1636"],
}
ALL = {
    "AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCHF", "USDJPY",
    "BTCUSD", "XAUUSD", "USOIL", "US30", "SP500", "US100", "GER40",
}

def read_root_json(path, maximum=300000):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError("UNTRUSTED_SOURCE_PERMISSIONS")
        if not (40 < st.st_size < maximum):
            raise ValueError("INVALID_SOURCE_SIZE")
        raw = os.read(fd, maximum)
        st2 = os.fstat(fd)
        if len(raw) != st.st_size or (st.st_ino, st.st_mtime_ns, st.st_size) != (
                st2.st_ino, st2.st_mtime_ns, st2.st_size):
            raise ValueError("SOURCE_CHANGED_DURING_READ")
        return json.loads(raw)
    finally:
        os.close(fd)

def age(value, now):
    if not isinstance(value, str):
        raise ValueError("SOURCE_TIMESTAMP_MISSING")
    instant = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("SOURCE_TIMEZONE_MISSING")
    elapsed = now - instant.timestamp()
    if not -10 <= elapsed <= 160:
        raise ValueError("SOURCE_STALE")
    return round(elapsed, 1)

def build(now):
    out = {
        "schema": "abutron.apex.operator.fp_v9_demo_readiness.v1",
        "checked_at_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        "broker": "FP Markets",
        "environment": "DEMO",
        "status": "UNVERIFIED",
        "ready_for_order_execution": False,
        "orders_submitted_by_monitor": 0,
        "accounts": {},
    }
    try:
        m = read_root_json(MONITOR)
        f = read_root_json(COLLECTOR, 1000000)
        if m.get("schema") != "abutron.v9.fpmarkets.demo.13of13.monitor.v1":
            raise ValueError("MONITOR_SCHEMA_INVALID")
        if f.get("schema") != "abutron.fpmarkets.feed.readonly.v1":
            raise ValueError("BROKER_COLLECTOR_SCHEMA_INVALID")
        age(m.get("checked_at_utc"), now)
        age(f.get("checked_at_utc"), now)
        if f.get("orders_submitted") != 0 or f.get("execution_enabled") is not False:
            raise ValueError("COLLECTOR_NOT_READ_ONLY")
        reports = f.get("connections")
        if not isinstance(reports, list):
            raise ValueError("BROKER_ACCOUNT_LIST_MISSING")
        by_connection = {
            x["connection_id"]: x for x in reports
            if isinstance(x, dict) and x.get("connection_id") in ACCOUNTS.values()
        }
        if set(by_connection) != set(ACCOUNTS.values()):
            raise ValueError("BROKER_ACCOUNTS_MISSING")
        monitored = m.get("accounts")
        if not isinstance(monitored, dict) or set(monitored) != set(ACCOUNTS):
            raise ValueError("MONITOR_ACCOUNTS_MISSING")
        if m.get("status") not in (
            "COMPLETE_13_OF_13", "PARTIAL_INFERENCE", "INFERENCE_UNVERIFIED"
        ):
            raise ValueError("MONITOR_STATUS_INVALID")
        if m.get("status") == "INFERENCE_UNVERIFIED":
            raise ValueError("V9_INFERENCE_UNVERIFIED")
        age(m.get("inference_generated_utc"), now)
        for suffix, cid in ACCOUNTS.items():
            a = monitored[suffix]
            c = by_connection[cid]
            age(c.get("checked_at_utc"), now)
            if c.get("mode") != "DEMO" or c.get("broker") != "FP Markets":
                raise ValueError("BROKER_ACCOUNT_SCOPE_MISMATCH")
            if c.get("orders_submitted") != 0 or c.get("execution_enabled") is not False:
                raise ValueError("BROKER_ACCOUNT_NOT_READ_ONLY")
            count = a.get("symbols_current")
            missing = a.get("missing")
            if (type(count) is not int or not (0 <= count <= 13)
                    or type(missing) is not list or set(missing) - ALL
                    or len(set(missing)) != len(missing)
                    or len(missing) != 13 - count
                    or a.get("symbols_required") != 13):
                raise ValueError("INFERENCE_COUNTS_INVALID")
            certified = a.get("scope_certified") is True
            if certified != (count == 13):
                raise ValueError("INFERENCE_CERTIFICATION_CONFLICT")
            stale = c.get("stale_or_unquoted_symbols")
            if not isinstance(stale, list) or any(x not in ALL for x in stale):
                raise ValueError("INVALID_BROKER_STALE_SYMBOLS")
            reasons = [{
                "symbol": symbol,
                "reason_code": (
                    "BROKER_QUOTE_MISSING_OR_STALE" if symbol in stale
                    else "INFERENCE_CANDLE_OR_QUOTE_UNVERIFIED"
                ),
                "reason": (
                    "Fresh authenticated broker quote missing or stale"
                    if symbol in stale else "Native quote or closed M5 evidence unavailable"
                ),
            } for symbol in sorted(missing)]
            # If the collector is incomplete, never display completed inference as certified.
            full_feed = (
                c.get("status") == "BROKER_NATIVE_DATA_COMPLETE"
                and c.get("fresh_quotes") == 13
                and c.get("timeframes_verified") == 78
            )
            fully_ready = certified and full_feed
            if certified and not full_feed:
                reasons = [{"symbol": "FEED", "reason_code": "BROKER_FEED_INCOMPLETE",
                            "reason": "Current broker feed not complete at 13/13"}]
            out["accounts"][suffix] = {
                "label": "FP DEMO " + suffix,
                "symbols_current": count if fully_ready or not certified else 0,
                "symbols_required": 13,
                "inference_certified": fully_ready,
                "status": "COMPLETE_13_OF_13" if fully_ready else "PARTIAL_INFERENCE",
                "missing": sorted(missing) if not certified else [],
                "reasons": reasons,
                "broker_fresh_quotes": c.get("fresh_quotes"),
                "broker_verified_timeframes": c.get("timeframes_verified"),
                "native_feed_status": c.get("status"),
                "broker_export_age_seconds": age(c.get("checked_at_utc"), now),
            }
        out["status"] = ("COMPLETE_13_OF_13"
                         if all(x["inference_certified"] for x in out["accounts"].values())
                         else "PARTIAL_INFERENCE")
        out["available"] = True
        out["inference_generated_utc"] = m["inference_generated_utc"]
        out["source_age_seconds"] = age(m["inference_generated_utc"], now)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        out["status"] = "UNVERIFIED"
        out["available"] = False
        out["accounts"] = {}
        out["reason_code"] = str(error)[:75] if isinstance(error, ValueError) else "SOURCE_UNAVAILABLE"
    return out

def atomic_publish(result):
    # This directory is already private to the operator group.
    st = OUTPUT.parent.stat()
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError("OPERATOR_DIRECTORY_MISSING")
    fd, path = tempfile.mkstemp(prefix=".fp-v9-ui-", dir=str(OUTPUT.parent))
    try:
        os.fchmod(fd, 0o640)
        os.fchown(fd, 0, grp.getgrnam("apexagent").gr_gid)
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, OUTPUT)
    finally:
        if os.path.lexists(path):
            os.unlink(path)

if __name__ == "__main__":
    now = time.time()
    outcome, transitions = enrich(build(now), safe_previous(OUTPUT), now)
    atomic_publish(outcome)
    for event in transitions:
        print("FP_V9_COVERAGE_EVENT", event["type"], event["account"],
              event["id"], "NO_BROKER_ORDERS", flush=True)
    print("FP_V9_APEX_READINESS_PUBLISHED", outcome["status"],
          {k: x["symbols_current"] for k, x in outcome["accounts"].items()},
          outcome.get("reason_code", ""), "NO_ORDERS", flush=True)
