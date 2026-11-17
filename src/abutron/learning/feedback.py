"""Closed-trade feedback learning — broker-confirmed outcomes into the brain.

Phase 3 of the V9 verification checklist: take every closed trade whose
outcome was confirmed by the broker (native receipt → CLOSED/SETTLED),
compute the realised return, and feed it back into the brain's online
learning pipeline. The brain therefore learns from its own realised
wins and losses — not just from training labels.

This closes the loop: broker execution → receipt → close confirmation →
realised return → online_learn → updated brain weights.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import utc_now
from ..events import EventLog, make_event
from ..brain.unified import load_unified, unified_brain_dir


@dataclass
class FeedbackResult:
    """Result of feeding one closed trade back into the brain."""
    candidate_id: str
    symbol: str
    realised_return: float
    updated: bool
    reason: str = ""
    pnl_usd: float = 0.0
    closes: List[float] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "realised_return": self.realised_return,
            "updated": self.updated,
            "reason": self.reason,
            "pnl_usd": self.pnl_usd,
        }


def realised_return_from_pnl(*, direction: str, entry_price: float,
                              exit_price: float) -> float:
    """Compute the fractional return of a closed trade.

    For a long: (exit - entry) / entry. For a short: (entry - exit) / entry.
    """
    if entry_price <= 0:
        return 0.0
    diff = exit_price - entry_price
    if direction.lower() in ("short", "sell", "sell_limit"):
        diff = -diff
    return diff / entry_price


def learn_from_closed_trades(
    *, brain_dir: Optional[str] = None,
    closed_trades: List[Dict[str, Any]],
    event_log: Optional[EventLog] = None,
) -> List[FeedbackResult]:
    """Feed broker-confirmed closed trades back into the brain.

    Each closed trade dict must contain:
        candidate_id, symbol, direction, entry_price, exit_price,
        pnl_usd (optional)

    The realised return is computed from entry/exit geometry and fed
    to the brain's online_learn. The brain is saved after all trades.

    Args:
        brain_dir: Brain directory. Defaults to the unified_m5 brain.
        closed_trades: List of broker-confirmed closed trade records.
        event_log: Optional event log for audit.

    Returns:
        List of FeedbackResult for each trade.
    """
    brain_dir = brain_dir or unified_brain_dir()
    brain = load_unified(brain_dir)
    results: List[FeedbackResult] = []
    updated_count = 0

    for trade in closed_trades:
        cid = trade.get("candidate_id", "")
        symbol = trade.get("symbol", "")
        direction = trade.get("direction", "long")
        entry = float(trade.get("entry_price",
                                trade.get("fill_price", 0.0)) or 0.0)
        exit_price = float(trade.get("exit_price",
                                     trade.get("close_price", 0.0)) or 0.0)
        pnl = float(trade.get("pnl_usd", 0.0) or 0.0)

        realised = realised_return_from_pnl(
            direction=direction, entry_price=entry, exit_price=exit_price)

        # Build a minimal close series for feature extraction
        closes = [entry, exit_price]

        result = brain.online_learn(
            symbol=symbol, closes=closes, realised_return=realised,
            regime=trade.get("regime", ""))

        fr = FeedbackResult(
            candidate_id=cid, symbol=symbol,
            realised_return=realised,
            updated=result.get("updated", False),
            reason=result.get("reason", ""),
            pnl_usd=pnl,
            closes=closes,
        )
        results.append(fr)
        if fr.updated:
            updated_count += 1
        if event_log:
            event_log.append(make_event(
                "feedback.closed_trade_learned",
                order_request_id=cid, symbol=symbol,
                direction=direction, realised_return=realised,
                pnl_usd=pnl,
                execution_state="LEARNED" if fr.updated else "SKIPPED",
                reason_code=fr.reason or "OK"))

    # Save the updated brain
    if updated_count > 0:
        from ..brain.unified import save_unified
        save_unified(brain_dir, brain)
        if event_log:
            event_log.append(make_event(
                "feedback.brain_saved",
                execution_state="LEARNED",
                reason_code="CLOSED_TRADE_FEEDBACK",
                payload={"trades": len(closed_trades),
                         "updated": updated_count}))

    return results


def collect_closed_trades_from_receipts(
    connection_id: Optional[str] = None,
    *, limit: int = 100,
) -> List[Dict[str, Any]]:
    """Collect CLOSED/SETTLED receipts for feedback learning.

    Reads receipt records from the DB and filters to those in
    CLOSED or SETTLED state with a valid fill price.

    Args:
        connection_id: Optional filter by connection.
        limit: Max receipts to return.

    Returns:
        List of closed trade dicts ready for learn_from_closed_trades.
    """
    from .. import db
    receipts = db.engine_db.list_receipts(connection_id, limit)
    closed = []
    for r in receipts:
        state = r.get("reconciled_state", "")
        if state not in ("CLOSED", "SETTLED"):
            continue
        if not r.get("fill_price", 0.0):
            continue
        closed.append({
            "candidate_id": r.get("candidate_id", ""),
            "symbol": r.get("symbol", ""),
            "direction": r.get("direction", "long"),
            "entry_price": float(r.get("fill_price", 0.0) or 0.0),
            "exit_price": float(r.get("close_price",
                                      r.get("exit_price", 0.0)) or 0.0),
            "pnl_usd": float(r.get("pnl_usd",
                                   r.get("commission", 0.0)) or 0.0),
            "receipt_id": r.get("receipt_id", ""),
        })
    return closed
