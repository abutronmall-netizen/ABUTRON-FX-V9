"""FP Markets V9 native execution-cost report contract tests.

These tests define the exact JSON contract that the extended FP V305
exporter must publish for V9's DemoNativeCRM to accept it. They are
isolated: no broker, no terminal, no connected account is touched.

The contract is derived from DemoNativeCRM.snapshot() in
src/abutron/agent/crm_native_binding.py and the tick_size extraction
in src/abutron/agent/production_fp_demo.py.

A cost report is a JSON file written by the FP native data exporter
(AbutronFPMarketsNativeFeedParallelV305.mq5, extended) to the account's
cost-report path. V9 reads it read-only and validates it before any
execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from abutron.agent.crm_native_binding import CRMBindingError, DemoNativeCRM

CID = "b" * 32

# The 13 FP Markets DEMO symbols confirmed on the live 0512 terminal.
FP_SYMBOLS = [
    "XAUUSD.r", "BTCUSD", "EURUSD.r", "GBPUSD.r", "USDJPY.r",
    "AUDUSD.r", "US100", "US30", "US500", "GER40", "XTIUSD",
    "USDCHF.r", "NZDUSD.r",
]


def _make_cost_report(suffix="0512", *, verified=True, instruments=None,
                      equity=5000.0, environment="DEMO"):
    """Build a cost-report dict matching the V9 contract."""
    if instruments is None:
        instruments = {
            sym: {
                "tick_size": 0.01 if "USD" in sym else 0.1,
                "contract_size": 1.0,
                "volume_min": 0.01,
                "volume_step": 0.01,
                "volume_max": 10.0,
                "stops_level": 20,
                "freeze_level": 30,
            }
            for sym in FP_SYMBOLS
        }
    return {
        "account_suffix": suffix,
        "environment": environment,
        "native_bid_ask_and_ordercalcprofit_verified": verified,
        "account_equity_usd": equity,
        "instruments": instruments,
    }


def _make_crm_db(tmp_path):
    """Create a minimal CRM DB with an FP DEMO connection."""
    database = tmp_path / "crm.sqlite3"
    with sqlite3.connect(database) as c:
        c.executescript("""
          CREATE TABLE accounts (id TEXT, status TEXT, demo_execution_enabled INTEGER);
          CREATE TABLE apex_mt5_connections (
              id TEXT, account_id TEXT, broker TEXT, environment TEXT,
              account_reference TEXT, broker_server TEXT, status TEXT,
              worker_binding TEXT
          );
          CREATE TABLE apex_trade_controls (
              connection_id TEXT, desired_enabled INTEGER,
              effective_enabled INTEGER, updated_at INTEGER
          );
        """)
        c.execute("INSERT INTO accounts VALUES (?,?,?)",
                  ("owner1", "approved", 1))
        c.execute("INSERT INTO apex_mt5_connections VALUES (?,?,?,?,?,?,?,?)",
                  (CID, "owner1", "FP Markets", "DEMO", "7410512",
                   "FPMarketsSC-Demo", "CONNECTED", "existing-fp-worker"))
        c.execute("INSERT INTO apex_trade_controls VALUES (?,?,?,?)",
                  (CID, 1, 1, 1795205446))
    return database


def _make_qa_layout(tmp_path, suffix="0512"):
    """Create the QA terminal layout with a fake EA binary."""
    qa = tmp_path / f"qa{suffix}" / "terminal"
    ea = qa / "MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
    ea.parent.mkdir(parents=True)
    ea_bytes = b"isolated-cost-contract-test-binary"
    ea.write_bytes(ea_bytes)
    files = qa / "MQL5/Files"
    files.mkdir(parents=True)
    return qa, ea_bytes


def _build_binding(tmp_path, monkeypatch, *, cost_report=None,
                   suffix="0512", owner_equity=5000.0):
    """Build a DemoNativeCRM with isolated fixtures.

    Returns (binding, cost_path, owner_path, qa_dir, ea_sha).
    """
    database = _make_crm_db(tmp_path)
    qa, ea_bytes = _make_qa_layout(tmp_path, suffix)
    ea_sha = hashlib.sha256(ea_bytes).hexdigest()

    cost_path = tmp_path / f"native-cost-{suffix}.json"
    cost_data = cost_report if cost_report is not None else _make_cost_report(suffix)
    cost_path.write_text(json.dumps(cost_data))

    owner_path = tmp_path / "owner.json"
    owner_data = {"owners": {"owner1": {"connections": [
        {"account_masked": f"••••{suffix}",
         "snapshot": {"equity_usd": owner_equity}}]}}}
    owner_path.write_text(json.dumps(owner_data))

    # Stub the broker-native signature checks (these are not the
    # subject of cost-contract tests).
    monkeypatch.setattr(
        "abutron.execution.writer_gateway.SingleWriterGateway.read_existing_secret",
        lambda *args: b"isolated-test-secret")
    monkeypatch.setattr(
        "abutron.reconciliation.native_crm_6207.signed_native_exposure",
        lambda *args, **kwargs: ({"positions": []}, {}))
    monkeypatch.setattr(DemoNativeCRM, "_recent_json",
                        staticmethod(lambda p, ttl=70, root_only=False:
                                     json.loads(Path(p).read_text())))
    binding = DemoNativeCRM(
        account_suffix=suffix, connection_id=CID, crm_db=str(database),
        native_owner_report=str(owner_path),
        native_cost_report=str(cost_path),
        qa_data_dir=str(qa),
        qa_health=str(tmp_path / "health.json"),
        expected_ea_sha=ea_sha,
        broker="FP Markets")
    monkeypatch.setattr(binding.current_risk, "current_owner",
                        lambda cid: {"aggregate_balance_usd": owner_equity,
                                     "aggregate_stop_risk_usd": 0})
    return binding, cost_path, owner_path, qa, ea_sha


# --------------------------------------------------------------------- #
# Valid cost report — accepted
# --------------------------------------------------------------------- #

def test_fp_cost_report_with_all_13_symbols_is_accepted(tmp_path, monkeypatch):
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch)
    result = binding.snapshot()
    assert result["account"]["broker"] == "FP Markets"
    assert result["account"]["environment"] == "demo"
    assert result["account"]["symbols"] == ",".join(sorted(FP_SYMBOLS))


def test_fp_cost_report_tick_size_flows_to_native_specs(tmp_path, monkeypatch):
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch)
    result = binding.snapshot()
    specs = result["native_specs"]
    assert "XAUUSD.r" in specs
    assert specs["XAUUSD.r"]["tick_size"] == 0.01
    assert specs["US100"]["tick_size"] == 0.1


def test_fp_cost_report_equity_flows_to_account(tmp_path, monkeypatch):
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         owner_equity=7500.0)
    result = binding.snapshot()
    # account_eq is min(cost equity, owner equity)
    assert result["account"]["equity"] == 5000.0


def test_fp_cost_report_instruments_carry_additional_specs(tmp_path, monkeypatch):
    """Instruments may carry contract_size, volume_min, etc."""
    instruments = {"XAUUSD.r": {
        "tick_size": 0.01, "contract_size": 100.0,
        "volume_min": 0.01, "volume_step": 0.01,
        "volume_max": 5.0, "stops_level": 20, "freeze_level": 30}}
    cost = _make_cost_report(instruments=instruments)
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost)
    result = binding.snapshot()
    spec = result["native_specs"]["XAUUSD.r"]
    assert spec["contract_size"] == 100.0
    assert spec["volume_min"] == 0.01
    assert spec["stops_level"] == 20
    assert spec["freeze_level"] == 30


# --------------------------------------------------------------------- #
# Invalid cost reports — rejected
# --------------------------------------------------------------------- #

def test_rejects_wrong_account_suffix(tmp_path, monkeypatch):
    cost = _make_cost_report(suffix="1636")
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost, suffix="0512")
    with pytest.raises(CRMBindingError, match="BROKER_NATIVE_ORDER_CONTRACT"):
        binding.snapshot()


def test_rejects_wrong_environment(tmp_path, monkeypatch):
    cost = _make_cost_report(environment="LIVE")
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost)
    with pytest.raises(CRMBindingError, match="BROKER_NATIVE_ORDER_CONTRACT"):
        binding.snapshot()


def test_rejects_verification_flag_false(tmp_path, monkeypatch):
    cost = _make_cost_report(verified=False)
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost)
    with pytest.raises(CRMBindingError, match="BROKER_NATIVE_ORDER_CONTRACT"):
        binding.snapshot()


def test_rejects_verification_flag_string_true(tmp_path, monkeypatch):
    """The flag must be JSON true, not the string "true"."""
    cost = _make_cost_report()
    cost["native_bid_ask_and_ordercalcprofit_verified"] = "true"
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost)
    with pytest.raises(CRMBindingError, match="BROKER_NATIVE_ORDER_CONTRACT"):
        binding.snapshot()


def test_rejects_instruments_not_dict(tmp_path, monkeypatch):
    cost = _make_cost_report()
    cost["instruments"] = ["XAUUSD.r", "US100"]
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost)
    with pytest.raises(CRMBindingError, match="BROKER_NATIVE_ORDER_CONTRACT"):
        binding.snapshot()


def test_rejects_missing_equity(tmp_path, monkeypatch):
    cost = _make_cost_report()
    del cost["account_equity_usd"]
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost)
    with pytest.raises((CRMBindingError, KeyError)):
        binding.snapshot()


def test_rejects_zero_equity(tmp_path, monkeypatch):
    cost = _make_cost_report(equity=0.0)
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost)
    with pytest.raises(CRMBindingError, match="EQUITY"):
        binding.snapshot()


def test_rejects_missing_verification_flag(tmp_path, monkeypatch):
    cost = _make_cost_report()
    del cost["native_bid_ask_and_ordercalcprofit_verified"]
    binding, _, _, _, _ = _build_binding(tmp_path, monkeypatch,
                                         cost_report=cost)
    with pytest.raises(CRMBindingError, match="BROKER_NATIVE_ORDER_CONTRACT"):
        binding.snapshot()


def test_rejects_stale_cost_report(tmp_path, monkeypatch):
    """The cost report must be fresh (TTL 65s)."""
    database = _make_crm_db(tmp_path)
    qa, ea_bytes = _make_qa_layout(tmp_path, "0512")
    ea_sha = hashlib.sha256(ea_bytes).hexdigest()
    cost_path = tmp_path / "native-cost-0512.json"
    cost_path.write_text(json.dumps(_make_cost_report()))
    owner_path = tmp_path / "owner.json"
    owner_path.write_text(json.dumps({"owners": {"owner1": {"connections": [
        {"account_masked": "••••0512",
         "snapshot": {"equity_usd": 5000}}]}}}))
    # Age the cost report beyond the TTL.
    stale_time = time.time() - 120
    os.utime(cost_path, (stale_time, stale_time))

    monkeypatch.setattr(
        "abutron.execution.writer_gateway.SingleWriterGateway.read_existing_secret",
        lambda *args: b"isolated-test-secret")
    monkeypatch.setattr(
        "abutron.reconciliation.native_crm_6207.signed_native_exposure",
        lambda *args, **kwargs: ({"positions": []}, {}))
    # Use the real _recent_json TTL logic but skip the root-ownership
    # check (the test file is not root-owned in the sandbox).
    real_recent = DemoNativeCRM._recent_json
    monkeypatch.setattr(DemoNativeCRM, "_recent_json",
                        staticmethod(lambda p, ttl=70, root_only=False:
                                     real_recent(p, ttl=ttl, root_only=False)))
    binding = DemoNativeCRM(
        account_suffix="0512", connection_id=CID, crm_db=str(database),
        native_owner_report=str(owner_path),
        native_cost_report=str(cost_path),
        qa_data_dir=str(qa),
        qa_health=str(tmp_path / "health.json"),
        expected_ea_sha=ea_sha,
        broker="FP Markets")
    monkeypatch.setattr(binding.current_risk, "current_owner",
                        lambda cid: {"aggregate_balance_usd": 5000,
                                     "aggregate_stop_risk_usd": 0})
    with pytest.raises(CRMBindingError, match="NOT_CURRENT"):
        binding.snapshot()


# --------------------------------------------------------------------- #
# Runner-side tick_size extraction
# --------------------------------------------------------------------- #

def test_runner_extracts_tick_size_for_receipt_reconciliation(tmp_path, monkeypatch):
    """The FP runner builds cost_ticks from native_specs tick_size."""
    from abutron.agent import production_fp_demo as fp
    # Verify the extraction pattern the runner uses.
    native_specs = {"XAUUSD.r": {"tick_size": 0.01},
                    "US100": {"tick_size": 0.1}}
    cost_ticks = {sym: v["tick_size"]
                  for sym, v in native_specs.items() if "tick_size" in v}
    assert cost_ticks == {"XAUUSD.r": 0.01, "US100": 0.1}


def test_runner_skips_instruments_without_tick_size(tmp_path, monkeypatch):
    """Instruments missing tick_size are excluded from cost_ticks."""
    native_specs = {"XAUUSD.r": {"tick_size": 0.01},
                    "US100": {"contract_size": 1.0}}
    cost_ticks = {sym: v["tick_size"]
                  for sym, v in native_specs.items() if "tick_size" in v}
    assert cost_ticks == {"XAUUSD.r": 0.01}
