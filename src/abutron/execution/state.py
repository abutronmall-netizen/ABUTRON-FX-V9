"""Execution evidence state machine (blueprint section 02).

RESERVED -> SEND -> UNKNOWN/ACK -> PROTECTED/OPEN -> CLOSED/SETTLED.

Exact ledger enum names differ between Deriv and FP and are normalized
here. An UNKNOWN submit outcome is never silently reclassified as success;
it blocks further sends until independently reconciled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


RESERVED = "RESERVED"
SEND = "SEND"
UNKNOWN = "UNKNOWN"
ACK = "ACK"
REJECTED = "REJECTED"
OPEN = "OPEN"
PROTECTED = "PROTECTED"
CLOSED = "CLOSED"
SETTLED = "SETTLED"
DUPLICATE = "DUPLICATE"
BLOCKED_INCOMPLETE_EXPOSURE = "BLOCKED_INCOMPLETE_EXPOSURE"

VALID_TRANSITIONS = {
    RESERVED: {SEND, REJECTED, DUPLICATE, BLOCKED_INCOMPLETE_EXPOSURE},
    SEND: {UNKNOWN, ACK, REJECTED},
    UNKNOWN: {ACK, REJECTED, OPEN, PROTECTED, CLOSED},
    ACK: {OPEN, PROTECTED, REJECTED},
    OPEN: {PROTECTED, CLOSED},
    PROTECTED: {CLOSED, PROTECTED},
    CLOSED: {SETTLED, CLOSED},
    BLOCKED_INCOMPLETE_EXPOSURE: {RESERVED, REJECTED},
    SETTLED: set(),
    REJECTED: set(),
    DUPLICATE: set(),
}


@dataclass
class ExecutionState:
    candidate_id: str
    state: str
    previous: Optional[str] = None
    reason: str = ""

    def transition(self, new_state: str) -> bool:
        if new_state not in VALID_TRANSITIONS.get(self.state, set()):
            return False
        self.previous = self.state
        self.state = new_state
        return True


class StateMachine:
    def __init__(self):
        self._states: Dict[str, ExecutionState] = {}

    def start(self, candidate_id: str, state: str = RESERVED) -> ExecutionState:
        st = ExecutionState(candidate_id=candidate_id, state=state)
        self._states[candidate_id] = st
        return st

    def get(self, candidate_id: str) -> Optional[ExecutionState]:
        return self._states.get(candidate_id)

    def move(self, candidate_id: str, new_state: str,
             reason: str = "") -> bool:
        st = self._states.get(candidate_id)
        if st is None:
            st = self.start(candidate_id)
        ok = st.transition(new_state)
        if ok and reason:
            st.reason = reason
        return ok

    def snapshot(self) -> Dict[str, Any]:
        return {cid: {"state": s.state, "previous": s.previous,
                      "reason": s.reason}
                for cid, s in self._states.items()}


state_machine = StateMachine()
