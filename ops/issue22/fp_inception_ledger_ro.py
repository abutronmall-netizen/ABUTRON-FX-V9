"""Issue #22 — read-only broker-native account-inception ledger reconciliation.

Inputs:
- Original, unchanged bytes from an account/session-bound MQL5
  HistorySelect(0) read-only QA snapshot.
- Fresh independently authenticated broker-native history export.
- The dated original account-opening notice metadata.

This is an ACCOUNT-level accounting cross-check, NOT fleet-wide owner-risk
certification, not historical intraminute equity peak reconstruction, not
broker execution ACK verification and never a trading authorization.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

SCHEMA="ABUTRON_FP_RESTART_SNAPSHOT_V103"
class LedgerEvidenceError(ValueError):
    pass

def amount(raw):
    if not isinstance(raw,str) or len(raw)>40:
        raise LedgerEvidenceError("BAD_MONETARY_VALUE")
    try: d=Decimal(raw)
    except InvalidOperation as e: raise LedgerEvidenceError("BAD_MONETARY_VALUE") from e
    if not d.is_finite() or abs(d)>Decimal("1000000000000"):
        raise LedgerEvidenceError("BAD_MONETARY_VALUE")
    return d

def index(raw):
    if not isinstance(raw,str) or len(raw)>24 or not raw.isdecimal():
        raise LedgerEvidenceError("INVALID_NATIVE_INTEGER")
    return int(raw)

def parse_native(raw):
    if not isinstance(raw,bytes) or not 200<len(raw)<6_000_000:
        raise LedgerEvidenceError("NATIVE_SOURCE_BYTES_INVALID")
    try: rows=raw.decode("utf-8-sig").splitlines()
    except UnicodeError as e: raise LedgerEvidenceError("NATIVE_NOT_UTF8") from e
    if len(rows)<21 or rows[0]!=SCHEMA or rows[19]!="BROKER_NATIVE_NO_ORDER_FUNCTIONS":
        raise LedgerEvidenceError("NATIVE_SOURCE_SCHEMA_INVALID")
    if (not re.fullmatch("[0-9a-f]{32}",rows[1]) or
        not re.fullmatch("[0-9a-f-]{36}",rows[2]) or
        rows[3]!="FPMarketsSC-Demo" or not rows[4].isdecimal() or
        rows[5]!="DEMO" or rows[8]!="USD"):
        raise LedgerEvidenceError("NATIVE_FP_DEMO_IDENTITY_INVALID")
    snapshot_utc=index(rows[6])
    offset=index(rows[7])
    if not (7170<=offset<=7230):
        raise LedgerEvidenceError("NATIVE_BROKER_CLOCK_OFFSET_INVALID")
    balance=amount(rows[9])
    position_count=index(rows[14])
    pending_count=index(rows[15])
    deal_count=index(rows[16])
    if (position_count>500 or pending_count!=0 or deal_count>10000 or
        index(rows[17])!=0 or index(rows[18])<snapshot_utc+offset):
        raise LedgerEvidenceError("NATIVE_ALLTIME_HISTORY_WINDOW_UNTRUSTED")
    positions=set()
    deals={}
    for r in rows[20:-1]:
        parts=r.split("\t")
        if parts[0]=="P":
            if len(parts)!=13:raise LedgerEvidenceError("INVALID_NATIVE_POSITION_ROW")
            ticket=index(parts[1])
            if ticket==0 or ticket in positions or amount(parts[7])<=0 or amount(parts[8])<=0:
                raise LedgerEvidenceError("UNPROTECTED_OR_DUPLICATE_NATIVE_POSITION")
            positions.add(ticket)
        elif parts[0]=="O":
            raise LedgerEvidenceError("UNEXPECTED_PENDING_BROKER_ORDER")
        elif parts[0]=="D":
            if len(parts)!=15:raise LedgerEvidenceError("INVALID_NATIVE_DEAL_ROW")
            ticket=index(parts[1])
            if ticket==0 or ticket in deals:raise LedgerEvidenceError("DUPLICATED_BROKER_DEAL")
            deal_type=index(parts[5])
            entry=index(parts[6])
            if deal_type>99 or entry>99:raise LedgerEvidenceError("INVALID_BROKER_DEAL_CODE")
            server_time=index(parts[4])
            profits=[amount(x) for x in parts[11:15]]
            deals[ticket]={
                "deal_type":deal_type,
                "entry":entry,
                "broker_server_time":server_time,
                "cashflow":sum(profits,Decimal("0")),
            }
        else:
            raise LedgerEvidenceError("UNKNOWN_NATIVE_EVIDENCE_ROW")
    if (rows[-1]!=f"END\t{position_count}\t{pending_count}\t{deal_count}" or
        position_count!=len(positions) or deal_count!=len(deals)):
        raise LedgerEvidenceError("TRUNCATED_NATIVE_RECORD_COUNT_FENCE")
    if not deals:raise LedgerEvidenceError("NO_BROKER_NATIVE_DEALS")
    total=sum((x["cashflow"] for x in deals.values()),Decimal("0"))
    trading=[x for x in deals.values() if x["deal_type"] in (0,1)]
    nontrading=[x for x in deals.values() if x["deal_type"] not in (0,1)]
    trading_cash=sum((x["cashflow"] for x in trading),Decimal("0"))
    closing_cash=sum((x["cashflow"] for x in trading if x["entry"]==1),Decimal("0"))
    if abs(total-balance)>Decimal("0.01"):
        raise LedgerEvidenceError("ACCOUNT_BALANCE_DOES_NOT_EQUAL_NATIVE_DEAL_LEDGER")
    return {
        "native_snapshot_sha256":sha256(raw).hexdigest(),
        "account_suffix":rows[4][-4:],
        "snapshot_utc":snapshot_utc,
        "broker_offset_seconds":offset,
        "balance":balance,
        "deal_count":len(deals),
        "trade_deals":len(trading),
        "closing_deals":sum(x["entry"]==1 for x in trading),
        "nontrading_deals":len(nontrading),
        "trade_cashflow":trading_cash,
        "closing_cashflow":closing_cash,
        "nontrading_cashflow":sum((x["cashflow"] for x in nontrading),Decimal("0")),
        "ledger_cashflow":total,
        "earliest_deal_utc":min(x["broker_server_time"]-offset for x in deals.values()),
        "latest_deal_utc":max(x["broker_server_time"]-offset for x in deals.values()),
        "position_count":position_count,
        "broker_deal_ticket_count_unique":len(deals),
    }

def reconcile(native,broker,inception_notice_iso):
    if not isinstance(broker,dict) or broker.get("schema") is None:
        raise LedgerEvidenceError("INVALID_FRESH_BROKER_HISTORY_SCHEMA")
    # Bind independent sources to the SAME masked broker login. Their
    # nonce/owner token schemes differ, so compare the trusted MT5 login
    # suffix rather than copying a private login into any public report.
    if (not re.fullmatch(r"[0-9]{4}",native.get("account_suffix","")) or
        broker.get("account_masked")!="••••"+native["account_suffix"]):
        raise LedgerEvidenceError("INDEPENDENT_BROKER_ACCOUNT_IDENTITY_MISMATCH")
    if (broker.get("history_verified_from_mt5") is not True or
        broker.get("broker")!="FP Markets" or broker.get("environment")!="DEMO" or
        broker.get("account_currency")!="USD" or
        broker.get("history_complete_within_selected_window") is not True or
        broker.get("orders_submitted_by_exporter")!=0 or
        broker.get("pending_orders_count")!=0):
        raise LedgerEvidenceError("FRESH_ACCOUNT_HISTORY_NOT_AUTHENTICATED")
    try:
        inception=datetime.fromisoformat(inception_notice_iso.replace("Z","+00:00"))
    except ValueError as e:raise LedgerEvidenceError("INVALID_ACCOUNT_OPENING_NOTICE_TIME") from e
    if inception.tzinfo is None or inception.utcoffset() is None:
        raise LedgerEvidenceError("UNTRUSTED_NAIVE_INCEPTION_TIMESTAMP")
    created_utc=int(inception.timestamp())
    if created_utc<1700000000 or created_utc>native["snapshot_utc"]:
        raise LedgerEvidenceError("ACCOUNT_NOTICE_NOT_BEFORE_NATIVE_SNAPSHOT")
    if native["earliest_deal_utc"]<created_utc:
        raise LedgerEvidenceError("NATIVE_DEALS_PREDATE_ACCOUNT_NOTICE")
    # Source is a separate 90-day broker export. Its window must begin
    # BEFORE original creation and end AFTER original epoch-zero snapshot.
    history_from=broker.get("history_from_broker_time")
    history_to=broker.get("history_to_broker_time")
    if (not isinstance(history_from,int) or not isinstance(history_to,int) or
        history_from>created_utc+native["broker_offset_seconds"] or
        history_to-native["broker_offset_seconds"]<native["snapshot_utc"]):
        raise LedgerEvidenceError("SELECTED_WINDOW_DOES_NOT_COVER_ORIGINAL_INCEPTION")
    if not isinstance(broker.get("native_file_sha256"),str) or not re.fullmatch(
        "[0-9a-f]{64}",broker["native_file_sha256"]):
        raise LedgerEvidenceError("INDEPENDENT_BROKER_SOURCE_DIGEST_INVALID")
    fields=[
        ("broker_deals_count","deal_count"),
        ("trade_deals_count","trade_deals"),
        ("closing_deals_count","closing_deals"),
        ("open_positions_count","position_count"),
    ]
    for source,target in fields:
        if broker.get(source)!=native[target]:
            raise LedgerEvidenceError("INDEPENDENT_NATIVE_DEAL_OR_POSITION_COUNT_DRIFT")
    cash_fields=[
        ("balance_usd","balance"),
        ("total_trade_deal_cashflow_usd","trade_cashflow"),
        ("closing_deal_cashflow_usd","closing_cashflow"),
    ]
    for source,target in cash_fields:
        if not isinstance(broker.get(source),(float,int)):
            raise LedgerEvidenceError("FRESH_NATIVE_BALANCE_OR_CASHFLOW_MISSING")
        if abs(Decimal(str(broker[source]))-native[target])>Decimal("0.01"):
            raise LedgerEvidenceError("INDEPENDENT_BROKER_CASHFLOW_MISMATCH")
    if broker.get("open_positions_count")!=len(broker.get("positions",[])):
        raise LedgerEvidenceError("FRESH_BROKER_POSITION_ROWS_MISMATCH")
    if any(float(p.get("stop_loss",0))<=0 or float(p.get("take_profit",0))<=0
           for p in broker["positions"]):
        raise LedgerEvidenceError("CURRENT_OPEN_POSITION_UNPROTECTED")
    return {
        "schema":"abutron.issue22.fp_demo_inception_ledger_reconciliation.v1",
        "status":"ACCOUNT_LEVEL_NATIVE_INCEPTION_LEDGER_BALANCE_AND_TRADE_MATCH",
        "notice_time_utc":inception.astimezone(timezone.utc).isoformat(),
        "earliest_native_deal_utc":datetime.fromtimestamp(
            native["earliest_deal_utc"],timezone.utc).isoformat(),
        "latest_native_deal_utc":datetime.fromtimestamp(
            native["latest_deal_utc"],timezone.utc).isoformat(),
        "source_immutable_alltime_sha256":native["native_snapshot_sha256"],
        "broker_selected_window_source_sha256":broker.get("native_file_sha256"),
        "native_deals":native["deal_count"],
        "native_trading_deals":native["trade_deals"],
        "native_closing_deals":native["closing_deals"],
        "native_nontrading_deals":native["nontrading_deals"],
        "trade_net_cashflow_usd":float(native["trade_cashflow"]),
        "nontrading_cashflow_usd":float(native["nontrading_cashflow"]),
        "reconstructed_balance_usd":float(native["ledger_cashflow"]),
        "native_and_fresh_broker_balance_usd":float(native["balance"]),
        "difference_usd":0.0,
        "current_protected_open_positions":native["position_count"],
        "account_bounded_ledger_strongly_supported":True,
        "broker_account_inception_independently_certified":False,
        "broker_historical_intraminute_equity_peaks_certified":False,
        "other_linked_accounts_lifetime_certified":False,
        "current_execution_ack_or_chandelier_certified":False,
        "owner_risk_and_v9_flags_changed":False,
    }

def read_regular(path,cap):
    if path.is_symlink() or not path.is_file():
        raise LedgerEvidenceError("SOURCE_FILE_NOT_TRUSTED")
    st=path.stat()
    if st.st_size<=0 or st.st_size>cap:
        raise LedgerEvidenceError("SOURCE_LENGTH_INVALID")
    raw=path.read_bytes()
    if len(raw)!=st.st_size or path.stat().st_mtime_ns!=st.st_mtime_ns:
        raise LedgerEvidenceError("SOURCE_CHANGED_DURING_AUDIT")
    return raw

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--native",required=True)
    parser.add_argument("--fresh",required=True)
    parser.add_argument("--notice-utc",required=True)
    parser.add_argument("--out",required=True)
    parser.add_argument("--max-fresh-age",type=int,default=90)
    a=parser.parse_args()
    if os.geteuid()!=0:raise LedgerEvidenceError("ROOT_PRIVATE_AUDIT_ONLY")
    source=Path(a.native);live=Path(a.fresh);out=Path(a.out)
    if (out.parent.is_symlink() or out.parent.stat().st_mode&0o077 or
        a.max_fresh_age<1 or a.max_fresh_age>300):
        raise LedgerEvidenceError("UNTRUSTED_REPORT_LOCATION_OR_FRESHNESS_BOUND")
    if not 0<=time.time()-live.stat().st_mtime<a.max_fresh_age:
        raise LedgerEvidenceError("FRESH_BROKER_SOURCE_STALE")
    native=parse_native(read_regular(source,6_000_000))
    history=json.loads(read_regular(live,6_000_000))
    report=reconcile(native,history,a.notice_utc)
    report["verified_at_utc"]=datetime.now(timezone.utc).isoformat()
    report["source_account_masked"]="••••0512"
    fd,name=tempfile.mkstemp(dir=out.parent,prefix=".fp-inception-evidence-")
    try:
        with os.fdopen(fd,"w") as f:
            os.fchmod(f.fileno(),0o600)
            json.dump(report,f,sort_keys=True,indent=2);f.write("\n")
            f.flush();os.fsync(f.fileno())
        os.replace(name,out)
    finally:
        if os.path.lexists(name):os.unlink(name)
    print(report["status"],"deals",report["native_deals"],
          "balance_difference",report["difference_usd"],
          "OWNER_AND_TRADING_CERTIFICATION_UNCHANGED")

if __name__=="__main__":
    main()
