#!/usr/bin/env python3
"""ABUTRON FX V9 a88f373 trained unified causal-signal native observer.

This sidecar loads the exact full-system trained checkpoint and the release's
signal/guard implementation. Reads authenticated broker-native M5 history
from existing RO databases, writes a JSON observation only. It intentionally
does NOT import the legacy dispatcher, contact MT5 sockets, change CRM switches,
or submit any orders. Source SHA pinned; rollout is rollback-safe.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

RELEASE = Path("/opt/abutron-v9-brain/releases/a88f373001ee1b8fcc934a1e3d068ed5b90f947b")
MODEL_DIR = RELEASE / "data/brains/unified_m5"
MODEL = MODEL_DIR / "nexus_brain.json"
MODEL_SHA = "2667997b1498aae316c4103ec1f3d0bd2efb805f034836455ed10019284302df"
DERIV_DB = Path(os.environ.get("ABUTRON_V9_NATIVE_CANDLES_DB", "/var/lib/apex/precision-history/closed-candles.sqlite3"))
FP_DB = Path("/var/lib/abutron-fp-native-m5-archive/broker-closed-m5.sqlite3")
OUT = Path("/var/lib/abutron-v9-brain/decisions/latest.json")
BAR_SECONDS = 300
HISTORY_BARS = 120

def query_only(path: Path):
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("NATIVE_HISTORY_SOURCE_INVALID")
    c = sqlite3.connect("file:" + str(path) + "?mode=ro",uri=True,timeout=8)
    c.execute("PRAGMA query_only=ON")
    return c

def validated(rows):
    if len(rows) < 65:
        return None
    last = -1
    for ts, op, hi, lo, cl in rows:
        if (type(ts) is not int or ts <= last or ts % BAR_SECONDS or
                any(not math.isfinite(float(v)) or float(v)<=0
                    for v in (op,hi,lo,cl)) or
                lo > min(op,cl) or hi < max(op,cl)):
            return None
        last = ts
    return rows

def deriv_bars(conn, symbol):
    rs = conn.execute(
        "SELECT start_time_utc,open,high,low,close FROM candles "
        "WHERE symbol=? AND timeframe='M5' "
        "ORDER BY start_time_utc DESC LIMIT ?",(symbol,HISTORY_BARS)
    ).fetchall()
    return validated(rs[::-1])

def fp_bars(conn, symbol):
    # Authenticated DEMO 0512 immutable M5 source with native broker symbols,
    # no synthetic/relabelled candles and no trading/account mutation.
    rs = conn.execute(
        "SELECT start_time_utc,open,high,low,close FROM closed_m5 "
        "WHERE account='0512' AND broker_symbol=? "
        "ORDER BY start_time_utc DESC LIMIT ?", (symbol,HISTORY_BARS)
    ).fetchall()
    return validated(rs[::-1])

def safe_write(path, obj):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix=".unified-a88-",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(obj,f,allow_nan=False,separators=(",",":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp,0o640)
        os.replace(tmp,path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)

def main():
    from abutron.brain.unified import load_unified
    from abutron.brain.signals import (SIGNAL_DATA_VERSION, SIGNAL_SLOT_INDICES,
                                       build_signal_bundle)
    from abutron.brain.features import FEATURE_VERSION, N_FEATURES
    cli=argparse.ArgumentParser()
    cli.add_argument("--output",default=str(OUT))
    args=cli.parse_args()
    if hashlib.sha256(MODEL.read_bytes()).hexdigest()!=MODEL_SHA:
        raise RuntimeError("RETRAINED_MERGE_MODEL_PIN_MISMATCH")
    brain=load_unified(str(MODEL_DIR),mc_samples=16)
    if brain is None:
        raise RuntimeError("MERGED_FULL_SIGNAL_MODEL_LOAD_FAILED")
    if (len(brain.symbol_ids)!=26 or len(set(brain.symbol_ids))!=26 or
        len(brain.ensemble.members)!=3 or N_FEATURES!=23 or
        FEATURE_VERSION!="nexus_v1"):
        raise RuntimeError("FEATURE_UNIVERSE_DRIFT")
    now=time.time()
    output=[]
    dconn=query_only(DERIV_DB)
    fconn=query_only(FP_DB)
    try:
        for symbol in brain.symbol_ids:
            is_deriv=symbol.endswith(" Index.0") and symbol.startswith(
                ("Boom ","Crash ","Volatility "))
            broker="Deriv" if is_deriv else "FP Markets"
            try:
                rows=deriv_bars(dconn,symbol) if is_deriv else fp_bars(fconn,symbol)
                if rows is None:
                    output.append({"symbol":symbol,"broker":broker,
                                   "state":"NATIVE_M5_UNAVAILABLE"})
                    continue
                candles=[{"symbol":symbol,
                          "open_time":dt.datetime.fromtimestamp(
                              r[0],dt.timezone.utc).isoformat(),
                          "open":r[1],"high":r[2],"low":r[3],"close":r[4],
                          "volume":0.0}
                         for r in rows]
                bundle=build_signal_bundle(candles,symbol=symbol,
                                           use_gemma=False)
                signal=bundle.at(-1)
                proposal=brain.decide(
                    symbol=symbol,closes=bundle.closes,
                    highs=bundle.highs,lows=bundle.lows,
                    orderflow=signal["orderflow"],
                    footprint=signal["footprint"],
                    agent_scores=signal["agent_scores"],
                    regime=signal["regime"])
                if (proposal.brain_version!="unified_v1" or
                    proposal.trained is not True or
                    len(proposal.features)!=49 or
                    proposal.direction not in ("BUY","SELL","HOLD") or
                    not all(math.isfinite(float(v)) for v in (
                        proposal.confidence,proposal.conviction,
                        proposal.uncertainty))):
                    raise ValueError("MERGED_PROPOSAL_INVALID")
                full_signal_slots=sum(
                    abs(float(proposal.features[i]))>1e-12
                    for i in SIGNAL_SLOT_INDICES)
                last_close=rows[-1][0]+BAR_SECONDS
                age=int(now-last_close)
                freshness=("CURRENT_NATIVE_M5" if -30<=age<=600
                           else "STALE_NATIVE_M5")
                output.append({
                    "symbol":symbol,"broker":broker,"state":freshness,
                    "last_closed_m5_utc":dt.datetime.fromtimestamp(
                        last_close,dt.timezone.utc).isoformat(),
                    "bar_age_seconds":age,
                    "model_direction":proposal.direction,
                    "model_confidence":round(float(proposal.confidence),6),
                    "model_conviction":round(float(proposal.conviction),6),
                    "model_uncertainty":round(float(proposal.uncertainty),6),
                    "loss_pattern_guard_blockers":list(
                        getattr(proposal,"blockers",[]) or []),
                    "full_signal_feature_slots_nonzero":full_signal_slots,
                    "signal_data_version":SIGNAL_DATA_VERSION,
                    "features_count":len(proposal.features),
                    "feature_source":"OHLCV_PROXY_NO_NATIVE_TICK_OR_DEPTH",
                    "trained_model":True,
                    "order_authority":False
                })
            except (TypeError,ValueError,OverflowError,sqlite3.Error) as ex:
                output.append({"symbol":symbol,"broker":broker,
                               "state":"MODEL_INFERENCE_ERROR",
                               "error_type":type(ex).__name__,
                               "error_detail":str(ex)[:105]})
    finally:
        dconn.close()
        fconn.close()
    def by(broker,field="model_direction"):
        return sum(x["broker"]==broker and field in x for x in output)
    if len(output)!=26 or by("Deriv","symbol")!=13 or by("FP Markets","symbol")!=13:
        raise RuntimeError("MERGED_CHECKPOINT_SYMBOL_SCOPE_MISMATCH")
    report={
        "schema":"abutron.v9.a88f373.unified_native_causal_readonly.v1",
        "generated_utc":dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),
        "release_commit":"a88f373001ee1b8fcc934a1e3d068ed5b90f947b",
        "model_sha256":MODEL_SHA,"model_loaded":True,"trained":True,
        "members":len(brain.ensemble.members),"model_features":49,
        "model_symbols":26,"recorded_online_updates":brain.online_updates,
        "deriv_inferred":by("Deriv"),"fp_inferred":by("FP Markets"),
        "deriv_current":sum(x["broker"]=="Deriv" and
                            x["state"]=="CURRENT_NATIVE_M5" for x in output),
        "fp_current":sum(x["broker"]=="FP Markets" and
                         x["state"]=="CURRENT_NATIVE_M5" for x in output),
        "signal_data_version":SIGNAL_DATA_VERSION,
        "microstructure_source":"OHLCV_PROXY_NOT_AUTHENTIC_TICK_OR_DEPTH",
        "native_order_dispatch":"NOT_CONNECTED",
        "orders_submitted":0,"crm_mutated":False,
        "risk_policy_modified":False,"mt5_terminals_modified":False,
        "symbols":output,
    }
    safe_write(Path(args.output),report)
    print("ABUTRON_V9_FULL_SIGNAL_BRAIN_LOADED",
          report["release_commit"][:8],
          "deriv",report["deriv_inferred"],"fp",report["fp_inferred"],
          "current_deriv",report["deriv_current"],
          "current_fp",report["fp_current"],
          "orders",report["orders_submitted"])

if __name__=="__main__":
    main()
