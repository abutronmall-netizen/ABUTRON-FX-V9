"""V9 executes only on current signed broker evidence, not a 300s QA report."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from abutron.agent import production_6207 as runner
from abutron.agent.crm_native_binding import Demo6207NativeCRM, CRMBindingError
from abutron.reconciliation import native_crm_6207 as native
from abutron.execution.writer_gateway import SingleWriterGateway
from abutron.risk.owner_native_authority import CurrentBrokerOwnerRisk


def setup_native(monkeypatch, tmp_path):
    cid = "a" * 32
    root = tmp_path / "terminal"
    files = root / "MQL5/Files"
    files.mkdir(parents=True)
    preset = root / "MQL5/Presets/PR37WriterExposureOnly.set"
    preset.parent.mkdir(parents=True)
    preset.write_text("InpExposureOnly=false\nInpTrailingEnabled=true\n")
    ea = root / "MQL5/Experts/ABUTRON/ApexNativeWriter.ex5"
    ea.parent.mkdir(parents=True)
    ea.write_bytes(b"synthetic unit test binary only")
    ea_sha = hashlib.sha256(ea.read_bytes()).hexdigest()
    db = tmp_path / "crm.sqlite3"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE accounts (id TEXT,status TEXT,demo_execution_enabled INTEGER)")
        c.execute("INSERT INTO accounts VALUES (?,?,?)", ("b"*32,"approved",1))
        c.execute("""CREATE TABLE apex_mt5_connections
          (id TEXT,account_id TEXT,broker TEXT,environment TEXT,
          account_reference TEXT,broker_server TEXT,status TEXT,worker_binding TEXT)""")
        c.execute("INSERT INTO apex_mt5_connections VALUES (?,?,?,?,?,?,?,?)",
                  (cid, "b"*32,"Deriv","DEMO","12346207","Deriv-Demo",
                   "CONNECTED","abutron-client-mt5-unit-test"))
        c.execute("""CREATE TABLE abutron_v4_6207_activation_revision
          (connection_id TEXT,revision INTEGER,desired_enabled INTEGER,
          effective_enabled INTEGER,connection_status TEXT,
          ack_revision INTEGER,ack_expires_utc INTEGER)""")
        c.execute("INSERT INTO abutron_v4_6207_activation_revision VALUES (?,?,?,?,?,?,?)",
                  (cid,91,1,1,"CONNECTED",91,int(time.time()+90)))
    monkeypatch.setattr(runner,"QA_TERMINAL",root)
    monkeypatch.setattr(runner,"CRM_DB",str(db))
    monkeypatch.setattr(runner,"resolve_crm_demo_6207_connection",lambda:cid)
    monkeypatch.setattr(SingleWriterGateway,"read_existing_secret",
                        lambda *_:"synthetic-test-secret")
    monkeypatch.setattr(native,"signed_native_exposure",
                        lambda *a,**kw:({"position_count":0},"synthetic-sha"))
    return cid,db,root,ea_sha


def test_v9_session_uses_signed_native_without_slow_qa_health(monkeypatch,tmp_path):
    setup_native(monkeypatch,tmp_path)
    # Deliberately no issue38-health.json: the legacy report has a 300s timer.
    assert runner.execution_session_enabled() is True


def test_v9_session_rejects_stale_signed_broker_feed(monkeypatch,tmp_path):
    setup_native(monkeypatch,tmp_path)
    def stale(*args,**kwargs):
        raise native.NativeCRMReconcileError("BROKER_NATIVE_SIGNED_EXPOSURE_STALE")
    monkeypatch.setattr(native,"signed_native_exposure",stale)
    assert runner.execution_session_enabled() is False


def test_crm_binding_uses_direct_hmac_not_qa_report(monkeypatch,tmp_path):
    cid,db,root,ea_sha=setup_native(monkeypatch,tmp_path)
    def recent_json(path,*args,**kwargs):
        name=str(path)
        if name.endswith("native-cost.json"):
            return {"account_suffix":"6207","environment":"DEMO",
                    "native_bid_ask_and_ordercalcprofit_verified":True,
                    "instruments":{"Volatility 50 Index.0":{"tick_size":0.0001}},
                    "account_equity_usd":10000}
        if name.endswith("native-owner.json"):
            return {"owners":{"owner":{"connections":[
                {"account_masked":"••••6207","snapshot":{"equity_usd":10000}}
            ]}}}
        raise AssertionError("obsolete QA health report must not be read")
    monkeypatch.setattr(Demo6207NativeCRM,"_recent_json",staticmethod(recent_json))
    monkeypatch.setattr(CurrentBrokerOwnerRisk,"current_owner",
                        lambda self,cid:{"aggregate_balance_usd":10000,
                                        "aggregate_stop_risk_usd":0})
    binding=Demo6207NativeCRM(
        connection_id=cid,crm_db=str(db),
        native_owner_report=str(tmp_path/"native-owner.json"),
        native_cost_report=str(tmp_path/"native-cost.json"),
        qa_data_dir=str(root),
        qa_health=str(tmp_path/"obsolete-report-that-does-not-exist.json"),
        expected_ea_sha=ea_sha)
    assert binding.snapshot()["qa_authenticated"] is True
    def stale(*args,**kwargs):
        raise native.NativeCRMReconcileError("BROKER_NATIVE_SIGNED_EXPOSURE_STALE")
    monkeypatch.setattr(native,"signed_native_exposure",stale)
    with pytest.raises(CRMBindingError,match="SIGNED_BROKER_EXPOSURE_NOT_CURRENT"):
        binding.snapshot()
