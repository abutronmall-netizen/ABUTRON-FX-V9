"""Multi-agent LLM debate layer (advisory-only) — TradingAgents pattern.

This upgrades the single Gemma advisor into a structured **bull/bear debate
plus risk-debator consensus**, following the TradingAgents multi-agent
framework. The flow per decision:

    1. Bull researcher and Bear researcher argue opposing cases from the
       closed-candle context (regime, deterministic agent opinions, order
       flow, footprint).
    2. Aggressive / Conservative / Neutral risk debators challenge the
       deterministic agents' direction, weighing the bull and bear cases.
    3. A Research Manager synthesises the debate into one advisory signal
       with a confidence and narrative.

Like the base Gemma layer, this is **advisory only**: it never places,
modifies, or blocks orders. Any agent outage, refusal, malformed answer, or
budget exhaustion degrades the whole debate to NO_ADVISORY so execution is
unaffected. The debate history is journaled for traceability.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from ..config import (DEBATE_ENABLED, DEBATE_MAX_CALLS, DEBATE_ROUNDS,
                      GEMMA_BASE_URL, GEMMA_MAX_RETRIES, GEMMA_MODEL,
                      GEMMA_TIMEOUT_S)

# Debater / researcher roles (TradingAgents taxonomy, FX-adapted).
ROLE_BULL = "bull"
ROLE_BEAR = "bear"
ROLE_AGGRESSIVE = "aggressive"
ROLE_CONSERVATIVE = "conservative"
ROLE_NEUTRAL = "neutral"
ROLE_MANAGER = "manager"


# Debate rounds and total LLM-call budget. Kept modest so the advisory does
# not dominate the decision-loop latency; degrade on budget exhaustion.
DEBATE_ROUNDS = max(1, DEBATE_ROUNDS)
DEBATE_MAX_CALLS = max(2, DEBATE_MAX_CALLS)


@dataclass
class AgentOpinion:
    role: str
    stance: str          # 'BUY' | 'SELL' | 'NEUTRAL'
    conviction: float    # 0..1
    argument: str

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "stance": self.stance,
                "conviction": self.conviction, "argument": self.argument}


@dataclass
class DebateAdvisory:
    available: bool
    signal: str               # 'BUY' | 'SELL' | 'NEUTRAL' | 'NO_ADVISORY'
    confidence: float         # 0..1
    narrative: str
    model: str
    latency_ms: float = 0.0
    reason: str = ""
    rounds: int = 0
    debate: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"available": self.available, "signal": self.signal,
                "confidence": self.confidence, "narrative": self.narrative,
                "model": self.model, "latency_ms": self.latency_ms,
                "reason": self.reason, "rounds": self.rounds,
                "debate": self.debate}


_ROLE_PROMPTS = {
    ROLE_BULL: (
        "You are the BULL researcher in an FX/CFD trading debate. Given the "
        "market context, argue the strongest case for going LONG (BUY). Push "
        "back on bear risks with data. Respond with JSON: "
        '{"stance":"BUY|SELL|NEUTRAL","conviction":0.0-1.0,'
        '"argument":"2-4 sentence case"}.'),
    ROLE_BEAR: (
        "You are the BEAR researcher in an FX/CFD trading debate. Given the "
        "market context, argue the strongest case for going SHORT (SELL) or "
        "against the bull case. Respond with JSON: "
        '{"stance":"BUY|SELL|NEUTRAL","conviction":0.0-1.0,'
        '"argument":"2-4 sentence case"}.'),
    ROLE_AGGRESSIVE: (
        "You are the AGGRESSIVE risk analyst. Champion the upside and "
        "challenge the conservative/neutral caution. Respond with JSON: "
        '{"stance":"BUY|SELL|NEUTRAL","conviction":0.0-1.0,'
        '"argument":"2-4 sentence case"}.'),
    ROLE_CONSERVATIVE: (
        "You are the CONSERVATIVE risk analyst. Highlight downside, "
        "drawdown and risk-of-ruin concerns. Respond with JSON: "
        '{"stance":"BUY|SELL|NEUTRAL","conviction":0.0-1.0,'
        '"argument":"2-4 sentence case"}.'),
    ROLE_NEUTRAL: (
        "You are the NEUTRAL risk analyst. Weigh upside and downside "
        "evenly. Respond with JSON: "
        '{"stance":"BUY|SELL|NEUTRAL","conviction":0.0-1.0,'
        '"argument":"2-4 sentence case"}.'),
    ROLE_MANAGER: (
        "You are the Research Manager synthesising a bull/bear debate and "
        "three risk analysts into ONE advisory for an FX/CFD decision. You "
        "do NOT place orders. Weight the strongest evidence. Respond with "
        'JSON: {"signal":"BUY|SELL|NEUTRAL","confidence":0.0-1.0,'
        '"narrative":"2-4 sentence rationale"}.'),
}


class DebateAdvisor:
    """Multi-agent bull/bear debate + risk-debator consensus (advisory-only)."""

    def __init__(self, base_url: str = GEMMA_BASE_URL,
                 model: str = GEMMA_MODEL,
                 enabled: bool = DEBATE_ENABLED,
                 timeout: float = GEMMA_TIMEOUT_S,
                 max_retries: int = GEMMA_MAX_RETRIES,
                 rounds: int = DEBATE_ROUNDS):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enabled = enabled
        self.timeout = timeout
        self.max_retries = max_retries
        self.rounds = max(1, rounds)

    def is_configured(self) -> bool:
        return self.enabled and bool(self.base_url)

    # ------------------------------------------------------------------ #
    def advise(self, context: Dict[str, Any]) -> DebateAdvisory:
        t0 = time.time()
        if not self.is_configured():
            return _no_advisory(self.model, "GEMMA_DISABLED", t0)

        base_ctx = self._render_context(context)
        debate: List[AgentOpinion] = []
        calls = 0
        degraded_reason = ""

        try:
            for _ in range(self.rounds):
                if calls >= DEBATE_MAX_CALLS:
                    degraded_reason = "DEBATE_BUDGET_EXHAUSTED"
                    break
                # Bull and bear argue from the base context.
                bull = self._run_agent(ROLE_BULL, base_ctx, calls)
                calls += 1
                bear = self._run_agent(ROLE_BEAR, base_ctx, calls)
                calls += 1
                for op in (bull, bear):
                    if op is not None:
                        debate.append(op)

                # Risk debators challenge the deterministic direction.
                risk_ctx = self._render_risk_context(base_ctx, bull, bear)
                for role in (ROLE_AGGRESSIVE, ROLE_CONSERVATIVE, ROLE_NEUTRAL):
                    if calls >= DEBATE_MAX_CALLS:
                        degraded_reason = "DEBATE_BUDGET_EXHAUSTED"
                        break
                    op = self._run_agent(role, risk_ctx, calls)
                    calls += 1
                    if op is not None:
                        debate.append(op)

                if degraded_reason:
                    break

            # Research manager synthesis.
            if calls >= DEBATE_MAX_CALLS and not degraded_reason:
                degraded_reason = "DEBATE_BUDGET_EXHAUSTED"
            synthesis = self._run_manager(base_ctx, debate, calls)
            calls += 1
        except (requests.RequestException, KeyError, ValueError,
                json.JSONDecodeError) as e:
            return _no_advisory(self.model, f"DEBATE_ERROR:{type(e).__name__}", t0)

        latency = (time.time() - t0) * 1000.0

        # If the manager synthesis failed but we have debate content, degrade
        # gracefully to a simple aggregation rather than dropping everything.
        if synthesis is None:
            if not debate:
                return _no_advisory(self.model,
                                    degraded_reason or "DEBATE_UNAVAILABLE",
                                    t0, latency=latency)
            signal, confidence, narrative = _aggregate(debate)
            return DebateAdvisory(True, signal, confidence, narrative,
                                  self.model, latency_ms=latency,
                                  reason="DEBATE_MANAGER_UNAVAILABLE",
                                  rounds=self.rounds,
                                  debate=[o.to_dict() for o in debate])

        return DebateAdvisory(
            True, synthesis["signal"], synthesis["confidence"],
            synthesis["narrative"], self.model, latency_ms=latency,
            reason=degraded_reason, rounds=self.rounds,
            debate=[o.to_dict() for o in debate])

    # ------------------------------------------------------------------ #
    def _run_agent(self, role: str, context_json: str,
                   call_index: int) -> Optional[AgentOpinion]:
        """Run one debater; return None on failure (degrade, don't abort)."""
        if call_index >= DEBATE_MAX_CALLS:
            return None
        system = _ROLE_PROMPTS[role]
        payload = self._payload(system, context_json)
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.post(f"{self.base_url}/chat/completions",
                                  json=payload, timeout=self.timeout,
                                  headers={"Content-Type": "application/json"})
                if r.status_code != 200:
                    if attempt < self.max_retries:
                        continue
                    return None
                content = r.json()["choices"][0]["message"]["content"]
                obj = _extract_json(content)
                stance = str(obj.get("stance", "NEUTRAL")).upper()
                if stance not in ("BUY", "SELL", "NEUTRAL"):
                    stance = "NEUTRAL"
                return AgentOpinion(
                    role, stance,
                    max(0.0, min(1.0, float(obj.get("conviction", 0.0)))),
                    str(obj.get("argument", "")))
            except (requests.RequestException, KeyError, ValueError,
                    json.JSONDecodeError):
                if attempt < self.max_retries:
                    continue
                return None
        return None

    def _run_manager(self, context_json: str,
                     debate: List[AgentOpinion], call_index: int
                     ) -> Optional[Dict[str, Any]]:
        if call_index >= DEBATE_MAX_CALLS:
            return None
        debate_json = json.dumps(
            [o.to_dict() for o in debate], indent=2) if debate else "[]"
        user = f"Market context:\n{context_json}\n\nDebate so far:\n{debate_json}"
        payload = self._payload(_ROLE_PROMPTS[ROLE_MANAGER], user)
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.post(f"{self.base_url}/chat/completions",
                                  json=payload, timeout=self.timeout,
                                  headers={"Content-Type": "application/json"})
                if r.status_code != 200:
                    if attempt < self.max_retries:
                        continue
                    return None
                content = r.json()["choices"][0]["message"]["content"]
                obj = _extract_json(content)
                signal = str(obj.get("signal", "NEUTRAL")).upper()
                if signal not in ("BUY", "SELL", "NEUTRAL"):
                    signal = "NEUTRAL"
                return {"signal": signal,
                        "confidence": max(0.0, min(1.0,
                                float(obj.get("confidence", 0.0)))),
                        "narrative": str(obj.get("narrative", ""))}
            except (requests.RequestException, KeyError, ValueError,
                    json.JSONDecodeError):
                if attempt < self.max_retries:
                    continue
                return None
        return None

    # ------------------------------------------------------------------ #
    def _payload(self, system: str, user: str) -> Dict[str, Any]:
        return {"model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0.2, "max_tokens": 300}

    def _render_context(self, ctx: Dict[str, Any]) -> str:
        opinions = ctx.get("opinions", [])
        of = ctx.get("orderflow", {})
        fp = ctx.get("footprint", {})
        return json.dumps({
            "symbol": ctx.get("symbol"),
            "regime": ctx.get("regime", ""),
            "deterministic_agent_opinions": [
                {"agent": o.get("agent"), "direction": o.get("direction"),
                 "conviction": o.get("conviction"),
                 "reasons": o.get("reasons", [])}
                for o in opinions
            ],
            "orderflow": {k: v for k, v in of.items() if k in
                          ("delta", "imbalance", "buy_ratio",
                           "absorption_bias")},
            "footprint": {k: v for k, v in fp.items() if k in
                          ("poc_price", "value_area_low",
                           "value_area_high", "va_imbalance")},
            "timeframe_alignment": ctx.get("alignment", "aligned"),
        }, indent=2)

    def _render_risk_context(self, base_ctx: str, bull: Optional[AgentOpinion],
                             bear: Optional[AgentOpinion]) -> str:
        parts = [base_ctx, "\nBull case:\n" + (bull.argument if bull else "n/a")]
        parts.append("\nBear case:\n" + (bear.argument if bear else "n/a"))
        return "\n".join(parts)


def _aggregate(debate: List[AgentOpinion]) -> tuple:
    """Fallback consensus when the manager call is unavailable."""
    buys = sum(o.conviction for o in debate if o.stance == "BUY")
    sells = sum(o.conviction for o in debate if o.stance == "SELL")
    total = buys + sells
    if total <= 0:
        return "NEUTRAL", 0.0, "No aligned debate evidence."
    if buys > sells:
        signal = "BUY"
    elif sells > buys:
        signal = "SELL"
    else:
        signal = "NEUTRAL"
    confidence = round(min(1.0, abs(buys - sells) / (total + 1e-9)), 3)
    return signal, confidence, "Consensus aggregated from debater opinions."


def _extract_json(content: str) -> Dict[str, Any]:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in LLM response")
    return json.loads(content[start:end + 1])


def _no_advisory(model: str, reason: str, t0: float,
                 latency: float = 0.0) -> DebateAdvisory:
    if latency == 0.0:
        latency = (time.time() - t0) * 1000.0
    return DebateAdvisory(False, "NO_ADVISORY", 0.0,
                          "Debate advisory unavailable.", model,
                          latency_ms=latency, reason=reason)


debate_advisor = DebateAdvisor()
