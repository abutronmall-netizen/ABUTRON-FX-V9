"""Prevent false-positive owner-risk, stop and trailing broker evidence."""
import types
from abutron.risk import risk_engine


def _engine(monkeypatch, account, positions):
    monkeypatch.setattr(risk_engine, "engine_db", types.SimpleNamespace(
        list_accounts=lambda owner_id: [account],
        recent_open_positions=lambda cid: positions,
    ))
    return risk_engine.RiskEngine.__new__(risk_engine.RiskEngine)


def test_unknown_index_symbol_does_not_invent_native_point_value(monkeypatch):
    re = _engine(monkeypatch,
        {"connection_id":"fp", "equity":10000.0,
         "native_positions_verified":True,"account_currency":"USD"},
        [{"symbol":"UNREGISTERED_NATIVE_INDEX_2026", "fill_price":10000.0,
          "stop_loss":9999.0,"volume":1.0}])
    result = re.aggregate_owner_risk("owner")
    assert result["owner_risk_certified"] is False
    assert result["cap_exceeded"] is None
    assert result["headroom_usd"] is None
    assert result["risk_usd_is_lower_bound_only"] is True
    assert "UNREGISTERED_NATIVE_INDEX_2026" in result["unknown_symbols"]


def test_missing_position_snapshot_is_not_flat(monkeypatch):
    re = _engine(monkeypatch,
        {"connection_id":"fp","equity":10000.0,
         "native_positions_verified":False,"account_currency":"USD"},[])
    result = re.aggregate_owner_risk("owner")
    assert result["owner_risk_certified"] is False
    assert result["cap_exceeded"] is None
    assert "fp" in result["unknown_exposure_connections"]


def test_verified_flat_and_usd_equity_can_pass(monkeypatch):
    re = _engine(monkeypatch,
        {"connection_id":"fp","equity":10000.0,
         "native_positions_verified":True,"account_currency":"USD"},[])
    result = re.aggregate_owner_risk("owner")
    assert result["owner_risk_certified"] is True
    assert result["cap_exceeded"] is False
    assert result["headroom_usd"] == 2000.0


def test_non_usd_account_cannot_be_summed_as_usd(monkeypatch):
    re = _engine(monkeypatch,
        {"connection_id":"fp","equity":10000.0,
         "native_positions_verified":True,"account_currency":"EUR"},[])
    result = re.aggregate_owner_risk("owner")
    assert result["owner_risk_certified"] is False
    assert result["cap_exceeded"] is None


def test_known_symbol_registry_is_not_mt5_stop_risk_proof(monkeypatch):
    obj = _engine(monkeypatch,
        {"connection_id":"fp", "equity":10000.0,
         "native_positions_verified":True,"account_currency":"USD"},
        [{"symbol":"EURUSD.r", "direction":"BUY", "fill_price":1.10,
          "stop_loss":1.09,"volume":0.10}])
    result=obj.aggregate_owner_risk("owner")
    assert result["owner_risk_certified"] is False
    assert result["cap_exceeded"] is None
    assert result["aggregate_stop_risk_usd"] is None
    assert "EURUSD.r" in result["unknown_symbols"]


def test_native_ordercalcprofit_actual_usd_risk_can_be_certified(monkeypatch):
    obj = _engine(monkeypatch,
        {"connection_id":"fp", "equity":10000.0,
         "native_positions_verified":True,"account_currency":"USD"},
        [{"symbol":"US30", "direction":"SELL", "fill_price":50100,
          "stop_loss":50200, "volume":0.03,
          "broker_native_ordercalcprofit_verified":True,
          "broker_native_stop_risk_usd":3.00}])
    result=obj.aggregate_owner_risk("owner")
    assert result["owner_risk_certified"] is True
    assert result["aggregate_stop_risk_usd"] == 3.00
    assert result["headroom_usd"] == 1997.0


def test_missing_owner_account_cannot_certify_zero_risk(monkeypatch):
    monkeypatch.setattr(risk_engine,"engine_db",types.SimpleNamespace(
        list_accounts=lambda owner_id: [], recent_open_positions=lambda cid: []))
    obj=risk_engine.RiskEngine.__new__(risk_engine.RiskEngine)
    result=obj.aggregate_owner_risk("missing-owner")
    assert result["owner_risk_certified"] is False
    assert result["cap_exceeded"] is None


def test_pending_trailing_cannot_write_native_ledger(monkeypatch):
    calls=[]
    monkeypatch.setattr(risk_engine,"engine_db",types.SimpleNamespace(
        add_ledger=lambda item:calls.append(item)))
    events=[]
    obj=risk_engine.RiskEngine.__new__(risk_engine.RiskEngine)
    obj.log=types.SimpleNamespace(append=lambda event:events.append(event))
    obj._apply_trail("fp","cand","EURUSD.r","BUY",1.10,1.09,0,False)
    assert calls == []
    assert len(events)==1


def test_confirmed_native_trailing_has_ticket(monkeypatch):
    calls=[]
    monkeypatch.setattr(risk_engine,"engine_db",types.SimpleNamespace(
        add_ledger=lambda item:calls.append(item)))
    events=[]
    obj=risk_engine.RiskEngine.__new__(risk_engine.RiskEngine)
    obj.log=types.SimpleNamespace(append=lambda event:events.append(event))
    obj._apply_trail("fp","cand","EURUSD.r","BUY",1.10,1.09,1234,True)
    assert len(calls)==1 and calls[0]["native_ticket"]==1234
    assert calls[0]["stop_loss"]==1.10


