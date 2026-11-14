"""Gemma LLM advisory layer (optional, advisory-only).

Gemma provides narrative context and a confidence overlay for the APEX
decision journal. It is explicitly NOT on the order path: the blueprint's
signal-only bridge keeps LLM paths out of production decision authority.
A Gemma outage, refusal, or low-confidence answer degrades to NO_ADVISORY
and changes nothing about execution.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from ..config import (GEMMA_ENABLED, GEMMA_BASE_URL, GEMMA_MODEL,
                      GEMMA_TIMEOUT_S, GEMMA_MAX_RETRIES)


@dataclass
class GemmaAdvisory:
    available: bool
    signal: str               # 'BUY' | 'SELL' | 'NEUTRAL' | 'NO_ADVISORY'
    confidence: float         # 0..1
    narrative: str
    model: str
    latency_ms: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"available": self.available, "signal": self.signal,
                "confidence": self.confidence, "narrative": self.narrative,
                "model": self.model, "latency_ms": self.latency_ms,
                "reason": self.reason}


SYSTEM_PROMPT = (
    "You are the Gemma advisory layer of APEX, a FX/CFD trading decision "
    "engine. You receive closed-candle market context, order-flow and "
    "footprint features, and the deterministic agents' opinions. You do NOT "
    "place orders. Respond with a JSON object: "
    '{"signal":"BUY|SELL|NEUTRAL","confidence":0.0-1.0,"narrative":"2-4 sentence rationale"}. '
    "Never invent broker state. If evidence is contradictory, say NEUTRAL "
    "with low confidence."
)


class GemmaAdvisor:
    def __init__(self, base_url: str = GEMMA_BASE_URL,
                 model: str = GEMMA_MODEL,
                 enabled: bool = GEMMA_ENABLED,
                 timeout: float = GEMMA_TIMEOUT_S,
                 max_retries: int = GEMMA_MAX_RETRIES):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enabled = enabled
        self.timeout = timeout
        self.max_retries = max_retries

    def is_configured(self) -> bool:
        return self.enabled and bool(self.base_url)

    def advise(self, context: Dict[str, Any]) -> GemmaAdvisory:
        if not self.is_configured():
            return GemmaAdvisory(False, "NO_ADVISORY", 0.0,
                                 "Gemma advisory disabled or not configured.",
                                 self.model, reason="GEMMA_DISABLED")
        user_prompt = self._render_prompt(context)
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user_prompt}],
            "temperature": 0.2,
            "max_tokens": 300,
        }
        for attempt in range(self.max_retries + 1):
            try:
                import time
                t0 = time.time()
                r = requests.post(f"{self.base_url}/chat/completions",
                                  json=payload, timeout=self.timeout,
                                  headers={"Content-Type": "application/json"})
                latency = (time.time() - t0) * 1000.0
                if r.status_code != 200:
                    if attempt < self.max_retries:
                        continue
                    return GemmaAdvisory(False, "NO_ADVISORY", 0.0,
                                         "Gemma gateway error.",
                                         self.model, latency_ms=latency,
                                         reason=f"GEMMA_HTTP_{r.status_code}")
                content = r.json()["choices"][0]["message"]["content"]
                parsed = self._parse(content)
                return GemmaAdvisory(
                    True, parsed["signal"], parsed["confidence"],
                    parsed["narrative"], self.model, latency_ms=latency)
            except (requests.RequestException, KeyError, ValueError,
                    json.JSONDecodeError) as e:
                if attempt < self.max_retries:
                    continue
                return GemmaAdvisory(False, "NO_ADVISORY", 0.0,
                                     "Gemma unavailable.", self.model,
                                     reason=f"GEMMA_ERROR:{type(e).__name__}")
        return GemmaAdvisory(False, "NO_ADVISORY", 0.0,
                             "Gemma unavailable after retries.", self.model,
                             reason="GEMMA_RETRIES_EXHAUSTED")

    def _render_prompt(self, ctx: Dict[str, Any]) -> str:
        opinions = ctx.get("opinions", [])
        regime = ctx.get("regime", "")
        of = ctx.get("orderflow", {})
        fp = ctx.get("footprint", {})
        return json.dumps({
            "symbol": ctx.get("symbol"),
            "regime": regime,
            "agent_opinions": [
                {"agent": o.get("agent"), "direction": o.get("direction"),
                 "conviction": o.get("conviction"),
                 "reasons": o.get("reasons", [])}
                for o in opinions
            ],
            "orderflow": {k: v for k, v in of.items() if k in
                          ("delta", "imbalance", "buy_ratio",
                           "absorption_bias")},
            "footprint": {k: v for k, v in fp.items() if k in
                          ("poc_price", "value_area_low", "value_area_high",
                           "va_imbalance")},
            "timeframe_alignment": ctx.get("alignment", "aligned"),
        }, indent=2)

    @staticmethod
    def _parse(content: str) -> Dict[str, Any]:
        # Extract the first JSON object from the model's response.
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON in Gemma response")
        obj = json.loads(content[start:end + 1])
        signal = str(obj.get("signal", "NEUTRAL")).upper()
        if signal not in ("BUY", "SELL", "NEUTRAL"):
            signal = "NEUTRAL"
        confidence = float(obj.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        narrative = str(obj.get("narrative", ""))
        return {"signal": signal, "confidence": confidence,
                "narrative": narrative}


gemma_advisor = GemmaAdvisor()
