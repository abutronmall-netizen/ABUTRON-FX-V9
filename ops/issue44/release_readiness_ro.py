"""Issue #44: read-only FP DEMO Chandelier release readiness.

This tool NEVER modifies a live chart/EA/CRM, grants trading authority,
changes protection, or sends a broker order. It makes a reproducible,
SHA-pinned GO/NO-GO evidence record that separates:
  - current production and broker-native exposure,
  - a signed compiled *candidate* not yet promoted,
  - an isolated, trading-disabled native runtime proof,
  - the genuine broker modification certification still missing.

All customer identity, account IDs, paths, and expected hashes are injected
through a root-private runtime JSON manifest; none belong in public source.
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
import stat
import tempfile
import time

class ReleaseEvidenceError(ValueError):
    pass

def _safe_read(p:Path,limit:int)->bytes:
    if not isinstance(p,Path) or not 0<limit<=6_000_000:
        raise ReleaseEvidenceError("INVALID_READ_LIMIT")
    st=p.lstat()
    if not stat.S_ISREG(st.st_mode) or p.is_symlink() or st.st_nlink!=1 or not 0<st.st_size<=limit:
        raise ReleaseEvidenceError("UNTRUSTED_SOURCE_FILE")
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        before=os.fstat(fd)
        raw=os.read(fd,limit+1)
        after=os.fstat(fd)
        fingerprint=lambda x:(x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns)
        if len(raw)!=before.st_size or fingerprint(before)!=fingerprint(after):
            raise ReleaseEvidenceError("SOURCE_REWRITTEN_DURING_READ")
        return raw
    finally:
        os.close(fd)

def safe_json(p:Path,limit:int=5_000_000)->dict:
    d=json.loads(_safe_read(p,limit))
    if not isinstance(d,dict):raise ReleaseEvidenceError("SOURCE_JSON_NOT_OBJECT")
    return d

def validated_sha(p:Path,expected:str)->str:
    if not isinstance(expected,str) or len(expected)!=64 or any(c not in "0123456789abcdef" for c in expected):
        raise ReleaseEvidenceError("INVALID_SHA_PIN")
    actual=hashlib.sha256(_safe_read(p,5_000_000)).hexdigest()
    if actual!=expected:raise ReleaseEvidenceError("SOURCE_SHA_MISMATCH")
    return actual

def age_seconds(p:Path,now:float)->float:
    age=now-p.stat().st_mtime
    if not 0<=age<=10*86400:raise ReleaseEvidenceError("INVALID_SOURCE_CLOCK")
    return age

def live_count(db_path:Path)->int:
    if not db_path.is_file() or db_path.is_symlink():raise ReleaseEvidenceError("CRM_DATABASE_UNAVAILABLE")
    db=sqlite3.connect(f"file:{db_path}?mode=ro",uri=True,timeout=2)
    try:
        db.execute("PRAGMA query_only=ON")
        row=db.execute('''SELECT COUNT(*) FROM apex_mt5_connections c
                       JOIN apex_trade_controls t ON t.connection_id=c.id
                       WHERE c.environment='LIVE' AND t.effective_enabled=1''').fetchone()
        if not row or type(row[0]) is not int:raise ReleaseEvidenceError("LIVE_STATUS_UNKNOWN")
        return row[0]
    finally:db.close()

def evaluate(current:dict)->dict:
    """Pure release decision. No scenario here produces execution authority."""
    issues=[]
    if current.get("live_on")!=0:issues.append("LIVE_ACCOUNT_EFFECTIVE_ON")
    if current.get("crm_demo")!=("CONNECTED",1,1):issues.append("DEMO_CRM_NOT_CONNECTED_ON")
    if current.get("worker_fresh") is not True:issues.append("NATIVE_BROKER_HEARTBEAT_NOT_FRESH")
    if current.get("broker_history_authentic") is not True:issues.append("BROKER_NATIVE_HISTORY_NOT_AUTHENTIC")
    if current.get("native_reconciled") is not True:issues.append("NATIVE_BROKER_POSITIONS_OR_STOP_RISK_NOT_RECONCILED")
    if current.get("all_open_protected") is not True:issues.append("OPEN_BROKER_POSITION_MISSING_SLTP")
    if current.get("pending_orders")!=0:issues.append("PENDING_BROKER_ORDERS")
    if current.get("owner_status")!="BLOCKED_INCOMPLETE_EXPOSURE" or current.get("owner_blockers")!=3:
        issues.append("OWNER_PROOF_STATE_DRIFT_REVIEW")
    if current.get("v9_status")!="NOT_CERTIFIED_FOR_NEW_ORDERS" or current.get("v9_blockers")!=7:
        issues.append("V9_CERTIFICATION_STATE_DRIFT_REVIEW")
    if current.get("source_sha_pins_verified") is not True:issues.append("RELEASE_SOURCES_NOT_SHA_PINNED")
    if current.get("isolated_qa_original_optionc_verified") is not True:
        issues.append("ISOLATED_NATIVE_OPTIONC_NOT_PROVEN")
    if current.get("isolated_qa_no_order") is not True:
        issues.append("ISOLATED_NATIVE_NO_ORDER_QA_NOT_PROVEN")
    if current.get("trailing_archive_fresh") is not True:
        issues.append("IMMUTABLE_TRAILING_ARCHIVE_UNAVAILABLE")
    if current.get("native_trailing_records",0)>0 and current.get("trailing_broker_modification_certified") is not True:
        issues.append("UNATTESTED_NATIVE_TRAILING_SOURCE_RECORDS")
    n=current.get("open_positions")
    if type(n) is not int or n<0 or n>100:issues.append("INVALID_OPEN_POSITION_COUNT")
    elif n>0:issues.append("OPEN_PROTECTED_POSITIONS_ROLLING_RESTART_NOT_PROVEN")
    risk=current.get("stop_risk_usd")
    equity=current.get("equity_usd")
    if not all(type(x) in (int,float) and math.isfinite(x) and x>=0 for x in (risk,equity)) or equity<=0:
        issues.append("CURRENT_EXPOSURE_OR_EQUITY_INVALID")
    elif risk>equity*.20:
        issues.append("OPEN_STOP_RISK_EXCEEDS_20_PERCENT")
    # The existing promotion mechanism was engineered for a flat terminal.
    # Even perfect source and a flat account do not authorize auto-promotion.
    return {
       "status":"RELEASE_PROMOTION_INHIBITED" if issues else "INSPECTED_CANDIDATE_ONLY_DEPLOYMENT_REQUIRES_REVIEW",
       "inhibitors":sorted(set(issues)),
       "production_promoted":False,
       "broker_trailing_sltp_amendment_certified":False,
       "new_order_execution_authorized":False,
       "live_trading_enabled_by_audit":False,
       "crm_or_risk_flags_modified":False,
       "requires_independent_broker_readback":True,
    }

def build(config:dict,now:float|None=None)->dict:
    t=time.time() if now is None else now
    if config.get("schema")!="abutron.issue44.fp_demo_readonly_release_config.v1":
        raise ReleaseEvidenceError("ACCOUNT_BOUND_CONFIG_SCHEMA_INVALID")
    fs=config.get("sha_pinned_files")
    if not isinstance(fs,dict) or not all(k in fs for k in
      ("production_ex5","production_chart","production_restager","candidate_ex5")):
        raise ReleaseEvidenceError("RELEASE_MANIFEST_INCOMPLETE")
    hashes={}
    for name,info in fs.items():
        if not isinstance(info,dict) or set(info)!={"path","sha256"}:
            raise ReleaseEvidenceError("INVALID_RELEASE_SHA_MANIFEST")
        hashes[name]=validated_sha(Path(info["path"]),info["sha256"])
    sources=config.get("sources")
    if not isinstance(sources,dict) or not all(k in sources for k in
      ("native_health","native_history","native_recon","owner","v9","trailing_archive","isolated_qa","crm_db")):
        raise ReleaseEvidenceError("PRIVATE_SOURCE_SET_INCOMPLETE")
    docs={}
    for key in ("native_health","native_history","native_recon","owner","v9","trailing_archive","isolated_qa"):
        p=Path(sources[key])
        docs[key]=safe_json(p)
        if age_seconds(p,t)>({"isolated_qa":86400,"owner":180,"v9":180,"trailing_archive":45}.get(key,45)):
            if key=="isolated_qa":raise ReleaseEvidenceError("ISOLATED_QA_PROOF_TOO_OLD")
            raise ReleaseEvidenceError("STALE_NATIVE_"+key.upper())
    health,history,recon,owner,v9,archive,qa=(docs[k] for k in
        ("native_health","native_history","native_recon","owner","v9","trailing_archive","isolated_qa"))
    if (health.get("id")!=config.get("connection_id") or health.get("mode")!="DEMO"
        or health.get("broker")!="FP Markets" or health.get("server")!=config.get("broker_server")
        or history.get("environment")!="DEMO" or history.get("broker")!="FP Markets"):
        raise ReleaseEvidenceError("BROKER_ACCOUNT_IDENTITY_NOT_MATCHED")
    if health.get("status")!="CONNECTED" or health.get("heartbeat_fresh") is not True:
        raise ReleaseEvidenceError("ACCOUNT_NOT_BROKER_AUTHENTICATED")
    positions=history.get("positions")
    if type(positions) is not list or history.get("open_positions_count")!=len(positions):
        raise ReleaseEvidenceError("BROKER_POSITION_SET_COUNT_INVALID")
    equity=history.get("equity_usd")
    risk=recon.get("verified_open_gross_stop_risk_usd")
    if (recon.get("status")!="RECONCILED_READONLY" or
        recon.get("native_broker_open_risk_verified") is not True or
        recon.get("broker_open_positions")!=len(positions)):
        raise ReleaseEvidenceError("RECONCILER_NOT_INDEPENDENTLY_PROVEN")
    if type(equity) not in (float,int) or type(risk) not in (float,int):
        raise ReleaseEvidenceError("NATIVE_FINANCIAL_AMOUNTS_INVALID")
    if (not math.isfinite(equity) or not math.isfinite(risk) or
        equity<=0 or risk<0):raise ReleaseEvidenceError("NATIVE_FINANCIAL_AMOUNTS_INVALID")
    if abs(float(recon.get("broker_equity_usd",float("nan")))-equity)>.15:
        raise ReleaseEvidenceError("RECONCILER_EQUITY_NOT_CURRENT")
    with sqlite3.connect(f"file:{sources['crm_db']}?mode=ro",uri=True,timeout=2) as db:
        db.execute("PRAGMA query_only=ON")
        crm=db.execute('''SELECT c.status,t.desired_enabled,t.effective_enabled
            FROM apex_mt5_connections c JOIN apex_trade_controls t ON t.connection_id=c.id
            WHERE c.id=?''',(config["connection_id"],)).fetchone()
    data={
        "live_on":live_count(Path(sources["crm_db"])),
        "crm_demo":crm,
        "worker_fresh":True,
        "broker_history_authentic":history.get("history_verified_from_mt5") is True,
        "native_reconciled":True,
        "all_open_protected":all(
            isinstance(p,dict) and type(p.get("stop_loss")) in (int,float) and
            type(p.get("take_profit")) in (int,float) and
            p["stop_loss"]>0 and p["take_profit"]>0
            for p in positions),
        "pending_orders":history.get("pending_orders_count"),
        "owner_status":owner.get("status"),"owner_blockers":len(owner.get("blockers",[])),
        "v9_status":v9.get("status"),"v9_blockers":len(v9.get("blockers",[])),
        "source_sha_pins_verified":True,
        "isolated_qa_original_optionc_verified":
            qa.get("status")=="ISOLATED_ORIGINAL_OPTIONC_OPEN_PROTECTED_VERIFIED_NO_TRADING"
            and qa.get("ex5_sha256")==hashes["candidate_ex5"],
        "isolated_qa_no_order":qa.get("native_broker_orders_sent_by_this_qa")==0,
        "trailing_archive_fresh":archive.get("status") in
            ("NO_NATIVE_TRAILING_RECORDS_YET",
             "ARCHIVED_SOURCE_ONLY_BROKER_AMENDMENT_NOT_INDEPENDENTLY_CERTIFIED"),
        "native_trailing_records":sum(archive.get("native_trailing_source_counts",{}).values()),
        "trailing_broker_modification_certified":False,
        "open_positions":len(positions),
        "stop_risk_usd":risk,"equity_usd":equity,
    }
    evaluation=evaluate(data)
    return {
      "schema":"abutron.issue44.fp_demo_sha_pinned_readonly_release_review.v1",
      "checked_at_utc":dt.datetime.fromtimestamp(t,dt.timezone.utc).isoformat(),
      "account_masked":"••••"+str(config["connection_suffix"]),
      "broker":"FP Markets","environment":"DEMO",
      "source_pins_sha256":hashes,
      "native_current_open_positions":len(positions),
      "native_current_pending_orders":data["pending_orders"],
      "native_current_open_stop_risk_usd":round(risk,3),
      "native_current_equity_usd":round(equity,2),
      "owner_certification_status":owner["status"],
      "owner_unresolved_blockers":owner.get("blockers",[]),
      "v9_certification_status":v9["status"],
      "v9_unresolved_blockers":v9.get("blockers",[]),
      **evaluation
    }

def emit(output:Path,report:dict)->None:
    if output.parent.is_symlink() or not output.parent.is_dir() or
        output.parent.stat().st_uid!=0 or output.parent.stat().st_mode&0o077:
        raise ReleaseEvidenceError("ROOT_PRIVATE_OUTPUT_REQUIRED")
    fd,tmp=tempfile.mkstemp(prefix=".fp-demo-release-review-",dir=output.parent)
    try:
        with os.fdopen(fd,"w") as w:
            os.fchmod(w.fileno(),0o600)
            json.dump(report,w,sort_keys=True,indent=2,allow_nan=False)
            w.write("\n")
            w.flush();os.fsync(w.fileno())
        os.replace(tmp,output)
    finally:
        if os.path.lexists(tmp):os.unlink(tmp)

def main()->None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",required=True)
    ap.add_argument("--output",required=True)
    args=ap.parse_args()
    try:
        report=build(safe_json(Path(args.config),100_000))
    except (ReleaseEvidenceError,OSError,ValueError,TypeError,sqlite3.Error) as exc:
        report={"schema":"abutron.issue44.fp_demo_sha_pinned_readonly_release_review.v1",
                "checked_at_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
                "status":"RELEASE_PROMOTION_INHIBITED",
                "inhibitors":["PRECHECK_INVALID_"+type(exc).__name__],
                "production_promoted":False,
                "broker_trailing_sltp_amendment_certified":False,
                "new_order_execution_authorized":False,
                "live_trading_enabled_by_audit":False,
                "crm_or_risk_flags_modified":False}
    emit(Path(args.output),report)
    print(report["status"],",".join(report["inhibitors"]))
if __name__=="__main__":
    main()
