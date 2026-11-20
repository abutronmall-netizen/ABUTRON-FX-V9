"""V9 DEMO 6207 binding to the real CRM and authenticated native MT5 source.

Read-only CRM inputs; writes only to the dedicated V9 engine DB via explicit
upsert by the production runner. Never grants LIVE execution.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any, Dict

from ..risk.owner_native_authority import CurrentBrokerOwnerRisk, NativeOwnerRiskError

EX5_SHA = "9d4f6701993865d32fcd5d9e9781c7803e5f1a688bd8f1ca63e85b8eed173b86"


class CRMBindingError(ValueError):
    pass


class Demo6207NativeCRM:
    def __init__(self, *, connection_id: str, crm_db: str, native_owner_report: str,
                 native_cost_report: str, qa_data_dir: str, qa_health: str,
                 expected_ea_sha: str = EX5_SHA):
        self.connection_id = connection_id
        self.crm_db = Path(crm_db)
        self.native_owner_report = native_owner_report
        self.native_cost_report = Path(native_cost_report)
        self.qa_data_dir = Path(qa_data_dir)
        self.qa_health = Path(qa_health)
        self.expected_ea_sha = expected_ea_sha
        self.current_risk = CurrentBrokerOwnerRisk(
            crm_db=str(self.crm_db), native_risk_report=self.native_owner_report)

    @staticmethod
    def _recent_json(p: Path, ttl: int = 70, root_only: bool = False):
        st = p.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_size < 30
                or st.st_size > 1_500_000 or (st.st_mode & 0o022)
                or (root_only and st.st_uid != 0)):
            raise CRMBindingError("NATIVE_ACCOUNT_SOURCE_NOT_TRUSTED")
        if not 0 <= time.time()-st.st_mtime <= ttl:
            raise CRMBindingError("NATIVE_ACCOUNT_SOURCE_NOT_CURRENT")
        with p.open("r",encoding="utf-8") as handle:
            return json.load(handle)

    def snapshot(self) -> Dict[str, Any]:
        cid=self.connection_id
        if len(cid)!=32:
            raise CRMBindingError("CRM_CONNECTION_ID_INVALID")
        with sqlite3.connect("file:"+str(self.crm_db)+"?mode=ro",uri=True) as db:
            db.row_factory=sqlite3.Row
            row=db.execute(
                """SELECT c.id,c.account_id,c.broker,c.environment,
                          c.account_reference,c.broker_server,c.status,
                          c.worker_binding,a.status AS owner_status,
                          a.demo_execution_enabled
                   FROM apex_mt5_connections c
                   JOIN accounts a ON a.id=c.account_id
                   WHERE c.id=?""",(cid,)).fetchone()
            if row is None:
                raise CRMBindingError("CRM_CONNECTION_NOT_REGISTERED")
            if not (row["environment"]=="DEMO" and row["broker"]=="Deriv" and
                    str(row["account_reference"]).endswith("6207") and
                    row["status"]=="CONNECTED" and row["worker_binding"] and
                    row["owner_status"]=="approved"):
                raise CRMBindingError("CRM_DEMO_6207_BINDING_NOT_ACTIVE")
            activation=db.execute(
                """SELECT revision,desired_enabled,effective_enabled,
                          connection_status,ack_revision,ack_expires_utc
                   FROM abutron_v4_6207_activation_revision WHERE connection_id=?""",
                (cid,)).fetchone()
        if activation is None:
            raise CRMBindingError("CRM_DEMO_6207_ACTIVATION_MISSING")
        revision=activation["revision"]
        try:
            ack_current=float(activation["ack_expires_utc"]) > time.time()
        except (TypeError,ValueError,OverflowError):
            ack_current=False
        enabled=(activation["desired_enabled"]==1 and
                 activation["effective_enabled"]==1 and
                 activation["connection_status"]=="CONNECTED" and
                 activation["ack_revision"]==revision and ack_current)
        if not enabled:
            raise CRMBindingError("CRM_DEMO_6207_ORDER_SWITCH_OFF_OR_ACK_STALE")
        native=self._recent_json(self.qa_health,ttl=75)
        if (native.get("status")!="SIGNED_NATIVE_EXPOSURE_VERIFIED" or
                native.get("signature_verified") is not True or
                native.get("native_probe_sha256")!=self.expected_ea_sha):
            raise CRMBindingError("MT5_NATIVE_ACCOUNT_AUTHENTICATION_UNAVAILABLE")
        cost=self._recent_json(self.native_cost_report,ttl=65,root_only=True)
        if (cost.get("account_suffix")!="6207" or
                cost.get("environment")!="DEMO" or
                cost.get("native_bid_ask_and_ordercalcprofit_verified") is not True
                or not isinstance(cost.get("instruments"),dict)):
            raise CRMBindingError("BROKER_NATIVE_ORDER_CONTRACT_NOT_AVAILABLE")
        risk=self.current_risk.current_owner(cid)
        ea=self.qa_data_dir/"MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
        if not ea.is_file() or ea.is_symlink():
            raise CRMBindingError("NATIVE_EA_BINARY_NOT_ATTACHED")
        if hashlib.sha256(ea.read_bytes()).hexdigest()!=self.expected_ea_sha:
            raise CRMBindingError("NATIVE_EA_BINARY_CHANGED")
        owner_eq=float(risk["aggregate_balance_usd"])
        account_eq=min(float(cost["account_equity_usd"]),
                       float(next(
                           a["snapshot"]["equity_usd"]
                           for g in self._recent_json(Path(self.native_owner_report),
                                                      ttl=70,root_only=True)["owners"].values()
                           for a in g["connections"]
                           if str(a.get("account_masked","")).endswith("6207"))))
        if account_eq<=0 or owner_eq<=0:
            raise CRMBindingError("BROKER_ACCOUNT_EQUITY_UNAVAILABLE")
        account=dict(
            connection_id=cid,owner_id=str(row["account_id"]),
            broker="Deriv",mt5_login=int(row["account_reference"]),
            environment="demo",server=str(row["broker_server"]),
            desired_on=1,effective_on=1,
            conn_status="CONNECTED",worker_binding=str(row["worker_binding"]),
            activation_revision=str(revision),
            ea_sha256=self.expected_ea_sha,
            equity=account_eq,account_currency="USD",
            per_trade_risk_pct=0.15,portfolio_cap_pct=0.20,
            symbols="Volatility 50 Index.0",
            native_positions_verified=1,
        )
        writer=dict(ea_path=str(ea),ea_sha256=self.expected_ea_sha,
                    mql5_files_dir=str(self.qa_data_dir/"MQL5/Files"),
                    crm_activation_revision=str(revision),
                    activation_seed=0,use_existing_secret=True)
        return {"account":account,"writer":writer,
                "native_specs":cost["instruments"],
                "owner_open_risk_usd":risk["aggregate_stop_risk_usd"],
                "owner_equity_usd":owner_eq,
                "current_owner_risk":risk,
                "global_demo_flag":bool(row["demo_execution_enabled"]),
                "qa_authenticated":True}