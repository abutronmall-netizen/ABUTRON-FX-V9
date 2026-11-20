#!/usr/bin/python3
"""V9-native Deriv DEMO 6207 CRM activation authority.

Independent of legacy APEX11/V4 supervisor certifications.
Only current authenticated broker source and CRM client intent.
Does not send broker orders or enable LIVE.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from abutron.execution.writer_gateway import SingleWriterGateway
from abutron.reconciliation.native_crm_6207 import signed_native_exposure
from abutron.risk.owner_native_authority import CurrentBrokerOwnerRisk

CRM = "/var/lib/apex-signup/accounts.sqlite3"
QA_ROOT = Path("/var/lib/private/abutron-pr37-direct-writer-demo6207-qa")
FILES = QA_ROOT / "terminal/MQL5/Files"
EA = QA_ROOT / "terminal/MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
EA_SHA = "7D0BA974885A98C3D90902EAF5F038808C0369ED3229DCF342DFE435AD1318C7"
OWNER_REPORT = Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json")
COST_REPORT = Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/v4-native-execution-cost-6207.json")
ACCOUNTSQL = """SELECT c.id,c.account_id,c.account_reference,c.broker_server,
                       c.status,c.worker_binding,a.status,t.desired_enabled,
                       t.effective_enabled,f.revision,f.desired_enabled,
                       f.effective_enabled,f.connection_status
                  FROM apex_mt5_connections c
                  JOIN accounts a ON a.id=c.account_id
                  JOIN apex_trade_controls t ON t.connection_id=c.id
                  JOIN abutron_v4_6207_activation_revision f ON f.connection_id=c.id
                 WHERE c.broker='Deriv' AND c.environment='DEMO'
                   AND c.account_reference LIKE '%6207'"""

def recent_json(path: Path, maximum_age: int) -> dict:
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022
            or st.st_size < 20 or st.st_size > 2_000_000
            or not 0 <= time.time() - st.st_mtime <= maximum_age):
        raise ValueError("NATIVE_SOURCE_NOT_CURRENT_OR_TRUSTED")
    return json.loads(path.read_text())

def crm_row(conn: sqlite3.Connection) -> tuple:
    matches = conn.execute(ACCOUNTSQL).fetchall()
    if len(matches) != 1:
        raise ValueError("CRM_DEMO6207_NOT_UNIQUE")
    row = matches[0]
    (cid, account, login, server, status, binding, approved,
     desired, effective, rev, f_desired, f_effective, f_status) = row
    if (len(cid) != 32 or (not str(login).isdecimal() or not str(login).endswith("6207")) or server != "Deriv-Demo"
            or approved != "approved" or not binding
            or desired not in (0, 1) or effective not in (0, 1)
            or rev < 1 or (f_desired, f_effective, f_status)
            != (desired, effective, status)):
        raise ValueError("CRM_IDENTITY_OR_REVISION_INCONSISTENT")
    return row

def inspect() -> dict:
    with sqlite3.connect("file:" + CRM + "?mode=ro", uri=True) as db:
        row = crm_row(db)
    cid, _account, login, server, status, binding, _approved, desired, effective, rev, *_ = row
    if desired != 1:
        return {"ready": False, "reason": "CLIENT_REQUESTED_OFF",
                "desired": 0, "effective": effective, "revision": rev}
    if status != "CONNECTED":
        return {"ready": False, "reason": "BROKER_CONNECTION_NOT_CURRENT",
                "desired": desired, "effective": effective, "revision": rev}
    # V9 authenticates the pinned native EA and fresh HMAC-signed broker
    # exposure below; the 300-second legacy QA health report is diagnostic.
    if not EA.is_file() or EA.is_symlink() or hashlib.sha256(EA.read_bytes()).hexdigest() != EA_SHA:
        raise ValueError("NATIVE_EA_BINARY_CHANGED")
    cost = recent_json(COST_REPORT, 65)
    if (cost.get("account_suffix") != "6207" or cost.get("environment") != "DEMO"
            or cost.get("native_bid_ask_and_ordercalcprofit_verified") is not True):
        raise ValueError("NATIVE_BROKER_CONTRACT_NOT_VERIFIED")
    native, exposure_sha = signed_native_exposure(
        str(FILES), cid=cid, login=int(login), server=server,
        secret=SingleWriterGateway.read_existing_secret(str(FILES)), ttl=70)
    risk = CurrentBrokerOwnerRisk(
        crm_db=CRM, native_risk_report=str(OWNER_REPORT)).current_owner(cid)
    if risk.get("owner_current_native_reconciled") is not True:
        raise ValueError("BROKER_OWNER_POSITIONS_NOT_RECONCILED")
    return {"ready": True, "reason": "V9_NATIVE_ACCOUNT_AUTHENTICATED",
            "cid": cid, "desired": desired, "effective": effective,
            "revision": rev, "exposure_sha": exposure_sha,
            "position_count": native["position_count"],
            "owner_native_risk_verified": True}

def renew() -> dict:
    check = inspect()
    if check["ready"] is not True:
        return {"state": "CLIENT_OFF_OR_BROKER_UNAVAILABLE_NO_CHANGE",
                "reason": check["reason"], "orders_submitted": 0}
    now = int(time.time())
    with sqlite3.connect(CRM, isolation_level=None, timeout=10) as db:
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("BEGIN IMMEDIATE")
        try:
            row = crm_row(db)
            cid, owner, _login, _server, status, _binding, _approved, desired, effective, rev, *_ = row
            if (cid != check["cid"] or desired != 1 or status != "CONNECTED"
                    or rev != check["revision"]):
                raise ValueError("CONCURRENT_CRM_REVISION_CHANGED")
            restored = False
            if effective != 1:
                count = db.execute(
                    """UPDATE apex_trade_controls SET effective_enabled=1,
                              last_error=NULL, updated_at=?
                        WHERE connection_id=? AND desired_enabled=1 AND effective_enabled=0""",
                    (now, cid)).rowcount
                if count != 1:
                    raise ValueError("CONCURRENT_CLIENT_CONTROL_CHANGED")
                restored = True
            revision, desired2, effective2, status2 = db.execute(
                """SELECT revision,desired_enabled,effective_enabled,connection_status
                   FROM abutron_v4_6207_activation_revision WHERE connection_id=?""",
                (cid,)).fetchone()
            if (desired2, effective2, status2) != (1, 1, "CONNECTED"):
                raise ValueError("CRM_CONTROL_FENCE_CHANGED")
            changed = db.execute(
                """UPDATE abutron_v4_6207_activation_revision
                      SET ack_revision=?, ack_expires_utc=?, updated_utc=?,
                          last_event='V9_NATIVE_DEMO_READY_ACK'
                    WHERE connection_id=? AND revision=? AND desired_enabled=1
                      AND effective_enabled=1 AND connection_status='CONNECTED'""",
                (revision, now + 45,
                 dt.datetime.now(dt.timezone.utc).isoformat(), cid, revision)).rowcount
            if changed != 1:
                raise ValueError("ACTIVATION_REVISION_NOT_CURRENT")
            if restored:
                db.execute(
                    """INSERT INTO apex_trade_control_audit
                       (account_id,connection_id,requested_enabled,outcome,occurred_at)
                       VALUES(?,?,?,?,?)""",
                    (owner,cid,1,"V9_AUTHENTIC_NATIVE_DEMO_CLIENT_ON_RESTORED_NO_ORDER",now))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"state": "V9_NATIVE_DEMO_READY_ACK", "revision": revision,
            "effective_demo_on": True, "restored_after_heartbeat": restored,
            "broker_native_position_count": check["position_count"],
            "orders_submitted": 0}

def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--inspect", action="store_true")
    group.add_argument("--ack", action="store_true")
    args = parser.parse_args()
    if args.ack and os.geteuid() != 0:
        raise RuntimeError("ROOT_REQUIRED")
    try:
        result = inspect() if args.inspect else renew()
        # No secrets or private identifiers appear in stdout.
        result.pop("cid", None)
        result.pop("exposure_sha", None)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as e:
        print(json.dumps({"state": "V9_NATIVE_ACK_UNAVAILABLE",
                          "reason": type(e).__name__ + ":" + str(e)[:100],
                          "orders_submitted": 0}))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
