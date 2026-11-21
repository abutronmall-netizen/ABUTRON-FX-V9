"""Regression proofs for nonce-account binding and safe release checks."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
from unittest.mock import patch

import pytest
from abutron.feeds.mt5_terminal import Mt5TerminalManager

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT / "ops/v9-cutover/enable_trading_all_accounts.py"


def load_script():
    spec=importlib.util.spec_from_file_location("enable_trading_audit_test", SCRIPT)
    obj=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def manager_in_cycle(directory, nonce="nonce123", cid="a"*32):
    manager=Mt5TerminalManager.__new__(Mt5TerminalManager)
    manager._terminals={cid:SimpleNamespace(export_dir=str(directory))}
    ts=time.time()
    manager.read_native_quotes=lambda identity: {
        "_nonce":nonce,"_native_file_mtime":ts,"quotes":[{"symbol":"X"}]}
    manager.read_native_export=lambda identity,required_nonce=None:{
        "identity":{"nonce":nonce,"_native_file_mtime":ts},
        "bars":{"bars":[{"symbol":"X"}]}}
    return manager


def orderflow_file(directory, nonce, cid, source="trade_ticks"):
    path=directory / ("ABUTRON_ORDER_FLOW_V2_"+nonce+".txt")
    path.write_text(json.dumps({
        "connection_id":cid,"nonce":nonce,"source":source,
        "trades":[{"symbol":"X","price":101.0,"size":1.0,"side":"buy","time":"2026.11.20 19:00:00"}]
    }))
    return path


def test_optional_orderflow_does_not_remove_valid_market_data(tmp_path):
    manager=manager_in_cycle(tmp_path)
    result=manager.read_native_export_full("a"*32)
    assert result is not None and "bars" in result and "quotes" in result
    assert "orderflow" not in result


def test_exact_nonce_file_selected_even_if_newer_cycle_exists(tmp_path):
    cid="a"*32
    valid=orderflow_file(tmp_path,"nonce123",cid)
    foreign=orderflow_file(tmp_path,"nonce124",cid)
    os.utime(foreign,(time.time()+1,time.time()+1))
    manager=manager_in_cycle(tmp_path)
    result=manager.read_native_export_full(cid)
    assert result["orderflow"]["nonce"]=="nonce123"
    assert result["orderflow"]["trades"][0]["symbol"]=="X"


def test_mismatched_account_orderflow_ignored_not_market_data(tmp_path):
    cid="a"*32
    orderflow_file(tmp_path,"nonce123","b"*32)
    result=manager_in_cycle(tmp_path).read_native_export_full(cid)
    assert result is not None and "orderflow" not in result


def test_missing_account_identity_not_accepted(tmp_path):
    cid="a"*32
    p=orderflow_file(tmp_path,"nonce123",cid)
    j=json.loads(p.read_text());j.pop("connection_id");p.write_text(json.dumps(j))
    assert "orderflow" not in manager_in_cycle(tmp_path).read_native_export_full(cid)


def test_nonce_inside_json_must_match_filename_and_cycle(tmp_path):
    cid="a"*32
    p=orderflow_file(tmp_path,"nonce123",cid)
    j=json.loads(p.read_text());j["nonce"]="nonce124";p.write_text(json.dumps(j))
    assert "orderflow" not in manager_in_cycle(tmp_path).read_native_export_full(cid)


def test_malformed_orderflow_preserves_bars_and_quotes(tmp_path):
    cid="a"*32
    p=orderflow_file(tmp_path,"nonce123",cid)
    p.write_text('{"bad_json":')
    export=manager_in_cycle(tmp_path).read_native_export_full(cid)
    assert export is not None and "bars" in export and "orderflow" not in export


def test_unknown_source_does_not_masquerade_as_actual_trades(tmp_path):
    cid="a"*32
    orderflow_file(tmp_path,"nonce123",cid,source="genuine_unknown")
    assert "orderflow" not in manager_in_cycle(tmp_path).read_native_export_full(cid)


def test_mql5_orderflow_json_has_quoted_comma_delimiters():
    src=(ROOT/"mql5/ApexNativeWriter.mq5").read_text()
    lines=src.splitlines()
    i=next(i for i,line in enumerate(lines) if "string ofJson =" in line)
    assert r',\"nonce\"' in lines[i+1]
    assert r',\"source\"' in lines[i+2]
    assert r',\"trades\"' in lines[i+3]
    assert "_Symbol" in src[src.index("Order flow: genuine trade ticks"):src.index("Broker-native exposure snapshot")]


def test_mql5_concatenation_emits_parseable_bound_orderflow_json():
    """Check emitted JSON semantics, not merely delimiter substrings."""
    source=(ROOT/"mql5/ApexNativeWriter.mq5").read_text()
    expression=source.split("string ofJson =",1)[1].split(";",1)[0]
    fields={
        "InpConnectionId":"a"*32,
        "cycleNonce":"nonce123",
        "source":"trade_ticks",
        "tradesJson":'[{"symbol":"X","price":101.0,"size":2.0,"side":"buy","time":"2026.11.20 19:00:00"}]',
    }
    pieces=[]
    for token in expression.split("+"):
        token=token.strip()
        pieces.append(json.loads(token) if token.startswith('"') else fields[token])
    result=json.loads("".join(pieces))
    assert result["connection_id"]=="a"*32
    assert result["nonce"]=="nonce123"
    assert result["source"]=="trade_ticks"
    assert len(result["trades"])==1


def test_compiled_source_sha_must_equal_deploy_pin(tmp_path,monkeypatch):
    mod=load_script()
    p=tmp_path/"EX5.sha256.txt";p.write_text("ApexNativeWriter.ex5 SHA256 " + "a"*64+"\\n")
    monkeypatch.setattr(mod,"CI_SHA_FILE",p)
    monkeypatch.setattr(mod,"EXPECTED_EX5_SHA","b"*64)
    with pytest.raises(ValueError,match="EX5_CI_SHA_DIFFERS"):
        mod.verify_compiled_release_pin()
    monkeypatch.setattr(mod,"EXPECTED_EX5_SHA","a"*64)
    mod.verify_compiled_release_pin()


def test_successful_subprocess_with_stale_status_is_not_success(tmp_path,monkeypatch):
    mod=load_script()
    p=tmp_path/"status.json"
    p.write_text(json.dumps({"status":"V9_NATIVE_DISPATCH_EVALUATED","account_suffix":"6207"}))
    os.utime(p,(time.time()-120,time.time()-120))
    monkeypatch.setattr(mod,"_account_paths",lambda suffix:{"data_root":str(tmp_path)})
    monkeypatch.setattr("subprocess.run",lambda *args,**kwargs: SimpleNamespace(returncode=0,stdout="",stderr=""))
    assert mod.run_execute_cycle("6207")["status"]=="EXECUTE_CYCLE_STALE_OR_MISSING_STATUS"


def test_nonzero_subprocess_cannot_use_old_success_status(tmp_path,monkeypatch):
    mod=load_script()
    p=tmp_path/"status.json"
    p.write_text(json.dumps({"status":"V9_NATIVE_DISPATCH_EVALUATED","account_suffix":"6207"}))
    monkeypatch.setattr(mod,"_account_paths",lambda suffix:{"data_root":str(tmp_path)})
    monkeypatch.setattr("subprocess.run",lambda *args,**kwargs: SimpleNamespace(returncode=1,stdout="",stderr="err"))
    assert mod.run_execute_cycle("6207")["status"]=="EXECUTE_CYCLE_FAILED"


def test_successful_new_status_must_be_for_requested_account(tmp_path,monkeypatch):
    mod=load_script()
    p=tmp_path/"status.json"
    monkeypatch.setattr(mod,"_account_paths",lambda suffix:{"data_root":str(tmp_path)})
    def run(*args,**kwargs):
        p.write_text(json.dumps({"status":"V9_NATIVE_DISPATCH_EVALUATED","account_suffix":"7593"}))
        # Synthetic result must be unambiguously newer than subprocess start.
        stamp=time.time_ns()+1_000_000_000
        os.utime(p,ns=(stamp,stamp))
        return SimpleNamespace(returncode=0,stdout="",stderr="")
    monkeypatch.setattr("subprocess.run",run)
    assert mod.run_execute_cycle("6207")["status"]=="EXECUTE_CYCLE_ACCOUNT_MISMATCH"
