"""Verified geometry is reserved before account-scoped dispatch."""
from abutron.agent.runtime import run_agentic_pass
from test_agent_runtime_gating import (
    CID, BROKER, _PositiveEngine, _adapter, _patch_db)


def native_spec():
    return dict(symbol="EURUSD.r", bid=1.1001, ask=1.1003,
                tick_age_ms=0, tick_size=0.0001, point=0.0001,
                min_lots=0.01, max_lots=10, volume_step=0.01,
                stops_level_points=2, freeze_level_points=2,
                pnl_probe_displacement_ticks=10,
                buy_probe_native_ordercalcprofit_usd=-1,
                broker_tick_value_loss=1)


def test_authoritative_brain_uses_actual_broker_sizing_and_scoped_queue(monkeypatch):
    account=dict(connection_id=CID, broker=BROKER, mt5_login=7410512,
                 server="DerivDemo", owner_id="owner1", environment="demo",
                 effective_on=1, equity=10000)
    db=_patch_db(monkeypatch, account)
    reserved=[]
    db.add_candidate=lambda row: reserved.append(row) or True
    class Dispatcher:
        def dispatch_account(self, cid, candidate_ids=None):
            assert cid==CID and candidate_ids==["cand-001"]
            return dict(sent=0, ack=0, unknown=0, rejected=0, details=[])
    result=run_agentic_pass(
        CID, engine=_PositiveEngine(), adapter=_adapter(),
        dispatcher=Dispatcher(), native_specs={"EURUSD.r":native_spec()},
        owner_open_risk_usd=0, owner_equity_usd=10000)
    assert result.reserved_candidate_ids==["cand-001"]
    assert len(reserved)==1
    assert reserved[0]["volume"]!=1.0
    assert reserved[0]["stop_loss"]<reserved[0]["price"]<reserved[0]["take_profit"]
    assert reserved[0]["replay_key"].startswith(CID+":")
    assert reserved[0]["execution_state"]=="RESERVED"


def test_no_proven_broker_geometry_never_reserves(monkeypatch):
    db=_patch_db(monkeypatch, dict(connection_id=CID, broker=BROKER,
        mt5_login=7410512, server="DerivDemo", owner_id="owner1",
        environment="demo", effective_on=1, equity=10000))
    db.add_candidate=lambda row: (_ for _ in ()).throw(AssertionError("unexpected order"))
    class Dispatcher:
        def dispatch_account(self,*a,**kw):raise AssertionError("unexpected dispatch")
    result=run_agentic_pass(CID, engine=_PositiveEngine(),
                           adapter=_adapter(),dispatcher=Dispatcher())
    assert result.dispatch_reason=="NATIVE_BROKER_ORDER_GEOMETRY_MISSING"
    assert result.reserved_candidate_ids==[]
