"""Native production execution guard: no speculative owner-risk headroom."""
from unittest.mock import MagicMock, patch

from abutron.execution.dispatcher import NativeDispatcher
from abutron.risk.contract_verify import verify_contract


def test_registry_contract_is_not_broker_certification():
    estimate = verify_contract("EURUSD.r", equity=10_000,
                               stop_distance_points=26)
    assert estimate.to_dict()["verified"] is False
    assert "BROKER_NATIVE_ORDER_CALC_PROFIT_NOT_VERIFIED" in estimate.issues


def test_dispatcher_rejects_unknown_owner_without_sending():
    gateway = MagicMock()
    dispatcher = NativeDispatcher(MagicMock(), gateway, MagicMock())
    with patch("abutron.execution.dispatcher.db.engine_db.get_account",
               return_value={"connection_id": "demo", "owner_id": ""}):
        result = dispatcher.dispatch_account("demo")
    assert result["error"] == "OWNER_ID_UNVERIFIED"
    assert result["sent"] == 0
    gateway.send_order.assert_not_called()


def test_dispatcher_blocks_missing_native_exposure_not_registry_estimate():
    gateway = MagicMock()
    dispatcher = NativeDispatcher(MagicMock(), gateway, MagicMock())
    account = {"connection_id": "demo", "owner_id": "owner",
               "broker": "fpmarkets", "mt5_login": 12345}
    partial = {"owner_risk_certified": False,
               "aggregate_stop_risk_usd": None,
               "cap_exceeded": None,
               "unknown_exposure_connections": ["demo"]}
    with patch("abutron.execution.dispatcher.db.engine_db.get_account",
               return_value=account), \
         patch("abutron.risk.risk_engine.RiskEngine.aggregate_owner_risk",
               return_value=partial), \
         patch.object(dispatcher, "_block_for_incomplete_exposure",
                      return_value=["candidate-1"]):
        result = dispatcher.dispatch_account("demo")
    assert result["error"] == "BLOCKED_INCOMPLETE_EXPOSURE"
    assert result["sent"] == 0
    assert result["blocked"] == ["candidate-1"]
    gateway.send_order.assert_not_called()


def test_dispatcher_blocks_even_when_observed_risk_is_below_cap():
    gateway = MagicMock()
    dispatcher = NativeDispatcher(MagicMock(), gateway, MagicMock())
    account = {"connection_id": "demo", "owner_id": "owner",
               "broker": "fpmarkets", "mt5_login": 12345}
    partial = {"owner_risk_certified": False,
               "aggregate_stop_risk_usd": 1.0,
               "cap_exceeded": False,
               "unknown_exposure_connections": ["other-account"]}
    with patch("abutron.execution.dispatcher.db.engine_db.get_account",
               return_value=account), \
         patch("abutron.risk.risk_engine.RiskEngine.aggregate_owner_risk",
               return_value=partial), \
         patch.object(dispatcher, "_block_for_incomplete_exposure",
                      return_value=[]):
        result = dispatcher.dispatch_account("demo")
    assert result["sent"] == 0
    assert result["error"] == "BLOCKED_INCOMPLETE_EXPOSURE"
    gateway.send_order.assert_not_called()
