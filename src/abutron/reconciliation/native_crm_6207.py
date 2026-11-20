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
                           server: str, secret: str, ttl: int = 70,
                           source_file: Path | None = None):
    root=Path(files_dir)
    paths=([source_file] if source_file is not None else
           sorted(root.glob("ABUTRON_NATIVE_EXPOSURE_V2_ABUTRON_NATIVE_*.txt"),
                  key=lambda p:p.stat().st_mtime,reverse=True))
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


def broker_position_from_native_deal(receipt: dict, candidate: dict,
                                     history_root: Path = Path(
    "/var/lib/abutron-v9-eight-account-learning/owners")) -> int | None:
    """Bind a native entry deal to its POSITION, not OrderSend's order ticket.

    The owner-isolated, immutable archive independently verifies broker-native
    history. An ACK order ticket is not necessarily a MT5 position identifier.
    """
    deal_id=str(receipt.get("broker_deal_id") or "")
    if not deal_id.isdecimal() or int(deal_id)<=0:
        return None
    paths=list(history_root.glob("*/6207/native-trade-history.sqlite3"))
    if len(paths)!=1:
        return None
    path=paths[0]
    st=path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode & 0o022:
        return None
    summary=json.loads(_source(path.parent/"latest.json").decode("utf-8"))
    if (summary.get("broker")!="Deriv" or
        summary.get("environment")!="DEMO" or
        not str(summary.get("account_masked","")).endswith("6207")):
        return None
    with sqlite3.connect("file:"+str(path)+"?mode=ro",uri=True) as db:
        row=db.execute("SELECT body,sha256 FROM deals WHERE ticket=?",
                       (deal_id,)).fetchone()
    if not row or hashlib.sha256(row[0].encode()).hexdigest()!=row[1]:
        return None
    deal=json.loads(row[0])
    try:
        observed=dt.datetime.fromisoformat(
            str(receipt["received_at"]).replace("Z","+00:00")).timestamp()
        native_time=float(deal["time_utc"])
        position=int(deal["position"])
        native_lots=float(deal["volume"])
        receipt_lots=float(receipt["volume"])
    except (KeyError,ValueError,TypeError,OverflowError):
        return None
    if (position<=0 or abs(native_time-observed)>90 or
        abs(native_lots-receipt_lots)>1e-5 or
        str(deal.get("ticket"))!=deal_id or
        deal.get("kind")!="TRADE" or deal.get("entry")!="IN" or
        deal.get("symbol")!=candidate["symbol"] or
        deal.get("side")!=candidate["direction"]):
        return None
    return position


def historical_signed_protection(files_dir: str, *, cid: str, login: int,
                                 server: str, secret: str, receipt: dict,
                                 candidate: dict, tick_size: float,
                                 position: int):
    """Find an HMAC-authenticated protected position shortly after native fill."""
    try:
        entry_ts=dt.datetime.fromisoformat(
            str(receipt["received_at"]).replace("Z","+00:00")).timestamp()
    except (KeyError,ValueError,TypeError):
        return None
    root=Path(files_dir)
    prefix="ABUTRON_NATIVE_EXPOSURE_V2_ABUTRON_NATIVE_"+str(login)+"_"
    # A ten-minute post-entry window captures native protection, including
    # positions that have since closed; it does not infer current exposure.
    for path in sorted(root.glob(prefix+"*.txt")):
        try:
            stamp=int(path.stem[len(prefix):])
        except ValueError:
            continue
        if not entry_ts<=stamp<=entry_ts+600:
            continue
        try:
            exposure,source_sha=signed_native_exposure(
                files_dir,cid=cid,login=login,server=server,secret=secret,
                ttl=1_000_000_000,source_file=path)
            matched=match_protective_order(
                {"ticket":position,"state":"ACK"},
                candidate,exposure,tick_size)
        except (OSError,ValueError,KeyError,TypeError):
            continue
        if matched.get("status")=="BROKER_PROTECTED_SLTP":
            return matched,source_sha
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
        if (receipt.get("reconciled_state")=="PROTECTED" and
            receipt.get("broker_native_ordercalcprofit_verified") and
            float(receipt.get("broker_native_stop_risk_usd") or 0)>0 and
            int(receipt.get("native_position_ticket") or 0)>0):
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
        # MT5 native OrderSend.order is not necessarily POSITION_TICKET.
        # Resolve through authenticated native DEAL_ENTRY_IN history when
        # available, then use HMAC position evidence (current or at entry).
        position=broker_position_from_native_deal(receipt,candidate)
        native_position=position or receipt["native_ticket"]
        status=match_protective_order(
            {"ticket":native_position,"state":"ACK"},
            candidate,exposure,tick)
        proof_sha=source_sha
        if status.get("status")!="BROKER_PROTECTED_SLTP" and position:
            historical=historical_signed_protection(
                files_dir,cid=cid,login=login,server=server,secret=secret,
                receipt=receipt,candidate=candidate,tick_size=tick,
                position=position)
            if historical:
                status,proof_sha=historical
        if status.get("status")!="BROKER_PROTECTED_SLTP":
            result["unresolved"]+=1
            continue
        record_reconciled_crm(
            crm_db,request_id=candidate["candidate_id"],
            connection_id=cid,symbol=symbol,
            position=status,source_sha=proof_sha)
        updates={"reconciled_state":"PROTECTED",
                 "broker_native_stop_risk_usd":float(status["native_stop_risk_usd"]),
                 "broker_native_ordercalcprofit_verified":True,
                 "native_position_ticket":int(status["ticket"])}
        if delayed:
            updates["execution_state"]="ACK"
            updates["native_ticket"]=int(receipt["native_ticket"])
            engine_db.set_candidate_state(
                receipt["candidate_id"],"ACK","DELAYED_NATIVE_BROKER_PROOF")
        engine_db.update_receipt(receipt["receipt_id"],**updates)
        result["protected"]+=1
    return result