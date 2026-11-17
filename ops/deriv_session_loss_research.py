#!/usr/bin/env python3
"""Deriv DEMO broker-confirmed closed-trade entry-hour SHADOW analytics.

Consumes broker-native authenticated deals previously matched by independent
read-only importer to CLOSED_RECONCILED signal ledger rows. No trading APIs.
Never changes production brain, ON/OFF switches, risk policies or orders.
"""
from __future__ import annotations
import datetime as dt
import importlib.util
import json
import math
import pwd
from pathlib import Path
import sqlite3
import stat
from zoneinfo import ZoneInfo

from session_loss_research import hour_profile, atomic_write

ROOT = Path("/var/lib/apex-broker-deal-evidence")
DB = ROOT/"verified_trades.sqlite3"
LATEST = ROOT/"latest.json"
LEDGER = Path("/var/lib/apex-client-feed/execution-ledger.sqlite3")
ACCOUNTS = Path("/var/lib/apex-signup/accounts.sqlite3")
OUTPUT = Path("/var/lib/abutron-v9-deriv-session-loss/latest.json")
SUFFIXES = {"6207", "7593", "9244"}
UTC = dt.timezone.utc
SAST = ZoneInfo("Africa/Johannesburg")

def ensure(condition, reason):
    if not condition:
        raise ValueError(reason)

def trusted_file(path, strict_private=False, allowed_uids=(0,)):
    st=path.lstat()
    ensure(stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_uid in allowed_uids,
           "AUTHENTICATED_DERIV_SOURCE_NOT_ROOT_OWNED")
    ensure(not (st.st_mode&0o022) and (not strict_private or stat.S_IMODE(st.st_mode)==0o600),
           "DERIV_SOURCE_PERMISSIONS_UNTRUSTED")
    return st

def parse_utc(s):
    stamp=dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    ensure(stamp.tzinfo is not None,"NATIVE_TIMEZONE_MISSING")
    return stamp.astimezone(UTC)

def check_deal(row, ledger):
    suffix,signal,position,symbol,side,volume,entry,exited,gross,commission,fee,swap,net = row
    ensure(suffix in SUFFIXES and len(signal)==64
           and all(ch in "0123456789abcdef" for ch in signal)
           and isinstance(position,int) and position>0 and symbol
           and side in ("BUY","SELL") and volume>0
           and isinstance(entry,int) and isinstance(exited,int)
           and entry>0 and entry<=exited,
           "INVALID_NATIVE_DERIV_POSITION")
    native_values=(gross,commission,fee,swap,net)
    ensure(all(isinstance(v,(int,float)) and math.isfinite(v) for v in native_values)
           and abs(round(sum(native_values[:4]),2)-round(net,2))<0.011,
           "NATIVE_DERIV_CASH_COMPONENTS_NOT_RECONCILED")
    linked=ledger.get(signal)
    ensure(linked is not None, "BROKER_CLOSED_SIGNAL_MISSING_FROM_LEDGER")
    broker,environment,lsymbol,lside,lposition,status,created,cid = linked
    ensure(broker=="Deriv" and environment=="DEMO"
           and status=="CLOSED_RECONCILED"
           and lsymbol==symbol and lside==side and lposition==position,
           "BROKER_CLOSE_AND_LEDGER_IDENTITY_MISMATCH")
    entry_utc=dt.datetime.fromtimestamp(entry,UTC)
    ensure(abs((entry_utc-parse_utc(created)).total_seconds())<=10
           and exited<=dt.datetime.now(UTC).timestamp()+2,
           "DERIV_SERVER_TIME_NOT_ALIGNED_WITH_UTC_LEDGER")
    local=entry_utc.astimezone(SAST)
    return dict(account=suffix,symbol=symbol,direction=side,
                date_sast=local.date().isoformat(),hour_sast=local.hour,
                pnl_usd=float(net),signal_key=signal,connection_id=cid)

def load_verified():
    lateststat=trusted_file(LATEST,True)
    latest=json.loads(LATEST.read_text())
    age=(dt.datetime.now(UTC)-parse_utc(latest["created_utc"])).total_seconds()
    ensure(-10<=age<=900 and lateststat.st_size<100000,
           "STALE_DERIV_NATIVE_HISTORY_EVIDENCE")
    ensure(latest.get("schema")=="abutron.apex.deriv_demo.authenticated_deal_history.v1"
           and latest.get("mode")=="READ_ONLY_BROKER_DEAL_IMPORT_NO_MT5_EXECUTION"
           and latest.get("orders_submitted")==0
           and latest.get("trade_switches_modified") is False,
           "DERIV_NATIVE_IMPORT_IDENTITY_NOT_VERIFIED")
    reports=latest.get("accounts") or []
    ensure(len(reports)==3 and {a.get("account_suffix") for a in reports}==SUFFIXES
           and all(a.get("status")=="BROKER_NATIVE_EXPORT_VERIFIED" for a in reports),
           "DERIV_NATIVE_SESSION_ACCOUNT_SCOPE_INCOMPLETE")
    trusted_file(DB,True)
    ensure(ROOT.stat().st_uid==0 and stat.S_IMODE(ROOT.stat().st_mode)==0o700,
           "DERIV_IMPORT_DIR_NOT_PRIVATE")
    # The connection ledger is independently populated from execution receipts.
    trusted_file(LEDGER)
    trusted_file(ACCOUNTS, True, (pwd.getpwnam('apexsignup').pw_uid,))
    with sqlite3.connect("file:"+str(ACCOUNTS)+"?mode=ro",uri=True) as ac:
        current={}
        for cid,account in ac.execute("""SELECT id,account_reference
                    FROM apex_mt5_connections WHERE broker='Deriv' AND environment='DEMO'"""):
            suffix=str(account)[-4:]
            if suffix in SUFFIXES:
                ensure(suffix not in current and len(cid)==32,"AMBIGUOUS_DERIV_ACCOUNT")
                current[suffix]=cid
        ensure(set(current)==SUFFIXES,"DERIV_DEMO_ACCOUNT_CONNECTION_UNAVAILABLE")
    with sqlite3.connect("file:"+str(LEDGER)+"?mode=ro",uri=True) as ledger:
        ledger.execute("PRAGMA query_only=ON")
        ensure(ledger.execute("PRAGMA quick_check").fetchone()[0]=="ok","DERIV_LEDGER_CORRUPT")
        links={}
        for s,broker,environment,symbol,side,pos,state,created,cid in ledger.execute(
                """SELECT signal_key,broker,environment,symbol,side,broker_position,state,
                   created_at_utc,connection_id FROM attempts WHERE state='CLOSED_RECONCILED'"""):
            ensure(s not in links,"DUPLICATE_EXECUTION_LEDGER_SIGNAL")
            links[s]=(broker,environment,symbol,side,pos,state,created,cid)
    with sqlite3.connect("file:"+str(DB)+"?mode=ro",uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        ensure(db.execute("PRAGMA quick_check").fetchone()[0]=="ok",
               "DERIV_BROKER_CLOSE_ARCHIVE_CORRUPT")
        rows=list(db.execute("""SELECT account_suffix,signal_key,broker_position,
             symbol,side,volume,entry_time_server,last_exit_time_server,
             gross_profit_usd,commission_usd,fee_usd,swap_usd,deal_component_net_usd
             FROM broker_closed_trades"""))
    ensure(len(rows)==latest["closed_broker_positions_verified_total"]
           and sum(a["verified_closed_positions"] for a in reports)==len(rows),
           "DERIV_NATIVE_RECONCILED_POSITION_COUNT_MISMATCH")
    clean=[];seen=set()
    for row in rows:
        item=check_deal(row,links)
        ensure(item["connection_id"]==current[item["account"]]
               and (item["account"],row[2]) not in seen,
               "DERIV_POSITION_TO_ACCOUNT_IDENTITY_MISMATCH")
        seen.add((item["account"],row[2]))
        clean.append(item)
    return clean,latest

def analyze(clean):
    import collections
    account_hour=collections.defaultdict(list)
    setup_hour=collections.defaultdict(list)
    for row in clean:
        account_hour[(row["account"],row["hour_sast"])].append(row)
        setup_hour[(row["account"],row["symbol"],row["direction"],row["hour_sast"])].append(row)
    hourly=[{"account":a,"hour_sast":h,**hour_profile(rows)}
            for (a,h),rows in sorted(account_hour.items())]
    setups=[{"account":a,"symbol":s,"direction":side,"hour_sast":h,**hour_profile(rows)}
            for (a,s,side,h),rows in sorted(setup_hour.items()) if len(rows)>=3]
    return {"schema":"abutron.v9.deriv_broker_hour_loss_shadow.v1",
            "generated_utc":dt.datetime.now(UTC).isoformat(),
            "timezone":"Africa/Johannesburg",
            "account_scope":sorted(SUFFIXES),"closed_positions":len(clean),
            "account_hours":hourly,"setup_hours":setups,
            "shadow_hours_flagged":sum(x["shadow_avoid_hour"] for x in hourly),
            "entry_time_utc_ledger_alignment_verified":True,
            "pnl_definition":"BROKER_TRADE_DEAL_COMPONENTS_ONLY_USD",
            "external_nontrade_charges_certified":False,
            "production_veto_enabled":False,"model_promotion_allowed":False,
            "orders_submitted":0,"live_accounts_modified":False}

def main():
    rows,provenance=load_verified()
    report=analyze(rows)
    report["native_source_created_utc"]=provenance["created_utc"]
    atomic_write(OUTPUT,report)
    print("DERIV_CLOSED_TRADE_HOURLY_SHADOW","closed",len(rows),
          "hours",len(report["account_hours"]),"flagged",report["shadow_hours_flagged"],
          "orders",0,"promotion",False,flush=True)

if __name__=="__main__":
    main()