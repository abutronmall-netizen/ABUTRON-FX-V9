"""APEX agent base contract.

Each agent receives the closed-candle context + order-flow / footprint
context and returns a structured opinion. Agents never place orders and
never access broker credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentOpinion:
    agent: str
    symbol: str
    direction: str          # 'BUY' | 'SELL' | 'HOLD'
    conviction: float       # 0..1
    regime: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "agent": self.agent, "symbol": self.symbol,
            "direction": self.direction, "conviction": self.conviction,
            "regime": self.regime, "evidence": self.evidence,
            "reasons": self.reasons,
        }


class BaseAgent:
    name = "base"

    def vote(self, context: Dict[str, Any]) -> AgentOpinion:
        raise NotImplementedError
