"""APEX decision journal — durable, traceable decision records.

Every decision (HOLD / REJECTED / BUY / SELL) with regime, agent votes,
risk detail and Gemma advisory is journaled so a signal can be traced
back to its closed candle and candidate reason.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ..config import DECISIONS_ROOT, ENGINE_VERSION, utc_now
from ..events import EventLog, make_event


class DecisionJournal:
    def __init__(self, event_log: EventLog):
        self.log = event_log
        os.makedirs(DECISIONS_ROOT, exist_ok=True)

    def _path(self, connection_id: str) -> str:
        safe = connection_id.replace("/", "_")
        return os.path.join(DECISIONS_ROOT, f"decisions-{safe}.jsonl")

    def record(self, *, connection_id: str, broker: str, owner_id: str,
               symbol: str, timeframe: str, bar_open: str,
               regime: str = "", opinions: Optional[List[Dict[str, Any]]] = None,
               decision: str, reason: str, gemma: Optional[Dict] = None,
               candidate: Any = None, risk: Optional[Dict] = None,
               detail: Optional[Dict] = None,
               brain: Optional[Dict] = None) -> str:
        opinions = opinions or []
        record_id = f"dec_{int(time.time() * 1000)}_{abs(hash(symbol + bar_open)) % 100000}"
        entry = {
            "record_id": record_id,
            "observed_at_utc": utc_now(),
            "connection_id": connection_id, "broker": broker,
            "owner_id": owner_id, "symbol": symbol,
            "timeframe": timeframe, "bar_open": bar_open,
            "regime": regime, "opinions": opinions,
            "decision": decision, "reason": reason,
            "gemma": gemma,
            "apex_version": ENGINE_VERSION,
        }
        if brain:
            entry["brain"] = brain
        if candidate is not None:
            entry["candidate"] = {
                "candidate_id": candidate.candidate_id,
                "direction": candidate.direction,
                "conviction": candidate.conviction,
                "volume": candidate.volume,
                "stop_loss": candidate.stop_loss,
                "take_profit": candidate.take_profit,
                "replay_key": candidate.replay_key,
            }
        if risk:
            entry["risk"] = risk
        if detail:
            entry["detail"] = detail

        with open(self._path(connection_id), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

        self.log.append(make_event(
            "apex.decision", connection_id=connection_id, broker=broker,
            owner_id=owner_id, symbol=symbol, timeframe=timeframe,
            native_bar_open=bar_open, regime=regime,
            agent_votes={o.get("agent"): {"direction": o.get("direction"),
                                          "conviction": o.get("conviction")}
                         for o in opinions},
            conviction=(candidate.conviction if candidate else None),
            apex_version=ENGINE_VERSION,
            brain=brain,
            execution_state=decision, reason_code=reason,
            risk_fraction=(candidate.risk_fraction if candidate else None),
            stop_risk_usd=(candidate.stop_risk_usd if candidate else None)))
        return record_id

    def tail(self, connection_id: str, n: int = 50) -> List[Dict]:
        path = self._path(connection_id)
        out: List[Dict] = []
        if not os.path.exists(path):
            return out
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out[-n:]
