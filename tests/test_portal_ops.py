"""Tests for portal ops — run/trace logging, analytics, leaderboard, and
model-provider registry (AgenticTrading dashboard pattern, ABUTRON-adapted)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.db import (CandidateRow, LedgerRow, ModelProviderRow,
                        OrderReceiptRow, RunRow, engine_db)
from abutron.ops.portal_ops import PortalOps


@pytest.fixture
def ops():
    # Clear the relevant tables for a deterministic test. Roll back any
    # pending transaction left by other tests on the shared engine_db session.
    s = engine_db.session
    s.rollback()
    for model in (RunRow, ModelProviderRow, CandidateRow, OrderReceiptRow,
                  LedgerRow):
        s.query(model).delete()
    s.commit()
    return PortalOps()


def test_record_and_list_run(ops):
    rid = ops.record_run(connection_id="conn1", owner_id="owner1",
                         symbol="EURUSD", signal_id="sig1",
                         decision="BUY", status="READY", regime="trend",
                         conviction=1.2, gemma_signal="BUY",
                         gemma_confidence=0.7)
    assert rid.startswith("run_")
    runs = ops.list_runs(connection_id="conn1")
    assert len(runs) == 1
    r = runs[0]
    assert r["symbol"] == "EURUSD"
    assert r["status"] == "READY"
    assert r["signal_id"] == "sig1"
    assert r["gemma_signal"] == "BUY"


def test_get_run(ops):
    rid = ops.record_run(connection_id="conn1", owner_id="o", symbol="X",
                         signal_id="s", decision="HOLD", status="HOLD")
    assert ops.get_run(rid)["run_id"] == rid
    assert ops.get_run("nope") is None


def test_analytics_empty(ops):
    a = ops.analytics()
    assert a["candidates_total"] == 0
    assert a["trades_closed"] == 0
    assert a["win_rate_pct"] == 0.0
    assert a["net_pnl_usd"] == 0.0


def test_analytics_with_ledger(ops):
    engine_db.add_ledger({"connection_id": "conn1", "owner_id": "owner1",
                          "native_ticket": 1, "event": "OPEN",
                          "symbol": "EURUSD", "direction": "BUY",
                          "volume": 1.0, "price": 1.1, "realized_pnl": 0.0,
                          "commission": 0.0, "swap": 0.0, "balance": 100000.0,
                          "observed_at": "t", "source": "native"})
    engine_db.add_ledger({"connection_id": "conn1", "owner_id": "owner1",
                          "native_ticket": 1, "event": "CLOSE",
                          "symbol": "EURUSD", "direction": "BUY",
                          "volume": 1.0, "price": 1.102,
                          "realized_pnl": 200.0, "commission": 5.0,
                          "swap": -1.0, "balance": 100194.0,
                          "observed_at": "t2", "source": "native"})
    a = ops.analytics()
    assert a["trades_closed"] == 1
    assert a["wins"] == 1
    assert a["win_rate_pct"] == 100.0
    assert a["realized_pnl_usd"] == 200.0
    assert a["commission_usd"] == 5.0
    assert a["net_pnl_usd"] == 194.0


def test_leaderboard_ranks_by_pnl(ops):
    for owner, pnl in (("ownerA", 500.0), ("ownerB", -200.0),
                       ("ownerC", 100.0)):
        engine_db.add_ledger({"connection_id": f"c-{owner}",
                              "owner_id": owner, "native_ticket": 1,
                              "event": "CLOSE", "symbol": "EURUSD",
                              "direction": "BUY", "volume": 1.0,
                              "price": 1.1, "realized_pnl": pnl,
                              "commission": 0.0, "swap": 0.0,
                              "balance": 100000.0, "observed_at": "t",
                              "source": "native"})
    lb = ops.leaderboard()
    assert len(lb) == 3
    assert lb[0]["owner_id"] == "ownerA"
    assert lb[0]["realized_pnl_usd"] == 500.0
    assert lb[-1]["owner_id"] == "ownerB"


def test_model_provider_registry(ops):
    ops.upsert_model_provider({"provider_id": "mp_gemma",
                               "provider": "gemma",
                               "base_url": "http://127.0.0.1:11434/v1",
                               "model": "gemma2:9b", "enabled": 1,
                               "env_secret_var": "GEMMA_API_KEY"})
    provs = ops.list_model_providers()
    assert len(provs) == 1
    assert provs[0]["provider"] == "gemma"
    assert provs[0]["env_secret_var"] == "GEMMA_API_KEY"
    # No secret VALUE stored — only the env var name (non-secret config).
    assert "secret" not in provs[0]
    assert "api_key" not in provs[0]
    assert "api_key_value" not in provs[0]


def test_model_provider_upsert_and_delete(ops):
    ops.upsert_model_provider({"provider_id": "mp_x", "provider": "ollama",
                               "base_url": "http://x/v1", "model": "m",
                               "enabled": 0})
    ops.upsert_model_provider({"provider_id": "mp_x", "provider": "ollama",
                               "base_url": "http://y/v1", "model": "m2",
                               "enabled": 1})
    provs = ops.list_model_providers()
    assert len(provs) == 1  # upsert, not duplicate
    assert provs[0]["base_url"] == "http://y/v1"
    assert ops.delete_model_provider("mp_x") is True
    assert ops.list_model_providers() == []
    assert ops.delete_model_provider("mp_x") is False
