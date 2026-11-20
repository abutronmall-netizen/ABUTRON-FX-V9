"""Regression tests for FP V9 support using existing Ubuntu CRM controls.

All fixtures are isolated temp files; no connected account is modified.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest
from abutron.agent.crm_native_binding import CRMBindingError, DemoNativeCRM


ROOT = Path(__file__).resolve().parents[1]
FP_ENABLE = ROOT / "ops/v9-cutover/enable_trading_fp_demo.py"
CID = "a" * 32


def fixture_fp(tmp_path: Path, monkeypatch, enabled=1):
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
        c.execute("INSERT INTO accounts VALUES (?,?,?)", ("owner1", "approved", 1))
        c.execute("INSERT INTO apex_mt5_connections VALUES (?,?,?,?,?,?,?,?)",
                  (CID, "owner1", "FP Markets", "DEMO", "7410512",
                   "FPMarkets-Demo", "CONNECTED", "existing-fp-worker"))
        c.execute("INSERT INTO apex_trade_controls VALUES (?,?,?,?)",
                  (CID, 1, enabled, 1795205446))
    qa = tmp_path / "qa" / "terminal"
    ea = qa / "MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
    ea.parent.mkdir(parents=True)
    ea.write_bytes(b"isolated-v9-test-binary")
    files = qa / "MQL5/Files"
    files.mkdir()
    cost = tmp_path / "native-cost.json"
    cost.write_text("{}")
    owner = tmp_path / "owner.json"
    owner.write_text("{}")
    binding = DemoNativeCRM(
        account_suffix="0512", connection_id=CID, crm_db=str(database),
        native_owner_report=str(owner), native_cost_report=str(cost),
        qa_data_dir=str(qa), qa_health=str(tmp_path / "health.json"),
        expected_ea_sha=hashlib.sha256(ea.read_bytes()).hexdigest(),
        broker="FP Markets")
    monkeypatch.setattr(
        "abutron.execution.writer_gateway.SingleWriterGateway.read_existing_secret",
        lambda *args: b"isolated-test-secret")
    monkeypatch.setattr(
        "abutron.reconciliation.native_crm_6207.signed_native_exposure",
        lambda *args, **kwargs: ({"positions": []}, {}))
    def recent(p, ttl=70, root_only=False):
        if Path(p) == cost:
            return {
                "account_suffix":"0512","environment":"DEMO",
                "native_bid_ask_and_ordercalcprofit_verified":True,
                "instruments":{
                    "XAUUSD.r":{"tick_size":0.01},
                    "US100":{"tick_size":0.1}},
                "account_equity_usd":5000}
        return {"owners":{"owner1":{"connections":[{
            "account_masked":"••••0512","snapshot":{"equity_usd":5000}}]}}}
    monkeypatch.setattr(DemoNativeCRM, "_recent_json", staticmethod(recent))
    monkeypatch.setattr(binding.current_risk, "current_owner",
                        lambda cid: {"aggregate_balance_usd":5000,
                                     "aggregate_stop_risk_usd":0})
    return database, binding


def test_fp_uses_existing_trade_controls_no_new_activation_tables(tmp_path,monkeypatch):
    db, binding = fixture_fp(tmp_path,monkeypatch)
    with sqlite3.connect(db) as c:
        assert c.execute("select count(*) from sqlite_master where name='abutron_v4_0512_activation_revision'").fetchone()[0]==0
    result = binding.snapshot()
    assert result["account"]["broker"] == "FP Markets"
    assert result["account"]["symbols"] == "US100,XAUUSD.r"
    assert result["writer"]["crm_activation_revision"] == "1795205446"
    assert result["account"]["desired_on"] == 1


def test_fp_existing_client_off_is_not_reenabled(tmp_path,monkeypatch):
    _, binding = fixture_fp(tmp_path,monkeypatch,enabled=0)
    with pytest.raises(CRMBindingError,match="ORDER_SWITCH_OFF"):
        binding.snapshot()


def test_deriv_still_requires_original_revision_table():
    source=(ROOT/"src/abutron/agent/crm_native_binding.py").read_text()
    assert "FROM {self.activation_table}" in source
    assert 'self.broker == "FP Markets"' in source


def load_enablement():
    spec=importlib.util.spec_from_file_location("fp_enablement_parity", FP_ENABLE)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enablement_rejects_missing_fp_writer_before_preset_write(tmp_path,monkeypatch):
    m=load_enablement()
    monkeypatch.setattr(m,"resolve_connection_id",lambda s: CID)
    monkeypatch.setattr(m,"_account_paths",
                        lambda s: {"qa_root":tmp_path/f"fp{s}",
                                   "data_root":str(tmp_path/f"data{s}")})
    writes=[]
    monkeypatch.setattr(m,"write_preset",lambda s:writes.append(s))
    assert m.main([])==1
    assert writes==[]


def test_enablement_never_changes_first_account_if_second_not_ready(tmp_path,monkeypatch):
    m=load_enablement()
    first=tmp_path/"fp0512/terminal/MQL5/Presets/PR37WriterExposureOnly.set"
    first.parent.mkdir(parents=True)
    original="InpConnectionId="+CID+"\nInpBrokerServer=FPMarkets-Demo\nInpActivationSeed=test-only-seed\nInpExposureOnly=false\n"
    first.write_text(original)
    (tmp_path/"data0512").mkdir()
    monkeypatch.setattr(m,"resolve_connection_id",lambda s: CID)
    monkeypatch.setattr(m,"_account_paths",
                        lambda s: {"qa_root":tmp_path/f"fp{s}",
                                   "data_root":str(tmp_path/f"data{s}")})
    monkeypatch.setattr(m,"verify_ex5_sha",lambda s: "verified")
    writes=[]
    monkeypatch.setattr(m,"write_preset",lambda s:writes.append(s))
    assert m.main([])==1
    assert writes==[]
    assert first.read_text()==original


def test_successful_subprocess_cannot_reuse_stale_fp_status(tmp_path,monkeypatch):
    m=load_enablement()
    status=tmp_path/"status.json"
    status.write_text(json.dumps({"account_suffix":"0512",
                                  "status":"V9_NATIVE_DISPATCH_EVALUATED"}))
    os.utime(status,(time.time()-120,time.time()-120))
    monkeypatch.setattr(m,"_account_paths",lambda s:{"data_root":str(tmp_path)})
    monkeypatch.setattr("subprocess.run",
                        lambda *args,**kwargs: SimpleNamespace(
                            returncode=0,stdout="",stderr=""))
    assert m.run_execute_cycle("0512")["status"]=="EXECUTE_CYCLE_STALE_OR_MISSING_STATUS"


def test_fp_status_rejects_wrong_account(tmp_path,monkeypatch):
    m=load_enablement()
    status=tmp_path/"status.json"
    monkeypatch.setattr(m,"_account_paths",lambda s:{"data_root":str(tmp_path)})
    def run(*args,**kwargs):
        status.write_text(json.dumps({"account_suffix":"1636",
                                      "status":"V9_NATIVE_DISPATCH_EVALUATED"}))
        future=time.time_ns()+1_000_000_000
        os.utime(status,ns=(future,future))
        return SimpleNamespace(returncode=0,stdout="",stderr="")
    monkeypatch.setattr("subprocess.run",run)
    assert m.run_execute_cycle("0512")["status"]=="EXECUTE_CYCLE_ACCOUNT_MISMATCH"


def test_enablement_supports_single_account_selector(tmp_path, monkeypatch):
    """--account 0512 must restrict enablement to 0512 alone."""
    m = load_enablement()
    first = tmp_path / "fp0512/terminal/MQL5/Presets/PR37WriterExposureOnly.set"
    first.parent.mkdir(parents=True)
    original = "InpConnectionId=" + CID + "\nInpBrokerServer=FPMarkets-Demo\n" \
               "InpActivationSeed=test-only-seed\nInpExposureOnly=false\n"
    first.write_text(original)
    (tmp_path / "data0512").mkdir()
    monkeypatch.setattr(m, "resolve_connection_id", lambda s: CID)
    monkeypatch.setattr(m, "_account_paths",
                        lambda s: {"qa_root": tmp_path / f"fp{s}",
                                   "data_root": str(tmp_path / f"data{s}")})
    monkeypatch.setattr(m, "verify_ex5_sha", lambda s: "verified")
    writes = []
    monkeypatch.setattr(m, "write_preset", lambda s: (writes.append(s), ["preset"])[1])
    executed = []
    monkeypatch.setattr(m, "run_execute_cycle", lambda s: executed.append(s) or {
        "status": "V9_NATIVE_DISPATCH_EVALUATED", "native_orders_sent": 0})
    assert m.main(["--account", "0512"]) == 0
    assert writes == ["0512"]
    assert executed == ["0512"]


def test_enablement_rejects_unknown_account_selector(tmp_path, monkeypatch):
    """--account with an unknown suffix must fail before any action."""
    m = load_enablement()
    writes = []
    monkeypatch.setattr(m, "write_preset", lambda s: (writes.append(s), ["preset"])[1])
    assert m.main(["--account", "9999"]) == 1
    assert writes == []


def test_enablement_no_selector_processes_all_four(tmp_path, monkeypatch):
    """Without --account, all four DEMO accounts are targeted."""
    m = load_enablement()
    for suffix in ("0512", "1636", "1622", "2582"):
        preset = tmp_path / f"fp{suffix}/terminal/MQL5/Presets/PR37WriterExposureOnly.set"
        preset.parent.mkdir(parents=True, exist_ok=True)
        preset.write_text(
            "InpConnectionId=" + CID + "\nInpBrokerServer=FPMarkets-Demo\n"
            "InpActivationSeed=test-only-seed\nInpExposureOnly=false\n")
        (tmp_path / f"data{suffix}").mkdir(exist_ok=True)
    monkeypatch.setattr(m, "resolve_connection_id", lambda s: CID)
    monkeypatch.setattr(m, "_account_paths",
                        lambda s: {"qa_root": tmp_path / f"fp{s}",
                                   "data_root": str(tmp_path / f"data{s}")})
    monkeypatch.setattr(m, "verify_ex5_sha", lambda s: "verified")
    writes = []
    monkeypatch.setattr(m, "write_preset", lambda s: (writes.append(s), ["preset"])[1])
    monkeypatch.setattr(m, "run_execute_cycle", lambda s: {
        "status": "V9_NATIVE_DISPATCH_EVALUATED", "native_orders_sent": 0})
    assert m.main([]) == 0
    assert writes == ["0512", "1636", "1622", "2582"]


def test_fp_risk_override_applied_to_preset(tmp_path, monkeypatch):
    """The FP-authorized per-trade risk overrides the Deriv template default."""
    m = load_enablement()
    preset = tmp_path / "fp0512/terminal/MQL5/Presets/PR37WriterExposureOnly.set"
    preset.parent.mkdir(parents=True)
    original = "InpConnectionId=" + CID + "\nInpBrokerServer=FPMarkets-Demo\n" \
               "InpActivationSeed=test-only-seed\nInpExposureOnly=false\n" \
               "InpRiskFraction=0.15\nInpRiskFractionPct=15\n"
    preset.write_text(original)
    (tmp_path / "data0512").mkdir()
    monkeypatch.setattr(m, "resolve_connection_id", lambda s: CID)
    monkeypatch.setattr(m, "_account_paths",
                        lambda s: {"qa_root": tmp_path / f"fp{s}",
                                   "data_root": str(tmp_path / f"data{s}")})
    monkeypatch.setattr(m, "verify_ex5_sha", lambda s: "verified")
    monkeypatch.setattr(m, "run_execute_cycle", lambda s: {
        "status": "V9_NATIVE_DISPATCH_EVALUATED", "native_orders_sent": 0})
    assert m.main(["--account", "0512"]) == 0
    written = preset.read_text()
    assert "InpRiskFraction=0.1" in written
    assert "InpRiskFractionPct=10" in written
    assert "InpRiskFraction=0.15" not in written



def test_fp_native_mql5_risk_units_are_percent_not_fraction():
    """Check the broker-side OrderCalcProfit expression and real input types."""
    src=(ROOT / "mql5/ApexNativeWriter.mq5").read_text()
    assert "input double  InpRiskFraction" in src
    assert "input int     InpRiskFractionPct" in src
    assert "accountEquity * InpRiskFraction / 100.0" in src
    # These EA values both represent 0.10% (10 basis points).
    module=load_enablement()
    assert module.FP_PER_TRADE_RISK_PCT == 0.10
    assert module.FP_PER_TRADE_RISK_PCT / 100.0 == pytest.approx(0.001)


def test_fp_portfolio_cap_20_percent_not_0_2_percent(monkeypatch):
    """RiskGate expects 0.20 fraction for the user-authorized 20% ceiling."""
    from abutron.agent import production_fp_demo as fp
    assert fp.FP_PORTFOLIO_CAP_PCT == pytest.approx(20.0)
    assert fp.FP_PORTFOLIO_CAP_PCT / 100.0 == pytest.approx(0.20)
    assert fp.FP_PER_TRADE_RISK_PCT / 100.0 == pytest.approx(0.001)
