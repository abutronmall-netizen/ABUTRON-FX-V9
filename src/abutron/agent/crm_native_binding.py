"""V9 DEMO Deriv native CRM binding for multi-account execution.

Generalized from the proven 6207 binding to support any Deriv DEMO
account (6207, 7593, 9244, etc.). Each account has its own QA root,
activation revision table, owner report and cost report.

Read-only CRM inputs; writes only to the dedicated V9 engine DB via
explicit upsert by the production runner. Never grants LIVE execution.
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

EX5_SHA = "7D0BA974885A98C3D90902EAF5F038808C0369ED3229DCF342DFE435AD1318C7"


class CRMBindingError(ValueError):
    pass


def _activation_table(account_suffix: str) -> str:
    """Return the CRM activation revision table name for an account."""
    return f"abutron_v4_{account_suffix}_activation_revision"


class DemoNativeCRM:
    """Native CRM binding for a single Deriv DEMO account.

    Parameters:
        account_suffix: The account reference suffix (e.g. "6207",
            "7593", "9244"). Used for table names, path derivation,
            and account identity checks.
        connection_id: The 32-char CRM connection ID.
        crm_db: Path to the CRM SQLite database.
        native_owner_report: Path to the owner-wide native risk report.
        native_cost_report: Path to the native execution cost report.
        qa_data_dir: Path to the account's QA terminal data directory
            (containing MQL5/Files and MQL5/Experts).
        qa_health: Path to the QA health report (diagnostic only).
        expected_ea_sha: Expected SHA-256 of the compiled EA binary.
    """

    def __init__(self, *, account_suffix: str = "6207",
                 connection_id: str,
                 crm_db: str, native_owner_report: str,
                 native_cost_report: str, qa_data_dir: str, qa_health: str,
                 expected_ea_sha: str = EX5_SHA):
        self.account_suffix = account_suffix
        self.connection_id = connection_id
        self.crm_db = Path(crm_db)
        self.native_owner_report = native_owner_report
        self.native_cost_report = Path(native_cost_report)
        self.qa_data_dir = Path(qa_data_dir)
        self.qa_health = Path(qa_health)
        self.expected_ea_sha = expected_ea_sha
        self.activation_table = _activation_table(account_suffix)
        self.current_risk = CurrentBrokerOwnerRisk(
            crm_db=str(self.crm_db), native_risk_report=self.native_owner_report)

    @staticmethod
    def _recent_json(p: Path, ttl: int = 70, root_only: bool = False):
        st = p.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_size < 30
                or st.st_size > 1_500_000 or (st.st_mode & 0o022)
                or (root_only and st.st_uid != 0)):
            raise CRMBindingError("NATIVE_ACCOUNT_SOURCE_NOT_TRUSTED")
        if not 0 <= time.time() - st.st_mtime <= ttl:
            raise CRMBindingError("NATIVE_ACCOUNT_SOURCE_NOT_CURRENT")
        with p.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def snapshot(self) -> Dict[str, Any]:
        cid = self.connection_id
        suffix = self.account_suffix
        if len(cid) != 32:
            raise CRMBindingError("CRM_CONNECTION_ID_INVALID")
        with sqlite3.connect("file:" + str(self.crm_db) + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """SELECT c.id,c.account_id,c.broker,c.environment,
                          c.account_reference,c.broker_server,c.status,
                          c.worker_binding,a.status AS owner_status,
                          a.demo_execution_enabled
                   FROM apex_mt5_connections c
                   JOIN accounts a ON a.id=c.account_id
                   WHERE c.id=?""", (cid,)).fetchone()
            if row is None:
                raise CRMBindingError("CRM_CONNECTION_NOT_REGISTERED")
            if not (row["environment"] == "DEMO" and row["broker"] == "Deriv"
                    and str(row["account_reference"]).endswith(suffix)
                    and row["status"] == "CONNECTED" and row["worker_binding"]
                    and row["owner_status"] == "approved"):
                raise CRMBindingError(
                    f"CRM_DEMO_{suffix}_BINDING_NOT_ACTIVE")
            activation = db.execute(
                f"""SELECT revision,desired_enabled,effective_enabled,
                          connection_status,ack_revision,ack_expires_utc
                   FROM {self.activation_table} WHERE connection_id=?""",
                (cid,)).fetchone()
        if activation is None:
            raise CRMBindingError(f"CRM_DEMO_{suffix}_ACTIVATION_MISSING")
        revision = activation["revision"]
        try:
            ack_current = float(activation["ack_expires_utc"]) > time.time()
        except (TypeError, ValueError, OverflowError):
            ack_current = False
        enabled = (activation["desired_enabled"] == 1 and
                   activation["effective_enabled"] == 1 and
                   activation["connection_status"] == "CONNECTED" and
                   activation["ack_revision"] == revision and ack_current)
        if not enabled:
            raise CRMBindingError(
                f"CRM_DEMO_{suffix}_ORDER_SWITCH_OFF_OR_ACK_STALE")
        # Authenticate the actual current HMAC-signed MT5 position feed.
        # The old QA report refreshes every 300 seconds and is not a
        # reliable authority for a 30-second V9 execution cycle.
        from ..execution.writer_gateway import SingleWriterGateway
        from ..reconciliation.native_crm_6207 import signed_native_exposure
        native_files = str(self.qa_data_dir / "MQL5/Files")
        try:
            signed_native_exposure(
                native_files, cid=cid,
                login=int(row["account_reference"]),
                server=str(row["broker_server"]),
                secret=SingleWriterGateway.read_existing_secret(native_files),
                ttl=70)
        except (OSError, ValueError, TypeError) as exc:
            raise CRMBindingError(
                "MT5_NATIVE_SIGNED_BROKER_EXPOSURE_NOT_CURRENT:"
                + type(exc).__name__) from exc
        cost = self._recent_json(self.native_cost_report, ttl=65, root_only=True)
        if (cost.get("account_suffix") != suffix or
                cost.get("environment") != "DEMO" or
                cost.get("native_bid_ask_and_ordercalcprofit_verified") is not True
                or not isinstance(cost.get("instruments"), dict)):
            raise CRMBindingError("BROKER_NATIVE_ORDER_CONTRACT_NOT_AVAILABLE")
        risk = self.current_risk.current_owner(cid)
        ea = self.qa_data_dir / "MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
        if not ea.is_file() or ea.is_symlink():
            raise CRMBindingError("NATIVE_EA_BINARY_NOT_ATTACHED")
        if hashlib.sha256(ea.read_bytes()).hexdigest() != self.expected_ea_sha:
            raise CRMBindingError("NATIVE_EA_BINARY_CHANGED")
        owner_eq = float(risk["aggregate_balance_usd"])
        account_eq = min(float(cost["account_equity_usd"]),
                         float(next(
                             a["snapshot"]["equity_usd"]
                             for g in self._recent_json(
                                 Path(self.native_owner_report),
                                 ttl=70, root_only=True)["owners"].values()
                             for a in g["connections"]
                             if str(a.get("account_masked", "")).endswith(suffix))))
        if account_eq <= 0 or owner_eq <= 0:
            raise CRMBindingError("BROKER_ACCOUNT_EQUITY_UNAVAILABLE")
        account = dict(
            connection_id=cid, owner_id=str(row["account_id"]),
            broker="Deriv", mt5_login=int(row["account_reference"]),
            environment="demo", server=str(row["broker_server"]),
            desired_on=1, effective_on=1,
            conn_status="CONNECTED",
            worker_binding=str(row["worker_binding"]),
            activation_revision=str(revision),
            ea_sha256=self.expected_ea_sha,
            equity=account_eq, account_currency="USD",
            per_trade_risk_pct=0.15, portfolio_cap_pct=0.20,
            symbols="Volatility 50 Index.0",
            native_positions_verified=1,
        )
        writer = dict(ea_path=str(ea), ea_sha256=self.expected_ea_sha,
                      mql5_files_dir=str(self.qa_data_dir / "MQL5/Files"),
                      crm_activation_revision=str(revision),
                      activation_seed=0, use_existing_secret=True)
        return {"account": account, "writer": writer,
                "native_specs": cost["instruments"],
                "owner_open_risk_usd": risk["aggregate_stop_risk_usd"],
                "owner_equity_usd": owner_eq,
                "current_owner_risk": risk,
                "global_demo_flag": bool(row["demo_execution_enabled"]),
                "qa_authenticated": True}


# Backward-compatible alias for the proven 6207 binding.
Demo6207NativeCRM = DemoNativeCRM
