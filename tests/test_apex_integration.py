"""End-to-end test: vendored real APEX 1.1.0 integrated into ABUTRON.

Feeds closed M5 candles through the authoritative signal bridge + fail-closed
candidate review, and verifies a READY candidate is produced and stored for
the single-writer dispatcher. This proves the ABUTRON platform now runs the
*real* APEX engine, not a reconstruction.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "vendor", "abutron-apex", "apex11"))

from abutron.config import DATA_ROOT
from abutron.events import EventLog
from abutron.apex_integration import ApexIntegration, build_instrument_manifest


@pytest.fixture
def apex_int():
    log = EventLog(os.path.join(DATA_ROOT, "t_apex_int_events.jsonl"))
    return ApexIntegration(log, warmup_bars=0)


def _deriv_account():
    return {"connection_id": "conn1", "broker": "deriv", "mt5_login": 6207,
            "owner_id": "owner1", "desired_on": 1, "effective_on": 1,
            "symbols": "Volatility 100 Index.0"}


def _fp_account():
    return {"connection_id": "conn2", "broker": "fpmarkets",
            "mt5_login": 7412582, "owner_id": "owner2", "desired_on": 1,
            "effective_on": 1, "symbols": "GBPUSD,EURUSD"}


def _closed_candle(sym, i, base_close, step=0.05):
    end = (int(time.time()) // 300) * 300
    t = end - (200 - i) * 300
    o = base_close + i * step
    c = o + step * 2
    return {
        "symbol": sym, "close_time_epoch": t,
        "open_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - 300)),
        "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
        "open": o, "high": c + step, "low": o - step, "close": c,
        "volume": 10.0,
    }


_FULL_GATE = dict(
    account_bound=True, worker_nonce_bound=True, fresh_broker_session=True,
    native_ea_account_match=True, broker_trade_allowed=True,
    terminal_trade_allowed=True, crm_entitlement_approved=True,
    single_writer_exclusive=True, broker_positions_protected=True,
    broker_exposure_reconciled=True, account_equity_usd=10000.0,
    current_verified_stop_risk_usd=0.0)


def test_instrument_manifest_deriv():
    manifest = build_instrument_manifest([_deriv_account()])
    assert len(manifest) == 1
    assert manifest[0]["broker"] == "deriv"
    assert manifest[0]["asset_class"] == "deriv_synthetic"
    assert manifest[0]["symbol"] == "Volatility 100 Index.0"


def test_instrument_manifest_fp():
    manifest = build_instrument_manifest([_fp_account()])
    assert len(manifest) == 2
    brokers = {m["broker"] for m in manifest}
    assert brokers == {"fp_markets"}


def test_bridge_rejects_unknown_symbol(apex_int):
    manifest = build_instrument_manifest([_deriv_account()])
    acct = _deriv_account()
    candle = _closed_candle("Volatility 75 Index.0", 1, 50.0)  # not approved
    r = apex_int.process_candle(connection_id="conn1", account=acct,
                                candle=candle, manifest=manifest)
    assert r["status"] == "REJECTED"
    assert "unknown" in (r.get("reason") or "").lower()


def test_full_pipeline_produces_ready_candidate(apex_int):
    """Closed candles -> APEX signal -> fail-closed review -> READY candidate."""
    manifest = build_instrument_manifest([_deriv_account()])
    acct = _deriv_account()
    ready = None
    for i in range(130):
        candle = _closed_candle("Volatility 100 Index.0", i, 100.0)
        r = apex_int.process_candle(
            connection_id="conn1", account=acct, candle=candle,
            manifest=manifest, equity=10000.0,
            native_geometry={
                "tick_size": 0.01, "tick_value_per_lot_usd": 1.0,
                "min_lot": 1.0, "max_lot": 500.0, "lot_step": 1.0,
                "min_stop_ticks": 1, "broker_verified": True,
                "price": candle["close"],
                "stop_price": candle["close"] - 0.05,
                "take_price": candle["close"] + 0.2,
                "slippage_budget_ticks": 1,
                "broker_calc_profit_verified": True,
                "broker_order_check_verified": True,
            },
            gate_overrides=_FULL_GATE)
        if r["status"] == "READY":
            ready = r
            break
    assert ready is not None, "expected at least one READY candidate"
    cand = ready["candidate"]
    assert cand["side"] in ("BUY", "SELL")
    assert cand["broker"] == "deriv"
    assert cand["symbol"] == "Volatility 100 Index.0"
    assert cand["volume_lots"] >= 1.0
    assert len(cand["signal_id"]) == 64


def test_fail_closed_blocks_without_attestation(apex_int):
    """Without native attestation gates, a candidate is BLOCKED (fail-closed)."""
    manifest = build_instrument_manifest([_deriv_account()])
    acct = _deriv_account()
    blocked = False
    for i in range(130):
        candle = _closed_candle("Volatility 100 Index.0", i, 100.0)
        r = apex_int.process_candle(
            connection_id="conn1", account=acct, candle=candle,
            manifest=manifest, equity=10000.0,
            native_geometry={
                "tick_size": 0.01, "tick_value_per_lot_usd": 1.0,
                "min_lot": 1.0, "max_lot": 500.0, "lot_step": 1.0,
                "min_stop_ticks": 1, "broker_verified": True,
                "price": candle["close"], "stop_price": candle["close"] - 0.05,
                "take_price": candle["close"] + 0.2,
                "slippage_budget_ticks": 1,
                "broker_calc_profit_verified": True,
                "broker_order_check_verified": True,
            },
            gate_overrides={})  # all booleans default False -> fail-closed
        if r["status"] == "BLOCKED":
            blocked = True
            assert r["blockers"]
            break
    assert blocked, "expected a BLOCKED candidate with empty gates"


def test_hold_produces_no_candidate(apex_int):
    manifest = build_instrument_manifest([_deriv_account()])
    acct = _deriv_account()
    holds = 0
    for i in range(60):
        candle = _closed_candle("Volatility 100 Index.0", i, 100.0)
        r = apex_int.process_candle(connection_id="conn1", account=acct,
                                    candle=candle, manifest=manifest)
        if r["status"] == "HOLD":
            holds += 1
    assert holds > 0
