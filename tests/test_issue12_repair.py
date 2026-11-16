"""Regression coverage for issue #12's safe V9 integration repairs."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.apex_integration import ApexIntegration
from abutron.events import EventLog
from abutron.execution.ipc import ManagerRequest, ManagerSocketServer, manager_socket
from abutron.crypto import hmac_sign
from abutron.main import ApexRuntime


def test_missing_trained_model_abstains_instead_of_using_apex_direction(tmp_path, monkeypatch):
    import abutron.apex_integration as integration

    monkeypatch.setattr(integration, "BRAIN_MODEL_DIR", str(tmp_path / "no-model"))
    app = ApexIntegration(EventLog(str(tmp_path / "events.jsonl")))
    assert not app.brain.trained
    status, brain, reason = app._brain_direction(
        symbol="Volatility 100 Index.0",
        candle={"close_time_epoch": 1_800_000_000, "open": 1.0,
                "high": 1.1, "low": 0.9, "close": 1.05},
        apex_decision={"status": "BUY"},
        orderflow_features=None, footprint_features=None)
    assert status == "HOLD"
    assert brain["trained"] is False
    assert reason.startswith("TRAINED_BRAIN_UNAVAILABLE")


def test_loss_pattern_repeat_is_an_explicit_hold(tmp_path, monkeypatch):
    import abutron.apex_integration as integration

    monkeypatch.setattr(integration, "BRAIN_MODEL_DIR", str(tmp_path / "no-model"))
    app = ApexIntegration(EventLog(str(tmp_path / "loss-events.jsonl")))

    class Proposal:
        direction = "BUY"
        trained = True
        blockers = ["LOSS_PATTERN_REPEAT"]

        def to_dict(self):
            return {"direction": self.direction, "blockers": self.blockers,
                    "trained": True}

    class TrainedBrain:
        trained = True

        def decide(self, **kwargs):
            return Proposal()

    app.brain = TrainedBrain()
    app._signal_history["X"] = [{"open": 1.0, "high": 1.1,
                                  "low": 0.9, "close": 1.0}] * 31
    status, _, reason = app._brain_direction(
        symbol="X", candle={"close_time_epoch": 1_800_000_000, "open": 1.0,
                            "high": 1.1, "low": 0.9, "close": 1.05},
        apex_decision={"status": "BUY"}, orderflow_features=None,
        footprint_features=None)
    assert status == "HOLD"
    assert reason == "LOSS_PATTERN_REPEAT"


def test_manager_never_fabricates_order_receipt(tmp_path):
    runtime = ApexRuntime(EventLog(str(tmp_path / "manager-events.jsonl")))
    runtime.register_manager_handlers()
    response = manager_socket._handlers["order"](
        ManagerRequest({"kind": "order", "request_id": "request-1234"}))
    assert response["status"] == "reject"
    assert response["reason"] == "NATIVE_ORDER_TRANSPORT_UNAVAILABLE"
    assert "ticket" not in response


def test_candidate_bound_order_signature_matches_writer_protocol():
    secret = "unit-test-secret"
    server = ManagerSocketServer(secret=secret)
    server.register("order_handoff", lambda req: {
        "status": "reject", "reason": "NATIVE_ORDER_TRANSPORT_UNAVAILABLE"})
    raw = {"kind": "order_handoff", "request_id": "nonce-1",
           "candidate_id": "candidate-1", "connection_id": "account-1"}
    raw["signed"] = hmac_sign(
        secret, "nonce-1|candidate-1|account-1")
    response = server.dispatch(ManagerRequest(raw))
    assert response["reason"] == "NATIVE_ORDER_TRANSPORT_UNAVAILABLE"
    raw["signed"] = hmac_sign(secret, "nonce-1|other-candidate|account-1")
    assert server.dispatch(ManagerRequest(raw))["reason"] == "BAD_SIGNATURE"


def test_v9_contract_approves_exact_restored_deriv_universe():
    from abutron.apex.v9_candidate_contract import DERIV_ALLOWED

    assert len(DERIV_ALLOWED) == 13
    assert "Boom 150 Index.0" in DERIV_ALLOWED
    assert "Crash 1000 Index.0" in DERIV_ALLOWED
    assert "Volatility 75 Index.0" in DERIV_ALLOWED


def test_account_percent_is_converted_to_fraction_and_invalid_fails_closed():
    from abutron.apex_integration import _account_risk_fraction

    assert _account_risk_fraction({"per_trade_risk_pct": 0.5},
                                  "per_trade_risk_pct", 0.5) == 0.005
    assert _account_risk_fraction({"per_trade_risk_pct": float("nan")},
                                  "per_trade_risk_pct", 0.5) == 0.0
