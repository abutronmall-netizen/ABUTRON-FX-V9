"""Canonical audit event envelope (blueprint section 08).

Every significant action — data ingestion, decision, reservation, order
request, broker receipt, reconciliation — is recorded as an immutable,
chained event. The event chain never invents broker ACK; a receipt is only
recorded when broker-native evidence exists.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional

from .config import AUDIT_HASH_ALGO, utc_now


def _hash(payload: Dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class AuditEvent:
    """Append-only event envelope. `previous_hash` chains the log."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    observed_at_utc: str = field(default_factory=utc_now)
    event_type: str = ""
    # Authority context
    owner_id: Optional[str] = None
    connection_id: Optional[str] = None
    broker: Optional[str] = None
    environment: Optional[str] = None
    mt5_login: Optional[int] = None
    # Data provenance / anti-replay
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    native_bar_open: Optional[str] = None
    closed_at: Optional[str] = None
    replay_key: Optional[str] = None
    # Decision provenance
    apex_version: Optional[str] = None
    agent_votes: Optional[Dict[str, Any]] = None
    regime: Optional[str] = None
    conviction: Optional[float] = None
    brain: Optional[Dict[str, Any]] = None
    # Risk / execution economics
    risk_fraction: Optional[float] = None
    stop_risk_usd: Optional[float] = None
    aggregated_owner_risk: Optional[float] = None
    ea_sha256: Optional[str] = None
    writer_id: Optional[str] = None
    activation_revision: Optional[str] = None
    order_request_id: Optional[str] = None
    native_ticket: Optional[int] = None
    broker_deal_id: Optional[str] = None
    volume: Optional[float] = None
    fill_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    commission: Optional[float] = None
    swap: Optional[float] = None
    execution_state: Optional[str] = None
    reconciliation_state: Optional[str] = None
    reason_code: Optional[str] = None
    # Optional chain
    previous_hash: Optional[str] = None
    source_artifact: Optional[str] = None
    proof_timestamp: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    event_hash: str = ""

    def compute_hash(self) -> str:
        body = {k: v for k, v in asdict(self).items() if k != "event_hash"}
        return _hash(body)

    def finalize(self) -> "AuditEvent":
        self.event_hash = self.compute_hash()
        return self

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EventLog:
    """Append-only JSONL event log per scope (e.g. per account/day)."""

    def __init__(self, path: str):
        self.path = path
        self._last_hash: Optional[str] = None

    def _tail_hash(self) -> Optional[str]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                last = None
                for line in fh:
                    line = line.strip()
                    if line:
                        last = json.loads(line)
                if last:
                    return last.get("event_hash")
        except FileNotFoundError:
            return None
        return None

    def append(self, event: AuditEvent) -> AuditEvent:
        if event.previous_hash is None:
            event.previous_hash = self._tail_hash() or hashlib.sha256(
                b"genesis").hexdigest()
        event.finalize()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict(), default=str) + "\n")
        self._last_hash = event.event_hash
        return event

    def tail(self, n: int = 50) -> list:
        out = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except FileNotFoundError:
            return []
        return out[-n:]


def make_event(event_type: str, **kw) -> AuditEvent:
    return AuditEvent(event_type=event_type, **kw)
