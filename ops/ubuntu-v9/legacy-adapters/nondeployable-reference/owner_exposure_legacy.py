# NONDEPLOYABLE reference — all private CRM/account identities redacted.
# For migration analysis only, replace with V9-native credential-free state handling.
#!/usr/bin/env python3
"""Root-only READ-ONLY ABUTRON owner risk/inventory evidence; no trades, no CRM mutations."""
import os
import sqlite3
import hashlib
import json
import datetime as dt
import time
import math
from pathlib import Path
from collections import defaultdict

CRM=Path("/var/lib/apex-signup/accounts.sqlite3")
BROKER=Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/gate-native-fp-deriv-inventory-20261108.json")
CUSTOMER=Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json")
SUPERVISOR=Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/evidence.json")
OUTPUT=Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/owner-exposure-inventory-20261109.json")
WORKERS={
  ("Deriv","DEMO","6207"):"REDACTED_CRM_ID_01",
  ("Deriv","DEMO","7593"):"REDACTED_CRM_ID_02",
  ("Deriv","DEMO","9244"):"REDACTED_CRM_ID_03",
  ("FP Markets","DEMO","0512"):"REDACTED_CRM_ID_04",
  ("FP Markets","DEMO","1636"):"REDACTED_CRM_ID_05",
  ("FP Markets","DEMO","1622"):"REDACTED_CRM_ID_06",
  ("Deriv","LIVE","0178"):"REDACTED_CRM_ID_07"
}
def require(condition,message):
    if not condition: raise ValueError(message)
def hash_id(owner):
    return hashlib.sha256(str(owner).encode()).hexdigest()[:12]
def load_json(path,now,age_seconds):
    require(path.is_file() and not path.is_symlink(),"SOURCE_FILE_UNTRUSTED")
    with path.open() as fh: d=json.load(fh)
    require(0<=now-path.stat().st_mtime<=age_seconds,"SOURCE_FILE_STALE")
    return d
def money(x):
    v=float(x);require(math.isfinite(v) and v>=0,"INVALID_CASH_VALUE");return v
def get_connections():
    with sqlite3.connect("file:"+str(CRM)+"?mode=ro",uri=True,timeout=7) as db:
        db.execute("PRAGMA query_only=ON")
        return db.execute("""
          SELECT x.id,x.account_id,x.account_reference,x.broker,x.environment,
                 x.broker_server,x.status,coalesce(t.desired_enabled,0),
                 coalesce(t.effective_enabled,0),x.worker_binding
          FROM apex_mt5_connections x LEFT JOIN apex_trade_controls t
            ON x.id=t.connection_id ORDER BY x.account_id,x.id
        """).fetchall()
def aggregate(now):
    state={"schema":"abutron.customer.owner_portfolio.readonly.inventory.v1",
           "checked_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
           "read_only":True,"execution_authority":False,"orders_submitted":0,
           "crm_modified":False,"live_authorized":False,"risk_certified":False,
           "v4_handover_permitted":False}
    gate=load_json(BROKER,now,170)
    customer=load_json(CUSTOMER,now,170)
    sup=load_json(SUPERVISOR,now,170)
    require(gate.get("read_only") is True and gate.get("execution_authority") is False,"BROKER_AUDITOR_NOT_READONLY")
    require(sup.get("order_authorized") is False,"V4_ORDER_AUTHORITY_ALREADY_ON_REQUIRES_ESCALATION")
    require(customer.get("customer_wide_risk_certified") is False,"CUSTOMER_RISK_STATUS_CHANGED")
    positions={}
    for v in gate.get("deriv",[]):
        key=("Deriv","DEMO",v.get("account_suffix"))
        require(key not in positions,"DUPLICATE_BROKER_EVIDENCE")
        require(v.get("primary_worker_fresh") is True and v.get("independent_native_trade_deal_receipts_matched") is True,"DERIV_BROKER_PROOF_INCOMPLETE")
        positions[key]=v
    for v in gate.get("fp",[]):
        key=("FP Markets","DEMO",v.get("account_suffix"))
        require(key not in positions,"DUPLICATE_BROKER_EVIDENCE")
        require(v.get("primary_worker_fresh") is True and v.get("broker_history_fresh") is True,"FP_BROKER_PROOF_INCOMPLETE")
        positions[key]=v
    # Accept only fresh, independently verified, ORDERLESS LIVE evidence.
    # This is owner exposure coverage, never LIVE admission or risk approval.
    try:
        # Reuse only the native reader function, NOT the retired monitor/timer.
        # This obtains a new MT5 heartbeat and position inventory in this run.
        import runpy
        native=runpy.run_path(
            "/opt/abutron-deriv-live-proof-staging-20261109/live0178_admission_readonly.py",
            run_name="_read_only_native_import_")
        f=native["fixed_identity"](now)
        if (f.get("native_authenticated") is True
            and f.get("native_no_order_ea_pinned") is True
            and f.get("connected") is True and f.get("crm_off") is True
            and f.get("account_suffix")=="0178"
            and f.get("mode")=="LIVE"
            and f.get("server")=="DerivSVG-Server"
            and f.get("open_positions")==0 and f.get("pending_orders")==0):
            positions[("Deriv","LIVE","0178")]={
                "equity_usd":f["equity_usd"],"positions":0,"pending_orders":0,
                "native_loss_to_stops_usd":0.0,
                "native_usd_stop_calculation_count":0,"quotes_stale":False,
                "primary_worker_fresh":True,
                "native_live_readonly_inventory":True,
                "broker_live_trading_allowed":bool(f.get("broker_trade_allowed"))}
    except (ValueError,KeyError,TypeError,OSError):
        # Missing/stale LIVE proof remains an UNKNOWN exposure, never zero.
        pass
    # FP DEMO 1622: recover independent VERIFIED ZERO exposure only from its
    # exact, freshly broker-bound V101 native MT5 history. Never convert an
    # unverified/protected open position to zero, or authorize an order.
    try:
        import importlib.util
        reader_path=Path("/opt/apex-client-mt5/fpmarkets-history/fp_native_history_collect.py")
        approved_sha="e33c08f59097a694f890ad4a3c3b93a924070fa762b4280567e84630336afb89"
        require(reader_path.is_file() and not reader_path.is_symlink()
                and hashlib.sha256(reader_path.read_bytes()).hexdigest()==approved_sha,
                "FP1622_NATIVE_READER_SOURCE_UNTRUSTED")
        spec=importlib.util.spec_from_file_location("fp1622_independent_owner_native",str(reader_path))
        require(spec is not None and spec.loader is not None,"FP1622_NATIVE_READER_UNAVAILABLE")
        native1622=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native1622)
        native1622.CID="REDACTED_CRM_ID_06"
        cid,owner,history=native1622.snapshot(now=now)
        if (cid==native1622.CID and isinstance(owner,str) and len(owner)==32
            and history.get("account_masked")=="••••1622"
            and history.get("history_verified_from_mt5") is True
            and history.get("execution_enabled") is False
            and history.get("orders_submitted_by_exporter")==0
            and history.get("open_positions_count")==0
            and history.get("pending_orders_count")==0
            and history.get("broker_execution_ack_certified") is False
            and history.get("customer_portfolio_risk_certified") is False
            and money(history.get("equity_usd"))>0):
            positions[("FP Markets","DEMO","1622")]={
                "equity_usd":history["equity_usd"],"positions":0,"pending_orders":0,
                "native_loss_to_stops_usd":0.0,"native_usd_stop_calculation_count":0,
                "quotes_stale":True,"primary_worker_fresh":True,
                "native_fp_v101_zero_exposure_verified":True}
    except (ValueError,KeyError,TypeError,OSError,RuntimeError,ImportError,AttributeError):
        # Unknown exposure remains UNKNOWN whenever real broker proof is absent.
        pass
    rows=get_connections()
    identity=defaultdict(list)
    for cid,owner,login,broker,mode,server,status,desired,effective,binding in rows:
        require(mode in ("DEMO","LIVE") and status in ("CONNECTED","CONNECTING","DISCONNECTED","ERROR"),"UNKNOWN_CRM_STATUS")
        require(desired in (0,1) and effective in (0,1),"UNKNOWN_CONTROL_STATUS")
        identity[(str(owner),str(broker),str(mode),str(login))].append(
            {"connection_suffix":str(cid)[-6:],"broker_server":str(server),
             "crm_status":status,"desired_enabled":bool(desired),
             "effective_enabled":bool(effective),
             "native_worker_binding":str(binding or "")})
    owner_details=defaultdict(lambda:{"connected_verified_identity_count":0,
                                     "linked_connection_count":0,
                                     "unique_linked_account_identities":0,
                                     "known_connected_equity_usd":0.0,
                                     "known_gross_entry_to_stop_usd":0.0,
                                     "known_positions":0,"known_pending_orders":0,
                                     "unverified_account_identities":[],
                                     "connected_broker_inventory":[],
                                     "has_unresolved_crm_alias":False})
    unknown=[];duplicate_links=[];missing_evidence=[]
    for (owner,broker,mode,login),links in identity.items():
        label=hash_id(owner);a=owner_details[label]
        a["unique_linked_account_identities"]+=1
        a["linked_connection_count"]+=len(links)
        suffix=login[-4:]
        crm_connected=[x for x in links if x["crm_status"]=="CONNECTED"]
        match=positions.get((broker,mode,suffix))
        expected=WORKERS.get((broker,mode,suffix))
        native_verified=bool(crm_connected and match and expected and
            len(crm_connected)==1 and
            crm_connected[0]["native_worker_binding"]=="abutron-client-mt5-"+expected)
        if len(links)>1:
            unique_servers=sorted(set(x["broker_server"] for x in links))
            for l in links:
                if l not in crm_connected:
                    duplicate_links.append({
                        "owner_hash":label,"account_suffix":suffix,
                        "broker":broker,"environment":mode,
                        "connection_suffix":l["connection_suffix"],
                        "status":l["crm_status"],
                        "server_identity_disagreement":len(unique_servers)>1,
                        "exposure_covered_by_verified_same_login":native_verified})
                    if len(unique_servers)>1:a["has_unresolved_crm_alias"]=True
        if native_verified:
            eq=money(match["equity_usd"]);open_count=int(match["positions"])
            pending=int(match["pending_orders"])
            risk=money(match.get("native_loss_to_stops_usd",0))
            require(open_count>=0 and pending>=0,"NEGATIVE_POSITION_COUNTS")
            require(risk==0 if open_count==0 else broker=="FP Markets","UNEXPLAINED_RISK")
            if open_count>0:
                require(match.get("native_usd_stop_calculation_count")==open_count,
                        "OPEN_POSITION_NATIVE_USD_STOP_MISSING")
            entry={"account_suffix":suffix,"broker":broker,
                   "mode":mode,"native_evidence":True,
                   "equity_usd":round(eq,2),"positions":open_count,
                   "pending_orders":pending,
                   "gross_entry_to_sl_usd_not_remaining_risk":round(risk,2),
                   "quote_fresh_for_all_positions":not match.get("quotes_stale",False),
                   "open_sl_mark_to_stop_risk_certified":False,
                   "day_start_drawdown_certified":False}
            a["connected_broker_inventory"].append(entry)
            a["connected_verified_identity_count"]+=1
            a["known_connected_equity_usd"]+=eq
            a["known_gross_entry_to_stop_usd"]+=risk
            a["known_positions"]+=open_count
            a["known_pending_orders"]+=pending
        else:
            reason=("NATIVE_BROKER_EVIDENCE_OR_CRM_BINDING_NOT_MATCHED"
                    if crm_connected else "NO_CONNECTED_AUTHENTICATED_BROKER_SESSION")
            entry={"owner_hash":label,"broker":broker,"environment":mode,
                   "account_suffix":suffix,"reason":reason,
                   "linked_records":[{"connection_suffix":x["connection_suffix"],
                                      "crm_status":x["crm_status"]}
                                      for x in links],
                   "exposure_unknown_not_zero":True}
            a["unverified_account_identities"].append(entry)
            unknown.append(entry)
            if crm_connected:missing_evidence.append(entry)
    for label,a in owner_details.items():
        a["known_connected_equity_usd"]=round(a["known_connected_equity_usd"],2)
        a["known_gross_entry_to_stop_usd"]=round(a["known_gross_entry_to_stop_usd"],2)
        a["known_gross_entry_to_stop_as_pct_of_known_equity"]=(
           round(100*a["known_gross_entry_to_stop_usd"]/a["known_connected_equity_usd"],4)
           if a["known_connected_equity_usd"]>0 else None)
        a["owner_full_portfolio_certified"]=False
    state["total_linked_connections"]=len(rows)
    state["distinct_broker_owner_mode_account_identities"]=len(identity)
    state["nonconnected_crm_links"]=sum(x["crm_status"]!="CONNECTED" for links in identity.values() for x in links)
    state["nonconnected_links_in_duplicate_identity_groups"]=duplicate_links
    state["redundant_connection_count_by_exact_owner_broker_mode_login"]=(
        len(rows)-len(identity))
    state["unique_account_identities_without_authenticated_broker_evidence"]=unknown
    state["covered_connected_broker_accounts"]=sum(x["connected_verified_identity_count"] for x in owner_details.values())
    state["known_connected_open_positions"]=sum(x["known_positions"] for x in owner_details.values())
    state["known_connected_pending_orders"]=sum(x["known_pending_orders"] for x in owner_details.values())
    state["known_gross_entry_to_sl_usd_not_remaining_risk"]=round(
       sum(x["known_gross_entry_to_stop_usd"] for x in owner_details.values()),2)
    state["owner_groups"]=dict(sorted(owner_details.items()))
    issues=list(gate.get("blockers",[]))
    if ("Deriv","LIVE","0178") in positions:
        issues.append("LIVE_0178_SEPARATE_REAL_MONEY_EXECUTION_NOT_APPROVED")
        if positions[("Deriv","LIVE","0178")]["equity_usd"]<=0:
            issues.append("LIVE_0178_ACCOUNT_UNFUNDED")
        if not positions[("Deriv","LIVE","0178")]["broker_live_trading_allowed"]:
            issues.append("LIVE_0178_BROKER_TRADE_PERMISSION_DENIED")
    if unknown:issues.append("UNVERIFIED_ACCOUNT_IDENTITIES_WITH_UNKNOWN_EXPOSURE")
    if duplicate_links:issues.append("DUPLICATE_CONNECTION_SERVER_IDENTITY_UNVERIFIED")
    if missing_evidence:issues.append("CONNECTED_CRM_NATIVE_PROOF_MISSING")
    if any(x["effective_enabled"] for links in identity.values() for x in links):
        issues.append("UNEXPECTED_EFFECTIVE_ENABLED")
    issues+=["NO_CERTIFIED_DAY_START_OR_EQUITY_PEAK",
             "NO_MARK_TO_STOP_GAP_SLIPPAGE_PROOF",
             "NO_FULL_ACCOUNT_WIDE_BROKER_LEDGER_RECONCILIATION",
             "NO_AUTHENTICATED_V4_EXECUTION_ACK"]
    state["blockers"]=sorted(set(issues))
    state["status"]="BLOCKED_INCOMPLETE_CUSTOMER_EXPOSURE"
    return state
def main():
    now=time.time()
    try:d=aggregate(now)
    except Exception as e:
        d={"schema":"abutron.customer.owner_portfolio.readonly.inventory.v1",
           "checked_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
           "read_only":True,"execution_authority":False,"risk_certified":False,
           "v4_handover_permitted":False,"status":"BLOCKED_AUDIT_ERROR",
           "blockers":[type(e).__name__+":"+str(e)[:100]]}
    temp=OUTPUT.with_name(OUTPUT.name+".tmp")
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"w") as f:
        json.dump(d,f,sort_keys=True,indent=2,allow_nan=False)
        f.flush();os.fsync(f.fileno())
    os.replace(temp,OUTPUT);OUTPUT.chmod(0o600)
    print(json.dumps(d,sort_keys=True))
if __name__=="__main__":main()

