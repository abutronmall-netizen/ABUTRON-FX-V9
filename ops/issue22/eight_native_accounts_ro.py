"""Issue 22 — source-bound, fail-closed eight-account native history audit.

Exactly 3 Deriv DEMO, 4 FP Markets DEMO, and 1 FP Markets LIVE
read-only sources. DO NOT interpret observed deal count or cash balance
reconciliation as complete broker inception history, daily high-water
equity, order ACK, or execution authority.

No credentials, client IDs, full account logins, trade tickets or
individual deals are copied to the output. Never changes broker, CRM
or trading state.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from decimal import Decimal,InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

DERIV_SUFFIXES=("6207","7593","9244")
FP_SUFFIXES=("0512","1636","1622","2582")
LIVE_SUFFIX="9392"
class AccountCoverageError(ValueError):pass

def money(v):
    if type(v) not in (int,float,str) or len(str(v))>40:
        raise AccountCoverageError("INVALID_NATIVE_CASH_VALUE")
    try:d=Decimal(str(v))
    except InvalidOperation as e:raise AccountCoverageError("INVALID_NATIVE_CASH_VALUE") from e
    if not d.is_finite() or abs(d)>Decimal("100000000000"):
        raise AccountCoverageError("INVALID_NATIVE_CASH_VALUE")
    return d

def read_json(path,age_seconds=160,max_bytes=650000):
    p=Path(path)
    if p.is_symlink() or not p.is_file():raise AccountCoverageError("SOURCE_FILE_NOT_REGULAR")
    st=p.stat()
    if (st.st_size<=0 or st.st_size>max_bytes or
        not 0<=time.time()-st.st_mtime<=age_seconds):
        raise AccountCoverageError("BROKER_SOURCE_STALE_OR_UNBOUNDED")
    raw=p.read_bytes()
    if len(raw)!=st.st_size or p.stat().st_mtime_ns!=st.st_mtime_ns:
        raise AccountCoverageError("BROKER_SOURCE_DRIFT")
    return json.loads(raw),hashlib.sha256(raw).hexdigest()

def _require(condition,reason):
    if not condition:raise AccountCoverageError(reason)

def _fp(mask,d):
    _require(d.get("schema")=="abutron.fpmarkets.native.account_history.readonly.v1"
             and d.get("broker")=="FP Markets" and d.get("environment")=="DEMO"
             and d.get("account_masked")=="••••"+mask and
             d.get("history_verified_from_mt5") is True and
             d.get("history_complete_within_selected_window") is True and
             d.get("account_currency")=="USD" and
             d.get("orders_submitted_by_exporter")==0,
             "FP_NATIVE_ACCOUNT_AUTHENTICATION_FAILED")
    _require(isinstance(d.get("native_file_sha256"),str) and
             re.fullmatch("[0-9a-f]{64}",d["native_file_sha256"]) is not None,
             "FP_NATIVE_ORIGINAL_SOURCE_SHA_MISSING")
    _require(d.get("history_window_days")==90 and
             isinstance(d.get("history_from_broker_time"),int) and
             isinstance(d.get("history_to_broker_time"),int) and
             d["history_from_broker_time"]<d["history_to_broker_time"],
             "FP_WINDOW_RANGE_INVALID")
    t=d.get("trade_deals")
    _require(isinstance(t,list) and
             len(t)==d.get("trade_deals_count")==d.get("history_trade_deals_shown")
             and d.get("trade_deals_count")==d.get("trade_deals_count")
             and d.get("broker_deals_count",0)>=len(t),
             "FP_TRADE_DEAL_COUNT_INCONSISTENT")
    ids=set()
    net=Decimal("0")
    closing=Decimal("0")
    closes=0
    for z in t:
        _require(isinstance(z,dict) and str(z.get("ticket","")).isdigit(),
                 "FP_NATIVE_DEAL_TICKET_MISSING")
        k=str(z["ticket"])
        _require(k not in ids,"FP_DUPLICATE_NATIVE_DEAL_TICKET")
        ids.add(k)
        _require(z.get("entry") in ("IN","OUT"),"FP_NATIVE_ENTRY_TYPE_INVALID")
        fields=sum((money(z.get(x)) for x in
                    ("profit_usd","commission_usd","fee_usd","swap_usd")),Decimal("0"))
        cash=money(z.get("net_cashflow_usd"))
        _require(abs(fields-cash)<=Decimal("0.005"),
                 "FP_DEAL_COMPONENTS_DO_NOT_RECONCILE")
        net+=cash
        if z["entry"]=="OUT":
            closing+=cash
            closes+=1
    _require(closes==d.get("closing_deals_count") and
             abs(net-money(d.get("total_trade_deal_cashflow_usd")))<=Decimal("0.01") and
             abs(closing-money(d.get("closing_deal_cashflow_usd")))<=Decimal("0.01"),
             "FP_BROKER_NET_OR_CLOSING_CASHFLOW_DRIFT")
    positions=d.get("positions")
    _require(isinstance(positions,list) and
             len(positions)==d.get("open_positions_count") and
             d.get("pending_orders_count")==0,
             "FP_CURRENT_BROKER_POSITION_SET_MISMATCH")
    for p in positions:
        _require(money(p.get("stop_loss"))>0 and money(p.get("take_profit"))>0,
                 "FP_OPEN_POSITION_MISSING_BROKER_PROTECTION")
    balance=money(d.get("balance_usd"))
    return {
      "broker":"FP Markets","environment":"DEMO","account_masked":"••••"+mask,
      "evidence_class":"ACCOUNT_BROKER_NATIVE_SELECTED_90DAY",
      "native_deals_returned":d["broker_deals_count"],
      "native_trade_deals":len(t),"native_closing_deals":closes,
      "net_trade_cashflow_usd":float(net),"broker_balance_usd":float(balance),
      "inferred_funding_minus_trading_cashflow_usd":float(balance-net),
      "nontrading_cashflows_independently_attested_by_this_source":False,
      "native_open_positions":len(positions),
      "broker_native_selected_window_verified":True,
      "issuer_independent_full_account_lifetime_certified":False,
      "historical_equity_highwater_certified":False,
    }

def _deriv(d):
    _require(d.get("three_native_select_from_zero_fullrange_proved") is True and
             d.get("three_native_funding_and_trade_cashflow_balance_reconciled") is True,
             "DERIV_NATIVE_EPOCH_ZERO_OR_BALANCE_INVALID")
    accounts=d.get("accounts")
    _require(isinstance(accounts,list) and len(accounts)==3,
             "DERIV_ACCOUNT_SET_NOT_THREE")
    result=[]
    for suffix in DERIV_SUFFIXES:
        found=[a for a in accounts if isinstance(a,dict)
               and a.get("account_suffix")==suffix]
        _require(len(found)==1,"DERIV_ORIGINAL_ACCOUNT_IDENTITY_MISMATCH")
        a=found[0]
        deals=a.get("native_broker_fullrange_deals")
        trade=a.get("native_broker_trade_deals")
        funding=a.get("native_deposit_deals")
        _require(all(type(x)==int and x>=0 for x in (deals,trade,funding))
                 and deals==trade+funding and
                 a.get("history_query_from_unix_epoch_zero") is True and
                 a.get("all_returned_deals_cashflow_to_balance_proved") is True,
                 "DERIV_NATIVE_ACCOUNT_LEDGER_COUNT_INVALID")
        bal=money(a.get("native_broker_cash_balance_usd"))
        tradecash=money(a.get("trading_net_cashflow_usd"))
        fundcash=money(a.get("funding_cashflow_usd"))
        _require(abs((tradecash+fundcash)-bal)<=Decimal("0.01"),
                 "DERIV_NATIVE_ACCOUNT_BALANCE_MISMATCH")
        result.append({
          "broker":"Deriv","environment":"DEMO","account_masked":"••••"+suffix,
          "evidence_class":"BROKER_NATIVE_EPOCH_ZERO_RETURNED",
          "native_deals_returned":deals,"native_trade_deals":trade,
          "native_funding_and_other_deals":funding,
          "net_trade_cashflow_usd":float(tradecash),
          "independently_returned_funding_cashflow_usd":float(fundcash),
          "broker_balance_usd":float(bal),
          "native_open_positions":a.get("native_open_positions"),
          "broker_native_epoch_zero_query_proved":True,
          "issuer_independent_full_account_lifetime_certified":False,
          "historical_equity_highwater_certified":False,
        })
    _require(sum(x["native_deals_returned"] for x in result)==d.get("native_deals_total"),
             "DERIV_THREE_NATIVE_DEAL_TOTAL_INVALID")
    return result

def _live(d):
    _require(d.get("account_masked")=="••••"+LIVE_SUFFIX and
             d.get("broker")=="FP Markets" and d.get("environment")=="LIVE" and
             d.get("live_execution_authorized") is False and
             d.get("orders_submitted")==0 and d.get("crm_switch_modified") is False and
             d.get("broker_query_started_at_epoch_zero") is True and
             d.get("current_pending_orders_count")==0,
             "LIVE_NATIVE_READONLY_IDENTITY_UNVERIFIED")
    n=d.get("broker_returned_fullrange_deals")
    _require(type(n)==int and n>=0,"LIVE_NATIVE_DEAL_COUNT_INVALID")
    return {
      "broker":"FP Markets","environment":"LIVE",
      "account_masked":"••••"+LIVE_SUFFIX,
      "evidence_class":"BROKER_NATIVE_EPOCH_ZERO_ZERO_DEALS_UNCERTIFIED" if n==0
                       else "BROKER_NATIVE_EPOCH_ZERO_RETURNED_UNCERTIFIED",
      "native_deals_returned":n,
      "native_open_positions":d.get("current_positions_count"),
      "broker_balance_usd":float(money(d.get("current_balance_usd"))),
      "issuer_independent_full_account_lifetime_certified":False,
      "historical_equity_highwater_certified":False,
      "live_trading_enabled_by_audit":False,
    }

def build(deriv,fp_map,live,owner):
    _require(owner.get("connected_accounts")==8 and owner.get("linked_accounts")==8
             and owner.get("native_position_sets_verified")==8 and
             owner.get("unknown_connected_position_sets")==0,
             "OWNER_CURRENT_POSITION_INVENTORY_NOT_VERIFIED")
    _require(owner.get("status")=="BLOCKED_INCOMPLETE_EXPOSURE" and
             len(owner.get("blockers",[]))==3 and
             owner.get("live_authorized") is False,
             "OWNER_EXPECTED_HISTORICAL_BLOCKERS_CHANGED")
    rows=_deriv(deriv)
    _require(set(fp_map)==set(FP_SUFFIXES),"FP_DEMO_HISTORY_SET_NOT_FOUR")
    for suffix in FP_SUFFIXES:
        rows.append(_fp(suffix,fp_map[suffix]))
    rows.append(_live(live))
    _require(len(rows)==8 and len({r["account_masked"] for r in rows})==8,
             "DUPLICATED_ACCOUNT_HISTORY_ROWS")
    _require(sum(r["native_open_positions"] for r in rows)==
             owner.get("confirmed_fp_open_positions"),
             "BROKER_ACCOUNT_OPEN_POSITIONS_DO_NOT_MATCH_OWNER")
    _require(rows[-1]["environment"]=="LIVE" and
             rows[-1]["issuer_independent_full_account_lifetime_certified"] is False,
             "LIVE_HISTORY_INCORRECTLY_CERTIFIED")
    return {
      "schema":"abutron.issue22.eight_account_broker_native_coverage.v1",
      "status":"EIGHT_ACCOUNT_NATIVE_COVERAGE_ASSESSED_NOT_LIFETIME_CERTIFIED",
      "checked_at_utc":datetime.now(timezone.utc).isoformat(),
      "accounts":rows,
      "native_account_sources_checked":len(rows),
      "deriv_epoch_zero_native_queries":3,
      "fp_demo_native_90day_ledgers":4,
      "live_native_epoch_zero_queries":1,
      "current_broker_position_sets_verified":8,
      "unknown_connected_position_sets":0,
      "native_returned_deals_total_observed":sum(x["native_deals_returned"] for x in rows),
      "all_eight_issuer_certified_lifetime":False,
      "historical_intraminute_equity_peak_certified":False,
      "broker_order_ack_and_chandelier_certified":False,
      "owner_status_unchanged":owner["status"],
      "owner_blockers_unchanged":owner["blockers"],
      "live_trading_modified":False,
      "broker_orders_submitted":0,
    }

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--deriv",required=True)
    for s in FP_SUFFIXES:p.add_argument("--fp-"+s,required=True)
    p.add_argument("--live",required=True)
    p.add_argument("--owner",required=True)
    p.add_argument("--out",required=True)
    a=p.parse_args()
    if os.geteuid()!=0:raise AccountCoverageError("ROOT_PRIVATE_AUDIT_ONLY")
    deriv,_=read_json(a.deriv,age_seconds=180)
    fp={s:read_json(getattr(a,"fp_"+s),age_seconds=160)[0] for s in FP_SUFFIXES}
    live,_=read_json(a.live,age_seconds=160)
    owner,_=read_json(a.owner,age_seconds=100)
    report=build(deriv,fp,live,owner)
    out=Path(a.out)
    if out.parent.is_symlink() or out.parent.stat().st_mode&0o077:
        raise AccountCoverageError("REPORT_DESTINATION_NOT_ROOT_PRIVATE")
    fd,tmp=tempfile.mkstemp(prefix=".issue22-eight-native-",dir=out.parent)
    try:
        with os.fdopen(fd,"w") as f:
            os.fchmod(f.fileno(),0o600)
            json.dump(report,f,sort_keys=True,indent=2);f.write("\n")
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,out)
    finally:
        if os.path.lexists(tmp):os.unlink(tmp)
    print(report["status"],"account_sources",len(report["accounts"]),
          "all_eight_lifetime_certified",False)
if __name__=="__main__":
    main()
