"""Issue #12: trained directional authority, symbol/risk scope, genuine receipts."""
import time
from types import SimpleNamespace
import pytest
from abutron.apex_integration import ApexIntegration, build_instrument_manifest
from abutron.apex.v9_candidate_contract import AccountGate, BrokerNativeRisk, DERIV_ALLOWED, FP_SUPPORTED, review_candidate
from abutron.events import EventLog

def test_thirteen_native_deriv_symbols():
    assert len(DERIV_ALLOWED)==13
    assert [sum(s.startswith(p) for s in DERIV_ALLOWED) for p in ("Volatility ","Crash ","Boom ")]==[5,4,4]
    assert "GBPUSD.r" in FP_SUPPORTED
    fp_manifest=build_instrument_manifest([{"broker":"fpmarkets","symbols":"GBPUSD.r"}])
    assert len(fp_manifest)==1 and fp_manifest[0]["asset_class"]=="fx_index"
    assert {m["symbol"] for m in build_instrument_manifest([{"broker":"deriv","symbols":""}])}==DERIV_ALLOWED

def test_account_specific_risk_and_crash_candidate():
    t=(int(time.time())//300)*300
    gate=AccountGate(
        broker="deriv",environment="DEMO",server="Deriv-Demo",
        owner="a"*32,connection_id="b"*32,account_bound=True,
        worker_nonce_bound=True,fresh_broker_session=True,native_ea_account_match=True,
        broker_trade_allowed=True,terminal_trade_allowed=True,
        customer_switch_desired=True,customer_switch_effective=True,
        crm_entitlement_approved=True,single_writer_exclusive=True,
        existing_pending_orders=0,broker_positions_protected=True,
        broker_exposure_reconciled=True,account_equity_usd=10000.,
        current_verified_stop_risk_usd=0.,live_utc=t)
    sig={"engine":"APEX_1_1_0","broker":"deriv",
         "symbol":"Crash 1000 Index.0","timeframe":"M5",
         "bar_closed_ts":t,"status":"BUY","decision_id":"c"*64,"order":None}
    native=BrokerNativeRisk(tick_size=.01,tick_value_per_lot_usd=.1,
        min_lot=1.,max_lot=200.,lot_step=1.,min_stop_ticks=1,
        broker_verified=True,price=100.,stop_price=98.,take_price=103.,
        slippage_budget_ticks=1,broker_calc_profit_verified=True,
        broker_order_check_verified=True)
    a=review_candidate(signal=sig,gate=gate,native=native,now=t+1,
                       per_trade_risk_pct=.005,portfolio_ceiling_pct=.20)
    b=review_candidate(signal=sig,gate=gate,native=native,now=t+1,
                       per_trade_risk_pct=.03,portfolio_ceiling_pct=.20)
    assert a["engineering_gates_passed"],a["blockers"]
    assert b["engineering_gates_passed"],b["blockers"]
    assert a["risk_budget_usd"]==50 and b["risk_budget_usd"]==300
    assert b["proposed_lots_research_only"]>a["proposed_lots_research_only"]
    assert b["risk_fraction"]==.03 and b["orders_submitted"]==0

@pytest.mark.parametrize("direction",["SELL","HOLD"])
def test_brain_owns_direction_and_model_hold(tmp_path,direction):
    symbol="Boom 1000 Index.0"
    ai=ApexIntegration(EventLog(str(tmp_path/"events.jsonl")))
    assert ai.apex_available()
    ai._signal_hist[symbol]=[SimpleNamespace(close=100+i*.1,high=100.5+i*.1,low=99.5+i*.1) for i in range(40)]
    ai._run_selflearn=lambda *a,**kw:{"orderflow":{},"footprint":{},"agent_scores":{},"regime":"TREND"}
    ai.eligibility.min_equity_for=lambda *a,**kw:0
    ai.brain=SimpleNamespace(trained=True,decide=lambda **kw:SimpleNamespace(
        direction=direction,brain_version="unified_v1",trained=True,
        confidence=.8,conviction=.7,blockers=["LOSS_PATTERN_REPEAT"] if direction=="HOLD" else []))
    ts=(int(time.time())//300)*300
    ai._bridges["b"*32]=SimpleNamespace(on_closed_candle=lambda *a,**kw:{
        "engine":"APEX_1_1_0","broker":"deriv","symbol":symbol,"timeframe":"M5",
        "bar_closed_ts":ts,"status":"BUY","decision_id":"d"*64,"order":None,
        "regime":{},"signals":[]})
    observed=[]
    def fake_review(**kw):
        observed.append(kw)
        return {"engineering_gates_passed":False,"blockers":["BROKER_RECEIPT_PENDING"],"order_candidate":None}
    ai._apex_cache["review_candidate"]=fake_review
    acct={"connection_id":"b"*32,"broker":"deriv","owner_id":"a"*32,
          "mt5_login":6207,"desired_on":1,"effective_on":1,
          "per_trade_risk_pct":3.0,"portfolio_cap_pct":20.0}
    candle={"symbol":symbol,"close_time_epoch":ts,
            "open_time":"2026-11-16T14:00:00Z","close_time":"2026-11-16T14:05:00Z",
            "open":100.,"high":102.,"low":99.,"close":101.,"volume":1.}
    result=ai.process_candle(connection_id="b"*32,account=acct,
        candle=candle,manifest=build_instrument_manifest([acct]),equity=10000.)
    assert result["decision"]["status"]==direction
    assert result["decision"]["brain_authority"]["trained"]
    if direction=="HOLD":
        assert result["status"]=="HOLD" and result["candidate"] is None and not observed
    else:
        assert result["status"]=="BLOCKED" and len(observed)==1
        assert observed[0]["signal"]["status"]=="SELL"
        assert observed[0]["per_trade_risk_pct"]==pytest.approx(.03)
        assert observed[0]["portfolio_ceiling_pct"]==pytest.approx(.20)

def test_no_synthetic_manager_ack():
    from abutron.main import ApexRuntime
    from abutron.execution.ipc import manager_socket
    runtime=ApexRuntime.__new__(ApexRuntime)
    runtime.register_manager_handlers()
    result=manager_socket._handlers["order_handoff"](SimpleNamespace(raw={"request_id":"10001"},connection_id="b"*32))
    assert result["status"]=="reject" and "ticket" not in result
    assert result["reason"]=="NATIVE_BROKER_ORDER_WRITER_NOT_CONNECTED"
