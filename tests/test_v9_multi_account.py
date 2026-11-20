"""V9 multi-account Deriv DEMO execution — regression tests.

Verifies that the generalized CRM binding and production runner
support any Deriv DEMO account suffix (6207, 7593, 9244) with the
same architecture proven on 6207.

All fixtures are synthetic; no real terminal, credentials or broker
data.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from abutron.agent.crm_native_binding import (
    DemoNativeCRM, Demo6207NativeCRM, CRMBindingError, EX5_SHA,
)


# =========================================================================== #
# Generalized CRM binding tests
# =========================================================================== #

class _FakeOwnerRisk:
    """Mock CurrentBrokerOwnerRisk that returns certified exposure."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def current_owner(self, connection_id):
        return {
            "owner_id": "owner1",
            "aggregate_balance_usd": 10000.0,
            "aggregate_stop_risk_usd": 0.0,
            "owner_current_native_reconciled": True,
            "unknown_connected_position_sets": 0,
        }


def _make_crm_db(tmp_path, account_suffix="6207", cid="a" * 32):
    """Create a minimal CRM SQLite DB with the needed tables."""
    db_path = tmp_path / "crm.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE apex_mt5_connections ("
                 "id TEXT, account_id TEXT, broker TEXT, environment TEXT,"
                 "account_reference TEXT, broker_server TEXT, status TEXT,"
                 "worker_binding TEXT)")
    conn.execute("CREATE TABLE accounts ("
                 "id TEXT, status TEXT, demo_execution_enabled INTEGER)")
    conn.execute("CREATE TABLE apex_trade_controls ("
                 "connection_id TEXT, desired_enabled INTEGER,"
                 "effective_enabled INTEGER)")
    table = f"abutron_v4_{account_suffix}_activation_revision"
    conn.execute(f"CREATE TABLE {table} (connection_id TEXT, revision INTEGER,"
                 "desired_enabled INTEGER, effective_enabled INTEGER,"
                 "connection_status TEXT, ack_revision INTEGER,"
                 "ack_expires_utc REAL)")
    conn.execute(
        "INSERT INTO apex_mt5_connections VALUES (?,?,?,?,?,?,?,?)",
        (cid, "owner1", "Deriv", "DEMO", account_suffix, "Deriv-Demo",
         "CONNECTED", "worker-1"))
    conn.execute("INSERT INTO accounts VALUES (?,?,?)",
                 ("owner1", "approved", 1))
    conn.execute("INSERT INTO apex_trade_controls VALUES (?,?,?)",
                 (cid, 1, 1))
    conn.execute(
        f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
        (cid, 5, 1, 1, "CONNECTED", 5, 9999999999.0))
    conn.commit()
    conn.close()
    return str(db_path)


def _make_cost_report(tmp_path, account_suffix="6207"):
    """Create a minimal native cost report JSON."""
    path = tmp_path / f"cost_{account_suffix}.json"
    import json
    path.write_text(json.dumps({
        "account_suffix": account_suffix,
        "environment": "DEMO",
        "native_bid_ask_and_ordercalcprofit_verified": True,
        "account_equity_usd": 5000.0,
        "instruments": {"Volatility 50 Index.0": {"tick_size": 0.1}},
    }))
    return str(path)


def _make_owner_report(tmp_path, account_suffix="6207"):
    """Create a minimal owner risk report JSON."""
    path = tmp_path / f"owner_{account_suffix}.json"
    import json
    from datetime import datetime, timezone
    path.write_text(json.dumps({
        "native_current_open_exposure_reconciled": True,
        "unknown_connected_position_sets": 0,
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "owners": {
            "owner1": {
                "connections": [
                    {"account_masked": f"••••{account_suffix}",
                     "snapshot": {"equity_usd": 5000.0}}
                ]
            }
        }
    }))
    return str(path)


def _make_qa_dir(tmp_path, account_suffix="6207"):
    """Create a minimal QA terminal directory structure."""
    qa = tmp_path / f"qa_{account_suffix}"
    files = qa / "MQL5" / "Files"
    experts = qa / "MQL5" / "Experts" / "ABUTRON"
    files.mkdir(parents=True)
    experts.mkdir(parents=True)
    ea = experts / "ApexNativeWriter.ex5"
    ea.write_bytes(b"fake-ea-binary")
    return str(qa)


def _patch_signed_exposure(monkeypatch):
    """Patch signed_native_exposure, read_existing_secret and _recent_json."""
    import abutron.agent.crm_native_binding as mod

    def _fake_signed_exposure(files_dir, *, cid, login, server, secret, ttl):
        return ({"position_count": 0, "positions": []}, "fake-sha")

    def _fake_read_secret(files_dir):
        return "test-secret-1234567890"

    def _fake_recent_json(p, ttl=70, root_only=False):
        import json as _json
        return _json.loads(open(str(p)).read())

    monkeypatch.setattr(
        "abutron.reconciliation.native_crm_6207.signed_native_exposure",
        _fake_signed_exposure)
    monkeypatch.setattr(
        "abutron.execution.writer_gateway.SingleWriterGateway.read_existing_secret",
        staticmethod(_fake_read_secret))
    monkeypatch.setattr(mod.DemoNativeCRM, "_recent_json", staticmethod(_fake_recent_json))

    class _FakeOwnerRisk:
        def __init__(self, **kwargs):
            pass

        def current_owner(self, connection_id):
            return {
                "owner_id": "owner1",
                "aggregate_balance_usd": 10000.0,
                "aggregate_stop_risk_usd": 0.0,
                "owner_current_native_reconciled": True,
                "unknown_connected_position_sets": 0,
            }

    monkeypatch.setattr(
        "abutron.agent.crm_native_binding.CurrentBrokerOwnerRisk",
        _FakeOwnerRisk)


def _patch_ea_sha(binding, qa_dir):
    """Set the binding's expected EA SHA to the actual test file's hash."""
    import hashlib
    from pathlib import Path
    ea_path = Path(qa_dir) / "MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
    binding.expected_ea_sha = hashlib.sha256(ea_path.read_bytes()).hexdigest()


def test_demo_native_crm_accepts_6207(monkeypatch, tmp_path):
    """DemoNativeCRM works with account_suffix='6207'."""
    _patch_signed_exposure(monkeypatch)
    cid = "a" * 32
    crm_db = _make_crm_db(tmp_path, "6207", cid)
    cost = _make_cost_report(tmp_path, "6207")
    owner = _make_owner_report(tmp_path, "6207")
    qa = _make_qa_dir(tmp_path, "6207")

    binding = DemoNativeCRM(
        account_suffix="6207", connection_id=cid, crm_db=crm_db,
        native_owner_report=owner, native_cost_report=cost,
        qa_data_dir=qa, qa_health="")
    _patch_ea_sha(binding, qa)
    snapshot = binding.snapshot()
    assert snapshot["account"]["connection_id"] == cid
    assert snapshot["account"]["broker"] == "Deriv"
    assert snapshot["account"]["mt5_login"] == 6207
    assert snapshot["qa_authenticated"] is True


def test_demo_native_crm_accepts_7593(monkeypatch, tmp_path):
    """DemoNativeCRM works with account_suffix='7593'."""
    _patch_signed_exposure(monkeypatch)
    cid = "b" * 32
    crm_db = _make_crm_db(tmp_path, "7593", cid)
    cost = _make_cost_report(tmp_path, "7593")
    owner = _make_owner_report(tmp_path, "7593")
    qa = _make_qa_dir(tmp_path, "7593")

    binding = DemoNativeCRM(
        account_suffix="7593", connection_id=cid, crm_db=crm_db,
        native_owner_report=owner, native_cost_report=cost,
        qa_data_dir=qa, qa_health="")
    _patch_ea_sha(binding, qa)
    snapshot = binding.snapshot()
    assert snapshot["account"]["connection_id"] == cid
    assert snapshot["account"]["mt5_login"] == 7593


def test_demo_native_crm_accepts_9244(monkeypatch, tmp_path):
    """DemoNativeCRM works with account_suffix='9244'."""
    _patch_signed_exposure(monkeypatch)
    cid = "c" * 32
    crm_db = _make_crm_db(tmp_path, "9244", cid)
    cost = _make_cost_report(tmp_path, "9244")
    owner = _make_owner_report(tmp_path, "9244")
    qa = _make_qa_dir(tmp_path, "9244")

    binding = DemoNativeCRM(
        account_suffix="9244", connection_id=cid, crm_db=crm_db,
        native_owner_report=owner, native_cost_report=cost,
        qa_data_dir=qa, qa_health="")
    _patch_ea_sha(binding, qa)
    snapshot = binding.snapshot()
    assert snapshot["account"]["connection_id"] == cid
    assert snapshot["account"]["mt5_login"] == 9244


def test_demo_6207_alias_works(monkeypatch, tmp_path):
    """Demo6207NativeCRM is an alias for DemoNativeCRM."""
    assert Demo6207NativeCRM is DemoNativeCRM


def test_activation_table_name_derived(monkeypatch, tmp_path):
    """The activation table name is derived from the account suffix."""
    binding = DemoNativeCRM(
        account_suffix="7593", connection_id="d" * 32,
        crm_db="/nonexistent", native_owner_report="/nonexistent",
        native_cost_report="/nonexistent", qa_data_dir="/nonexistent",
        qa_health="/nonexistent")
    assert binding.activation_table == "abutron_v4_7593_activation_revision"


def test_wrong_account_suffix_rejected(monkeypatch, tmp_path):
    """A binding configured for one suffix rejects another account."""
    _patch_signed_exposure(monkeypatch)
    cid = "e" * 32
    crm_db = _make_crm_db(tmp_path, "7593", cid)
    cost = _make_cost_report(tmp_path, "7593")
    owner = _make_owner_report(tmp_path, "7593")
    qa = _make_qa_dir(tmp_path, "7593")

    binding = DemoNativeCRM(
        account_suffix="9244", connection_id=cid, crm_db=crm_db,
        native_owner_report=owner, native_cost_report=cost,
        qa_data_dir=qa, qa_health="")
    with pytest.raises(CRMBindingError):
        binding.snapshot()


def test_production_runner_path_derivation():
    """_account_paths derives correct paths for any suffix.

    Owner-risk report is shared across all Deriv DEMO accounts (published
    by the 6207 supervisor).  Cost report is account-specific: 6207 keeps
    the verified supervisor-dir location; 7593/9244 use per-account
    abutron-deriv-v4-{suffix} directories.
    """
    from abutron.agent.production_deriv_demo import _account_paths
    paths = _account_paths("6207")
    assert "6207" in str(paths["qa_root"])
    assert paths["owner"] == "/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json"
    assert paths["cost"] == "/var/lib/abutron-v4-broker-crm-supervisor-6207/v4-native-execution-cost-6207.json"
    assert "6207" in str(paths["data_root"])

    paths = _account_paths("7593")
    assert "7593" in str(paths["qa_root"])
    # Owner is shared — points at the 6207 supervisor dir, not a 7593 dir.
    assert paths["owner"] == "/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json"
    assert paths["cost"] == "/var/lib/abutron-deriv-v4-7593/v4-native-execution-cost-7593.json"
    assert "7593" in str(paths["data_root"])

    paths = _account_paths("9244")
    assert "9244" in str(paths["qa_root"])
    assert paths["owner"] == "/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json"
    assert paths["cost"] == "/var/lib/abutron-deriv-v4-9244/v4-native-execution-cost-9244.json"
    assert "9244" in str(paths["data_root"])


def test_activation_module_account_sql():
    """The generalized activation module builds correct SQL per account."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "activation_deriv_demo",
        os.path.join(os.path.dirname(__file__), "..",
                     "ops", "v9-cutover", "activation_deriv_demo.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sql = mod._account_sql("7593")
    assert "abutron_v4_7593_activation_revision" in sql
    assert "LIKE" in sql
    sql2 = mod._account_sql("9244")
    assert "abutron_v4_9244_activation_revision" in sql2


def test_activation_module_paths():
    """The generalized activation module derives paths per account."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "activation_deriv_demo2",
        os.path.join(os.path.dirname(__file__), "..",
                     "ops", "v9-cutover", "activation_deriv_demo.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    paths = mod._account_paths("7593")
    assert "7593" in str(paths["qa_root"])
    # Owner is shared — published by the 6207 supervisor, read by all.
    assert "abutron-v4-broker-crm-supervisor-6207" in str(paths["owner_report"])
    assert "customer-wide-risk-evidence.json" in str(paths["owner_report"])
    assert str(paths["cost_report"]) == "/var/lib/abutron-deriv-v4-7593/v4-native-execution-cost-7593.json"
    assert paths["activation_table"] == "abutron_v4_7593_activation_revision"

    # 6207 keeps its verified supervisor-dir cost report.
    paths_6207 = mod._account_paths("6207")
    assert str(paths_6207["cost_report"]) == "/var/lib/abutron-v4-broker-crm-supervisor-6207/v4-native-execution-cost-6207.json"
    assert "abutron-v4-broker-crm-supervisor-6207" in str(paths_6207["owner_report"])

def test_v9_native_runner_uses_proven_account_sized_dispatch(monkeypatch):
    from abutron.agent import runtime
    from unittest.mock import Mock
    marker=object()
    native=Mock(return_value=marker)
    monkeypatch.setattr(runtime, "_run_native_agentic_pass",native)
    result=runtime.run_agentic_pass(
        "a"*32, broker="Deriv", owner_id="owner",
        native_specs={"Volatility 50 Index.0": {"tick_size": 0.01}},
        native_writer={"ea_path":"account-specific"},
        owner_open_risk_usd=0.0, owner_equity_usd=1000.0)
    assert result is marker
    assert native.call_args.kwargs["native_writer"]["ea_path"] == "account-specific"
    assert native.call_args.kwargs["native_specs"]["Volatility 50 Index.0"]["tick_size"] == 0.01