"""Core data contracts for the agentic multi-agent system.

These dataclasses are the lingua franca between agents, the reasoning
layer, the risk gate, and the execution layer. They are deliberately
serialisable so every decision can be journalled as evidence.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class Direction(str, Enum):
    """Market direction an agent or signal can express."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    @classmethod
    def from_str(cls, value: str) -> "Direction":
        v = (value or "").upper()
        if v in ("BUY", "LONG"):
            return cls.BUY
        if v in ("SELL", "SHORT"):
            return cls.SELL
        return cls.HOLD


class Verdict(str, Enum):
    """Outcome of the risk gate for an action."""
    APPROVE = "APPROVE"          # passes all invariants
    REJECT = "REJECT"            # blocked by a hard invariant
    SHADOW = "SHADOW"            # allowed but recorded only (live off)
    ESCALATE = "ESCALATE"        # requires human/operator confirmation


class AgentRole(str, Enum):
    """Specialised responsibilities in the multi-agent system."""
    SIGNAL = "signal"
    RISK = "risk"
    EXECUTION = "execution"
    MONITORING = "monitoring"
    RECOVERY = "recovery"
    ORCHESTRATOR = "orchestrator"


@dataclass
class AgentCapability:
    """A capability an agent can provide or a tool it can call."""
    name: str
    description: str = ""


@dataclass
class Signal:
    """The output of the brain signal substrate for one symbol/bar.

    Mirrors the V9 ``BrainProposal`` essentials: direction, conviction,
    confidence and uncertainty, plus provenance so the decision can be
    audited back to the exact brain / feature vector used.
    """
    symbol: str
    direction: Direction
    conviction: float          # 0..1, strength of the edge
    confidence: float          # 0..1, model confidence
    uncertainty: float         # 0..1, ensemble/MC disagreement
    position_size: float       # conviction-scaled size, capped
    regime: str = ""
    features: List[float] = field(default_factory=list)
    brain_source: str = ""     # e.g. "nexus_unified" | "rule_fallback"
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentContext:
    """The shared context handed to every agent in a decision cycle.

    Carries the closed-candle market state, the brain signal, optional
    order-flow / footprint context, and any prior decisions in the cycle.
    """
    symbol: str
    timeframe: str = "M5"
    bar_open: float = 0.0
    bar_close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    spread: float = 0.0
    now_utc: float = field(default_factory=lambda: time.time())
    signal: Optional[Signal] = None
    orderflow: Dict[str, Any] = field(default_factory=dict)
    footprint: Dict[str, Any] = field(default_factory=dict)
    portfolio: Dict[str, Any] = field(default_factory=dict)
    prior_decisions: List["AgentDecision"] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # AgentDecision is not JSON-native; keep it out of the flat dict.
        d.pop("prior_decisions", None)
        return d


@dataclass
class AgentAction:
    """A concrete action an agent proposes to take.

    The only action types that reach the RiskGate are those that can
    affect money or broker state. Read-only / informational actions skip
    the gate by design.
    """
    action_id: str
    kind: str                  # "submit_order" | "close_position" | "adjust_stop" | "record"
    symbol: str
    direction: Direction
    quantity: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    price: float = 0.0
    rationale: str = ""
    requires_gate: bool = True
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, kind: str, symbol: str, direction: Direction,
            **kwargs: Any) -> "AgentAction":
        return cls(
            action_id=uuid.uuid4().hex,
            kind=kind, symbol=symbol, direction=direction, **kwargs)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentDecision:
    """The full, evidence-backed record of one decision cycle."""
    decision_id: str
    symbol: str
    cycle_started_utc: float
    cycle_finished_utc: float = 0.0
    signal: Optional[Signal] = None
    reasoning: Dict[str, Any] = field(default_factory=dict)
    action: Optional[AgentAction] = None
    verdict: Verdict = Verdict.SHADOW
    gate_reason: str = ""
    executed: bool = False
    execution_receipt: Optional[Dict[str, Any]] = None
    agent_trace: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @classmethod
    def new(cls, symbol: str, cycle_started_utc: float) -> "AgentDecision":
        return cls(decision_id=uuid.uuid4().hex, symbol=symbol,
                   cycle_started_utc=cycle_started_utc)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentResult:
    """The outcome an agent returns from ``act``."""
    agent: str
    role: AgentRole
    ok: bool
    summary: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
