#!/usr/bin/env python3
"""Production ABUTRON V9 unified trained-model diagnostics, broker-native READ ONLY.

Loads exactly pinned GitHub trained weights and performs symbol-conditioned M5
inference. Consumes broker-native history SQLite via query-only connections.
No MT5 terminal controls, CRM mutation, order commands or risk-policy writes.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

RELEASE = Path("/opt/abutron-v9-brain/releases/9234236dc8981b2000e70b915d1f5d0285247462")
MODEL_DIR = RELEASE / "data/brains/unified_m5"
MODEL = MODEL_DIR / "nexus_brain.json"
EXPECTED_SHA = "377a7590344efc52ff66a4ff739709c62437dc942ea293010b63864944e7d032"
DERIV_DB = Path("/var/lib/apex/precision-history/closed-candles.sqlite3")
FP_DB = Path("/var/lib/abutron-quant-first/dual-broker/fp-immutable-native-candles.sqlite3")
OUT = Path("/var/lib/abutron-v9-brain/decisions/latest.json")
CANDLE_SECS = 300
FRAMES_TO_READ = 120

def read_only_db(path):
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("AUTHENTIC_NATIVE_HISTORY_MISSING")
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=8)
    conn.execute("PRAGMA query_only=ON")
    return conn

def valid_bars(rows):
    if len(rows) < 65:
        return None
    last = -1
    for ts, close, high, low in rows:
        if (not isinstance(ts, int) or ts <= last or ts % CANDLE_SECS
                or any(not math.isfinite(float(x)) or float(x) <= 0
                       for x in (close, high, low))
                or low > close or high < close):
            return None
        last = ts
    return rows

def deriv_bars(conn, symbol):
    rows = conn.execute(
        "SELECT start_time_utc,close,high,low FROM candles "
        "WHERE symbol=? AND timeframe='M5' "
        "ORDER BY start_time_utc DESC LIMIT ?", (symbol, FRAMES_TO_READ)
    ).fetchall()
    return valid_bars(rows[::-1])

def fp_bars(conn, symbol):
    # Select one broker-export account per symbol to avoid joining independent accounts.
    row = conn.execute(
        "SELECT account_fingerprint,MAX(start_time_utc) AS newest "
        "FROM fp_candles WHERE actual=? AND timeframe='M5' "
        "GROUP BY account_fingerprint ORDER BY newest DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    if not row:
        return None
    rows = conn.execute(
        "SELECT start_time_utc,close,high,low FROM fp_candles "
        "WHERE account_fingerprint=? AND actual=? AND timeframe='M5' "
        "ORDER BY start_time_utc DESC LIMIT ?",
        (row[0], symbol, FRAMES_TO_READ)
    ).fetchall()
    return valid_bars(rows[::-1])

def atomic_json(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".v9-nexus-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, allow_nan=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
        d = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def main():
    from abutron.brain.unified import load_unified
    from abutron.brain.features import N_FEATURES, FEATURE_VERSION
    sha = hashlib.sha256(MODEL.read_bytes()).hexdigest()
    if sha != EXPECTED_SHA:
        raise RuntimeError("PINNED_TRAINED_MODEL_CHECKSUM_MISMATCH")
    brain = load_unified(str(MODEL_DIR), mc_samples=16)
    if brain is None:
        raise RuntimeError("TRAINED_UNIFIED_MODEL_COULD_NOT_LOAD")
    symbol_ids = brain.symbol_ids
    if (len(symbol_ids) != len(set(symbol_ids)) or len(symbol_ids) != 26
            or len(brain.ensemble.members) != 3 or N_FEATURES != 23
            or FEATURE_VERSION != "nexus_v1"):
        raise RuntimeError("TRAINED_BRAIN_FEATURE_UNIVERSE_MISMATCH")
    now = time.time()
    entries = []
    deriv = read_only_db(DERIV_DB)
    fp = read_only_db(FP_DB)
    try:
        for symbol in symbol_ids:
            is_deriv = symbol.endswith(" Index.0") and symbol.startswith(
                ("Volatility ", "Crash ", "Boom "))
            broker = "Deriv" if is_deriv else "FP Markets"
            try:
                rows = (deriv_bars(deriv, symbol) if is_deriv else
                        fp_bars(fp, symbol))
                if rows is None:
                    entries.append({"symbol":symbol, "broker":broker,
                                    "state":"NATIVE_M5_UNAVAILABLE"})
                    continue
                proposal = brain.decide(
                    symbol=symbol, closes=[r[1] for r in rows],
                    highs=[r[2] for r in rows],
                    lows=[r[3] for r in rows])
                if (proposal.brain_version != "unified_v1" or
                        proposal.trained is not True or
                        len(proposal.features) != 49 or
                        proposal.direction not in ("BUY", "SELL") or
                        not all(math.isfinite(float(x)) for x in
                                (proposal.confidence, proposal.conviction,
                                 proposal.uncertainty))):
                    raise ValueError("MODEL_INFERENCE_INVALID")
                last_close = rows[-1][0] + CANDLE_SECS
                age_seconds = int(now - last_close)
                freshness = ("CURRENT_NATIVE_M5" if -30 <= age_seconds <= 600
                             else "STALE_NATIVE_M5")
                entries.append({
                    "symbol":symbol, "broker":broker, "state":freshness,
                    "source":"broker-native stored closed M5",
                    "last_bar_closed_utc":dt.datetime.fromtimestamp(
                        last_close,dt.timezone.utc).isoformat(),
                    "last_close_age_seconds":age_seconds,
                    "model_direction":proposal.direction,
                    "model_confidence":round(proposal.confidence,6),
                    "model_conviction":round(proposal.conviction,6),
                    "model_uncertainty":round(proposal.uncertainty,6),
                    "trained_model":True,
                    "position_order_authorized_by_this_service":False
                })
            except (ValueError, TypeError, OverflowError, sqlite3.Error) as e:
                entries.append({
                    "symbol":symbol, "broker":broker,
                    "state":"INFERENCE_ERROR", "error_type":type(e).__name__})
    finally:
        deriv.close()
        fp.close()
    count = lambda broker: sum(x["broker"]==broker for x in entries)
    current = lambda broker: sum(x["broker"]==broker and
                                 x["state"]=="CURRENT_NATIVE_M5"
                                 for x in entries)
    predicted = lambda broker: sum(x["broker"]==broker and
                                   "model_direction" in x for x in entries)
    if count("Deriv") != 13 or count("FP Markets") != 13:
        raise RuntimeError("MODEL_26_SYMBOL_UNIVERSE_NOT_ACCOUNTED")
    payload = {
        "schema":"abutron.v9.trained_unified.brain.native_readonly.v1",
        "generated_utc":dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),
        "release_commit":"9234236dc8981b2000e70b915d1f5d0285247462",
        "model_path":str(MODEL),
        "model_sha256":sha,
        "model_loaded":True,
        "trained":True,
        "ensemble_members":len(brain.ensemble.members),
        "model_input_features":49,
        "online_updates_from_training":brain.online_updates,
        "total_model_symbols":len(symbol_ids),
        "deriv_covered":count("Deriv"),
        "deriv_inferred":predicted("Deriv"),
        "deriv_current_native_m5":current("Deriv"),
        "fp_covered":count("FP Markets"),
        "fp_inferred":predicted("FP Markets"),
        "fp_current_native_m5":current("FP Markets"),
        "execution_interface":"NOT_WIRED_TO_NATIVE_ORDER_DISPATCHER",
        "crm_switches_modified":False,
        "mt5_terminal_state_modified":False,
        "orders_submitted_by_this_service":0,
        "symbols":entries,
    }
    atomic_json(OUT,payload)
    print("TRAINED_V9_MODEL_ACTIVE",sha,
          "DERIV",predicted("Deriv"),"/13",
          "FP",predicted("FP Markets"),"/13",
          "FRESH_DERIV",current("Deriv"),
          "FRESH_FP",current("FP Markets"),
          "ORDERS_SUBMITTED_BY_THIS_SERVICE",0)

if __name__ == "__main__":
    main()
