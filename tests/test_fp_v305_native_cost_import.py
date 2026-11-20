
"""FP Markets DEMO V305 native cost import — isolated feed/CRM fixtures."""
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from abutron.feeds.fp_native_cost import (
    FPMarketCostError, read_native_fp_cost, refresh_fp_v9_cost_report,
)

CID = "b" * 32
NONCE = "a" * 32
OWNER = "11111111-1111-1111-1111-111111111111"
SERVER = "FPMarketsSC-Demo"
LOGIN = "7410512"
SYMBOLS = [
    "XAUUSD.r", "BTCUSD", "EURUSD.r", "GBPUSD.r", "USDJPY.r",
    "AUDUSD.r", "US100", "US30", "US500", "GER40", "XTIUSD",
    "USDCHF.r", "NZDUSD.r",
]


def fixture(tmp_path, *, suffix="0512"):
    database = tmp_path / "crm.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE apex_mt5_connections
                   (id TEXT, account_reference TEXT, broker_server TEXT,
                    broker TEXT, environment TEXT, status TEXT)""")
        db.execute("INSERT INTO apex_mt5_connections VALUES (?,?,?,?,?,?)",
                   (CID, LOGIN, SERVER, "FP Markets", "DEMO", "CONNECTED"))
    folder = tmp_path / "MQL5" / "Files"
    folder.mkdir(parents=True)
    now = int(time.time())
    (folder / f"ABUTRON_SESSION_HEARTBEAT_V1_{NONCE}.txt").write_text(
        "\n".join(["ABUTRON_MT5_NATIVE_HEARTBEAT_V1", NONCE, OWNER,
                   "FP_MARKETS", SERVER, "DEMO", "CONNECTED",
                   "NO_ORDER_FUNCTIONS"]) + "\n")
    cat = ["ABUTRON_FP_CATALOG_V1", NONCE, OWNER,
           SERVER, LOGIN, "DEMO", str(now), "943"]
    for i, symbol in enumerate(SYMBOLS):
        cat.append("M\t" + "\t".join(
            [symbol, symbol, "MAPPED", "4", "100.00000000",
             "0.01000000", "0.01000000", "2"]))
    cat.append("END\t943\t13")
    (folder / f"ABUTRON_FP_CATALOG_V305_{NONCE}.txt").write_text(
        "\n".join(cat)+"\n")
    quotes = ["ABUTRON_FP_QUOTES_V1", NONCE, OWNER,
              SERVER, LOGIN, "DEMO", str(now)]
    quotes.extend(f"{symbol}\t{symbol}\t{now}\t100.00000000\t100.02000000"
                  for symbol in SYMBOLS)
    quotes.append("END\t13")
    (folder / f"ABUTRON_FP_QUOTES_V305_{NONCE}.txt").write_text(
        "\n".join(quotes)+"\n")
    instruments = {}
    for sym in SYMBOLS:
        instruments[sym] = {
            "symbol": sym, "tick_size": 0.01, "point": 0.01,
            "contract_size": 100.0, "volume_min": 0.01,
            "volume_step": 0.01, "volume_max": 10.0,
            "stops_level": 20, "freeze_level": 10,
            "digits": 2, "trade_mode": 4,
            "bid": 100.0, "ask": 100.02, "tick_epoch": now,
            "buy_probe_native_ordercalcprofit_usd": -1.0,
            "sell_probe_native_ordercalcprofit_usd": -1.0,
        }
    report = {
        "schema": "abutron.fpmarkets.native.cost_snapshot.v1",
        "source": "MT5_OrderCalcProfit", "nonce": NONCE,
        "heartbeat_owner": OWNER, "broker_server": SERVER,
        "mt5_login": LOGIN, "account_suffix": suffix,
        "environment": "DEMO", "sample_epoch": now,
        "account_currency": "USD", "account_equity_usd": 10000.0,
        "native_bid_ask_and_ordercalcprofit_verified": True,
        "verified_instrument_count": 13,
        "expected_instrument_count": 13,
        "instruments": instruments,
    }
    filename = folder / f"ABUTRON_FP_COST_V1_{NONCE}.json"
    filename.write_text(json.dumps(report))
    def read(**kwargs):
        return read_native_fp_cost(
            feed_files=folder, crm_db=str(database),
            connection_id=CID, expected_suffix="0512", **kwargs)
    return folder, database, filename, report, read


def test_native_13_symbols_and_provenance_accepted(tmp_path):
    _, _, _, _, read = fixture(tmp_path)
    cost = read()
    assert cost["account_suffix"] == "0512"
    assert cost["native_bid_ask_and_ordercalcprofit_verified"] is True
    assert len(cost["instruments"]) == 13
    assert cost["instruments"]["XAUUSD.r"]["tick_size"] == 0.01
    assert cost["instruments"]["XAUUSD.r"]["contract_size"] == 100.0
    assert cost["source"] == "abutron.fpmarkets.native.cost_snapshot.v1"


def test_readonly_does_not_write_report(tmp_path):
    _, _, _, _, read = fixture(tmp_path)
    before = sorted(p.as_posix() for p in tmp_path.rglob("*"))
    read()
    assert before == sorted(p.as_posix() for p in tmp_path.rglob("*"))


def test_root_report_written_atomic_and_private(tmp_path):
    directory, database, _, _, _ = fixture(tmp_path)
    output = tmp_path / "v9.json"
    result = refresh_fp_v9_cost_report(
        feed_files=directory, crm_db=str(database),
        connection_id=CID, expected_suffix="0512", target=output)
    written = json.loads(output.read_text())
    assert result == written
    assert written["native_bid_ask_and_ordercalcprofit_verified"] is True
    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".fp-cost-*"))


@pytest.mark.parametrize("key,value", [
    ("broker_server", "Other-Demo"),
    ("mt5_login", "7411636"),
    ("environment", "LIVE"),
    ("account_suffix", "1636"),
    ("nonce", "9"*32),
    ("heartbeat_owner", "22222222-2222-2222-2222-222222222222"),
    ("account_currency", "EUR"),
    ("source", "unknown_calculation"),
    ("native_bid_ask_and_ordercalcprofit_verified", False),
    ("native_bid_ask_and_ordercalcprofit_verified", "true"),
    ("verified_instrument_count", 12),
    ("account_equity_usd", 0.0),
])
def test_wrong_provenance_rejected(tmp_path, key, value):
    _, _, filename, report, read = fixture(tmp_path)
    report[key] = value
    filename.write_text(json.dumps(report))
    with pytest.raises(FPMarketCostError):
        read()


def test_missing_instrument_rejected(tmp_path):
    _, _, filename, report, read = fixture(tmp_path)
    del report["instruments"]["US100"]
    filename.write_text(json.dumps(report))
    with pytest.raises(FPMarketCostError, match="SYMBOL_SET"):
        read()


def test_invalid_broker_profit_probe_rejected(tmp_path):
    _, _, filename, report, read = fixture(tmp_path)
    report["instruments"]["XAUUSD.r"]["buy_probe_native_ordercalcprofit_usd"] = 0
    filename.write_text(json.dumps(report))
    with pytest.raises(FPMarketCostError, match="BROKER_PROBE"):
        read()


def test_catalog_contract_mismatch_rejected(tmp_path):
    _, _, filename, report, read = fixture(tmp_path)
    report["instruments"]["US100"]["contract_size"] = 1.0
    filename.write_text(json.dumps(report))
    with pytest.raises(FPMarketCostError, match="CATALOG_PROBE"):
        read()


def test_stale_native_sample_rejected_even_when_mtime_fresh(tmp_path):
    _, _, filename, report, read = fixture(tmp_path)
    report["sample_epoch"] = int(time.time()) - 120
    filename.write_text(json.dumps(report))
    with pytest.raises(FPMarketCostError, match="SAMPLE_STALE"):
        read()


def test_stale_native_heartbeat_rejected(tmp_path):
    folder, _, _, _, read = fixture(tmp_path)
    p = folder / f"ABUTRON_SESSION_HEARTBEAT_V1_{NONCE}.txt"
    os.utime(p, (time.time()-120, time.time()-120))
    with pytest.raises(FPMarketCostError, match="NOT_CURRENT"):
        read()


def test_foreign_heartbeat_nonce_rejected(tmp_path):
    folder, _, _, _, read = fixture(tmp_path)
    p = folder / f"ABUTRON_SESSION_HEARTBEAT_V1_{NONCE}.txt"
    p.rename(folder / ("ABUTRON_SESSION_HEARTBEAT_V1_" + "9"*32 + ".txt"))
    with pytest.raises(FPMarketCostError):
        read()


def test_live_crm_cannot_use_demo_cost(tmp_path):
    _, database, _, _, read = fixture(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE apex_mt5_connections SET environment='LIVE'")
    with pytest.raises(FPMarketCostError, match="NOT_CONNECTED_DEMO"):
        read()


def test_invalid_output_does_not_replace_prior_cost(tmp_path):
    folder, database, filename, report, _ = fixture(tmp_path)
    output = tmp_path / "v9.json"
    output.write_text('{"previous_valid":true}')
    report["native_bid_ask_and_ordercalcprofit_verified"] = False
    filename.write_text(json.dumps(report))
    with pytest.raises(FPMarketCostError):
        refresh_fp_v9_cost_report(
            feed_files=folder, crm_db=str(database),
            connection_id=CID, expected_suffix="0512", target=output)
    assert json.loads(output.read_text()) == {"previous_valid": True}


def test_v305_cost_export_is_no_order_and_uses_real_symbol_info():
    source=(Path(__file__).resolve().parents[1] /
            "mql5/AbutronFPMarketsNativeFeedParallelV305.mq5").read_text()
    assert "OrderSend(" not in source
    assert "TRADE_ACTION" not in source
    assert "OrderCalcProfit(ORDER_TYPE_BUY" in source
    assert "OrderCalcProfit(ORDER_TYPE_SELL" in source
    assert "SYMBOL_TRADE_TICK_SIZE" in source
    assert "SYMBOL_TRADE_CONTRACT_SIZE" in source
    assert "FileWriteString(handle,json)" in source
    assert "FileMove(temporary,0,prefix,FILE_REWRITE)" in source
    assert 'all_verified=(all_verified && valid_count==13)' in source
    assert "PublishV9Cost(FP_NONCE" in source


def test_runner_opt_in_only():
    source=(Path(__file__).resolve().parents[1] /
            "src/abutron/agent/production_fp_demo.py").read_text()
    assert 'os.environ.get("ABUTRON_FP_FEED_FILES", "")' in source
    assert "refresh_fp_v9_cost_report" in source
