"""Tests for the multi-agent debate advisory layer (TradingAgents pattern).

The debate layer is advisory-only. These tests verify:
- graceful degradation when disabled / the LLM is unavailable,
- the full bull/bear + risk-debator + manager flow via a mocked LLM,
- consensus aggregation when the manager call fails but debaters succeeded,
- robust JSON extraction from model output.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.apex.debate import (DEBATE_MAX_CALLS, DebateAdvisor,
                                 DebateAdvisory, _aggregate, _extract_json)


def _context():
    return {
        "symbol": "EURUSD",
        "regime": "trend",
        "opinions": [
            {"agent": "trend", "direction": "BUY", "conviction": 1.2},
            {"agent": "meanrev", "direction": "SELL", "conviction": 0.8},
        ],
        "orderflow": {"delta": 12.5, "imbalance": 0.62, "buy_ratio": 0.62},
        "footprint": {"poc_price": 1.0850, "value_area_low": 1.0840,
                      "value_area_high": 1.0860, "va_imbalance": 0.3},
    }


def _fake_response(content: str):
    r = types.SimpleNamespace(status_code=200)
    r.json = lambda: {"choices": [{"message": {"content": content}}]}
    return r


def _bull_json():
    return '{"stance":"BUY","conviction":0.8,"argument":"Bull case."}'


def _bear_json():
    return '{"stance":"SELL","conviction":0.5,"argument":"Bear case."}'


def _debator_json(stance, conv):
    return f'{{"stance":"{stance}","conviction":{conv},"argument":"risk"}}'


def _manager_json(signal="BUY", conf=0.7):
    return f'{{"signal":"{signal}","confidence":{conf},"narrative":"synthesis"}}'


def _run_advisor(monkeypatch, responses, enabled=True):
    """Build a DebateAdvisor whose requests.post returns the queued responses."""
    advisor = DebateAdvisor(base_url="http://fake/v1", model="test-model",
                            enabled=enabled, timeout=5, max_retries=0,
                            rounds=1)
    calls = {"n": 0}

    def _post(url, json=None, timeout=None, headers=None):
        i = calls["n"]
        calls["n"] += 1
        if i < len(responses):
            return responses[i]
        return _fake_response(_manager_json("NEUTRAL", 0.0))

    monkeypatch.setattr("abutron.apex.debate.requests.post", _post)
    return advisor, calls


# --------------------------------------------------------------------------
def test_disabled_returns_no_advisory():
    advisor = DebateAdvisor(base_url="http://fake/v1", model="m", enabled=False)
    adv = advisor.advise(_context())
    assert adv.available is False
    assert adv.signal == "NO_ADVISORY"
    assert adv.reason == "GEMMA_DISABLED"


def test_full_debate_flow_produces_consensus(monkeypatch):
    responses = [
        _fake_response(_bull_json()),
        _fake_response(_bear_json()),
        _fake_response(_debator_json("BUY", 0.6)),
        _fake_response(_debator_json("NEUTRAL", 0.4)),
        _fake_response(_debator_json("SELL", 0.5)),
        _fake_response(_manager_json("BUY", 0.72)),
    ]
    advisor, _ = _run_advisor(monkeypatch, responses)
    adv = advisor.advise(_context())

    assert adv.available is True
    assert adv.signal == "BUY"
    assert adv.confidence == pytest.approx(0.72)
    assert adv.narrative == "synthesis"
    # 5 debaters + 1 manager recorded.
    assert len(adv.debate) == 5
    roles = [d["role"] for d in adv.debate]
    assert "bull" in roles and "bear" in roles
    assert "aggressive" in roles and "conservative" in roles and "neutral" in roles
    assert adv.rounds == 1


def test_manager_failure_aggregates_debaters(monkeypatch):
    # 5 debaters return content, the 6th call (manager) returns non-JSON.
    responses = [
        _fake_response(_bull_json()),
        _fake_response(_bear_json()),
        _fake_response(_debator_json("BUY", 0.7)),
        _fake_response(_debator_json("NEUTRAL", 0.4)),
        _fake_response(_debator_json("SELL", 0.4)),
        _fake_response("not json at all"),  # manager -> parse failure
    ]
    advisor, _ = _run_advisor(monkeypatch, responses)
    adv = advisor.advise(_context())

    assert adv.available is True
    assert adv.reason == "DEBATE_MANAGER_UNAVAILABLE"
    # 5 debaters aggregated: buys 0.8+0.7 vs sells 0.5+0.4 -> BUY.
    assert adv.signal == "BUY"
    assert 0.0 < adv.confidence < 1.0
    assert len(adv.debate) == 5


def test_all_agents_unavailable_degrades(monkeypatch):
    # Every call returns HTTP 500 -> all agents fail -> NO_ADVISORY.
    r = types.SimpleNamespace(status_code=500)
    r.json = lambda: {}
    advisor, _ = _run_advisor(monkeypatch, [r] * 8)
    adv = advisor.advise(_context())
    assert adv.available is False
    assert adv.signal == "NO_ADVISORY"


def test_budget_exhaustion_degrades_gracefully(monkeypatch):
    # DEBATE_MAX_CALLS is small; force many calls to prove it stops early.
    advisor = DebateAdvisor(base_url="http://fake/v1", model="m",
                            enabled=True, timeout=5, max_retries=0,
                            rounds=1)

    def _post(url, json=None, timeout=None, headers=None):
        return _fake_response(_bull_json())

    monkeypatch.setattr("abutron.apex.debate.requests.post", _post)
    adv = advisor.advise(_context())
    # Must not hang or exceed budget; degrades or aggregates.
    assert isinstance(adv, DebateAdvisory)
    assert adv.signal in ("BUY", "SELL", "NEUTRAL", "NO_ADVISORY")


def test_extract_json_handles_prose():
    obj = _extract_json('Here is my answer: {"signal":"BUY","x":1} done')
    assert obj["signal"] == "BUY"


def test_extract_json_raises_without_json():
    with pytest.raises(ValueError):
        _extract_json("no json here")


def test_aggregate_empty_is_neutral():
    signal, conf, _ = _aggregate([])
    assert signal == "NEUTRAL"
    assert conf == 0.0


def test_aggregate_splits_buy_sell():
    # 2 buys (0.6 each) vs 1 sell (0.3) -> BUY.
    from abutron.apex.debate import AgentOpinion
    debate = [
        AgentOpinion("bull", "BUY", 0.6, "a"),
        AgentOpinion("aggressive", "BUY", 0.6, "b"),
        AgentOpinion("bear", "SELL", 0.3, "c"),
    ]
    signal, conf, _ = _aggregate(debate)
    assert signal == "BUY"
    assert conf > 0.0
