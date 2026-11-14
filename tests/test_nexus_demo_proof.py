"""DEMO-only V9 Nexus->APEX candidate controls: no MT5 dispatch involved."""
from __future__ import annotations
import time
from abutron.events import EventLog
from abutron.apex_integration import ApexIntegration, build_instrument_manifest


def _scenario(tmp_path, *, desired=True, effective=True, current_risk=0.0):
    a = ApexIntegration(EventLog(str(tmp_path / "demo_proof_events.jsonl")))
    acct = {"connection_id": "demo-6207-test", "broker": "deriv",
            "mt5_login": 6207, "owner_id": "owner-test",
            "desired_on": int(desired), "effective_on": int(effective),
            "symbols": "Volatility 100 Index.0"}
    t = (int(time.time())//300)*300-300
    close=100.0
    candle = {
        "symbol": "Volatility 100 Index.0", "close_time_epoch": t,
        "open_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t-300)),
        "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
        "open": close-0.02, "high": close+0.02, "low": close-0.04,
        "close": close, "volume": 10.0
    }
    return a,acct,candle,build_instrument_manifest([acct]),{
        "account_bound":True, "worker_nonce_bound":True,
        "fresh_broker_session":True, "native_ea_account_match":True,
        "broker_trade_allowed":True, "terminal_trade_allowed":True,
        "crm_entitlement_approved":True, "single_writer_exclusive":True,
        "broker_positions_protected":True,
        "broker_exposure_reconciled":True,
        "current_verified_stop_risk_usd":current_risk,
    }, {
        "tick_size":0.01, "tick_value_per_lot_usd":1.0,
        "min_lot":1.0, "max_lot":500.0, "lot_step":1.0,
        "min_stop_ticks":1, "broker_verified":True,
        "price":close, "stop_price":close-0.05,
        "take_price":close+0.2, "slippage_budget_ticks":1,
        "broker_calc_profit_verified":True, "broker_order_check_verified":True
    }


class BrainYes:
    def decide(self, **kwargs):
        return {"status": "BUY", "reason": "TEST_ONLY",
                "model_sha256": "a"*64, "proposal":{"direction":"BUY","trained":True}}


def _decide(a,acct,candle,manifest,overrides,geometry):
    return a.process_candle(connection_id=acct["connection_id"],account=acct,
        candle=candle,manifest=manifest,equity=10000.0,
        native_geometry=geometry,gate_overrides=overrides)


def test_untrained_v9_cannot_submit_candidate(tmp_path):
    a,acct,candle,manifest,overrides,geometry=_scenario(tmp_path)
    assert a.brain is None # V9 active model not provisioned in test environment
    out=_decide(a,acct,candle,manifest,overrides,geometry)
    assert out["status"]=="BRAIN_MODEL_UNAVAILABLE"
    assert out["candidate"] is None


def test_client_off_switch_blocks_candidate_even_when_model_votes_buy(tmp_path):
    a,acct,candle,manifest,overrides,geometry=_scenario(
        tmp_path,desired=False,effective=False)
    a.authority=BrainYes()
    out=_decide(a,acct,candle,manifest,overrides,geometry)
    assert out["status"]=="BLOCKED"
    assert out["candidate"] is None
    assert "CLIENT_AUTOMATIC_SWITCH_OFF" in out["blockers"]


def test_20_percent_portfolio_ceiling_blocks_candidate(tmp_path):
    a,acct,candle,manifest,overrides,geometry=_scenario(tmp_path,current_risk=1999.0)
    a.authority=BrainYes()
    out=_decide(a,acct,candle,manifest,overrides,geometry)
    assert out["status"]=="BLOCKED"
    assert out["candidate"] is None
    assert "PORTFOLIO_20_PERCENT_CEILING_EXCEEDED" in out["blockers"]


def test_broker_native_stop_validation_rejects_missing_stops(tmp_path):
    a,acct,candle,manifest,overrides,geometry=_scenario(tmp_path)
    a.authority=BrainYes()
    geometry["stop_price"]=0.0
    out=_decide(a,acct,candle,manifest,overrides,geometry)
    assert out["status"]=="BLOCKED"
    assert out["candidate"] is None
    assert "CONTRACT_VALUE_OR_NATIVE_PRICE_UNVERIFIED" in out["blockers"]


def test_missing_native_broker_session_blocks_candidate(tmp_path):
    a,acct,candle,manifest,overrides,geometry=_scenario(tmp_path)
    a.authority=BrainYes()
    overrides["fresh_broker_session"]=False
    out=_decide(a,acct,candle,manifest,overrides,geometry)
    assert out["status"]=="BLOCKED"
    assert out["candidate"] is None
    assert "BROKER_OR_EA_ATTESTATION_MISSING" in out["blockers"]
