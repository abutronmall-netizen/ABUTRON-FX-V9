"""Decision ledger — the evidence memory of the agentic system.

Every decision cycle appends a serialised ``AgentDecision`` to the
ledger, producing a tamper-evident, append-only audit trail. This is the
"evidence-only" discipline carried into the agentic era: the system may
be the primary operating layer, but every action is still journalled and
reconstructable.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .interfaces import AgentDecision


class DecisionLedger:
    """Append-only, thread-safe in-memory decision ledger.

    A lightweight persistence hook (``persist_path``) can be supplied to
    mirror records to disk as JSONL for durable evidence. The ledger never
    mutates prior records — only appends.
    """

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._persist_path = persist_path

    @property
    def count(self) -> int:
        return len(self._records)

    def append(self, decision: AgentDecision) -> Dict[str, Any]:
        """Record a decision. Returns the serialised record."""
        record = decision.to_dict()
        record["recorded_utc"] = time.time()
        with self._lock:
            self._records.append(record)
            if self._persist_path:
                self._persist(record)
        return record

    def recent(self, n: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._records[-n:])

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            verdicts: Dict[str, int] = {}
            for r in self._records:
                v = r.get("verdict", "?")
                verdicts[v] = verdicts.get(v, 0) + 1
            executed = sum(1 for r in self._records if r.get("executed"))
            return {
                "total_decisions": len(self._records),
                "executed": executed,
                "verdict_counts": verdicts,
            }

    def _persist(self, record: Dict[str, Any]) -> None:
        try:
            import json
            with open(self._persist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except OSError:
            # Evidence persistence must never break the decision cycle.
            pass
