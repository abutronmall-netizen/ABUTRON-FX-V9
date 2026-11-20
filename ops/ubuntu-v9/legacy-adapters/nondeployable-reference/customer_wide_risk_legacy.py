# NONDEPLOYABLE reference — private CRM identities redacted.
#!/usr/bin/python3
"""ABUTRON customer-wide broker exposure inventory (READ-ONLY, FAIL-CLOSED).

Does not authenticate for trading, set effective ON, send any order, or claim
market-closed estimated broker clock equals a fresh tradable quote. Missing
broker position/USD stop risk/day-start/peak snapshots are blockers.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time

CRM=Path("/var/lib/apex-signup/accounts.sqlite3")
PRIVATE=Path("/var/lib/private")
FP_PUBLISHED=Path("/var/lib/apex-signup/fp-native-history")
REPORT=Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json")
APPROVED_FP_V101_SHA="3157811099558f6fd140a11d7b8047cc466b0130043d5a02b441ef095a1007ac"
CAPS={"per_trade_pct":0.15,"aggregate_open_pct":2.0,
      "daily_loss_pct":3.0,"max_drawdown_pct":20.0}

def readfile(p:Path, maxbytes=2000000):
    if p.is_symlink(): raise ValueError("SYMLINK_DENIED")
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or
            before.st_mode&0o022 or before.st_size<2 or before.st_size>maxbytes):
            raise ValueError("FILE_NOT_TRUSTED")
        blob=os.read(fd,maxbytes+1)
        after=os.fstat(fd)
        if (len(blob)!=before.st_size or
            (before.st_ino,before.st_dev,before.st_mtime_ns,before.st_size)!=
            (after.st_ino,after.st_dev,after.st_mtime_ns,after.st_size)):
            raise ValueError("READ_RACE")
        return blob,before
    finally:os.close(fd)

def utc_age(iso,now):
    a=dt.datetime.fromisoformat(iso)
    if a.utcoffset() is None:raise ValueError("NAIVE_CLOCK")
    return now-a.timestamp()

def stop_geometry(pos):
    side=pos["side"]
    s=pos.get("stop_loss")
    t=pos.get("take_profit")
    mark=pos.get("current_price")
    entry=pos.get("open_price")
    lots=pos.get("volume")
    if (side not in ("BUY","SELL") or
        not all(type(v) in (float,int) and math.isfinite(v) and v>0
                for v in (s,t,mark,entry,lots))):
        return "MISSING_OR_INVALID_NATIVE_STOP_TP"
    if side=="BUY" and not (s<mark<t):
        return "STOP_GEOMETRY_BEYOND_LAST_BROKER_MARK"
    if side=="SELL" and not (t<mark<s):
        return "STOP_GEOMETRY_BEYOND_LAST_BROKER_MARK"
    return "LAST_MARK_GEOMETRY_PLAUSIBLE_NOT_USD_RISK_CERTIFIED"

def fp_verified_open_stop_risk(cid,snap,positions,now):
    if not positions:return 0.0
    if cid=="REDACTED_CRM_ID_01":
        raw,st=readfile(Path(
            "/var/lib/abutron-fp-execution/1636/reconciliation/latest.json"),300000)
        doc=json.loads(raw)
        receipts=doc.get("receipt_counts",{})
        risk=doc.get("verified_open_gross_stop_risk_usd")
        if (not -3<=now-st.st_mtime<=110
            or doc.get("broker")!="FP Markets" or doc.get("environment")!="DEMO"
            or doc.get("account_masked")!=snap.get("account_masked")
            or doc.get("history_sha256")!=snap.get("native_file_sha256")
            or doc.get("native_broker_open_risk_verified") is not True
            or doc.get("native_broker_open_risk_method")!="MT5_ORDER_CALC_PROFIT_ENTRY_TO_STOP"
            or doc.get("broker_history_verified") is not True
            or doc.get("bridge_proof_matches_broker") is not True
            or doc.get("verified_open_positions")!=positions
            or doc.get("broker_open_positions")!=positions
            or doc.get("broker_pending_orders")!=0
            or receipts.get("UNKNOWN")!=0 or receipts.get("errors")!=[]
            or doc.get("broker_verified_ack_count")!=receipts.get("ACK")
            or type(risk) not in (int,float) or not math.isfinite(risk)
            or risk<0 or risk>snap["equity_usd"]*.20):
            raise ValueError("FP1636_NATIVE_BROKER_STOP_RISK_NOT_VERIFIED")
        return float(risk)
    if cid=="REDACTED_CRM_ID_02":
        # FP DEMO 2582 is a separate owner account with its own V101 native
        # broker OrderCalcProfit and receipt reconciliation; never borrow
        # the 0512/1636 risk figures or infer that non-flat means zero.
        raw,st=readfile(Path(
            "/var/lib/abutron-fp-execution/2582/reconciliation/latest.json"),300000)
        doc=json.loads(raw)
        receipts=doc.get("receipt_counts",{})
        risk=doc.get("verified_open_gross_stop_risk_usd")
        if (not -3<=now-st.st_mtime<=110
            or doc.get("schema")!="abutron.fpmarkets.execution.native_reconciliation.readonly.v1"
            or doc.get("status")!="RECONCILED_READONLY"
            or doc.get("broker")!="FP Markets" or doc.get("environment")!="DEMO"
            or doc.get("account_masked")!=snap.get("account_masked")
            or doc.get("history_sha256")!=snap.get("native_file_sha256")
            or doc.get("broker_history_verified") is not True
            or doc.get("bridge_proof_matches_broker") is not True
            or doc.get("native_broker_open_risk_verified") is not True
            or doc.get("native_broker_open_risk_method")!="MT5_ORDER_CALC_PROFIT_ENTRY_TO_STOP"
            or doc.get("verified_open_positions")!=positions
            or doc.get("broker_open_positions")!=positions
            or doc.get("broker_pending_orders")!=snap.get("pending_orders_count")
            or doc.get("automatic_order_execution_authorized") is not False
            or receipts.get("UNKNOWN")!=0 or receipts.get("errors")!=[]
            or type(receipts.get("ACK")) is not int
            or doc.get("broker_verified_ack_count")!=receipts["ACK"]
            or type(doc.get("broker_equity_usd")) not in (int,float)
            or abs(doc["broker_equity_usd"]-snap.get("equity_usd",0))>0.05
            or type(risk) not in (int,float) or not math.isfinite(risk)
            or risk<0 or risk>snap["equity_usd"]*0.20):
            raise ValueError("FP2582_NATIVE_BROKER_STOP_RISK_NOT_VERIFIED")
        return float(risk)
    if cid!="REDACTED_CRM_ID_03":
        raise ValueError("FP_OPEN_RISK_RECONCILIATION_ACCOUNT_UNAVAILABLE")
    raw,st=readfile(Path("/var/lib/abutron-fp-execution/reconciliation/latest.json"),300000)
    if not -3<=now-st.st_mtime<=100:raise ValueError("FP_BROKER_RISK_RECONCILIATION_STALE")
    doc=json.loads(raw)
    receipts=doc.get("receipt_counts",{})
    risk=doc.get("verified_open_gross_stop_risk_usd")
    receipt_mismatch_only = (
        doc.get("status")=="BLOCKED"
        and doc.get("blockers")==[
            "FP_NATIVE_ACK_BROKER_POSITION_RECONCILIATION_FAILED"])
    current_broker_risk_checked = (
        doc.get("status")=="RECONCILED_READONLY" or receipt_mismatch_only)
    if (not current_broker_risk_checked
        or doc.get("broker")!="FP Markets" or doc.get("environment")!="DEMO"
        or doc.get("account_masked")!=snap.get("account_masked")
        or doc.get("history_sha256")!=snap.get("native_file_sha256")
        or doc.get("broker_history_verified") is not True
        or doc.get("bridge_proof_matches_broker") is not True
        or doc.get("verified_open_positions")!=positions
        or doc.get("broker_open_positions")!=positions
        or doc.get("broker_pending_orders")!=0
        or receipts.get("UNKNOWN")!=0 or receipts.get("errors")!=[]
        or type(doc.get("broker_verified_ack_count")) is not int
        or type(receipts.get("ACK")) is not int
        or not 0 <= doc["broker_verified_ack_count"] <= receipts["ACK"]
        or doc.get("native_broker_open_risk_verified") is not True
        or doc.get("native_broker_open_risk_method")!="MT5_ORDER_CALC_PROFIT_ENTRY_TO_STOP"
        or doc.get("verified_open_positions")!=positions
        or type(doc.get("broker_equity_usd")) not in (int,float)
        or abs(doc["broker_equity_usd"]-snap["equity_usd"])>0.05
        or type(risk) not in (int,float) or not math.isfinite(risk)
        or risk<0 or risk>snap["equity_usd"]*0.20):
        raise ValueError("FP_NATIVE_PROTECTED_STOP_RISK_UNVERIFIED")
    return float(risk)

def fp_snapshot(cid,login,worker,now):
    home=PRIVATE/("abutron-client-mt5-"+cid)
    binary=home/"terminal/MQL5/Indicators/ABUTRON/ABUTRON-FP-HISTORY-V101-READONLY/AbutronFPAccountHistoryV101.ex5"
    data,_=readfile(binary,100000)
    expected_fp_sha=("3157811099558f6fd140a11d7b8047cc466b0130043d5a02b441ef095a1007ac" if cid=="REDACTED_CRM_ID_01" else APPROVED_FP_V101_SHA)
    if hashlib.sha256(data).hexdigest()!=expected_fp_sha:
        raise ValueError("FP_V101_EX5_HASH_CHANGED")
    nonce=worker.get("nonce")
    if type(nonce) is not str or len(nonce)!=32 or any(c not in "0123456789abcdef" for c in nonce):
        raise ValueError("FP_NONCE_INVALID")
    raw,mtime=readfile(home/"terminal/MQL5/Files"/("ABUTRON_FP_ACCOUNT_HISTORY_V101_"+nonce+".txt"),4000000)
    if not -3<=now-mtime.st_mtime<=135:
        raise ValueError("FP_NATIVE_V101_NOT_FRESH")
    doc,st=readfile(FP_PUBLISHED/(cid+".json"),3000000)
    if not -3<=now-st.st_mtime<=150:
        raise ValueError("FP_PUBLISHER_STALE")
    snap=json.loads(doc)
    # Reader and publisher have independent 60-second schedules. Retain the
    # root-validated published snapshot if the native source has *advanced*
    # since publication; do not pretend the new bytes were part of that proof.
    published_digest=snap.get("native_file_sha256")
    native_digest=hashlib.sha256(raw).hexdigest()
    valid_published_digest=(type(published_digest) is str and
          len(published_digest)==64 and
          all(x in "0123456789abcdef" for x in published_digest))
    advanced_after_publish=(valid_published_digest and
          0<=mtime.st_mtime-st.st_mtime<=130)
    if not (valid_published_digest and
            (published_digest==native_digest or advanced_after_publish)):
        raise ValueError("FP_NATIVE_PUBLISHED_SHA_OR_ROLLOVER_UNVERIFIED")
    if (snap.get("schema")!="abutron.fpmarkets.native.account_history.readonly.v1" or
        snap.get("producer_build")!="FP_HISTORY_V101_READONLY_TIMETRADESERVER_ESTIMATED" or
        snap.get("account_masked")!="••••"+login[-4:] or
        snap.get("environment")!="DEMO" or snap.get("account_currency")!="USD" or
        snap.get("history_verified_from_mt5") is not True or
        snap.get("history_complete_within_selected_window") is not True or
        snap.get("execution_enabled") is not False or
        snap.get("broker_execution_ack_certified") is not False or
        snap.get("customer_portfolio_risk_certified") is not False or
        snap.get("market_quote_fresh_for_execution") is not False or
        snap.get("broker_clock_offset_seconds")!=7200 or
        not -3<=utc_age(snap["generated_at_utc"],now)<=150 or
        not -3<=utc_age(snap["broker_export_at_utc"],now)<=150):
        raise ValueError("FP_VERIFIED_PUBLISHER_IDENTITY_OR_TIME_INVALID")
    p=snap.get("positions")
    o=snap.get("pending_orders")
    if (not isinstance(p,list) or not isinstance(o,list) or
        snap["open_positions_count"]!=len(p) or
        snap["pending_orders_count"]!=len(o)):
        raise ValueError("FP_POSITION_COUNTS_INCONSISTENT")
    positions=[]
    for pos in p:
        geometry=stop_geometry(pos)
        positions.append({"symbol":pos["symbol"],"side":pos["side"],
                         "lots":pos["volume"],"floating_usd":pos["floating_profit_usd"],
                         "stop_geometry":geometry,
                         "broker_native_ordercalcprofit_usd_verified":
                             cid=="REDACTED_CRM_ID_01"})
    receipt=independently_verify_fp_native_receipts(
        cid,login,len(p),len(o),snap["equity_usd"],now,snap)
    native_stop_risk=fp_verified_open_stop_risk(cid,snap,len(p),now)
    return {"broker":"FP Markets","account":"••••"+login[-4:],
            "positions_count":len(p),"pending_orders_count":len(o),
            "deals_90d":snap.get("broker_deals_count"),
            "balance_usd":snap["balance_usd"],"equity_usd":snap["equity_usd"],
            "free_margin_usd":snap["free_margin_usd"],
            "floating_usd":snap["account_floating_profit_usd"],
            "positions":positions,"source":"AUTHENTICATED_V101_READONLY",
            "source_broker_server_clock_estimated":True,
            "source_advanced_since_publisher_read":published_digest!=native_digest,
            "all_position_usd_stop_risk_proved":len(o)==0,
            "open_position_usd_stop_risk":native_stop_risk,
            "historical_broker_receipt_reconciliation":receipt,
            "native_receipt_reconciliation_verified":
                receipt.get("receipt_proof_complete", True),
            "native_current_position_set_and_risk_verified":True,
            "market_quote_fresh_for_trading":False}

def fp_snapshot_coherent(cid,login,worker,now):
    """Retry only transient cross-generation FP evidence races; never relax checks.

    A retry re-reads *all* independent native publisher, receipt and stop-risk
    proof. Permanent invalid identity, stale broker worker or bad binary fails
    immediately. We never treat a previously verified snapshot as current.
    """
    transient={
        "FP_NATIVE_PUBLISHED_SHA_OR_ROLLOVER_UNVERIFIED",
        "FP2582_ACCOUNT_NATIVE_RECEIPT_PROOF_INVALID",
        "FP2582_NATIVE_BROKER_STOP_RISK_NOT_VERIFIED",
        "FP1636_NATIVE_BROKER_RECEIPT_LEDGER_UNVERIFIED",
        "FP1636_NATIVE_BROKER_STOP_RISK_NOT_VERIFIED",
        "FP_0512_INDEPENDENT_NATIVE_RECEIPT_OR_ACCOUNT_MISMATCH",
        "FP_NATIVE_PROTECTED_STOP_RISK_UNVERIFIED",
        "FP1622_ACCOUNT_NATIVE_RECEIPT_PROOF_INVALID",
        "READ_RACE",
    }
    approved_demo_cids={
        "REDACTED_CRM_ID_02",  # FP 2582
        "REDACTED_CRM_ID_01",  # FP 1636
        "REDACTED_CRM_ID_03",  # FP 0512
        "REDACTED_CRM_ID_04",  # FP 1622
    }
    for attempt in range(5):
        try:
            return fp_snapshot(cid,login,worker,time.time() if attempt else now)
        except ValueError as exc:
            if cid not in approved_demo_cids or str(exc) not in transient or attempt==4:
                raise
            time.sleep(0.65)
    raise AssertionError("UNREACHABLE")

FP_DUAL_RECONCILIATION=Path("/var/lib/abutron-fp-execution/dual-account-reconciliation/latest.json")

def independently_verify_fp_native_receipts(cid,login,positions,pending,equity,now,snap=None):
    """A second reader verifies broker-native FP 90d history and ACK ledger.

    Historical broker receipt ACK does not authorize a new execution ACK.
    """
    if cid=="REDACTED_CRM_ID_03" and type(snap)==dict:
        raw,st=readfile(Path("/var/lib/abutron-fp-execution/reconciliation/latest.json"),300000)
        proof=json.loads(raw);receipts=proof.get("receipt_counts",{})
        all_receipts_certified = (
            proof.get("status")=="RECONCILED_READONLY"
            and type(proof.get("broker_verified_ack_count")) is int
            and proof.get("broker_verified_ack_count")==receipts.get("ACK"))
        ack_backlog_only = (
            proof.get("status")=="BLOCKED"
            and proof.get("blockers")==[
                "FP_NATIVE_ACK_BROKER_POSITION_RECONCILIATION_FAILED"])
        if (not -3<=now-st.st_mtime<=110
            or not (all_receipts_certified or ack_backlog_only)
            or proof.get("account_masked")!="••••"+str(login)[-4:]
            or proof.get("history_sha256")!=snap.get("native_file_sha256")
            or proof.get("broker_history_verified") is not True
            or proof.get("bridge_proof_matches_broker") is not True
            or proof.get("broker_open_positions")!=positions
            or proof.get("broker_pending_orders")!=pending
            or type(proof.get("broker_equity_usd")) not in (float,int)
            or abs(proof["broker_equity_usd"]-equity)>0.01
            or receipts.get("UNKNOWN")!=0 or receipts.get("errors")!=[]
            or type(receipts.get("ACK")) is not int
            or type(proof.get("broker_verified_ack_count")) is not int
            or not 0<=proof["broker_verified_ack_count"]<=receipts["ACK"]
            or proof.get("native_broker_open_risk_verified") is not True
            or proof.get("native_broker_open_risk_method")!="MT5_ORDER_CALC_PROFIT_ENTRY_TO_STOP"
            or proof.get("verified_open_positions")!=positions):
            raise ValueError("FP_0512_INDEPENDENT_NATIVE_RECEIPT_OR_ACCOUNT_MISMATCH")
        return {"historical_broker_ack_count":proof["broker_verified_ack_count"],
                "ack_labeled_receipts":receipts["ACK"],
                "90day_broker_trade_deals":proof["broker_trade_deals_90d"],
                "unresolved_historical_receipts":
                    receipts["ACK"]-proof["broker_verified_ack_count"],
                "receipt_proof_complete":all_receipts_certified,
                "current_execution_ack_granted":False}
    if cid=="REDACTED_CRM_ID_01" and type(snap)==dict:
        raw,st=readfile(Path(
            "/var/lib/abutron-fp-execution/1636/reconciliation/latest.json"),300000)
        proof=json.loads(raw);receipts=proof.get("receipt_counts",{})
        if (not -3<=now-st.st_mtime<=110
            or proof.get("account_masked")!="••••"+str(login)[-4:]
            or proof.get("broker_history_verified") is not True
            or proof.get("native_broker_open_risk_verified") is not True
            or proof.get("history_sha256")!=snap.get("native_file_sha256")
            or proof.get("broker_open_positions")!=positions
            or proof.get("broker_pending_orders")!=pending
            or type(proof.get("broker_equity_usd")) not in (float,int)
            or abs(proof["broker_equity_usd"]-equity)>0.05
            or receipts.get("UNKNOWN")!=0 or receipts.get("errors")!=[]
            or proof.get("broker_verified_ack_count")!=receipts.get("ACK")):
            raise ValueError("FP1636_NATIVE_BROKER_RECEIPT_LEDGER_UNVERIFIED")
        return {"historical_broker_ack_count":receipts["ACK"],
                "90day_broker_trade_deals":proof["broker_trade_deals_90d"],
                "unresolved_historical_receipts":0,
                "broker_origin_unattributed_position_ids":
                    proof.get("broker_origin_unattributed_position_ids",[]),
                "current_execution_ack_granted":False}
    if cid=="REDACTED_CRM_ID_02" and type(snap) is dict:
        # FP2582's independent native receipt ledger, not FP0512/1636 dual ledger.
        raw,st=readfile(Path("/var/lib/abutron-fp-execution/2582/reconciliation/latest.json"),300000)
        proof=json.loads(raw); receipts=proof.get("receipt_counts",{})
        if (not -3<=now-st.st_mtime<=110
            or proof.get("schema")!="abutron.fpmarkets.execution.native_reconciliation.readonly.v1"
            or proof.get("status")!="RECONCILED_READONLY"
            or proof.get("automatic_order_execution_authorized") is not False
            or proof.get("orders_submitted")!=0
            or proof.get("broker")!="FP Markets" or proof.get("environment")!="DEMO"
            or proof.get("account_masked")!=snap.get("account_masked")
            or proof.get("history_sha256")!=snap.get("native_file_sha256")
            or proof.get("broker_history_verified") is not True
            or proof.get("native_broker_open_risk_verified") is not True
            or proof.get("bridge_proof_matches_broker") is not True
            or proof.get("broker_open_positions")!=positions
            or proof.get("verified_open_positions")!=positions
            or proof.get("broker_pending_orders")!=pending
            or proof.get("broker_origin_unattributed_position_ids")!=[]
            or type(proof.get("broker_equity_usd")) not in (int,float)
            or abs(proof["broker_equity_usd"]-equity)>0.05
            or receipts.get("UNKNOWN")!=0 or receipts.get("errors")!=[]
            or type(receipts.get("ACK")) is not int
            or proof.get("broker_verified_ack_count")!=receipts["ACK"]):
            raise ValueError("FP2582_ACCOUNT_NATIVE_RECEIPT_PROOF_INVALID")
        return {"historical_broker_ack_count":receipts["ACK"],
                "90day_broker_trade_deals":proof["broker_trade_deals_90d"],
                "unresolved_historical_receipts":0,
                "current_execution_ack_granted":False}
    if cid=="REDACTED_CRM_ID_04" and type(snap) is dict:
        # Independent FP1622 V101 native broker receipt ledger; never treat
        # an unrelated 0512/1636 dual report as this account's proof.
        raw,st=readfile(Path("/var/lib/abutron-fp-execution/1622/reconciliation/latest.json"),300000)
        proof=json.loads(raw);receipts=proof.get("receipt_counts",{})
        if (not -3<=now-st.st_mtime<=110
            or proof.get("schema")!="abutron.fpmarkets.execution.native_reconciliation.readonly.v1"
            or proof.get("status")!="RECONCILED_READONLY"
            or proof.get("automatic_order_execution_authorized") is not False
            or proof.get("orders_submitted")!=0
            or proof.get("broker")!="FP Markets" or proof.get("environment")!="DEMO"
            or proof.get("account_masked")!=snap.get("account_masked")
            or proof.get("history_sha256")!=snap.get("native_file_sha256")
            or proof.get("broker_history_verified") is not True
            or proof.get("native_broker_open_risk_verified") is not True
            or proof.get("bridge_proof_matches_broker") is not True
            or proof.get("broker_open_positions")!=positions
            or proof.get("verified_open_positions")!=positions
            or proof.get("broker_pending_orders")!=pending
            or proof.get("broker_origin_unattributed_position_ids")!=[]
            or type(proof.get("broker_equity_usd")) not in (int,float)
            or abs(proof["broker_equity_usd"]-equity)>0.05
            or receipts.get("UNKNOWN")!=0 or receipts.get("errors")!=[]
            or type(receipts.get("ACK")) is not int
            or proof.get("broker_verified_ack_count")!=receipts["ACK"]):
            raise ValueError("FP1622_ACCOUNT_NATIVE_RECEIPT_PROOF_INVALID")
        return {"historical_broker_ack_count":receipts["ACK"],
                "90day_broker_trade_deals":proof["broker_trade_deals_90d"],
                "unresolved_historical_receipts":0,
                "current_execution_ack_granted":False}
    blob,st=readfile(FP_DUAL_RECONCILIATION,300000)
    if not -3<=now-st.st_mtime<=145:
        raise ValueError("FP_DUAL_ACCOUNT_RECEIPT_REPORT_STALE")
    d=json.loads(blob)
    if (d.get("schema")!="abutron.fpmarkets.dual_demo_native_reconciliation.readonly.v1"
        or d.get("state")!="RECONCILED_READONLY"
        or d.get("broker_order_authorized") is not False
        or d.get("orders_submitted_by_job")!=0
        or d.get("client_switches_modified") is not False
        or not -3<=utc_age(d.get("at_utc",""),now)<=145):
        raise ValueError("FP_DUAL_BROKER_RECEIPT_CONTRACT_INVALID")
    accounts=d.get("accounts")
    if not isinstance(accounts,list) or len(accounts)!=2:
        raise ValueError("FP_DUAL_ACCOUNT_COUNT_INVALID")
    matches=[x for x in accounts if x.get("connection_suffix")==cid[-6:]
             and x.get("account_suffix")==str(login)[-4:]]
    if len(matches)!=1:
        raise ValueError("FP_INDEPENDENT_ACCOUNT_LINK_NOT_UNIQUE")
    account=matches[0]
    if (account.get("state")!="RECONCILED_READONLY"
        or account.get("connected") is not True
        or (account.get("effective_on") is True and login[-4:]!="0512")
        or account.get("unresolved_receipts")!=0
        or type(account.get("historical_broker_ack")) is not int
        or account["historical_broker_ack"]<0
        or account.get("positions")!=positions or account.get("pending")!=pending
        or type(account.get("equity_usd")) not in (int,float)
        or abs(account["equity_usd"]-equity)>0.01):
        raise ValueError("FP_NATIVE_HISTORY_POSITION_EQUITY_OR_RECEIPT_DIVERGENCE")
    return {"historical_broker_ack_count":account["historical_broker_ack"],
            "90day_broker_trade_deals":account["historical_trade_deals"],
            "unresolved_historical_receipts":0,
            "current_execution_ack_granted":False}

def verified_current_flat_exposure(rec):
    """Account-bound native exposure: flat or multi-position protected DEMO."""
    snap=rec.get("snapshot",{})
    if type(snap)!=dict or rec.get("connection_status")!="CONNECTED":
        return False
    if snap.get("pending_orders_count")!=0:
        return False
    if snap.get("positions_count")!=0:
        if rec.get("broker")=="FP Markets" and rec.get("environment")=="DEMO":
            risk=snap.get("open_position_usd_stop_risk")
            equity=snap.get("equity_usd")
            return (rec.get("broker_position_reconciliation_status")=="POSITIONS_COUNT_VERIFIED_READONLY"
                    and snap.get("native_current_position_set_and_risk_verified") is True
                    and snap.get("all_position_usd_stop_risk_proved") is True
                    and type(risk) in (int,float) and type(equity) in (int,float)
                    and equity>0 and 0<=risk<=equity*0.20)
        return (rec.get("broker")=="Deriv" and rec.get("environment")=="DEMO"
                and rec.get("broker_position_reconciliation_status")=="PROTECTED_NATIVE_DUAL_VERIFIED"
                and type(snap.get("positions_count")) is int
                and 0<snap["positions_count"]<=13
                and snap.get("all_current_position_counts_verified") is True
                and snap.get("all_current_positions_sl_tp_usd_risk_verified") is True
                and type(snap.get("open_position_usd_stop_risk")) in (int,float)
                and type(snap.get("equity_usd")) in (int,float)
                and snap["equity_usd"]>0
                and 0<=snap["open_position_usd_stop_risk"]<=
                    snap["equity_usd"]*min(0.02,0.0015*snap["positions_count"])+0.02)
    if rec.get("broker")=="Deriv" and rec.get("environment")=="DEMO":
        return (rec.get("broker_position_reconciliation_status")=="ZERO_EXPOSURE_DUAL_NATIVE_VERIFIED"
                and snap.get("all_current_position_counts_verified") is True
                and snap.get("all_current_positions_sl_tp_usd_risk_verified") is True
                and snap.get("open_position_usd_stop_risk")==0.0)
    if rec.get("broker")=="FP Markets" and rec.get("environment")=="DEMO":
        return (rec.get("broker_position_reconciliation_status")=="POSITIONS_COUNT_VERIFIED_READONLY"
                and snap.get("native_receipt_reconciliation_verified") is True
                and snap.get("all_position_usd_stop_risk_proved") is True)
    if rec.get("broker")=="FP Markets" and rec.get("environment")=="LIVE":
        return ((rec.get("account_masked"),rec.get("broker_position_reconciliation_status")) in (
                    ("••••6364","SIGNED_FP_LIVE_6364_NO_ORDER_FLAT_PROOF"),
                    ("••••9392","SIGNED_FP_LIVE_9392_NO_ORDER_FLAT_PROOF"))
                and snap.get("positions_count")==0
                and snap.get("pending_orders_count")==0
                and snap.get("positions_count_verified_from_live_broker") is True
                and snap.get("broker_history_verified") is True
                and snap.get("open_position_usd_stop_risk")==0.0
                and snap.get("account_execution_authorized") is False
                and snap.get("broker_trade_allowed") is False)
    if rec.get("broker")=="Deriv" and rec.get("environment")=="LIVE":
        return (rec.get("broker_position_reconciliation_status")=="SIGNED_LIVE_NO_ORDER_POSITION_COUNTS_VERIFIED"
                and snap.get("positions_count_verified_from_live_broker") is True
                and snap.get("account_execution_authorized") is False
                and snap.get("broker_trade_allowed") is False)
    return False

def fp_live6364_verified_flat_snapshot(cid,login,worker,now):
    """Strict independently authenticated LIVE position inventory, NO order authority.

    The signed LIVE history sidecar is mandatory; a GUI screenshot, stale
    broker prices, or a CRM OFF switch is never proof that an account is flat.
    """
    if (cid!="REDACTED_CRM_ID_05" or
        str(login)!="1234" or worker.get("id")!=cid or
        worker.get("broker")!="FP Markets" or worker.get("mode")!="LIVE" or
        worker.get("server")!="FPMarketsSC-Live" or
        worker.get("status")!="CONNECTED" or worker.get("heartbeat_fresh") is not True):
        raise ValueError("FP_LIVE_6364_IDENTITY_NOT_VERIFIED")
    import importlib.util
    source=Path("/opt/apex-client-mt5/fpmarkets-history/fp_live_6364_native_history_ro.py")
    if source.is_symlink() or not source.is_file():
        raise ValueError("FP_LIVE_6364_HISTORY_PARSER_MISSING")
    spec=importlib.util.spec_from_file_location("abutron_fp6364_native_readonly_current",str(source))
    if spec is None or spec.loader is None:
        raise ValueError("FP_LIVE_6364_HISTORY_PARSER_UNAVAILABLE")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native=module.current(now=now)
    if (not isinstance(native,dict) or
        native.get("schema")!="abutron.fp6364.live_history.readonly.v1" or
        native.get("status")!="BROKER_HISTORY_VERIFIED" or
        native.get("account")!="1234" or native.get("currency")!="USD" or
        native.get("live_execution_authorized") is not False or
        native.get("risk_calculation_verified") is not False or
        not isinstance(native.get("open_positions"),list) or
        not isinstance(native.get("pending_orders"),list) or
        type(native.get("snapshot_timestamp")) is not int or
        not -15<=now-native["snapshot_timestamp"]<=140):
        raise ValueError("FP_LIVE_6364_NATIVE_REPORT_UNTRUSTED")
    if native["open_positions"] or native["pending_orders"]:
        raise ValueError("FP_LIVE_6364_OPEN_EXPOSURE_REQUIRES_BROKER_NATIVE_STOP_RISK")
    equity=native.get("equity_usd")
    if type(equity) not in (float,int) or not math.isfinite(equity) or equity<0:
        raise ValueError("FP_LIVE_6364_EQUITY_NOT_VERIFIED")
    return {"broker":"FP Markets", "account":"••••6364", "environment":"LIVE",
            "positions_count":0,"pending_orders_count":0,
            "equity_usd":float(equity),"balance_usd":native.get("balance_usd"),
            "open_position_usd_stop_risk":0.0,
            "positions_count_verified_from_live_broker":True,
            "account_execution_authorized":False,
            "broker_trade_allowed":False,
            "source":"FP6364_AUTHENTICATED_READONLY_NATIVE_CURRENT_HISTORY",
            "source_sha256":native.get("source_sha256"),
            "broker_history_verified":True,
            "market_quote_fresh_for_execution":False,
            "customer_portfolio_risk_certified":False}


def fp_live9392_verified_flat_snapshot(cid,login,worker,now):
    """Strict independently authenticated LIVE position inventory, NO order authority.

    The signed LIVE history sidecar is mandatory; a GUI screenshot, stale
    broker prices, or a CRM OFF switch is never proof that an account is flat.
    """
    if (cid!="REDACTED_CRM_ID_06" or
        str(login)!="84419392" or worker.get("id")!=cid or
        worker.get("broker")!="FP Markets" or worker.get("mode")!="LIVE" or
        worker.get("server")!="FPMarketsSC-Live" or
        worker.get("status")!="CONNECTED" or worker.get("heartbeat_fresh") is not True):
        raise ValueError("FP_LIVE_9392_IDENTITY_NOT_VERIFIED")
    import importlib.util
    source=Path("/opt/apex-client-mt5/fpmarkets-history/fp_live_9392_native_history_ro.py")
    if source.is_symlink() or not source.is_file():
        raise ValueError("FP_LIVE_9392_HISTORY_PARSER_MISSING")
    spec=importlib.util.spec_from_file_location("abutron_fp9392_native_readonly_current",str(source))
    if spec is None or spec.loader is None:
        raise ValueError("FP_LIVE_9392_HISTORY_PARSER_UNAVAILABLE")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native=module.current(now=now)
    if (not isinstance(native,dict) or
        native.get("schema")!="abutron.fp9392.live_history.readonly.v1" or
        native.get("status")!="BROKER_HISTORY_VERIFIED" or
        native.get("account")!="84419392" or native.get("currency")!="USD" or
        native.get("live_execution_authorized") is not False or
        native.get("risk_calculation_verified") is not False or
        not isinstance(native.get("open_positions"),list) or
        not isinstance(native.get("pending_orders"),list) or
        type(native.get("snapshot_timestamp")) is not int or
        not -15<=now-native["snapshot_timestamp"]<=140):
        raise ValueError("FP_LIVE_9392_NATIVE_REPORT_UNTRUSTED")
    if native["open_positions"] or native["pending_orders"]:
        raise ValueError("FP_LIVE_9392_OPEN_EXPOSURE_REQUIRES_BROKER_NATIVE_STOP_RISK")
    equity=native.get("equity_usd")
    if type(equity) not in (float,int) or not math.isfinite(equity) or equity<0:
        raise ValueError("FP_LIVE_9392_EQUITY_NOT_VERIFIED")
    return {"broker":"FP Markets", "account":"••••9392", "environment":"LIVE",
            "positions_count":0,"pending_orders_count":0,
            "equity_usd":float(equity),"balance_usd":native.get("balance_usd"),
            "open_position_usd_stop_risk":0.0,
            "positions_count_verified_from_live_broker":True,
            "account_execution_authorized":False,
            "broker_trade_allowed":False,
            "source":"FP9392_AUTHENTICATED_READONLY_NATIVE_CURRENT_HISTORY",
            "source_sha256":native.get("source_sha256"),
            "broker_history_verified":True,
            "market_quote_fresh_for_execution":False,
            "customer_portfolio_risk_certified":False}


DERIV_NATIVE_RECONCILIATION=Path("/var/lib/abutron-deriv-multi-native-reconciliation/latest.json")

def deriv_demo_snapshot(cid,login,worker,now):
    """Independent triple-DEMO sidecars; inventory only, never order authority."""
    if (worker.get("id")!=cid or worker.get("broker")!="Deriv" or
        worker.get("mode")!="DEMO" or worker.get("server")!="Deriv-Demo" or
        worker.get("status")!="CONNECTED" or worker.get("heartbeat_fresh") is not True):
        raise ValueError("DERIV_DEMO_WORKER_IDENTITY_INVALID")
    raw,st=readfile(DERIV_NATIVE_RECONCILIATION,500000)
    if not -3<=now-st.st_mtime<=120:
        raise ValueError("DERIV_NATIVE_RECONCILIATION_STALE")
    d=json.loads(raw)
    if (d.get("schema")!="abutron.deriv.three_demo.native_dualproof.readonly.v1" or
        d.get("execution_authorized") is not False or
        d.get("customer_wide_risk_certified") is not False or
        d.get("complete_historical_reconciliation") is not False or
        d.get("orders_submitted")!=0 or
        not -3<=utc_age(d.get("generated_utc",""),now)<=120):
        raise ValueError("DERIV_NATIVE_ROOT_SOURCE_CONTRACT_INVALID")
    accounts=d.get("accounts")
    if not isinstance(accounts,list) or len(accounts)!=3:
        raise ValueError("DERIV_NATIVE_DEMO_ACCOUNT_COUNT_INVALID")
    suffixes=[a.get("account_suffix") for a in accounts]
    if sorted(suffixes)!=["6207","7593","9244"]:
        raise ValueError("DERIV_NATIVE_DEMO_ACCOUNT_IDENTITIES_INVALID")
    # Verify the requested account on its own native dual-session proof.
    # Other Deriv DEMO accounts are checked independently by inspect().
    # A transient failure of 7593/9244 must not erase verified 6207 risk.
    suffix=str(login)[-4:]
    hits=[a for a in accounts if a.get("account_suffix")==suffix]
    if len(hits)!=1 or not str(login).isdigit():
        raise ValueError("DERIV_NATIVE_ACCOUNT_SUFFIX_MISMATCH")
    a=hits[0]
    if (a.get("state")!="VERIFIED_READONLY" or
        a.get("current_exposure") not in
        ("ZERO_DUAL_NATIVE_PROOF","PROTECTED_NATIVE_POSITIONS_DUAL_PROOF") or
        a.get("broker_execution_authorized") is not False or
        type(a.get("positions")) is not int or a["positions"]<0 or
        a.get("pending_orders")!=0 or
        (a["positions"]>0 and
         (a.get("current_exposure")!="PROTECTED_NATIVE_POSITIONS_DUAL_PROOF"
          or a.get("all_current_positions_sl_tp_usd_risk_verified") is not True)) or
        a.get("window_complete") is not False or
        type(a.get("matched_position_ids")) is not int or
        a["matched_position_ids"]<=0 or
        not all(type(a.get(k)) in (int,float) and
                math.isfinite(a[k]) and a[k]>=0
                for k in ("balance_usd","equity_usd"))):
        raise ValueError("DERIV_NATIVE_DEMO_PROOF_INCOMPLETE")
    snap = {
        "broker":"Deriv","account":"••••"+suffix,"environment":"DEMO",
        "positions_count":a["positions"],"pending_orders_count":0,
        "balance_usd":a["balance_usd"],"equity_usd":a["equity_usd"],
        "open_position_usd_stop_risk":a.get("open_position_usd_stop_risk",0.0),
        "source":"AUTHENTICATED_INDEPENDENT_DUAL_NATIVE_DEMO_PROOF",
        "source_sha256":hashlib.sha256(raw).hexdigest(),
        "matched_position_ids":a["matched_position_ids"],
        "native_deals_exported":a["native_deals_in_export_window"],
        "all_current_position_counts_verified":True,
        "all_current_positions_sl_tp_usd_risk_verified":True,
        "full_historical_exit_ledger_certified":False,
        "account_equity_daystart_and_peak_certified":False,
        "broker_execution_ack_certified":False,
        "market_quote_fresh_for_execution":False,
        "execution_authorized":False}

    # Issue38 is a supplemental signed/independent no-order exposure proof.
    # It never authorizes entries, substitutes for lifetime history, or clears
    # the pre-existing owner-risk blockers. A stale/error QA report is visible.
    if suffix == "6207":
        try:
            qa_raw, qa_st = readfile(Path(
                "/var/lib/abutron-issue38-exposure-readonly/latest.json"), 10000)
            qa = json.loads(qa_raw)
            verified = (
                0 <= now - qa_st.st_mtime <= 130
                and qa.get("schema") == "abutron.issue38.deriv6207.signed_native_exposure_reconcile.readonly.v1"
                and qa.get("status") == "SIGNED_NATIVE_EXPOSURE_RECONCILED_READONLY"
                and qa.get("signature_verified") is True
                and qa.get("dual_native_crosscheck_verified") is True
                and qa.get("mode") == "DEMO"
                and qa.get("broker") == "Deriv"
                and qa.get("account_masked") == "••••6207"
                and qa.get("native_position_count") == a["positions"]
                and qa.get("trading_authorized") is False
                and qa.get("customer_wide_risk_certified") is False
                and qa.get("new_order_authority_changed") is False
                and qa.get("live_accounts_changed") is False
                and type(qa.get("native_equity_usd")) in (int, float)
                and type(qa.get("native_balance_usd")) in (int, float)
                and abs(qa["native_equity_usd"] - a["equity_usd"]) <= 0.05
                and abs(qa["native_balance_usd"] - a["balance_usd"]) <= 0.05
                and type(qa.get("signed_snapshot_sha256")) is str
                and len(qa["signed_snapshot_sha256"]) == 64
                and -3 <= utc_age(qa.get("checked_utc", ""), now) <= 130
            )
            if not verified:
                raise ValueError("ISSUE38_NOT_FRESH_OR_CROSSCHECK_UNVERIFIED")
            snap["issue38_signed_exposure_probe"] = {
                "status": "SIGNED_NATIVE_EXPOSURE_RECONCILED_READONLY",
                "signature_verified": True,
                "dual_native_crosscheck_verified": True,
                "signed_snapshot_sha256": qa["signed_snapshot_sha256"],
                "sample_age_seconds": qa.get("sample_age_seconds"),
                "checked_utc": qa["checked_utc"],
                "trading_authorized": False,
                "customer_wide_risk_certified": False}
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            snap["issue38_signed_exposure_probe"] = {
                "status": "NOT_VERIFIED",
                "signature_verified": False,
                "dual_native_crosscheck_verified": False,
                "trading_authorized": False,
                "customer_wide_risk_certified": False}
    return snap

def inspect(now=None):
    now=time.time() if now is None else float(now)
    with sqlite3.connect("file:"+str(CRM)+"?mode=ro",uri=True,timeout=6) as db:
        db.execute("PRAGMA query_only=ON")
        rows=db.execute("""
            SELECT x.id,x.account_id,x.account_reference,x.broker,x.environment,
                   x.status,coalesce(t.desired_enabled,0),coalesce(t.effective_enabled,0)
            FROM apex_mt5_connections x LEFT JOIN apex_trade_controls t
              ON t.connection_id=x.id ORDER BY x.account_id,x.broker,x.id
        """).fetchall()
    owners={}
    total_known_positions=0
    unknown_connected_exposure=0
    stop_geometry_bad=0
    connected_count=0
    for cid,owner,login,broker,environment,status,desired,effective in rows:
        owner_label=hashlib.sha256(str(owner).encode()).hexdigest()[:12]
        cust=owners.setdefault(owner_label,{"connections":[],"known_fp_positions":0,
                   "unknown_connected_broker_position_sets":0,
                   "disconnected_or_error_accounts":0,
                   "all_broker_equity_daystart_peak_reconciled":False,
                   "risk_certified":False})
        rec={"id_suffix":cid[-6:],"broker":broker,"environment":environment,
             "account_masked":"••••"+str(login)[-4:],
             "connection_status":status,"requested_on":desired==1,
             "effective_on":effective==1,
             "broker_position_reconciliation_status":"UNKNOWN",
             "risk_verified":False}
        if status!="CONNECTED":
            rec["blockers"]=["DISCONNECTED_OR_ERROR_ACCOUNT_EXPOSURE_UNKNOWN"]
            cust["disconnected_or_error_accounts"]+=1
        else:
            connected_count+=1
            try:
                worker,st=readfile(PRIVATE/("abutron-client-mt5-"+cid)/"worker-health.json")
                w=json.loads(worker)
                if (w.get("id")!=cid or w.get("status")!="CONNECTED" or
                    w.get("heartbeat_fresh") is not True or
                    not -3<=now-st.st_mtime<=30):
                    raise ValueError("BROKER_WORKER_NOT_CURRENT")
                if broker=="FP Markets" and environment=="DEMO":
                    snap=fp_snapshot_coherent(cid,str(login),w,now)
                    rec.update({"broker_position_reconciliation_status":"POSITIONS_COUNT_VERIFIED_READONLY",
                                "snapshot":snap})
                    cust["known_fp_positions"]+=snap["positions_count"]
                    total_known_positions+=snap["positions_count"]
                    stop_geometry_bad+=sum(p["stop_geometry"]!="LAST_MARK_GEOMETRY_PLAUSIBLE_NOT_USD_RISK_CERTIFIED"
                                          for p in snap["positions"])
                    rec["blockers"]=["ACCOUNT_EQUITY_DAYSTART_PEAK_UNVERIFIED",
                                    "NATIVE_BROKER_ACK_ORDER_RECONCILIATION_PENDING"]
                    rec["entry_blockers"]=["FP_QUOTE_CLOCK_OR_EXECUTABLE_COST_UNCERTIFIED"]
                    if (snap["positions_count"] or snap["pending_orders_count"]) and (
                            snap.get("all_position_usd_stop_risk_proved") is not True):
                        rec["blockers"] += ["NATIVE_ORDER_CALC_PROFIT_PER_POSITION_MISSING"]
                    if (snap.get("historical_broker_receipt_reconciliation", {})
                            .get("unresolved_historical_receipts",0)>0):
                        rec["blockers"].append(
                            "COMPLETE_BROKER_HISTORY_EXIT_LEDGER_UNVERIFIED")
                    # Zero currently open positions need no hypothetical USD stop
                    # calculation. Entry quote freshness is a separate guard.
                    if any(p["stop_geometry"]!="LAST_MARK_GEOMETRY_PLAUSIBLE_NOT_USD_RISK_CERTIFIED"
                           for p in snap["positions"]):
                        rec["blockers"].append("FP_STOP_BEYOND_LAST_BROKER_MARK")
                elif broker=="Deriv" and environment=="DEMO":
                    snap=deriv_demo_snapshot(cid,str(login),w,now)
                    rec.update({"broker_position_reconciliation_status":
                                ("ZERO_EXPOSURE_DUAL_NATIVE_VERIFIED" if snap["positions_count"]==0
                                 else "PROTECTED_NATIVE_DUAL_VERIFIED"),
                                "snapshot":snap})
                    cust["known_deriv_demo_positions"]=cust.get("known_deriv_demo_positions",0)+snap["positions_count"]
                    rec["blockers"]=["ACCOUNT_EQUITY_DAYSTART_PEAK_UNVERIFIED",
                                    "COMPLETE_BROKER_HISTORY_EXIT_LEDGER_UNVERIFIED",
                                    "NATIVE_BROKER_ACK_ORDER_RECONCILIATION_PENDING"]
                elif broker=="FP Markets" and environment=="LIVE" and cid=="REDACTED_CRM_ID_06":
                    snap=fp_live9392_verified_flat_snapshot(cid,str(login),w,now)
                    rec.update({"broker_position_reconciliation_status":
                                "SIGNED_FP_LIVE_9392_NO_ORDER_FLAT_PROOF",
                                "snapshot":snap})
                    cust["known_live_positions"]=cust.get("known_live_positions",0)
                    rec["blockers"]=["ACCOUNT_EQUITY_DAYSTART_PEAK_UNVERIFIED",
                                     "NATIVE_BROKER_ACK_ORDER_RECONCILIATION_PENDING"]
                    rec["entry_blockers"]=["LIVE_AUTOMATIC_EXECUTION_FORBIDDEN",
                                            "CUSTOMER_PORTFOLIO_RISK_UNVERIFIED"]
                elif broker=="FP Markets" and environment=="LIVE" and cid=="REDACTED_CRM_ID_05":
                    snap=fp_live6364_verified_flat_snapshot(cid,str(login),w,now)
                    rec.update({"broker_position_reconciliation_status":
                                "SIGNED_FP_LIVE_6364_NO_ORDER_FLAT_PROOF",
                                "snapshot":snap})
                    cust["known_live_positions"]=cust.get("known_live_positions",0)
                    rec["blockers"]=["ACCOUNT_EQUITY_DAYSTART_PEAK_UNVERIFIED",
                                     "NATIVE_BROKER_ACK_ORDER_RECONCILIATION_PENDING"]
                    rec["entry_blockers"]=["LIVE_AUTOMATIC_EXECUTION_FORBIDDEN",
                                            "CUSTOMER_PORTFOLIO_RISK_UNVERIFIED"]
                elif broker=="Deriv" and environment=="LIVE":
                    # Signed no-order LIVE MT5 proof. Inventory ONLY: never authorizes
                    # LIVE trading, day-start/peak loss, or customer-wide risk.
                    from abutron_deriv_live_snapshot_ro import native_live_snapshot
                    snap=native_live_snapshot(cid,str(login),w,now,readfile)
                    rec.update({"broker_position_reconciliation_status":
                                "SIGNED_LIVE_NO_ORDER_POSITION_COUNTS_VERIFIED",
                                "snapshot":snap})
                    cust["known_live_positions"]=cust.get("known_live_positions",0)+snap["positions_count"]
                    rec["blockers"]=["ACCOUNT_EQUITY_DAYSTART_PEAK_UNVERIFIED",
                                    "NATIVE_BROKER_ACK_ORDER_RECONCILIATION_PENDING"]
                    rec["entry_blockers"]=["LIVE_BROKER_TRADE_PERMISSION_OFF",
                                            "LIVE_EQUITY_ZERO"]
                    if snap["positions_count"] or snap["pending_orders_count"]:
                        rec["blockers"].append("LIVE_NATIVE_POSITION_USD_STOP_RISK_UNCERTIFIED")
                else:
                    unknown_connected_exposure+=1
                    cust["unknown_connected_broker_position_sets"]+=1
                    rec["blockers"]=["COMPLETE_BROKER_OPEN_POSITIONS_AND_PENDING_ORDERS_MISSING",
                                    "ACCOUNT_EQUITY_DAYSTART_PEAK_UNVERIFIED",
                                    "NATIVE_BROKER_ACK_ORDER_RECONCILIATION_PENDING"]
            except (OSError,ValueError,TypeError,KeyError,IndexError,UnicodeError) as exc:
                # Keep the actual source failure visible in this existing report.
                rec["native_snapshot_error"]=(type(exc).__name__+":"+str(exc))[:160]
                unknown_connected_exposure+=1
                cust["unknown_connected_broker_position_sets"]+=1
                rec["blockers"]=["AUTHENTICATED_BROKER_POSITION_SNAPSHOT_MISSING"]
        if effective==1 and not (
            desired==1 and environment=="DEMO" and status=="CONNECTED"
            and broker in ("Deriv","FP Markets")
            and verified_current_flat_exposure(rec)):
            rec.setdefault("blockers",[]).append("UNEXPECTED_EFFECTIVE_ON")
        certified_flat=verified_current_flat_exposure(rec)
        rec["current_open_exposure_reconciled"]=certified_flat
        rec["current_open_stop_risk_usd"]=(
            float(rec.get("snapshot",{}).get("open_position_usd_stop_risk",0.0))
            if certified_flat else None)
        cust["connections"].append(rec)
    # This is CURRENT observable open-position risk only; never represent
    # it as complete day-start/peak equity, historic trades or execution ACK.
    for owner in owners.values():
        flags=[a["current_open_exposure_reconciled"] for a in owner["connections"]]
        owner["current_open_exposure_reconciled"]=bool(flags) and all(flags)
        owner["current_open_stop_risk_usd"]=(
            sum(float(a["current_open_stop_risk_usd"]) for a in owner["connections"])
            if owner["current_open_exposure_reconciled"] else None)
    # Global completion follows the actual CRM roster, not a fixed six-account count.
    all_flat=(bool(rows) and len(rows)==connected_count
              and unknown_connected_exposure==0 and
              all(a["current_open_exposure_reconciled"]
                  for owner in owners.values() for a in owner["connections"]))
    blocked=sorted(set(b for cust in owners.values() for rec in cust["connections"]
                       for b in rec.get("blockers",[])))
    if not rows:blocked.append("NO_CUSTOMER_CONNECTIONS")
    return {"schema":"abutron.customer_wide.native_exposure.fail_closed.v1",
           "checked_utc":dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),
           "status":"BLOCKED_INCOMPLETE_EXPOSURE",
           "customer_owner_groups":len(owners),"linked_accounts":len(rows),
           "connected_accounts":connected_count,
           "connected_fp_position_sets_verified":sum(
              r["broker_position_reconciliation_status"]=="POSITIONS_COUNT_VERIFIED_READONLY"
              for c in owners.values() for r in c["connections"]),
           "confirmed_fp_open_positions":total_known_positions,
           "open_positions_stop_beyond_last_mark":stop_geometry_bad,
           "unknown_connected_position_sets":unknown_connected_exposure,
           "native_current_open_exposure_reconciled":all_flat,
           "native_current_open_stop_risk_usd":(
               sum(c["current_open_stop_risk_usd"] for c in owners.values())
               if all_flat else None),
           "native_position_sets_verified":sum(a["current_open_exposure_reconciled"]
                                                for c in owners.values()
                                                for a in c["connections"]),
           "native_flat_accounts_verified":sum(a["current_open_exposure_reconciled"]
                                                and a.get("snapshot",{}).get("positions_count")==0
                                                for c in owners.values()
                                                for a in c["connections"]),
           "current_open_exposure_vs_full_drawdown_risk_distinguished":True,
           # OBSERVATION and historical audit are separate from per-order execution.
           # The installed Deriv 6207 dispatcher independently requires current
           # broker exposure, live equity, CRM lease, owner caps, and a fresh
           # same-signal native OrderCheck. Do NOT use this read-only report to
           # grant execution, infer missing peaks, or override LIVE OFF.
           "current_exposure_reconciliation_status":(
               "CURRENT_NATIVE_EXPOSURE_VERIFIED" if all_flat
               else "CURRENT_NATIVE_EXPOSURE_UNVERIFIED"),
           "historical_audit_status":"INCOMPLETE_BROKER_LIFETIME_EQUITY_ACK_PROOF",
           "complete_historical_certification_is_6207_pretrade_veto":False,
           "fresh_same_signal_native_ordercheck_required_for_6207":True,
           "historical_audit_confers_trading_authority":False,
           "current_exposure_reconciliation_confers_trading_authority":False,
           "blockers":blocked,"owners":owners,"risk_caps_policy":CAPS,
           "portfolio_cash_usd_stop_risk_fully_quantified":False,
           "day_start_peak_drawdown_from_native_broker_certified":False,
           "customer_wide_risk_certified":False,
           "authenticated_broker_execution_ack_certified":False,
           "v4_handover_authorized":False,"live_authorized":False,
           "orders_submitted_by_audit":0,"client_switches_modified":False}

def main():
    doc=inspect()
    parent=REPORT.parent
    if not parent.is_dir() or parent.is_symlink() or parent.stat().st_uid!=0 or parent.stat().st_mode&0o077:
        raise SystemExit("UNTRUSTED_PRIVATE_EVIDENCE_DIRECTORY")
    temp=parent/(".customer-exposure-evidence.%d.tmp"%os.getpid())
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,"w") as f:
            json.dump(doc,f,sort_keys=True,separators=(",",":"),allow_nan=False)
            f.write("\n");f.flush();os.fsync(f.fileno())
        os.replace(temp,REPORT)
    finally:
        try:temp.unlink()
        except FileNotFoundError:pass
    print(json.dumps({"status":doc["status"],"owners":doc["customer_owner_groups"],
                      "linked":doc["linked_accounts"],"connected":doc["connected_accounts"],
                      "fp_native_positions":doc["confirmed_fp_open_positions"],
                      "stop_geometry_alerts":doc["open_positions_stop_beyond_last_mark"],
                      "unknown_connected_position_sets":doc["unknown_connected_position_sets"],
                      "broker_execution_ack_certified":False,"trading_authorized":False}))
if __name__=="__main__":main()
