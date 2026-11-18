"""Exact-source tests for read-only FP native-market monitor account resolution."""
import importlib.util
import json
import sqlite3
from pathlib import Path
import pytest

PATH=Path(__file__).resolve().parents[1]/"scripts"/"native_market_v3_status_ro.py"
spec=importlib.util.spec_from_file_location("v3_readonly_account_resolution",PATH)
monitor=importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

def crm(tmp_path, rows):
    p=tmp_path/"crm.sqlite3"
    with sqlite3.connect(p) as db:
        db.execute("CREATE TABLE apex_mt5_connections (id TEXT, account_reference TEXT, broker TEXT, environment TEXT)")
        db.executemany("INSERT INTO apex_mt5_connections VALUES(?,?,?,?)",rows)
    return p

def profile(ch,suffix,broker="FP Markets",mode="DEMO"):
    return (ch*32,"0000"+suffix,broker,mode)

def test_crm_readonly_fallback(monkeypatch,tmp_path):
    monkeypatch.delenv("ABUTRON_FP_ACCOUNT_PROFILES",raising=False)
    p=crm(tmp_path,[profile("a","0512"),profile("b","1636"),profile("c","7593","Deriv")])
    assert monitor._resolve_accounts(p)=={"0512":("a"*32,"00000512"),"1636":("b"*32,"00001636")}

def test_explicit_env_precedes_crm(monkeypatch,tmp_path):
    monkeypatch.setenv("ABUTRON_FP_ACCOUNT_PROFILES",json.dumps({"1636":["b"*32,"00001636"]}))
    assert monitor._resolve_accounts(crm(tmp_path,[profile("a","0512")]))=={"1636":("b"*32,"00001636")}

def test_malformed_env_does_not_silently_use_crm(monkeypatch,tmp_path):
    monkeypatch.setenv("ABUTRON_FP_ACCOUNT_PROFILES",json.dumps({"0512":["a"*32,"00001636"]}))
    assert monitor._resolve_accounts(crm(tmp_path,[profile("a","0512")]))=={}

def test_wrong_mode_fails_closed(monkeypatch,tmp_path):
    monkeypatch.delenv("ABUTRON_FP_ACCOUNT_PROFILES",raising=False)
    assert monitor._resolve_accounts(crm(tmp_path,[profile("a","0512",mode="LIVE")]))=={}

def test_duplicate_login_fails_closed(monkeypatch,tmp_path):
    monkeypatch.delenv("ABUTRON_FP_ACCOUNT_PROFILES",raising=False)
    assert monitor._resolve_accounts(crm(tmp_path,[profile("a","0512"),profile("b","0512")]))=={}

def test_other_accounts_not_selected(monkeypatch,tmp_path):
    monkeypatch.delenv("ABUTRON_FP_ACCOUNT_PROFILES",raising=False)
    assert list(monitor._resolve_accounts(crm(tmp_path,[profile("a","0512"),profile("c","9244","Deriv")])))==["0512"]

def test_unavailable_crm_returns_empty(monkeypatch,tmp_path):
    monkeypatch.delenv("ABUTRON_FP_ACCOUNT_PROFILES",raising=False)
    assert monitor._resolve_accounts(tmp_path/"missing.sqlite3")=={}

def test_blank_env_uses_crm(monkeypatch,tmp_path):
    monkeypatch.setenv("ABUTRON_FP_ACCOUNT_PROFILES","")
    assert monitor._resolve_accounts(crm(tmp_path,[profile("a","0512")]))=={"0512":("a"*32,"00000512")}