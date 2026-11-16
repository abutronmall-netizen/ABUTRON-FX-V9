"""V9 terminal/provenance boundary checks; no broker or Wine processes launched."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from abutron.events import EventLog
from abutron.feeds.broker_feeds import get_feed
from abutron.feeds.mt5_terminal import Mt5TerminalManager, Terminal
from abutron.feeds.mt5_verifier import (
    Mt5Verifier, REASON_NATIVE_VERIFICATION_INCONCLUSIVE,
    REASON_VERIFIER_UNAVAILABLE,
)


def _terminal(tmp_path, *, running: bool):
    class FakeProcess:
        def poll(self):
            return None if running else 1
    return Terminal(
        connection_id="fixture-conn", broker="fpmarkets", login=7410512,
        server="FPMarketsSC-Demo", wine_prefix=str(tmp_path),
        export_dir=str(tmp_path),
        proc=FakeProcess() if running else None,
    )


def test_stopped_terminal_not_reported_connected(tmp_path):
    mgr = Mt5TerminalManager(root=str(tmp_path / "terminal-root"))
    mgr._terminals["fixture-conn"] = _terminal(tmp_path, running=False)
    assert mgr.health("fixture-conn")["status"] == "DISCONNECTED"
    assert mgr.read_native_export("fixture-conn") is None


def test_running_terminal_reports_connected(tmp_path):
    mgr = Mt5TerminalManager(root=str(tmp_path / "terminal-root"))
    mgr._terminals["fixture-conn"] = _terminal(tmp_path, running=True)
    assert mgr.health("fixture-conn")["status"] == "CONNECTED"


def test_running_terminal_reads_native_file_without_launch(tmp_path):
    mgr = Mt5TerminalManager(root=str(tmp_path / "terminal-root"))
    mgr._terminals["fixture-conn"] = _terminal(tmp_path, running=True)
    ident = tmp_path / "ABUTRON_NATIVE_ACCOUNT_V2_fixture.txt"
    bars = tmp_path / "ABUTRON_NATIVE_BARS_V2_fixture.txt"
    ident.write_text(json.dumps({"connection_id": "fixture-conn",
                                 "login": 7410512, "broker": "fpmarkets"}))
    bars.write_text(json.dumps({"bars": []}))
    output = mgr.read_native_export("fixture-conn")
    assert output["identity"]["login"] == 7410512
    assert output["bars"]["bars"] == []


def _export(*, identity_overrides=None):
    end = int(time.time() // 300) * 300
    open_at = end - 300
    ident = {
        "connection_id": "fixture-conn", "login": 7410512,
        "broker": "fpmarkets", "nonce": "fixture-test-nonce",
        "challenge": "fixture-challenge",
        "worker_health_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
    }
    ident.update(identity_overrides or {})
    bar = {
        "symbol": "GBPUSD.r", "timeframe": "M5",
        "open_time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(open_at)),
        "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(end)),
        "open": 1.25, "high": 1.255,
        "low": 1.245, "close": 1.252,
        "volume": 100.0, "bid": 1.2520, "ask": 1.2522,
        "spread": 0.0002, "tick_size": 0.0001, "point": 0.0001,
        "closed": True,
    }
    return {"identity": ident, "bars": {"bars": [bar]}}


def test_native_export_identity_is_validated_from_real_subobject(
        tmp_path, monkeypatch):
    from abutron.feeds import broker_feeds
    monkeypatch.setattr(broker_feeds.terminal_manager,
                        "read_native_export", lambda _: _export())
    monkeypatch.setattr(broker_feeds.clock_estimator, "estimate",
                        lambda *_: SimpleNamespace(verified=True, status="VERIFIED"))
    result = get_feed("fpmarkets", EventLog(str(tmp_path / "feed.jsonl"))).read(
        "fixture-conn", 7410512, ["GBPUSD.r"])
    assert result.accepted is True
    assert len(result.candles) == 1


def test_native_export_wrong_broker_not_overwritten_by_expected_broker(
        tmp_path, monkeypatch):
    from abutron.feeds import broker_feeds
    monkeypatch.setattr(broker_feeds.terminal_manager, "read_native_export",
                        lambda _: _export(identity_overrides={"broker": "deriv"}))
    monkeypatch.setattr(broker_feeds.clock_estimator, "estimate",
                        lambda *_: SimpleNamespace(verified=True, status="VERIFIED"))
    result = get_feed("fpmarkets", EventLog(str(tmp_path / "feed2.jsonl"))).read(
        "fixture-conn", 7410512, ["GBPUSD.r"])
    assert result.accepted is False
    assert "EXPORT_BROKER_MISMATCH" in result.violations


def test_native_export_wrong_login_rejected(tmp_path, monkeypatch):
    from abutron.feeds import broker_feeds
    monkeypatch.setattr(broker_feeds.terminal_manager, "read_native_export",
                        lambda _: _export(identity_overrides={"login": 7411636}))
    monkeypatch.setattr(broker_feeds.clock_estimator, "estimate",
                        lambda *_: SimpleNamespace(verified=True, status="VERIFIED"))
    result = get_feed("fpmarkets", EventLog(str(tmp_path / "feed3.jsonl"))).read(
        "fixture-conn", 7410512, ["GBPUSD.r"])
    assert result.accepted is False
    assert "EXPORT_LOGIN_MISMATCH" in result.violations


def test_claimed_login_file_is_not_broker_authentication(tmp_path):
    term = _terminal(tmp_path, running=True)

    class FakeManager:
        def get(self, cid):
            return term
        def read_native_export(self, cid):
            return {"identity": {"login": 7410512}}
    verifier = Mt5Verifier(FakeManager())
    valid, reason = verifier.verify(connection_id="fixture-conn",
                                   broker="fpmarkets", server="FPMarketsSC-Demo",
                                   login=7410512, password="fixture-only")
    assert valid is False
    assert reason == REASON_NATIVE_VERIFICATION_INCONCLUSIVE


def test_verifier_on_stopped_terminal_rejects(tmp_path):
    term = _terminal(tmp_path, running=False)

    class FakeManager:
        def get(self, cid):
            return term
    valid, reason = Mt5Verifier(FakeManager()).verify(
        connection_id="fixture-conn", broker="fpmarkets",
        server="FPMarketsSC-Demo", login=7410512,
        password="fixture-only")
    assert valid is False
    assert reason == REASON_VERIFIER_UNAVAILABLE
