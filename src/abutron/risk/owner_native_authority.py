"""Actual current CRM owner/account risk authority for DEMO V9 dispatch.

Read-only against CRM and root-owned broker reconciler report.
Historical exit/daystart research is NOT confused with current open stop risk.
No database writes or broker orders in this module.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import stat
from pathlib import Path
from typing import Any


class NativeOwnerRiskError(ValueError):
    pass


def _finite_nonnegative(value: Any, label: str) -> float:
    if type(value) not in (float, int):
        raise NativeOwnerRiskError(label + "_NOT_NUMERIC")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise NativeOwnerRiskError(label + "_INVALID")
    return result


class CurrentBrokerOwnerRisk:
    """Require ALL of the owner's CRM connections in current native snapshot."""

    def __init__(self, *, crm_db: str, native_risk_report: str,
                 max_age_seconds: int = 90, require_root_owned: bool = True):
        self.crm_db = Path(crm_db)
        self.native_risk_report = Path(native_risk_report)
        self.max_age_seconds = max_age_seconds
        self.require_root_owned = require_root_owned

    def _source(self) -> dict:
        path = self.native_risk_report
        st = path.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_size < 100 or
                st.st_size > 2_000_000 or (st.st_mode & 0o022)
                or (self.require_root_owned and st.st_uid != 0)):
            raise NativeOwnerRiskError("OWNER_NATIVE_SOURCE_UNTRUSTED")
        with path.open("r", encoding="utf-8") as handle:
            report = json.load(handle)
        if report.get("native_current_open_exposure_reconciled") is not True:
            raise NativeOwnerRiskError("OWNER_CURRENT_POSITIONS_NOT_RECONCILED")
        if report.get("unknown_connected_position_sets") != 0:
            raise NativeOwnerRiskError("OWNER_NATIVE_POSITION_SETS_UNKNOWN")
        stamp = dt.datetime.fromisoformat(str(report["checked_utc"]).replace("Z","+00:00"))
        if stamp.tzinfo is None:
            raise NativeOwnerRiskError("OWNER_NATIVE_TIMESTAMP_NO_ZONE")
        age = (dt.datetime.now(dt.timezone.utc)-stamp).total_seconds()
        if not (0 <= age <= self.max_age_seconds):
            raise NativeOwnerRiskError("OWNER_NATIVE_REPORT_STALE")
        return report

    def current_owner(self, connection_id: str) -> dict:
        if not isinstance(connection_id, str) or len(connection_id) != 32:
            raise NativeOwnerRiskError("CRM_CONNECTION_ID_INVALID")
        with sqlite3.connect(
            "file:" + str(self.crm_db) + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT id,account_id,broker,environment,account_reference,
                          status,worker_binding
                   FROM apex_mt5_connections WHERE id=?""", (connection_id,)
            ).fetchone()
            if (row is None or row["status"] != "CONNECTED"
                    or not row["worker_binding"]
                    or row["environment"] != "DEMO"):
                raise NativeOwnerRiskError("CRM_DEMO_BROKER_CONNECTION_NOT_ACTIVE")
            owner_id = str(row["account_id"])
            linked = list(conn.execute(
                """SELECT id,broker,environment,account_reference,status,
                          worker_binding
                   FROM apex_mt5_connections WHERE account_id=?""",
                (owner_id,)))
        if not linked:
            raise NativeOwnerRiskError("CRM_OWNER_HAS_NO_CONNECTIONS")
        registered = {}
        for item in linked:
            suffix = str(item["account_reference"])[-4:]
            if (len(suffix) != 4 or not suffix.isdigit() or suffix in registered
                    or item["status"] != "CONNECTED" or not item["worker_binding"]):
                raise NativeOwnerRiskError("CRM_OWNER_NATIVE_ACCOUNT_SET_INCOMPLETE")
            registered[suffix] = item
        report = self._source()
        groups = []
        for g in report.get("owners", {}).values():
            entries = g.get("connections", [])
            if any(str(e.get("account_masked","")).endswith(
                    str(row["account_reference"])[-4:]) for e in entries):
                groups.append(g)
        if len(groups) != 1:
            raise NativeOwnerRiskError("OWNER_NATIVE_GROUP_NOT_UNIQUE")
        group = groups[0]
        if (group.get("current_open_exposure_reconciled") is not True
                or group.get("unknown_connected_broker_position_sets") != 0
                or group.get("disconnected_or_error_accounts") != 0):
            raise NativeOwnerRiskError("OWNER_NATIVE_CURRENT_INVENTORY_UNVERIFIED")
        seen = set()
        equity = 0.0
        for e in group["connections"]:
            suffix = str(e.get("account_masked", ""))[-4:]
            if suffix not in registered or suffix in seen:
                raise NativeOwnerRiskError("OWNER_NATIVE_BROKER_SET_MISMATCH")
            reg = registered[suffix]
            if (str(reg["broker"]).lower() != str(e.get("broker","")).lower()
                    or str(reg["environment"]).upper() != str(e.get("environment","")).upper()):
                raise NativeOwnerRiskError("OWNER_NATIVE_ACCOUNT_BINDING_MISMATCH")
            snap = e.get("snapshot") or {}
            count = snap.get("positions_count")
            pending = snap.get("pending_orders_count")
            if type(count) is not int or count < 0 or type(pending) is not int or pending < 0:
                raise NativeOwnerRiskError("OWNER_BROKER_POSITION_COUNTS_UNKNOWN")
            value = _finite_nonnegative(snap.get("equity_usd"), "BROKER_EQUITY")
            if pending != 0:
                raise NativeOwnerRiskError("PENDING_BROKER_ORDER_RISK_NOT_INCLUDED")
            if value == 0.0 and count != 0:
                raise NativeOwnerRiskError("ZERO_EQUITY_ACCOUNT_NOT_FLAT")
            equity += value
            seen.add(suffix)
        if seen != set(registered):
            raise NativeOwnerRiskError("OWNER_NATIVE_ACCOUNT_SET_INCOMPLETE")
        if equity <= 0:
            raise NativeOwnerRiskError("OWNER_EQUITY_NOT_POSITIVE")
        risk = _finite_nonnegative(group["current_open_stop_risk_usd"],
                                    "BROKER_CURRENT_STOP_RISK")
        policy = report.get("risk_caps_policy", {})
        pct = _finite_nonnegative(policy.get("aggregate_open_pct"), "OPEN_RISK_PCT") / 100.0
        if not (0 < pct <= 0.02):
            raise NativeOwnerRiskError("OWNER_AGGREGATE_RISK_POLICY_MISMATCH")
        cap = equity * pct
        return {
            "owner_id":owner_id,
            "aggregate_stop_risk_usd":risk,
            "aggregate_balance_usd":equity,
            "cap_pct":pct,
            "cap_usd":cap,
            "headroom_usd":cap-risk,
            "cap_exceeded":risk > cap,
            "owner_risk_certified":True,
            "owner_current_native_reconciled":True,
            "historical_research_not_trading_authority":True,
            "connection_count":len(seen),
        }