"""Portal operations — run/trace logging, analytics, leaderboard, and
model-provider registry (AgenticTrading dashboard pattern, ABUTRON-adapted).

These are operator-facing *observability and configuration* features that
read from the existing SQLite engine DB and the audit EventLog. They never
touch the order path: analytics/leaderboard are derived from reconciled
native facts, and the model-provider registry stores only non-secret config
(the actual secret is injected via environment variables).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from ..db import engine_db
from ..config import utc_now


class PortalOps:
    """Run/trace, analytics, leaderboard + model-provider registry."""

    # ------------------------------------------------------------------ #
    # Run / trace logging
    # ------------------------------------------------------------------ #
    def record_run(self, *, connection_id: str, owner_id: str, symbol: str,
                   signal_id: str, decision: str, status: str,
                   regime: str = "", conviction: float = 0.0,
                   gemma_signal: str = "", gemma_confidence: float = 0.0,
                   riskguard: str = "", blockers: Optional[List[str]] = None,
                   run_id: Optional[str] = None) -> str:
        rid = run_id or f"run_{uuid.uuid4().hex[:16]}"
        engine_db.add_run({
            "run_id": rid, "connection_id": connection_id,
            "owner_id": owner_id, "symbol": symbol, "signal_id": signal_id,
            "decision": decision, "status": status, "regime": regime,
            "conviction": conviction, "gemma_signal": gemma_signal,
            "gemma_confidence": gemma_confidence, "riskguard": riskguard,
            "blockers": ";".join(blockers or []),
            "created_at": utc_now(),
        })
        return rid

    def list_runs(self, connection_id: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        return engine_db.list_runs(connection_id, limit=limit)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return engine_db.get_run(run_id)

    # ------------------------------------------------------------------ #
    # Analytics
    # ------------------------------------------------------------------ #
    def analytics(self) -> Dict[str, Any]:
        """Aggregate decision/candidate/trade metrics from reconciled facts."""
        candidates = engine_db.list_candidates() if hasattr(
            engine_db, "list_candidates") else []
        receipts = engine_db.list_receipts()
        ledger = engine_db.list_ledger()

        # Candidates by execution state.
        by_state: Dict[str, int] = {}
        for c in candidates:
            by_state[c.get("execution_state", "?")] = \
                by_state.get(c.get("execution_state", "?"), 0) + 1

        # Receipts by execution / reconciled state.
        by_exec: Dict[str, int] = {}
        by_recon: Dict[str, int] = {}
        for r in receipts:
            by_exec[r.get("execution_state", "?")] = \
                by_exec.get(r.get("execution_state", "?"), 0) + 1
            by_recon[r.get("reconciled_state", "?")] = \
                by_recon.get(r.get("reconciled_state", "?"), 0) + 1

        # Realized P&L from the ledger.
        realized = sum(float(l.get("realized_pnl", 0.0)) for l in ledger)
        commission = sum(float(l.get("commission", 0.0)) for l in ledger)
        swap = sum(float(l.get("swap", 0.0)) for l in ledger)

        # Trade stats from CLOSE events.
        closed = [l for l in ledger if l.get("event") == "CLOSE"]
        wins = sum(1 for l in closed if float(l.get("realized_pnl", 0.0)) > 0)
        win_rate = (wins / len(closed) * 100.0) if closed else 0.0

        return {
            "candidates_total": len(candidates),
            "candidates_by_state": by_state,
            "receipts_total": len(receipts),
            "receipts_by_execution": by_exec,
            "receipts_by_reconciled": by_recon,
            "trades_closed": len(closed),
            "wins": wins,
            "win_rate_pct": round(win_rate, 2),
            "realized_pnl_usd": round(realized, 2),
            "commission_usd": round(commission, 2),
            "swap_usd": round(swap, 2),
            "net_pnl_usd": round(realized - commission + swap, 2),
        }

    # ------------------------------------------------------------------ #
    # Leaderboard
    # ------------------------------------------------------------------ #
    def leaderboard(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Rank owners by realized P&L and trade count (reconciled facts)."""
        ledger = engine_db.list_ledger()
        per_owner: Dict[str, Dict[str, Any]] = {}
        for l in ledger:
            owner = l.get("owner_id") or l.get("connection_id") or "unknown"
            entry = per_owner.setdefault(owner, {
                "owner_id": owner, "realized_pnl_usd": 0.0,
                "trades": 0, "wins": 0, "connections": set()})
            entry["realized_pnl_usd"] += float(l.get("realized_pnl", 0.0))
            entry["trades"] += 1
            if l.get("event") == "CLOSE":
                if float(l.get("realized_pnl", 0.0)) > 0:
                    entry["wins"] += 1
            cid = l.get("connection_id")
            if cid:
                entry["connections"].add(cid)

        rows = []
        for e in per_owner.values():
            closed = e["trades"]
            rows.append({
                "owner_id": e["owner_id"],
                "realized_pnl_usd": round(e["realized_pnl_usd"], 2),
                "trades": closed,
                "wins": e["wins"],
                "win_rate_pct": round(e["wins"] / closed * 100.0, 2)
                if closed else 0.0,
                "connections": len(e["connections"]),
            })
        rows.sort(key=lambda r: r["realized_pnl_usd"], reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------ #
    # Model-provider registry (non-secret config only)
    # ------------------------------------------------------------------ #
    def upsert_model_provider(self, prov: Dict[str, Any]) -> None:
        prov.setdefault("provider_id", f"mp_{uuid.uuid4().hex[:12]}")
        prov.setdefault("created_at", utc_now())
        engine_db.upsert_model_provider(prov)

    def list_model_providers(self) -> List[Dict[str, Any]]:
        return engine_db.list_model_providers()

    def delete_model_provider(self, provider_id: str) -> bool:
        return engine_db.delete_model_provider(provider_id)


portal_ops = PortalOps()
