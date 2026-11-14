"""Staged V9 DEMO broker-handoff invariants; all broker replies are fixtures."""
from __future__ import annotations
import copy
import os
import pytest

from abutron.events import EventLog
from abutron.execution.writer_gateway import SingleWriterGateway, WriterStatus
from abutron.execution.ipc import manager_socket, ManagerRequest
from abutron.main import ApexRuntime
from abutron.apex_integration import ApexIntegration


def fixture_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "fixture-only-activation-secret")
    gateway = SingleWriterGateway(EventLog(str(tmp_path / "writer.jsonl")))
    gateway._armed["demo-conn"] = WriterStatus(
        "demo-conn", True, "ea-verified", "ea-verified", "rev1",
        True, False, "fixture")
    account = {
        "connection_id": "demo-conn", "broker": "fpmarkets",
        "mt5_login": 7412582, "environment": "demo",
        "desired_on": True, "effective_on": True,
    }
    candidate = {
        "candidate_id": "cand-staging-fixture",
        "connection_id": "demo-conn",
        "symbol": "GBPUSD.r", "direction": "BUY", "volume": .1,
        "entry_type": "LIMIT", "trigger_price": 1.2540,
        "stop_loss": 1.2530, "take_profit": 1.2580,
    }
    return gateway, account, candidate


@pytest.mark.parametrize("changed,reason", [
    ({"desired_on": False}, "CLIENT_AUTOMATIC_SWITCH_OFF"),
    ({"effective_on": False}, "CLIENT_AUTOMATIC_SWITCH_OFF"),
    ({"environment": "live"}, "DEMO_ONLY_ACCOUNT_BOUNDARY"),
    ({"mt5_login": 12340178}, "RETIRED_ACCOUNT_NOT_ALLOWED"),
    ({"mt5_login": 12343492}, "RETIRED_ACCOUNT_NOT_ALLOWED"),
    ({"broker": "deriv"}, "BROKER_OR_CONNECTION_MISMATCH"),
])
def test_disallowed_account_never_reaches_native_writer(
        tmp_path, monkeypatch, changed, reason):
    gateway, account, candidate = fixture_gateway(tmp_path, monkeypatch)
    account.update(changed)

    def must_not_send(*_args):
        raise AssertionError("native writer called on disallowed account")
    gateway._deliver_to_ea = must_not_send
    result = gateway.send_order(connection_id="demo-conn", candidate=candidate,
                                account=account, broker="fpmarkets")
    assert result == {"outcome": "REJECTED", "reason": reason}


def test_invalid_stops_never_sent(tmp_path, monkeypatch):
    gateway, account, candidate = fixture_gateway(tmp_path, monkeypatch)
    candidate["stop_loss"] = candidate["trigger_price"] + 0.0001
    gateway._deliver_to_ea = lambda *_: pytest.fail("order should not send")
    result = gateway.send_order(connection_id="demo-conn", candidate=candidate,
                                account=account, broker="fpmarkets")
    assert result["outcome"] == "REJECTED"
    assert result["reason"] == "INVALID_BUY_STOPS"


def test_unarmed_ea_never_sent(tmp_path, monkeypatch):
    gateway, account, candidate = fixture_gateway(tmp_path, monkeypatch)
    gateway._armed["demo-conn"].armed = False
    gateway._deliver_to_ea = lambda *_: pytest.fail("disarmed writer sent")
    result = gateway.send_order(connection_id="demo-conn", candidate=candidate,
                                account=account, broker="fpmarkets")
    assert result["reason"] == "EA_WRITER_NOT_ATTESTED"


def test_missing_activation_secret_rejected(tmp_path, monkeypatch):
    gateway, account, candidate = fixture_gateway(tmp_path, monkeypatch)
    monkeypatch.delenv("ABUTRON_ACTIVATION_SECRET", raising=False)
    gateway._deliver_to_ea = lambda *_: pytest.fail("secretless request sent")
    result = gateway.send_order(connection_id="demo-conn", candidate=candidate,
                                account=account, broker="fpmarkets")
    assert result["reason"] == "ACTIVATION_SECRET_UNSET"


def test_forged_ticket_without_protected_deal_is_not_ack(tmp_path, monkeypatch):
    gateway, account, candidate = fixture_gateway(tmp_path, monkeypatch)
    gateway._deliver_to_ea = lambda req, _: ("ACK", {"status": "ack", "ticket": 12345})
    result = gateway.send_order(connection_id="demo-conn", candidate=candidate,
                                account=account, broker="fpmarkets")
    assert result["outcome"] == "UNKNOWN"
    assert result["receipt_ref"] is None


def test_mismatched_broker_native_receipt_is_not_ack(tmp_path, monkeypatch):
    gateway, account, candidate = fixture_gateway(tmp_path, monkeypatch)

    def wrong_account(req, _):
        return "ACK", {
            "status": "ack", "ticket": 12345, "deal": "fixture-deal",
            "connection_id": "another-tenant", "mt5_login": req["mt5_login"],
            "broker_native_protected": True, "fill_price": 1.2542,
            "stop_loss": 1.2530, "take_profit": 1.2580,
        }

    gateway._deliver_to_ea = wrong_account
    result = gateway.send_order(connection_id="demo-conn", candidate=candidate,
                                account=account, broker="fpmarkets")
    assert result["outcome"] == "UNKNOWN"


def test_runtime_manager_cannot_fabricate_order_ack(tmp_path):
    runtime = ApexRuntime(EventLog(str(tmp_path / "runtime.jsonl")))
    runtime.register_manager_handlers()
    for kind in ("order", "order_handoff"):
        response = manager_socket._handlers[kind](ManagerRequest({
            "kind": kind, "connection_id": "demo-conn",
            "request_id": "fixture-nonce",
        }))
        assert response["status"] == "unknown"
        assert "ticket" not in response


def test_ready_candidate_carries_only_broker_reviewed_price(tmp_path):
    integration = ApexIntegration(EventLog(str(tmp_path / "ready.jsonl")))
    signal = "f" * 64
    account = {
        "connection_id": "reviewed-price-fixture",
        "owner_id": "fixture-owner", "broker": "deriv",
        "equity": 10000.0,
    }
    candle = {"open_time": "2026-11-14T11:00:00Z"}
    ready = {
        "candidate": {
            "signal_id": signal, "side": "BUY", "broker": "deriv",
            "symbol": "Volatility 100 Index.0",
            "volume_lots": 1.0, "stop_price": 99.5, "take_price": 101.0,
        },
        "review": {"risk_budget_usd": 10.0, "nominal_stop_risk_usd": 3.0},
        "native_entry_price": 100.0,
        "decision": {},
    }
    assert integration.store_ready_candidate(
        connection_id=account["connection_id"], account=account,
        ready={k: v for k, v in ready.items() if k != "native_entry_price"},
        candle=candle) is None
    stored = integration.store_ready_candidate(
        connection_id=account["connection_id"], account=account,
        ready=ready, candle=candle)
    assert stored["price"] == 100.0
    assert stored["entry_type"] == "CONFIRM"
    assert stored["trigger_price"] == 100.0
    assert stored["stop_loss"] == 99.5
    assert stored["trail_k"] == 0.0


def test_internal_apex_engine_never_reserves_when_client_off(tmp_path):
    from abutron.apex.engine import ApexEngine
    engine = ApexEngine(EventLog(str(tmp_path / "legacy-engine.jsonl")))
    engine._owner_book = lambda *_: pytest.fail("OFF client reached owner book")
    for desired, effective in ((False, True), (True, False), (False, False)):
        assert engine.run_decision_pass(
            connection_id="client-off", broker="deriv", owner_id="owner",
            candles=[], quotes=[], account={
                "desired_on": desired, "effective_on": effective,
                "equity": 10000.0, "balance": 10000.0,
            }) == []
