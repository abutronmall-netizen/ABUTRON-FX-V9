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
    ai = ApexIntegration(log, warmup_bars=0)
    # The vendored APEX source is an optional dependency. These bridge-behaviour
    # tests require it; skip (not fail) when the platform runs without it.
    if not ai.apex_available():
        pytest.skip(
            f"vendored APEX source unavailable on this checkout: "
            f"{ai._apex_error}")
    return ai


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


def test_service_starts_without_vendored_apex():
    """The platform must boot and serve on the NexusBrain even when the
    vendored APEX source is absent — no runtime crash at startup."""
    log = EventLog(os.path.join(DATA_ROOT, "t_apex_optional_events.jsonl"))
    ai = ApexIntegration(log, warmup_bars=0)
    # Constructing must never raise regardless of vendor presence.
    assert ai.brain is not None
    # If the vendor is absent, process_candle reports a clear non-trading
    # status instead of crashing.
    out = ai.process_candle(
        connection_id="conn1",
        account={"broker": "deriv", "connection_id": "conn1"},
        candle={"symbol": "Volatility 100 Index.0",
                "open_time": "2026-11-14T08:00:00Z",
                "close_time": "2026-11-14T08:05:00Z",
                "close_time_epoch": 1781000000.0,
                "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
                "volume": 10.0},
        manifest=[{"broker": "deriv", "symbol": "Volatility 100 Index.0"}],
        equity=10000.0)
    if not ai.apex_available():
        assert out["status"] == "APEX_VENDOR_MISSING"
        assert out["candidate"] is None
    else:
        # The manifest here is intentionally incomplete (no broker-native
        # geometry). With the vendor present the bridge rejects it, and the
        # platform must report that as a status instead of raising out of the
        # engine tick loop.
        assert out["status"] in ("HOLD", "REJECTED", "BLOCKED", "READY",
                                 "MANIFEST_INVALID", "APEX_VENDOR_MISSING")
        assert out["candidate"] is None


# ---------------------------------------------------------------------------
# Instrument manifest / asset-class contract
# ---------------------------------------------------------------------------
def test_fp_manifest_asset_class_is_accepted_by_bridge():
    """FP Markets instruments must use the asset class the vendored bridge
    approves (`fx_index`), otherwise the FP bridge can never be built."""
    from abutron_replacement.bridge import Instrument
    manifest = build_instrument_manifest([_fp_account()])
    assert manifest, "expected FP instruments"
    for m in manifest:
        assert m["asset_class"] == "fx_index"
        # Parsing is what the bridge does at construction time.
        parsed = Instrument.parse(m)
        assert parsed.broker == "fp_markets"
        assert parsed.asset_class == "fx_index"


def test_deriv_manifest_asset_class_is_accepted_by_bridge():
    from abutron_replacement.bridge import Instrument
    for m in build_instrument_manifest([_deriv_account()]):
        assert Instrument.parse(m).asset_class == "deriv_synthetic"


def test_fp_bridge_is_constructed(apex_int):
    """Regression: an FP account could never build a bridge (asset class)."""
    manifest = build_instrument_manifest([_fp_account()])
    bridge = apex_int._bridge_for("conn2", _fp_account(), manifest)
    assert bridge is not None
    assert set(bridge.instrument_by_symbol) == {"GBPUSD", "EURUSD"}


def test_invalid_manifest_degrades_instead_of_raising(apex_int):
    """A bad manifest is reported as MANIFEST_INVALID, never raised out of
    the engine tick loop."""
    # An unapproved Deriv asset class / unknown symbol must not crash.
    bad = [{"broker": "deriv", "symbol": "Volatility 100 Index.0",
            "asset_class": "not_approved"}]
    r = apex_int.process_candle(
        connection_id="conn1", account=_deriv_account(),
        candle=_closed_candle("Volatility 100 Index.0", 1, 100.0),
        manifest=bad, equity=10000.0)
    assert r["status"] == "MANIFEST_INVALID"
    assert r["candidate"] is None
    assert r["blockers"]


def test_manifest_without_account_broker_degrades(apex_int):
    r = apex_int.process_candle(
        connection_id="conn2", account=_fp_account(),
        candle=_closed_candle("GBPUSD", 1, 1.2),
        manifest=build_instrument_manifest([_deriv_account()]),
        equity=10000.0)
    assert r["status"] == "MANIFEST_INVALID"


# ---------------------------------------------------------------------------
# AI entry pipeline: sniper geometry -> trailing -> brain lot sizing
# ---------------------------------------------------------------------------
_NATIVE_GEOMETRY = {
    "tick_size": 0.01, "tick_value_per_lot_usd": 1.0,
    "min_lot": 1.0, "max_lot": 500.0, "lot_step": 1.0,
    "min_stop_ticks": 1, "broker_verified": True,
    "slippage_budget_ticks": 1,
    "broker_calc_profit_verified": True,
    "broker_order_check_verified": True,
}


def _run_until_ready(apex_int, *, n=130, risk_pct=0.5, geometry=None):
    acct = dict(_deriv_account(), per_trade_risk_pct=risk_pct)
    manifest = build_instrument_manifest([acct])
    geom = dict(_NATIVE_GEOMETRY) if geometry is None else geometry
    for i in range(n):
        candle = _closed_candle("Volatility 100 Index.0", i, 100.0)
        r = apex_int.process_candle(
            connection_id="conn1", account=acct, candle=candle,
            manifest=manifest, equity=10000.0,
            native_geometry=geom, gate_overrides=_FULL_GATE)
        if r["status"] == "READY":
            return r
    return None


def test_ready_candidate_carries_sniper_geometry(apex_int):
    """The candidate must carry a real trigger/stop/take triple, not zeros."""
    ready = _run_until_ready(apex_int)
    assert ready is not None
    cand = ready["candidate"]
    assert cand["entry_type"] in ("LIMIT", "CONFIRM")
    assert cand["trigger_price"] > 0
    assert cand["stop_price"] > 0
    assert cand["take_price"] > 0
    assert 0.0 <= cand["entry_quality"] <= 1.0
    assert cand["invalidation"] > 0
    # tight sniper geometry: take-profit is on the correct side of the
    # trigger, stop on the other side.
    if cand["side"] == "BUY":
        assert cand["stop_price"] < cand["trigger_price"] < cand["take_price"]
    else:
        assert cand["take_price"] < cand["trigger_price"] < cand["stop_price"]


def test_ready_candidate_carries_trailing_stop(apex_int):
    ready = _run_until_ready(apex_int)
    assert ready is not None
    cand = ready["candidate"]
    assert cand["trail_k"] > 0
    assert cand["trail_stop"] > 0
    assert cand["trail_plan"]


def test_ready_candidate_carries_brain_lot_size(apex_int):
    ready = _run_until_ready(apex_int)
    assert ready is not None
    cand = ready["candidate"]
    assert cand["volume_lots"] > 0
    assert cand["stop_risk_usd"] > 0


def test_brain_sizing_never_exceeds_contract_volume(apex_int):
    """The contract validates lots against the 0.10% risk policy and the 20%
    portfolio ceiling. The brain's quality-scaled lot must be clamped to it."""
    ready = _run_until_ready(apex_int, risk_pct=0.5)
    assert ready is not None
    cand = ready["candidate"]
    assert cand["volume_lots"] <= cand["contract_volume_lots"] + 1e-9


def test_lot_sizing_reports_realised_risk():
    """size_position must report the risk the executed lot actually carries
    (after lot-step rounding / max clamping), not the target budget."""
    from abutron.brain import make_brain, default_ensemble
    brain = make_brain(ensemble=default_ensemble(seed=0))
    s = brain.size_position(equity=10_000_000.0, risk_pct=1.0,
                            stop_distance_points=1.0, point_value=1.0,
                            lot_max=50.0, entry_quality=1.0)
    # lots clamped to 50 -> realised risk is 50 * (1.0 * 1.0) = 50 USD,
    # far below the 10,000,000 USD target budget (equity * risk_pct * scale).
    assert s["lots"] == 50.0
    assert s["risk_usd"] == pytest.approx(50.0)
    assert s["risk_target_usd"] == pytest.approx(10_000_000.0)
    assert s["risk_usd"] < s["risk_target_usd"]


def test_entry_plan_history_is_bounded(apex_int):
    from abutron.apex_integration import ApexIntegration
    for i in range(ApexIntegration.HISTORY_BARS + 60):
        apex_int._push_history("X", {"open": 1.0, "high": 1.1,
                                     "low": 0.9, "close": 1.0})
    assert len(apex_int._history["X"]) == ApexIntegration.HISTORY_BARS


def test_sniper_prefers_limit_on_pullback(apex_int):
    """A trending tape with price above its EMA should produce a LIMIT
    pullback plan (entering at the level), not a market-at-close fill."""
    acct = dict(_deriv_account(), per_trade_risk_pct=0.5)
    manifest = build_instrument_manifest([acct])
    plan = None
    for i in range(60):
        candle = _closed_candle("Volatility 100 Index.0", i, 100.0)
        plan = apex_int.build_entry_plan(
            symbol="Volatility 100 Index.0", candle=candle, side="BUY",
            decision={"conviction": 0.6, "regime": {"name": "trend_up"}},
            equity=10000.0, risk_pct=0.5,
            native_geometry=_NATIVE_GEOMETRY)
    assert plan is not None
    assert plan["entry_type"] in ("LIMIT", "CONFIRM")
    assert plan["lots"] > 0
    assert plan["trail_k"] > 0
