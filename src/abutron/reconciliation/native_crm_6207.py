"""Broker-native HMAC position/SL/TP/trailing confirmation into existing CRM.

No fabricated ACK, no trade submission, no LIVE modification. A successful
OrderSend is a proposal to reconcile, not independent proof of protection.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import math
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any


class NativeCRMReconcileError(ValueError):
    pass


def _source(p: Path, maxbytes=1000000) -> bytes:
    if p.is_symlink() or not p.is_file():
        raise NativeCRMReconcileError("NATIVE_SOURCE_NOT_REGULAR")
    st=p.stat()
    if st.st_size > maxbytes or st.st_size < 8 or st.st_mode & 0o022:
        raise NativeCRMReconcileError("NATIVE_SOURCE_NOT_TRUSTED")
    return p.read_bytes()


def signed_native_exposure(files_dir: str, *, cid: str, login: int,
                           server: str, secret: str, ttl: int = 70):
    root=Path(files_dir)
    paths=sorted(root.glob("ABUTRON_NATIVE_EXPOSURE_V2_ABUTRON_NATIVE_*.txt"),
                 key=lambda p:p.stat().st_mtime,reverse=True)
    if not paths:
        raise NativeCRMReconcileError("BROKER_NATIVE_SIGNED_EXPOSURE_MISSING")
    p=paths[0]
    if not 0<=time.time()-p.stat().st_mtime<=ttl:
        raise NativeCRMReconcileError("BROKER_NATIVE_SIGNED_EXPOSURE_STALE")
    raw=_source(p)
    j=json.loads(raw.decode("utf-8-sig"))
    if (j.get("connection_id")!=cid or str(j.get("login"))!=str(login)
            or j.get("server")!=server or j.get("currency")!="USD"):
        raise NativeCRMReconcileError("BROKER_NATIVE_ACCOUNT_IDENTITY_MISMATCH")
    if not isinstance(j.get("positions"),list):
        raise NativeCRMReconcileError("BROKER_NATIVE_POSITIONS_INVALID")
    if j.get("position_count")!=len(j["positions"]):
        raise NativeCRMReconcileError("BROKER_NATIVE_POSITIONS_PARTIAL")
    nonce=str(j.get("nonce",""))
    prefix="ABUTRON_NATIVE_"+str(login)+"_"
    if not nonce.startswith(prefix):
        raise NativeCRMReconcileError("BROKER_NONCE_ACCOUNT_INVALID")
    try:
        age=time.time()-int(nonce[len(prefix):])
    except (ValueError,TypeError):
        raise NativeCRMReconcileError("BROKER_NONCE_TIMESTAMP_INVALID") from None
    if not 0<=age<=ttl:
        raise NativeCRMReconcileError("BROKER_NONCE_STALE")
    tickets=[]
    parts=[]
    for pos in j["positions"]:
        ticket=pos.get("ticket")
        if type(ticket)!=int or ticket<=0 or ticket in tickets:
            raise NativeCRMReconcileError("BROKER_POSITION_TICKET_INVALID")
        tickets.append(ticket)
        if pos.get("direction") not in ("BUY","SELL"):
            raise NativeCRMReconcileError("BROKER_POSITION_DIRECTION_INVALID")
        verified=pos.get("ordercalcprofit_verified") is True
        for key in ("volume","entry","sl","tp","stop_risk_usd"):
            if (type(pos.get(key)) not in (float,int) or
                    not math.isfinite(pos[key]) or pos[key]<0):
                raise NativeCRMReconcileError("BROKER_NATIVE_POSITION_GEOMETRY_INVALID")
        parts.append(f"{ticket}:{pos['symbol']}:{pos['direction']}:"
                     f"{pos['volume']:.2f}:{pos['entry']:.5f}:"
                     f"{pos['sl']:.5f}:{pos['tp']:.5f}:"
                     f"{pos['stop_risk_usd']:.2f}:"
                     f"{'true' if verified else 'false'}")
    if tickets!=sorted(tickets):
        raise NativeCRMReconcileError("BROKER_POSITION_ORDER_INVALID")
    canonical=(f"{cid}|{login}|{server}|USD|{j['equity']:.2f}|"
               f"{j['balance']:.2f}|{nonce}|{j['challenge']}|"
               f"{j['position_count']}|{';'.join(parts)}")
    expected=hmac.new(secret.encode(),canonical.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,str(j.get("sig",""))):
        raise NativeCRMReconcileError("BROKER_NATIVE_EXPOSURE_HMAC_INVALID")
    return j,hashlib.sha256(raw).hexdigest()


def match_protective_order(receipt: dict, expected: dict,
                           exposure: dict, tick_size: float) -> dict:
    """Only a broker-authenticated open position with exact protective stops counts."""
    ticket=int(receipt.get("ticket") or 0)
    if ticket<=0 or receipt.get("state")!="ACK":
        raise NativeCRMReconcileError("NATIVE_RECEIPT_NOT_BROKER_ACK")
    candidates=[p for p in exposure["positions"]
                if p["ticket"]==ticket and p["symbol"]==expected["symbol"]
                and p["direction"]==expected["direction"]]
    if len(candidates)!=1:
        return {"status":"BROKER_POSITION_PROOF_PENDING"}
    pos=candidates[0]
    tolerance=float(tick_size)*1.05
    sl=float(expected["stop_loss"])
    tp=float(expected["take_profit"])
    if (not pos.get("ordercalcprofit_verified") or
            pos["sl"]<=0 or pos["tp"]<=0 or
            abs(pos["tp"]-tp)>tolerance):
        return {"status":"BROKER_PROTECTION_NOT_CONFIRMED"}
    # A genuine TRAILING update may tighten the stop, never widen it.
    direction=expected["direction"]
    if ((direction=="BUY" and pos["sl"]<sl-tolerance) or
            (direction=="SELL" and pos["sl"]>sl+tolerance)):
        return {"status":"BROKER_STOP_WIDENED"}
    if abs(pos["volume"]-float(expected["volume"]))>1e-5:
        return {"status":"BROKER_VOLUME_MISMATCH"}
    return {"status":"BROKER_PROTECTED_SLTP",
            "ticket":ticket,"native_sl":pos["sl"],
            "native_tp":pos["tp"],"native_stop_risk_usd":pos["stop_risk_usd"]}


def record_reconciled_crm(crm_db: str, *, request_id: str,
                          connection_id: str, symbol: str,
                          position: dict, source_sha: str) -> None:
    if position.get("status")!="BROKER_PROTECTED_SLTP":
        raise NativeCRMReconcileError("DO_NOT_WRITE_UNPROTECTED_RECEIPT")
    with sqlite3.connect(crm_db,timeout=10) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS abutron_v9_native_execution_receipts
            (request_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
             symbol TEXT NOT NULL, ticket INTEGER NOT NULL,
             broker_sl REAL NOT NULL, broker_tp REAL NOT NULL,
             broker_stop_risk_usd REAL NOT NULL, source_sha256 TEXT NOT NULL,
             state TEXT NOT NULL, reconciled_utc TEXT NOT NULL)""")
        db.execute("""INSERT INTO abutron_v9_native_execution_receipts
            (request_id,connection_id,symbol,ticket,broker_sl,broker_tp,
             broker_stop_risk_usd,source_sha256,state,reconciled_utc)
             VALUES(?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(request_id) DO UPDATE SET
             broker_sl=excluded.broker_sl,broker_tp=excluded.broker_tp,
             broker_stop_risk_usd=excluded.broker_stop_risk_usd,
             source_sha256=excluded.source_sha256,state=excluded.state,
             reconciled_utc=excluded.reconciled_utc""",
            (request_id,connection_id,symbol,position["ticket"],
             position["native_sl"],position["native_tp"],
             position["native_stop_risk_usd"],source_sha,
             position["status"],dt.datetime.now(dt.timezone.utc).isoformat()))


def recover_native_delayed_receipt(files_dir: str, *, request_id: str,
                                   login: int) -> dict | None:
    """Find matching native EA receipt after a no-retry transport timeout.

    The receipt alone does not certify an order; independent signed native
    position proof remains mandatory before CRM marks it protected.
    """
    if not request_id.startswith("ABUTRON_REQ_") or len(request_id) > 90:
        return None
    directory=Path(files_dir)
    for path in sorted(directory.glob("ABUTRON_NATIVE_RECEIPTS_V2_*.txt"),
                       key=lambda entry:entry.stat().st_mtime,reverse=True)[:120]:
        try:
            raw=_source(path,maxbytes=8192)
            j=json.loads(raw.decode("utf-8-sig"))
        except (OSError,UnicodeError,ValueError,TypeError):
            continue
        if j.get("request_id")!=request_id:
            continue
        if str(j.get("login",""))!=str(login):
            return None
        if j.get("state") in ("ACK","REJECTED"):
            return j
        return None
    return None


def reconcile_engine_receipts(*, engine_db: Any, crm_db: str,
                              cid: str, login: int, server: str,
                              files_dir: str, tick_sizes: dict) -> dict:
    """Consume already-stored V9 ACKs; write CRM only on signed MT5 positions."""
    from ..execution.writer_gateway import SingleWriterGateway
    secret=SingleWriterGateway.read_existing_secret(files_dir)
    exposure,source_sha=signed_native_exposure(
        files_dir,cid=cid,login=login,server=server,secret=secret)
    result={"protected":0,"unresolved":0,"unknown":0,"scanned":0}
    for receipt in engine_db.list_receipts(cid,limit=500):
        if receipt.get("reconciled_state")=="PROTECTED":
            continue
        candidate=engine_db.get_candidate(receipt["candidate_id"])
        if receipt["execution_state"]=="UNKNOWN":
            reason=str(receipt.get("reason_code") or "")
            native_id=(reason.split(";",1)[0][len("REQUEST_ID="):]
                       if reason.startswith("REQUEST_ID=") else "")
            recovered=recover_native_delayed_receipt(
                files_dir,request_id=native_id,login=login)
            if recovered is None:
                result["unknown"]+=1
                continue
            if recovered.get("state")=="REJECTED":
                engine_db.update_receipt(
                    receipt["receipt_id"],reconciled_state="REJECTED",
                    reason_code=str(recovered.get("reason","NATIVE_REJECTED"))[:128])
                engine_db.set_candidate_state(
                    receipt["candidate_id"],"REJECTED","NATIVE_BROKER_REJECTED")
                result["unresolved"]+=1
                continue
            receipt=dict(receipt,native_ticket=recovered.get("ticket",0))
            if not int(receipt["native_ticket"] or 0)>0:
                result["unknown"]+=1
                continue
            delayed=True
        elif receipt["execution_state"]!="ACK":
            continue
        else:
            delayed=False
        result["scanned"]+=1
        if candidate is None:
            result["unresolved"]+=1
            continue
        symbol=candidate["symbol"]
        tick=float(tick_sizes.get(symbol,0.0))
        if not 0 < tick < 100000:
            result["unresolved"]+=1
            continue
        status=match_protective_order(
            {"ticket":receipt["native_ticket"],"state":"ACK"},
            candidate,exposure,tick)
        if status.get("status")!="BROKER_PROTECTED_SLTP":
            result["unresolved"]+=1
            continue
        record_reconciled_crm(
            crm_db,request_id=candidate["candidate_id"],
            connection_id=cid,symbol=symbol,
            position=status,source_sha=source_sha)
        updates={"reconciled_state":"PROTECTED"}
        if delayed:
            updates["execution_state"]="ACK"
            updates["native_ticket"]=int(receipt["native_ticket"])
            engine_db.set_candidate_state(
                receipt["candidate_id"],"ACK","DELAYED_NATIVE_BROKER_PROOF")
        engine_db.update_receipt(receipt["receipt_id"],**updates)
        result["protected"]+=1
    return result
