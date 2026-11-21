"""Native market V3 authenticity tests; no broker trades or MT5 calls."""
from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path

from abutron.feeds.native_market_v3 import read_v3

CONN="a"*32
LOGIN="7410512"
SERVER="FPMarketsSC-Demo"
SYMBOL="EURUSD.r"
BIN=b"dummy-compiled-binary-fixture"*150

def fixture(tmp_path:Path,*,rows=None):
    worker=tmp_path/"worker"
    files=worker/"terminal/MQL5/Files/ABUTRON_V9_NATIVE"
    files.mkdir(parents=True)
    binary=worker/"terminal/MQL5/Indicators/ABUTRON/ApexMarketFeedV3.ex5"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(BIN)
    (worker/"worker-health.json").write_text(json.dumps({
        "id":CONN,"broker":"FP Markets","mode":"DEMO",
        "server":SERVER,"status":"CONNECTED","heartbeat_fresh":True,
        "nonce":"b"*32
    }))
    ms=int(time.time()*1000)-2000
    base={"schema":"abutron.v9.mt5.market_microstructure.readonly.v3",
          "symbol":SYMBOL,"broker_server":SERVER,"account_login":LOGIN,
          "environment":"DEMO","time_msc":ms}
    default=[
        {"record_type":"QUOTE","flags":2,"price":1.1,"bid":1.1,"ask":1.1002,
         "volume_real":0.0,"volume":0,"aggressor_side":"NONE"},
        {"record_type":"MARKET_PRINT","flags":40,"price":1.1001,
         "bid":1.1,"ask":1.1002,"volume_real":1.2,"volume":1,
         "aggressor_side":"BUY"},
        {"record_type":"DOM","levels":[{"book_type":1,"price":1.1003,
          "volume_real":5.5,"volume":5}]},
        {"record_type":"STATUS","print_records":1,"quote_records":1,
         "dom_records":1,"dropped_records":0,"dom_subscribed":True}
    ]
    if rows is None:rows=default
    p=files/("ABUTRON_NATIVE_MARKET_V3_"+SYMBOL+"_20261117.jsonl")
    def broker_time(row):
        if row["record_type"] in ("QUOTE","MARKET_PRINT"):
            return {**base, **row, "time_msc":ms+7200000}
        return {**base, **row}
    p.write_text("".join(json.dumps(broker_time(v))+"\n" for v in rows))
    return worker,hashlib.sha256(BIN).hexdigest(),p

def scan(root,sha,**kw):
    return read_v3(worker_root=str(root),connection_id=CONN,login=LOGIN,
                   server=SERVER,symbol=SYMBOL,approved_binary_sha256=sha,
                   broker_clock_offset_seconds=kw.pop("broker_clock_offset_seconds",7200),**kw)

def test_broker_native_prints_without_chart_proof_never_become_verified(tmp_path):
    root,sha,_=fixture(tmp_path)
    result=scan(root,sha)
    assert result.identity_verified and result.pinned_binary_verified
    assert result.accepted_market_prints==1
    assert result.accepted_dom_snapshots==1
    assert result.accepted_quotes==1
    assert len(result.trade_bars)==1
    assert result.verified_broker_orderflow is False
    assert "NATIVE_V3_CHART_ATTACHMENT_ATTESTATION_PENDING" in result.blockers

def test_quote_only_never_verifies_aggressor_flow(tmp_path):
    root,sha,_=fixture(tmp_path,rows=[
        {"record_type":"QUOTE","flags":2,"price":1.1,"bid":1.1,"ask":1.2,
         "volume_real":0,"volume":0,"aggressor_side":"NONE"},
        {"record_type":"STATUS","dropped_records":0,"dom_subscribed":False}
    ])
    r=scan(root,sha)
    assert r.accepted_market_prints==0
    assert r.verified_broker_orderflow is False
    assert "NATIVE_V3_GENUINE_AGGRESSOR_PRINTS_UNAVAILABLE" in r.blockers

def test_wrong_server_rejected(tmp_path):
    root,sha,p=fixture(tmp_path)
    data=p.read_text().replace(SERVER,"Invented-Server")
    p.write_text(data)
    r=scan(root,sha)
    assert r.status=="NOT_CERTIFIED"
    assert any("IDENTITY" in b for b in r.blockers)

def test_missing_binary_pin_rejected(tmp_path):
    root,sha,_=fixture(tmp_path)
    r=scan(root,"c"*64)
    assert "NATIVE_V3_BINARY_SHA256_NOT_PINNED" in r.blockers
    assert not r.verified_broker_orderflow

def test_no_real_aggressor_flag_rejected(tmp_path):
    root,sha,p=fixture(tmp_path)
    p.write_text(p.read_text().replace('"flags": 40','"flags": 8'))
    r=scan(root,sha)
    assert r.status=="NOT_CERTIFIED"
    assert "NATIVE_V3_MARKET_PRINT_FLAGS_UNVERIFIED" in r.blockers

def test_stale_heartbeat_not_certified(tmp_path):
    root,sha,p=fixture(tmp_path)
    data=[json.loads(x) for x in p.read_text().splitlines()]
    for x in data:
        if x["record_type"]=="STATUS":
            x["time_msc"]-=1_000_000
    p.write_text("".join(json.dumps(x)+"\n" for x in data))
    r=scan(root,sha)
    assert r.status=="NOT_CERTIFIED"
    assert "NATIVE_V3_TERMINAL_HEARTBEAT_STALE" in r.blockers

def test_dropped_records_prohibit_certification(tmp_path):
    root,sha,p=fixture(tmp_path)
    p.write_text(p.read_text().replace('"dropped_records": 0','"dropped_records": 1'))
    r=scan(root,sha)
    assert "NATIVE_V3_DROPPED_RECORDS_BREAK_CONTINUITY" in r.blockers

def test_symlink_native_file_denied(tmp_path):
    root,sha,p=fixture(tmp_path)
    target=p.with_suffix(".real")
    p.rename(target)
    p.symlink_to(target)
    r=scan(root,sha)
    assert "NATIVE_SOURCE_SYMLINK" in r.blockers


def test_mql5_real_quote_flags_1030_and_broker_utc_offset(tmp_path):
    worker,sha,path=fixture(tmp_path)
    contents=path.read_text().replace('"flags": 2','"flags": 1030')
    path.write_text(contents)
    result=scan(worker,sha)
    assert result.accepted_quotes==1
    assert result.accepted_market_prints==1
    assert result.status=="BROKER_NATIVE_PRINTS_OBSERVED_NOT_CERTIFIED"
    from datetime import datetime,timezone
    key=next(iter(result.trade_bars.keys()))
    assert abs((datetime.strptime(key,"%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp())-time.time())<300


def test_broker_clock_offset_mismatch_rejected(tmp_path):
    worker,sha,_=fixture(tmp_path)
    result=scan(worker,sha,broker_clock_offset_seconds=0)
    assert "NATIVE_V3_MILLISECOND_CLOCK_INVALID" in result.blockers
    assert not result.verified_broker_orderflow


def test_broker_clock_missing_proof_rejected(tmp_path):
    worker,sha,_=fixture(tmp_path)
    result=scan(worker,sha,broker_clock_offset_seconds=None)
    assert "NATIVE_V3_BROKER_CLOCK_OFFSET_UNVERIFIED" in result.blockers
    assert result.status=="NOT_CERTIFIED"


def test_independent_native_history_clock_must_match_reconciliation(tmp_path):
    from abutron.feeds.native_market_v3 import trusted_fp_demo_clock_offset
    h=tmp_path/"history.json"
    r=tmp_path/"recon.json"
    digest="c"*64
    h.write_text(json.dumps({
        "schema":"abutron.fpmarkets.native.account_history.readonly.v1",
        "history_verified_from_mt5":True,"broker":"FP Markets",
        "environment":"DEMO","account_masked":"••••0512",
        "native_file_sha256":digest,"broker_clock_offset_seconds":7200
    }))
    r.write_text(json.dumps({
        "broker":"FP Markets","environment":"DEMO",
        "account_masked":"••••0512","status":"RECONCILED_READONLY",
        "history_sha256":digest,"native_broker_open_risk_verified":True,
        "bridge_proof_matches_broker":True,"blockers":[]
    }))
    args=dict(native_history_file=str(h),native_reconciliation_file=str(r),
              account_suffix="0512")
    assert trusted_fp_demo_clock_offset(**args)==7200
    values=json.loads(r.read_text());values["history_sha256"]="d"*64
    r.write_text(json.dumps(values))
    import pytest
    with pytest.raises(ValueError,match="RECONCILIATION_UNVERIFIED"):
        trusted_fp_demo_clock_offset(**args)


def test_deriv_symbol_with_spaces_accepted(tmp_path:Path):
    """Deriv symbols like 'Volatility 50 Index.0' must be accepted.

    The V3 indicator sanitizes filenames (spaces -> underscores) but
    preserves the original symbol in JSON content. read_v3 must
    glob by the sanitized filename while accepting the original
    symbol in identity checks.
    """
    worker=tmp_path/"worker"
    files=worker/"terminal/MQL5/Files/ABUTRON_V9_NATIVE"
    files.mkdir(parents=True)
    binary=worker/"terminal/MQL5/Indicators/ABUTRON/ApexMarketFeedV3.ex5"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(BIN)
    (worker/"worker-health.json").write_text(json.dumps({
        "id":CONN,"broker":"Deriv","mode":"DEMO",
        "server":"Deriv-Demo","status":"CONNECTED","heartbeat_fresh":True,
        "nonce":"b"*32
    }))
    deriv_symbol="Volatility 50 Index.0"
    deriv_file=deriv_symbol.replace(" ","_")
    ms=int(time.time()*1000)-2000
    base={"schema":"abutron.v9.mt5.market_microstructure.readonly.v3",
          "symbol":deriv_symbol,"broker_server":"Deriv-Demo",
          "account_login":LOGIN,"environment":"DEMO","time_msc":ms}
    rows=[
        {"record_type":"QUOTE","flags":2,"price":5000.0,
         "bid":4999.9,"ask":5000.1,"volume_real":0.0,
         "volume":0,"aggressor_side":"NONE"},
        {"record_type":"MARKET_PRINT","flags":40,"price":5000.05,
         "bid":4999.9,"ask":5000.1,"volume_real":1.0,
         "volume":1,"aggressor_side":"BUY"},
        {"record_type":"DOM","levels":[{"book_type":1,"price":5000.2,
          "volume_real":10.0,"volume":10}]},
        {"record_type":"STATUS","print_records":1,"quote_records":1,
         "dom_records":1,"dropped_records":0,"dom_subscribed":True}
    ]
    def broker_time(row):
        if row["record_type"] in ("QUOTE","MARKET_PRINT"):
            return {**base,**row,"time_msc":ms+7200000}
        return {**base,**row}
    p=files/("ABUTRON_NATIVE_MARKET_V3_"+deriv_file+"_20261121.jsonl")
    p.write_text("".join(json.dumps(broker_time(v))+"\n" for v in rows))
    sha=hashlib.sha256(BIN).hexdigest()
    result=read_v3(worker_root=str(worker),connection_id=CONN,
        login=LOGIN,server="Deriv-Demo",symbol=deriv_symbol,
        approved_binary_sha256=sha,broker_clock_offset_seconds=7200)
    assert result.identity_verified
    assert result.pinned_binary_verified
    assert result.accepted_quotes>=1
    assert result.accepted_market_prints>=1
    # Not fully certified without terminal-log chart attestation,
    # but the symbol with spaces is accepted and data is parsed.
    assert "NATIVE_V3_ROW_IDENTITY_MISMATCH" not in result.blockers


def test_deriv_symbol_sanitized_filename_identity(tmp_path:Path):
    """When the caller passes the sanitized filename variant as the
    symbol, read_v3 must still match it against the original symbol
    stored in JSON content."""
    worker=tmp_path/"worker"
    files=worker/"terminal/MQL5/Files/ABUTRON_V9_NATIVE"
    files.mkdir(parents=True)
    binary=worker/"terminal/MQL5/Indicators/ABUTRON/ApexMarketFeedV3.ex5"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(BIN)
    (worker/"worker-health.json").write_text(json.dumps({
        "id":CONN,"broker":"Deriv","mode":"DEMO",
        "server":"Deriv-Demo","status":"CONNECTED","heartbeat_fresh":True,
        "nonce":"b"*32
    }))
    deriv_symbol="Volatility 50 Index.0"
    deriv_file=deriv_symbol.replace(" ","_")
    ms=int(time.time()*1000)-2000
    base={"schema":"abutron.v9.mt5.market_microstructure.readonly.v3",
          "symbol":deriv_symbol,"broker_server":"Deriv-Demo",
          "account_login":LOGIN,"environment":"DEMO","time_msc":ms}
    rows=[
        {"record_type":"QUOTE","flags":2,"price":5000.0,
         "bid":4999.9,"ask":5000.1,"volume_real":0.0,
         "volume":0,"aggressor_side":"NONE"},
        {"record_type":"STATUS","print_records":0,"quote_records":1,
         "dom_records":0,"dropped_records":0,"dom_subscribed":False}
    ]
    def broker_time(row):
        if row["record_type"] in ("QUOTE","MARKET_PRINT"):
            return {**base,**row,"time_msc":ms+7200000}
        return {**base,**row}
    p=files/("ABUTRON_NATIVE_MARKET_V3_"+deriv_file+"_20261121.jsonl")
    p.write_text("".join(json.dumps(broker_time(v))+"\n" for v in rows))
    sha=hashlib.sha256(BIN).hexdigest()
    # Pass the sanitized filename variant as the symbol
    result=read_v3(worker_root=str(worker),connection_id=CONN,
        login=LOGIN,server="Deriv-Demo",symbol=deriv_file,
        approved_binary_sha256=sha,broker_clock_offset_seconds=7200)
    assert result.identity_verified
    assert result.accepted_quotes>=1
