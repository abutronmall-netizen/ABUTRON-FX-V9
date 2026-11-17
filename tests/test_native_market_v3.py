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
    p.write_text("".join(json.dumps({**base,**v})+"\n" for v in rows))
    return worker,hashlib.sha256(BIN).hexdigest(),p

def scan(root,sha,**kw):
    return read_v3(worker_root=str(root),connection_id=CONN,login=LOGIN,
                   server=SERVER,symbol=SYMBOL,approved_binary_sha256=sha,**kw)

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
