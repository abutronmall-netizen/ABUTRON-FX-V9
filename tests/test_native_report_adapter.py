"""Secondary native operator reports are diagnostics, NEVER order permission."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from abutron.execution.native_report_adapter import inspect_existing_native_reports


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _fixtures(tmp_path):
    fp = tmp_path / "fp" / "latest.json"
    deriv = tmp_path / "deriv" / "summary.json"
    audit = tmp_path / "deriv" / "native-receipts.json"
    for parent in (fp.parent, deriv.parent):
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
    accounts = []
    for suffix in ("0512", "1636"):
        accounts.append({
            "account_suffix": suffix,
            "state": "RECONCILED_READONLY",
            "connected": True, "desired_on": True,
            "effective_on": False, "positions": 0,
            "pending": 0, "unresolved_receipts": 0,
            "historical_broker_ack": 20, "equity_usd": 12000.0,
        })
    fp_doc = {
        "schema": "abutron.fpmarkets.dual_demo_native_reconciliation.readonly.v1",
        "at_utc": _utc_now(), "state": "RECONCILED_READONLY",
        "accounts": accounts, "orders_submitted_by_job": 0,
        "broker_order_authorized": False, "client_switches_modified": False,
    }
    deriv_doc = {
        "schema": "abutron.apex.native.router.summary.v1",
        "checked_at_utc": _utc_now(),
        "mode": "inspect", "status": "READ_ONLY",
        "accounts_checked": 2, "broker_feeds_verified": 2,
        "accounts_dispatch_authorized": 0,
        "orders_submitted": 0,
        "connections": [
            {"connection_id": "a"*32, "status": "BLOCKED",
             "blockers": ["INSPECTION_ONLY_NO_ORDERS"]},
            {"connection_id": "b"*32, "status": "BLOCKED",
             "blockers": ["INSPECTION_ONLY_NO_ORDERS"]},
        ],
    }
    audit_doc = {
        "schema": "abutron.apex.native.receipt.audit.v1",
        "checked_at_utc": _utc_now(), "unresolved_attempts": 0,
        "orders_submitted_by_audit": 0, "receipt_audit": [],
    }
    docs = [(fp, fp_doc), (deriv, deriv_doc), (audit, audit_doc)]
    for path, doc in docs:
        path.write_text(json.dumps(doc), encoding="utf-8")
        path.chmod(0o600)
    return fp, deriv, audit


def _inspect(sources):
    fp, deriv, audit = sources
    return inspect_existing_native_reports(
        fp_path=fp, deriv_path=deriv, deriv_receipts_path=audit)


def _mutate(path: Path, mutator):
    doc = json.loads(path.read_text())
    mutator(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")
    path.chmod(0o600)


def test_current_secondary_reports_valid_but_not_trading_authority(tmp_path):
    output = _inspect(_fixtures(tmp_path))
    assert output["status"] == "SECONDARY_REPORTS_VALID_READONLY"
    assert output["execution_authorized"] is False
    assert output["orders_submitted"] == 0
    for item in output["sources"].values():
        assert item["execution_authorized"] is False
        assert len(item["source_sha256"]) == 64
    assert {a["account_suffix"] for a in
            output["sources"]["fpmarkets"]["accounts"]} == {"0512", "1636"}


@pytest.mark.parametrize("field,value", [
    ("broker_order_authorized", True),
    ("orders_submitted_by_job", 1),
    ("client_switches_modified", True),
    ("schema", "spoofed-protocol"),
])
def test_fp_report_cannot_claim_native_order_permission(tmp_path, field, value):
    sources = _fixtures(tmp_path)
    _mutate(sources[0], lambda d: d.__setitem__(field, value))
    output = _inspect(sources)
    assert output["status"] == "SECONDARY_REPORTS_UNVERIFIED"
    assert output["sources"]["fpmarkets"]["state"] == "UNVERIFIED"
    assert output["execution_authorized"] is False


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "retired", "no_count"])
def test_fp_account_set_exact_no_retired_or_silent_gap(tmp_path, invalid):
    sources = _fixtures(tmp_path)
    def patch(d):
        if invalid == "missing":
            d["accounts"].pop()
        if invalid == "duplicate":
            d["accounts"][1]["account_suffix"] = "0512"
        if invalid == "retired":
            d["accounts"][1]["account_suffix"] = "3492"
        if invalid == "no_count":
            d["accounts"][0].pop("positions")
    _mutate(sources[0], patch)
    out = _inspect(sources)
    assert out["status"] == "SECONDARY_REPORTS_UNVERIFIED"
    assert out["execution_authorized"] is False


def test_effective_on_report_never_automatically_authorizes_v9(tmp_path):
    sources = _fixtures(tmp_path)
    _mutate(sources[0], lambda d: d["accounts"][0].__setitem__(
        "effective_on", True))
    out = _inspect(sources)
    assert out["sources"]["fpmarkets"]["accounts"][0]["effective_on"]
    assert not out["execution_authorized"]
    assert not out["sources"]["fpmarkets"]["execution_authorized"]


@pytest.mark.parametrize("mode", ["dispatch", "exec", ""])
def test_deriv_dispatch_mode_never_treated_as_readonly(tmp_path, mode):
    sources = _fixtures(tmp_path)
    _mutate(sources[1], lambda d: d.__setitem__("mode", mode))
    out = _inspect(sources)
    assert out["sources"]["deriv"]["state"] == "UNVERIFIED"


def test_deriv_summary_must_match_actual_account_count(tmp_path):
    sources = _fixtures(tmp_path)
    _mutate(sources[1], lambda d: d.__setitem__("accounts_checked", 3))
    assert _inspect(sources)["sources"]["deriv"]["state"] == "UNVERIFIED"


def test_deriv_duplicate_connection_ids_invalid(tmp_path):
    sources = _fixtures(tmp_path)
    _mutate(sources[1], lambda d: d["connections"][1].__setitem__(
        "connection_id", "a" * 32))
    assert _inspect(sources)["sources"]["deriv"]["state"] == "UNVERIFIED"


def test_deriv_receipt_audit_attempting_orders_fails(tmp_path):
    sources = _fixtures(tmp_path)
    _mutate(sources[2], lambda d: d.__setitem__(
        "orders_submitted_by_audit", 1))
    assert _inspect(sources)["sources"]["deriv_receipt_audit"]["state"] == "UNVERIFIED"


def test_symlink_and_world_readable_files_rejected(tmp_path):
    sources = _fixtures(tmp_path)
    original = sources[0]
    replacement = original.parent / "real.json"
    original.rename(replacement)
    original.symlink_to(replacement)
    assert _inspect(sources)["sources"]["fpmarkets"]["state"] == "UNVERIFIED"
    original.unlink()
    replacement.rename(original)
    original.chmod(0o644)
    assert _inspect(sources)["sources"]["fpmarkets"]["state"] == "UNVERIFIED"


def test_stale_file_and_stale_signed_clock_rejected(tmp_path):
    sources = _fixtures(tmp_path)
    old = time.time() - 240
    os.utime(sources[0], (old, old))
    assert _inspect(sources)["sources"]["fpmarkets"]["state"] == "UNVERIFIED"
    os.utime(sources[0], None)
    _mutate(sources[0], lambda d: d.__setitem__(
        "at_utc", "2024-01-01T00:00:00+00:00"))
    assert _inspect(sources)["sources"]["fpmarkets"]["state"] == "UNVERIFIED"


def test_malformed_json_does_not_break_other_sources(tmp_path):
    sources = _fixtures(tmp_path)
    sources[0].write_text("{broken!", encoding="utf-8")
    sources[0].chmod(0o600)
    out = _inspect(sources)
    assert out["sources"]["fpmarkets"]["state"] == "UNVERIFIED"
    assert out["sources"]["deriv"]["state"] == "READ_ONLY"
    assert out["execution_authorized"] is False
