#!/usr/bin/env python3
# Migration export: production account identity is loaded from secure environment.
"""Independent account-bound DEMO native preflight evidence progress.

Reports historical OrderCheck and a bounded actual broker min-lot cost probe.
No order/CRM/MT5 write, no certification flag, no new-order authorization.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import tempfile
import time

ARCHIVE = pathlib.Path("/var/lib/abutron-apex11-6207/native-check-archive")
COST = pathlib.Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/v4-native-execution-cost-6207.json")
OUT = pathlib.Path("/var/lib/abutron-v9-native-risk-audit/evidence-progress-latest.json")
EA_SHA = "c8422e0a98f9e02570b6f8b77e5cf890fd66676e1aa3b3cf9f7e552d08322d43"
SCHEMA = "abutron.v9.independent_demo6207_preflight_progress.readonly.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_COST_AGE=140
MAX_RECEIPTS=10000

def safe_bytes(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or
                st.st_mode & 0o077 or st.st_nlink != 1 or
                st.st_size < 5 or st.st_size > limit):
            raise ValueError("FILE_OWNER_MODE_OR_SIZE")
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(raw)!=st.st_size or (st.st_mtime_ns,st.st_ino,st.st_size)!=(after.st_mtime_ns,after.st_ino,after.st_size):
            raise ValueError("FILE_RACE")
        return raw, st
    finally:
        os.close(fd)

def valid_money(s, allow_zero=False):
    if not isinstance(s,str) or len(s)>40:
        raise ValueError("NATIVE_PROFIT_FORMAT")
    n=float(s)
    if not math.isfinite(n) or (n < 0 if allow_zero else n<=0):
        raise ValueError("NATIVE_PROFIT_RANGE")
    return n

def validate_original(meta_raw, orig_raw, filename):
    if not filename.endswith(".verified.json"):
        raise ValueError("ARCHIVE_FILENAME")
    d=json.loads(meta_raw)
    if (not isinstance(d,dict) or
        d.get("schema")!="abutron.apex11.6207.verified_original_no_send_ordercheck.v1" or
        d.get("broker")!="Deriv" or d.get("environment")!="DEMO" or
        d.get("account_masked")!="••••6207" or
        d.get("native_preflight_status")!="NATIVE_EXACT_ORDERCHECK_PROVEN_NO_SEND" or
        d.get("original_native_status")!="CHECK_PASS_NO_SEND" or
        d.get("original_native_provenance")!="BROKER_NATIVE_EXACT_CHECK_ONLY" or
        d.get("pinned_native_ea_sha256")!=EA_SHA or
        d.get("historical_only") is not True or
        d.get("current_order_authority") is not False or
        d.get("native_order_sent") is not False or
        d.get("broker_fill_certified") is not False or
        d.get("trailing_modification_certified") is not False or
        d.get("one_lot_contract_sizing_certified") is not False):
        raise ValueError("ARCHIVED_ATTESTATION_MISMATCH")
    raw_sha=hashlib.sha256(orig_raw).hexdigest()
    if raw_sha!=d.get("original_native_receipt_sha256") or not HEX64.fullmatch(raw_sha):
        raise ValueError("ORIGINAL_RAW_SHA_MISMATCH")
    lines=orig_raw.decode("utf-8-sig").splitlines()
    if (len(lines)!=24 or
        lines[0]!="ABUTRON_APEX11_EXACT_CHECK_NATIVE_NO_SEND_V1" or
        lines[2:4]!=[__import__("os").environ["ABUTRON_DERIV_DEMO_LOGIN"],"Deriv-Demo"] or
        lines[6]!=d["symbol"] or lines[7]!=d["direction"] or
        lines[7] not in ("BUY","SELL") or
        lines[13:16]!=["CHECK_PASS_NO_SEND","BROKER_NATIVE_EXACT_CHECK_ONLY","ORDER_CHECK_CALLED"] or
        lines[23]!="NO_ORDER_SEND;NO_CRM_CHANGE;NO_BROKER_ACK" or
        not HEX64.fullmatch(lines[4]) or not HEX64.fullmatch(lines[5]) or
        hashlib.sha256(lines[4].encode()).hexdigest()!=d.get("source_decision_sha256") or
        hashlib.sha256(lines[5].encode()).hexdigest()!=d.get("signal_key_sha256") or
        not filename.startswith(lines[5]+"."+raw_sha)):
        raise ValueError("ORIGINAL_NATIVE_IDENTITY_MISMATCH")
    issued=int(lines[12])
    timestamp=dt.datetime.fromisoformat(d["original_native_receipt_utc"].replace("Z","+00:00"))
    if timestamp.tzinfo is None or abs(timestamp.timestamp()-issued)>0.5:
        raise ValueError("ORIGINAL_RECEIPT_TIME_MISMATCH")
    if issued>time.time()+10:
        raise ValueError("FUTURE_NATIVE_TIMESTAMP")
    retcode=int(lines[16])
    if retcode not in (0,10009):
        raise ValueError("ORDER_CHECK_RETCODE_INVALID")
    volume=valid_money(lines[8]);entry=valid_money(lines[9]);sl=valid_money(lines[10])
    tp=valid_money(lines[11]);bid=valid_money(lines[21]);ask=valid_money(lines[22])
    stoploss=float(lines[18]);gain=valid_money(lines[19]);margin=valid_money(lines[20],allow_zero=True)
    if not all(math.isfinite(x) for x in (stoploss,margin)) or stoploss>=0 or gain/(-stoploss)<1.5 or bid>ask:
        raise ValueError("NATIVE_STOP_RR_OR_QUOTES_UNVERIFIED")
    if not (sl<entry<tp if lines[7]=="BUY" else tp<entry<sl):
        raise ValueError("ORDER_PROTECTIVE_STOP_GEOMETRY")
    if d.get("native_order_calc_profit_for_actual_lots_verified") is not True:
        raise ValueError("ACTUAL_LOTS_NATIVE_RISK_UNVERIFIED")
    return {"symbol":lines[6],"side":lines[7],"issued_utc":d["original_native_receipt_utc"],"original_sha256":raw_sha,"historical_only":True}


MINVOLUME_SYMBOLS=(
    "Volatility 10 Index.0",
    "Volatility 25 Index.0",
    "Volatility 50 Index.0",
    "Volatility 75 Index.0",
    "Volatility 100 Index.0",
)

def native_minimum_volume_costs(x):
    """Validate authentic minimum broker lots, margin and adverse test P&L.

    These are small displacement probes at the NATIVE MINIMUM volume.
    They are not necessarily one lot and NEVER authorize an exact sniper
    entry, future commission, real fills or a trailing SL amendment.
    """
    instruments=x.get("instruments")
    if not isinstance(instruments,dict):
        raise ValueError("NATIVE_INSTRUMENTS_NOT_MAPPED")
    result={}
    def number(item,key):
        v=item.get(key)
        if type(v) not in (int,float) or not math.isfinite(v):
            raise ValueError("NATIVE_MINLOT_NUMERIC_INVALID_"+key)
        return float(v)
    for symbol in MINVOLUME_SYMBOLS:
        item=instruments.get(symbol)
        if not isinstance(item,dict):
            raise ValueError("MISSING_NATIVE_BROKER_SYMBOL_"+symbol)
        minv=number(item,"min_lots")
        maxv=number(item,"max_lots")
        step=number(item,"volume_step")
        bid=number(item,"bid")
        ask=number(item,"ask")
        ticks=number(item,"pnl_probe_displacement_ticks")
        ticksize=number(item,"tick_size")
        age=number(item,"tick_age_ms")
        buy=number(item,"buy_probe_native_ordercalcprofit_usd")
        sell=number(item,"sell_probe_native_ordercalcprofit_usd")
        bm=number(item,"buy_minlot_native_ordercalcmargin_usd")
        sm=number(item,"sell_minlot_native_ordercalcmargin_usd")
        if (not 0<step<=minv<=maxv<=100000 or
            abs(minv/step-round(minv/step))>1e-5 or
            bid<=0 or ask<bid or ticks<1 or ticks>100000 or
            ticksize<=0 or age<0 or age>MAX_COST_AGE*1000 or
            buy>=0 or sell>=0 or bm<0 or sm<0):
            raise ValueError("NATIVE_MINIMUM_VOLUME_BROKER_PROBE_NOT_VERIFIED_"+symbol)
        result[symbol]={
            "broker_minimum_volume_lots":minv,
            "broker_volume_step_lots":step,
            "broker_maximum_volume_lots":maxv,
            "broker_sample_tick_age_ms":age,
            "adverse_displacement_ticks":ticks,
            "buy_minimum_volume_adverse_native_profit_usd":buy,
            "sell_minimum_volume_adverse_native_profit_usd":sell,
            "buy_minimum_volume_margin_usd":bm,
            "sell_minimum_volume_margin_usd":sm,
            "native_broker_cost_observed_only":True,
            "one_lot_claim":minv==1.0,
            "exact_new_order_sizing_certified":False,
        }
    return result


def collect(now=None):
    now=time.time() if now is None else float(now)
    d={
       "schema":SCHEMA,
       "checked_at_utc":dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),
       "account_masked":"••••6207","environment":"DEMO","broker":"Deriv",
       "historical_native_no_send_receipts_verified":0,
       "historical_native_no_send_symbols":[],
       "historical_originals_integrity_complete":False,
       "native_v100_one_lot_probe_verified":False,
       "native_minimum_volume_broker_costs_verified":False,
       "native_minimum_volume_by_symbol":{},
       "native_minimum_volume_unverified_reasons":[],
       "native_one_lot_probe_scope":"NOT_VERIFIED",
       "exact_sniper_entry_sizing_certified":False,
       "new_demo_deal_broker_ack_certified":False,
       "trailing_stop_modification_broker_ack_certified":False,
       "current_ordercheck_authority":False,
       "owner_risk_certified":False,
       "orders_submitted":0,"live_modified":False,"crm_modified":False,
       "status":"HISTORICAL_NATIVE_PREFLIGHT_UNVERIFIED",
       "unverified_codes":[]
    }
    try:
        if ARCHIVE.is_symlink() or not ARCHIVE.is_dir():
            raise ValueError("ARCHIVE_DIR_UNTRUSTED")
        st=ARCHIVE.stat()
        if st.st_uid !=0 or st.st_mode&0o077:
            raise ValueError("ARCHIVE_DIR_PERMISSIONS")
        entries=list(ARCHIVE.glob("*.verified.json"))
        if not entries or len(entries)>MAX_RECEIPTS:
            raise ValueError("ARCHIVE_COUNT_INVALID")
        originals=[]
        for meta in entries:
            try:
                raw_meta,_=safe_bytes(meta,10000)
                orig=ARCHIVE/meta.name.replace(".verified.json",".original-mt5.txt")
                raw_original,_=safe_bytes(orig,8000)
                originals.append(validate_original(raw_meta,raw_original,meta.name))
            except (OSError, ValueError, UnicodeError, KeyError, TypeError, OverflowError) as err:
                d["unverified_codes"].append(str(err)[:96] if isinstance(err,ValueError) else type(err).__name__)
        if originals and not d["unverified_codes"]:
            d["historical_originals_integrity_complete"]=True
            d["historical_native_no_send_receipts_verified"]=len(originals)
            d["historical_native_no_send_symbols"]=sorted(set(x["symbol"] for x in originals))
            d["last_historical_preflight_utc"]=max(x["issued_utc"] for x in originals)
            d["status"]="HISTORICAL_NATIVE_NO_SEND_PREFLIGHT_RETAINED"
        else:
            d["status"]="HISTORICAL_ARCHIVE_INCOMPLETE"
    except (OSError,ValueError,TypeError) as err:
        d["unverified_codes"].append(str(err)[:96] if isinstance(err,ValueError) else type(err).__name__)
    try:
        cost,st=safe_bytes(COST,100000)
        if not 0<=now-st.st_mtime<=MAX_COST_AGE:
            raise ValueError("COST_NATIVE_SOURCE_STALE")
        x=json.loads(cost)
        if (x.get("schema")!="abutron.deriv6207.native_ordercalcprofit_costs.readonly.v1" or
            x.get("environment")!="DEMO" or x.get("account_suffix")!="6207" or
            x.get("state")!="VERIFIED_NATIVE_SPREAD_AND_PROFIT_CALC_NO_ORDER_AUTHORITY" or
            x.get("native_ea_sha256")!=EA_SHA or
            x.get("native_bid_ask_and_ordercalcprofit_verified") is not True or
            x.get("native_minlot_ordercalcmargin_verified") is not True or
            x.get("live_authorized") is not False or
            x.get("order_submission_attempted") is not False or
            abs(now-float(x.get("native_sample_utc",0)))>MAX_COST_AGE):
            raise ValueError("NATIVE_COST_IDENTITY_OR_AUTHORITY")
        item=x["instruments"]["Volatility 100 Index.0"]
        if (type(item.get("min_lots")) not in (float,int) or item["min_lots"]!=1 or
            type(item.get("buy_probe_native_ordercalcprofit_usd")) not in (float,int) or
            type(item.get("sell_probe_native_ordercalcprofit_usd")) not in (float,int) or
            item["buy_probe_native_ordercalcprofit_usd"]>=0 or
            item["sell_probe_native_ordercalcprofit_usd"]>=0):
            raise ValueError("NATIVE_V100_MINLOT_PROBE_INVALID")
        d["native_v100_one_lot_probe_verified"]=True
        d["native_v100_source_sha256"]=x["broker_source_sha256"]
        try:
            minimums=native_minimum_volume_costs(x)
            d["native_minimum_volume_by_symbol"]=minimums
            d["native_minimum_volume_broker_costs_verified"]=len(minimums)==5
            d["native_one_lot_probe_scope"]=(
                "FIVE_NATIVE_MINIMUM_VOLUME_BOTH_SIDE_COST_PROBES;NO_EXACT_SNIPER_AUTHORITY")
        except (ValueError,TypeError,KeyError) as minerror:
            d["native_minimum_volume_unverified_reasons"].append(
                str(minerror)[:110])
            d["native_one_lot_probe_scope"]=(
                "V100_ONE_LOT_NATIVE_COST_ONLY;OTHER_SYMBOL_MINIMUMS_UNVERIFIED")
    except (OSError,ValueError,TypeError,KeyError,OverflowError) as err:
        d["unverified_codes"].append(str(err)[:96] if isinstance(err,ValueError) else type(err).__name__)
    return d

def main():
    d=collect()
    OUT.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if OUT.parent.is_symlink() or OUT.parent.stat().st_uid !=0 or OUT.parent.stat().st_mode&0o077:
        raise RuntimeError("OUTPUT_DIR_UNTRUSTED")
    fd,tmp=tempfile.mkstemp(prefix=".evidence-progress-",dir=OUT.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"w") as f:
            json.dump(d,f,sort_keys=True,separators=(",",":"),allow_nan=False)
            f.write("\n");f.flush();os.fsync(f.fileno())
        os.replace(tmp,OUT)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print("NATIVE_DEMO_PREFLIGHT_PROGRESS",d["status"],
          "historical_no_send",d["historical_native_no_send_receipts_verified"],
          "minlot_probe",d["native_v100_one_lot_probe_verified"],
          "orders_sent",0,flush=True)
if __name__=="__main__":
    main()
