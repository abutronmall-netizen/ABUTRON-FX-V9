"""ABUTRON Issue #44 — compare two broker-native, read-only MT5 snapshots.

Used by an ISOLATED FP Markets DEMO terminal, not the production worker.
A successful comparison is broker-account continuity evidence across the
isolated terminal restart, NOT certification of a production rollout.

Never imports trading libraries, submits orders, reveals native account IDs,
changes risk settings or touches CRM.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path

SCHEMA="ABUTRON_FP_RESTART_SNAPSHOT_V103"
HEADER=20
MAX_DEALS=10000
MAX_POSITIONS=500
MAX_ORDERS=500

class NativeRestartEvidenceError(ValueError):
    pass

def _number(value,positive=False):
    if not isinstance(value,str) or len(value)>40:
        raise NativeRestartEvidenceError("INVALID_NATIVE_NUMBER_FIELD")
    x=float(value)
    if not math.isfinite(x) or (positive and x<=0):
        raise NativeRestartEvidenceError("INVALID_NATIVE_NUMBER_VALUE")
    return x

def _integer(value,positive=False):
    if (not isinstance(value,str) or len(value)>20 or not value.isdecimal()
        or (positive and int(value)<=0)):
        raise NativeRestartEvidenceError("INVALID_NATIVE_INTEGER")
    return int(value)

def parse_native(raw:bytes):
    if not isinstance(raw,bytes) or not 100<len(raw)<3_000_000:
        raise NativeRestartEvidenceError("NATIVE_SNAPSHOT_BYTES_OUT_OF_BOUNDS")
    try:
        lines=raw.decode("utf-8-sig").splitlines()
    except UnicodeError as e:
        raise NativeRestartEvidenceError("NATIVE_SNAPSHOT_NOT_UTF8") from e
    if len(lines)<HEADER+1 or lines[0]!=SCHEMA:
        raise NativeRestartEvidenceError("NATIVE_SCHEMA_MISSING")
    nonce,owner,server,login,mode=lines[1:6]
    if (len(nonce)!=32 or any(ch not in "0123456789abcdef" for ch in nonce)
        or len(owner)!=36 or server!="FPMarketsSC-Demo"
        or not login.isdecimal() or mode!="DEMO"
        or lines[8]!="USD" or lines[19]!="BROKER_NATIVE_NO_ORDER_FUNCTIONS"):
        raise NativeRestartEvidenceError("BROKER_DEMO_IDENTITY_INVALID")
    utc=_integer(lines[6],positive=True)
    offset=_integer(lines[7],positive=True)
    if not 7170<=offset<=7230:
        raise NativeRestartEvidenceError("BROKER_CLOCK_OFFSET_INVALID")
    account=[_number(x) for x in lines[9:14]]
    npos=_integer(lines[14])
    norders=_integer(lines[15])
    ndeals=_integer(lines[16])
    start=_integer(lines[17])
    end=_integer(lines[18],positive=True)
    if (npos>MAX_POSITIONS or norders>MAX_ORDERS or
        ndeals>MAX_DEALS or start!=0 or end<=utc or end-utc>7400):
        raise NativeRestartEvidenceError("NATIVE_HISTORY_OR_POSITION_LIMIT_INVALID")
    positions={}
    orders={}
    deals=[]
    final=[]
    for row in lines[HEADER:]:
        cols=row.split("\t")
        if cols[0]=="END":
            final=cols
            break
        if cols[0]=="P":
            if len(cols)!=13:
                raise NativeRestartEvidenceError("MALFORMED_POSITION_ROW")
            ticket=_integer(cols[1],True)
            if ticket in positions:
                raise NativeRestartEvidenceError("DUPLICATED_BROKER_POSITION")
            side=_integer(cols[3])
            if side not in (0,1):
                raise NativeRestartEvidenceError("UNEXPECTED_POSITION_SIDE")
            nums=[_number(x) for x in cols[4:11]]
            if nums[0]<=0 or any(x<=0 for x in nums[1:5]):
                raise NativeRestartEvidenceError("NATIVE_POSITION_NOT_PROTECTED")
            if (side==0 and not nums[3]<nums[4]) or (side==1 and not nums[4]<nums[3]):
                raise NativeRestartEvidenceError("POSITION_STOP_TP_GEOMETRY_INVALID")
            positions[ticket]={
                "symbol":cols[2],"side":side,"volume":nums[0],
                "entry":nums[1],"mark":nums[2],"sl":nums[3],"tp":nums[4],
                "profit":nums[5],"swap":nums[6],
                "opened":_integer(cols[11],True),
                "modified":_integer(cols[12],True)}
        elif cols[0]=="O":
            if len(cols)!=11:
                raise NativeRestartEvidenceError("MALFORMED_BROKER_PENDING_ORDER")
            ticket=_integer(cols[1],True)
            if ticket in orders:
                raise NativeRestartEvidenceError("DUPLICATE_BROKER_ORDER")
            orders[ticket]=cols
        elif cols[0]=="D":
            if len(cols)!=15:
                raise NativeRestartEvidenceError("MALFORMED_HISTORICAL_DEAL")
            _integer(cols[1],True)
            deals.append({
                "position_id":_integer(cols[3]),
                "occurred":_integer(cols[4],True),
                "entry":_integer(cols[6]),
                "symbol":cols[8],
                "volume":_number(cols[9])})
        else:
            raise NativeRestartEvidenceError("UNKNOWN_NATIVE_RECORD_TYPE")
    if final!=["END",str(npos),str(norders),str(ndeals)]:
        raise NativeRestartEvidenceError("TRUNCATED_NATIVE_FINAL_FENCE")
    if len(positions)!=npos or len(orders)!=norders or len(deals)!=ndeals:
        raise NativeRestartEvidenceError("NATIVE_SOURCE_RECORD_COUNT_MISMATCH")
    if lines[-1]!="\t".join(final):
        raise NativeRestartEvidenceError("EXTRA_RECORDS_AFTER_END")
    return {
        "session":(nonce,owner,server,login,mode),
        "utc":utc,"broker_utc_offset_seconds":offset,"account":account,
        "positions":positions,"orders":orders,"deals":deals,
        "source_sha256":sha256(raw).hexdigest(),
    }

def compare(before,after):
    if before["session"]!=after["session"]:
        raise NativeRestartEvidenceError("ISOLATED_RESTART_ACCOUNT_SESSION_MISMATCH")
    if after["utc"]<=before["utc"] or after["utc"]-before["utc"]>900:
        raise NativeRestartEvidenceError("ISOLATED_RESTART_TIME_SEPARATION_INVALID")
    if before["orders"] or after["orders"]:
        raise NativeRestartEvidenceError("PENDING_BROKER_ORDERS_CANNOT_CERTIFY_RESTART")
    initial=before["positions"]
    final=after["positions"]
    overlap=set(initial)&set(final)
    missing=set(initial)-set(final)
    new=set(final)-set(initial)
    closed=0
    for ticket in missing:
        p=initial[ticket]
        exits=[d for d in after["deals"] if d["position_id"]==ticket
               and d["symbol"]==p["symbol"]
               and d["entry"] in (1,2,3) and
               d["occurred"]-after["broker_utc_offset_seconds"]>=before["utc"]-10]
        if not exits:
            raise NativeRestartEvidenceError("MISSING_POSITION_WITHOUT_AUTHENTIC_NATIVE_EXIT")
        closed+=1
    for ticket in overlap:
        p=initial[ticket]
        q=final[ticket]
        if (p["symbol"]!=q["symbol"] or p["side"]!=q["side"] or
            abs(p["entry"]-q["entry"])>0.000001 or
            q["volume"]>p["volume"]+0.000001):
            raise NativeRestartEvidenceError("RECONNECTED_POSITION_IDENTITY_CHANGED")
        if abs(p["tp"]-q["tp"])>0.000001:
            raise NativeRestartEvidenceError("RECONNECTED_TAKE_PROFIT_MUTATED")
        if (p["side"]==0 and q["sl"]<p["sl"]-0.000001) or (
            p["side"]==1 and q["sl"]>p["sl"]+0.000001):
            raise NativeRestartEvidenceError("RECONNECTED_STOP_WIDENED")
    return {
        "schema":"abutron.issue44.isolated_native_position_restart_continuity.v1",
        "status":"ISOLATED_BROKER_POSITION_CONTINUITY_OBSERVED",
        "snapshot_before_sha256":before["source_sha256"],
        "snapshot_after_sha256":after["source_sha256"],
        "snapshot_gap_seconds":after["utc"]-before["utc"],
        "before_positions":len(initial),"after_positions":len(final),
        "positions_confirmed_after_restart":len(overlap),
        "positions_closed_with_native_deal_evidence":closed,
        "positions_opened_during_restart":len(new),
        "before_pending_orders":0,"after_pending_orders":0,
        "two_independent_broker_native_snapshots_compared":True,
        "same_profile_restart_certified":False,
        "running_production_MT5_restarted":False,
        "broker_sltp_amendment_sent_by_test":False,
        "production_release_certified":False,
        "owner_risk_historical_certified":False,
        "live_trading_enabled_by_test":False,
    }

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--before",required=True)
    p.add_argument("--after",required=True)
    p.add_argument("--output",required=True)
    args=p.parse_args()
    result=compare(parse_native(Path(args.before).read_bytes()),
                   parse_native(Path(args.after).read_bytes()))
    result["evaluated_at_utc"]=datetime.now(timezone.utc).isoformat()
    out=Path(args.output)
    # Only root-private paths may receive sensitive native metadata.
    if out.parent.is_symlink() or out.parent.stat().st_mode&0o077:
        raise NativeRestartEvidenceError("ROOT_PRIVATE_REPORT_DESTINATION_REQUIRED")
    with out.open("x",encoding="utf-8") as f:
        json.dump(result,f,indent=2,sort_keys=True)
        f.write("\n")
    print(result["status"],"continuing_positions",
          result["positions_confirmed_after_restart"])
if __name__=="__main__":
    main()
