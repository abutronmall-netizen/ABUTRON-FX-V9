"""Strict, read-only adapter for existing ABUTRON native operator reports.

These are SECONDARY root-owned reconciliation reports, not direct authenticated
MT5 execution receipts. They may drive diagnostics, never order authorization.
No broker folders, CRM database, trades, settings or services are modified.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FP_REPORT = Path("/var/lib/abutron-fp-execution/dual-account-reconciliation/latest.json")
DERIV_REPORT = Path("/var/lib/apex-client-feed/execution-router/summary.json")
DERIV_RECEIPTS = Path("/var/lib/apex-client-feed/execution-router/native-receipts.json")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
MAX_BYTES = 96_000
MAX_AGE_SECONDS = 120


class NativeReportRefused(ValueError):
    """Read-only operator evidence cannot be trusted for inspection."""


def _read_json_root_owned(path: Path, *, now: float) -> tuple[dict, str]:
    """Read without following symlinks or racing on the content's inode."""
    parent = path.parent
    for directory in (parent, parent.parent):
        st = directory.lstat()
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != 0
                or st.st_mode & 0o022):
            raise NativeReportRefused("NATIVE_REPORT_DIRECTORY_NOT_ROOT_PROTECTED")
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0
            or st.st_mode & 0o077 or st.st_size > MAX_BYTES
            or not -5 <= now - st.st_mtime <= MAX_AGE_SECONDS):
        raise NativeReportRefused("NATIVE_REPORT_FILE_NOT_PROTECTED_OR_FRESH")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        fst = os.fstat(fd)
        if (fst.st_ino != st.st_ino or fst.st_dev != st.st_dev
                or fst.st_size != st.st_size):
            raise NativeReportRefused("NATIVE_REPORT_FILE_CHANGED_DURING_READ")
        raw = os.read(fd, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or len(raw) != fst.st_size:
            raise NativeReportRefused("NATIVE_REPORT_FILE_INCOMPLETE")
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeReportRefused("NATIVE_REPORT_JSON_INVALID") from exc
    if not isinstance(payload, dict):
        raise NativeReportRefused("NATIVE_REPORT_NOT_AN_OBJECT")
    return payload, hashlib.sha256(raw).hexdigest()


def _validate_utc(value: Any, *, now: float) -> None:
    if not isinstance(value, str):
        raise NativeReportRefused("NATIVE_REPORT_TIMESTAMP_MISSING")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timezone required")
        age = now - timestamp.timestamp()
        if not -5 <= age <= MAX_AGE_SECONDS:
            raise ValueError("stale timestamp")
    except (TypeError, ValueError, OverflowError) as exc:
        raise NativeReportRefused("NATIVE_REPORT_TIMESTAMP_NOT_FRESH") from exc


def _uint(value: Any, *, max_value: int = 10**9) -> int:
    if type(value) is not int or not 0 <= value <= max_value:
        raise NativeReportRefused("NATIVE_REPORT_COUNTER_INVALID")
    return value


def _fp_report(payload: dict, *, now: float) -> dict:
    if payload.get("schema") != "abutron.fpmarkets.dual_demo_native_reconciliation.readonly.v1":
        raise NativeReportRefused("FP_NATIVE_REPORT_SCHEMA_INVALID")
    _validate_utc(payload.get("at_utc"), now=now)
    if (payload.get("broker_order_authorized") is not False
            or payload.get("client_switches_modified") is not False
            or payload.get("orders_submitted_by_job") != 0):
        raise NativeReportRefused("FP_REPORT_NOT_READONLY")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or len(accounts) != 2:
        raise NativeReportRefused("FP_NATIVE_ACCOUNT_SET_INCOMPLETE")
    found: dict[str, dict] = {}
    for a in accounts:
        if not isinstance(a, dict):
            raise NativeReportRefused("FP_NATIVE_ACCOUNT_ROW_INVALID")
        suffix = a.get("account_suffix")
        if suffix not in ("0512", "1636") or suffix in found:
            raise NativeReportRefused("FP_NATIVE_ACCOUNT_SET_INVALID")
        state = a.get("state")
        if state not in ("RECONCILED_READONLY", "BLOCKED"):
            raise NativeReportRefused("FP_NATIVE_ACCOUNT_STATE_INVALID")
        if state == "RECONCILED_READONLY":
            if (type(a.get("connected")) is not bool or
                    type(a.get("desired_on")) is not bool or
                    type(a.get("effective_on")) is not bool):
                raise NativeReportRefused("FP_NATIVE_ACCOUNT_CONTROLS_INVALID")
            for k in ("positions", "pending", "historical_broker_ack",
                      "unresolved_receipts"):
                _uint(a.get(k))
            equity = a.get("equity_usd")
            if (type(equity) not in (int, float) or
                    not math.isfinite(equity) or equity <= 0):
                raise NativeReportRefused("FP_NATIVE_BROKER_EQUITY_INVALID")
        found[suffix] = {
            "account_suffix": suffix,
            "state": state,
            "connected": a.get("connected") is True,
            "effective_on": a.get("effective_on") is True,
            "pending_count": a.get("pending") if state == "RECONCILED_READONLY" else None,
            "positions_count": a.get("positions") if state == "RECONCILED_READONLY" else None,
            "unresolved_receipts": a.get("unresolved_receipts") if state == "RECONCILED_READONLY" else None,
            "orders_authorized_by_this_adapter": False,
        }
    if set(found) != {"0512", "1636"}:
        raise NativeReportRefused("FP_NATIVE_ACCOUNT_SET_INCOMPLETE")
    if (payload.get("state") != "RECONCILED_READONLY" or
            any(a["state"] != "RECONCILED_READONLY" for a in found.values())):
        # Report is readable, but does not count as current reconciliation.
        return {"state": "BLOCKED", "accounts": [found["0512"], found["1636"]],
                "execution_authorized": False}
    return {"state": "RECONCILED_READONLY",
            "accounts": [found["0512"], found["1636"]],
            "execution_authorized": False}


def _deriv_report(payload: dict, *, now: float) -> dict:
    if payload.get("schema") != "abutron.apex.native.router.summary.v1":
        raise NativeReportRefused("DERIV_NATIVE_REPORT_SCHEMA_INVALID")
    _validate_utc(payload.get("checked_at_utc"), now=now)
    if (payload.get("mode") != "inspect" or payload.get("status") != "READ_ONLY"
            or payload.get("orders_submitted") != 0
            or payload.get("accounts_dispatch_authorized") != 0):
        raise NativeReportRefused("DERIV_REPORT_NOT_READONLY")
    accounts = payload.get("connections")
    if not isinstance(accounts, list):
        raise NativeReportRefused("DERIV_REPORT_ACCOUNT_SET_INVALID")
    total = _uint(payload.get("accounts_checked"), max_value=1000)
    _uint(payload.get("broker_feeds_verified"), max_value=1000)
    if total != len(accounts):
        raise NativeReportRefused("DERIV_REPORT_ACCOUNT_COUNT_MISMATCH")
    seen = set()
    for row in accounts:
        if not isinstance(row, dict):
            raise NativeReportRefused("DERIV_REPORT_CONNECTION_INVALID")
        cid = row.get("connection_id")
        if not isinstance(cid, str) or not HEX32.fullmatch(cid) or cid in seen:
            raise NativeReportRefused("DERIV_REPORT_CONNECTION_ID_INVALID")
        seen.add(cid)
        if row.get("status") != "BLOCKED" or not isinstance(row.get("blockers"), list):
            raise NativeReportRefused("DERIV_REPORT_NOT_INSPECTION_ONLY")
    return {
        "state": "READ_ONLY",
        "accounts_checked": total,
        "broker_feeds_verified": payload["broker_feeds_verified"],
        "accounts_dispatch_authorized": 0,
        "execution_authorized": False,
    }


def _deriv_receipt_audit(payload: dict, *, now: float) -> dict:
    if payload.get("schema") != "abutron.apex.native.receipt.audit.v1":
        raise NativeReportRefused("DERIV_RECEIPT_AUDIT_SCHEMA_INVALID")
    _validate_utc(payload.get("checked_at_utc"), now=now)
    unresolved = _uint(payload.get("unresolved_attempts"))
    if payload.get("orders_submitted_by_audit") != 0:
        raise NativeReportRefused("DERIV_RECEIPT_AUDIT_UNEXPECTED_ORDER")
    if not isinstance(payload.get("receipt_audit"), list):
        raise NativeReportRefused("DERIV_RECEIPT_AUDIT_ROWS_INVALID")
    return {
        "state": "READ_ONLY",
        "unresolved_attempts": unresolved,
        "execution_authorized": False,
    }


def inspect_existing_native_reports(*, fp_path: Path = FP_REPORT,
                                    deriv_path: Path = DERIV_REPORT,
                                    deriv_receipts_path: Path = DERIV_RECEIPTS,
                                    now: float | None = None) -> dict:
    """Summarize validated secondary operator reports; no order capability.

    A valid report does NOT make a V9 candidate broker-approved and must not
    bypass the customer-wide native exposure/OrderCalcProfit audit.
    """
    instant = time.time() if now is None else float(now)
    sources = (
        ("fpmarkets", fp_path, _fp_report),
        ("deriv", deriv_path, _deriv_report),
        ("deriv_receipt_audit", deriv_receipts_path, _deriv_receipt_audit),
    )
    output = {
        "schema": "abutron.v9.secondary_native_report_adapter.readonly.v1",
        "evidence_class": "ROOT_OWNED_SECONDARY_OPERATOR_REPORT_NOT_DIRECT_MT5_PROOF",
        "checked_at_utc": datetime.fromtimestamp(
            instant, tz=timezone.utc).isoformat(),
        "execution_authorized": False,
        "orders_submitted": 0,
        "sources": {},
        "blockers": [],
    }
    for name, path, validator in sources:
        try:
            doc, sha = _read_json_root_owned(Path(path), now=instant)
            row = validator(doc, now=instant)
            output["sources"][name] = {**row, "source_sha256": sha}
            if row["state"] == "BLOCKED":
                output["blockers"].append(name.upper() + "_RECONCILIATION_BLOCKED")
        except (OSError, NativeReportRefused) as exc:
            reason = (str(exc) if isinstance(exc, NativeReportRefused) else
                      "NATIVE_REPORT_MISSING_OR_UNREADABLE")
            output["sources"][name] = {"state": "UNVERIFIED",
                                       "execution_authorized": False}
            output["blockers"].append(name.upper() + "_" + reason)
    output["status"] = "SECONDARY_REPORTS_VALID_READONLY" if not output["blockers"] else "SECONDARY_REPORTS_UNVERIFIED"
    return output
