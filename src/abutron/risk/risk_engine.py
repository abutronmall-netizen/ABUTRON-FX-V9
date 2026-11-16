"""Portfolio risk engine.

Computes per-trade USD stop-risk, aggregate owner exposure against the
20% ceiling, and manages trailing stops for open positions. Broker-native
SL/TP is authority; this engine computes risk from actual fill geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config import (DEFAULT_PORTFOLIO_RISK_CAP_PCT,
                      DEFAULT_TRAILING_ATR_MULT, utc_now)
from ..db import engine_db
from ..events import EventLog, make_event


@dataclass
class PositionRisk:
    connection_id: str
    candidate_id: str
    symbol: str
    direction: str
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    stop_risk_usd: float
    atr: float
    point_value: float
    stop_dist_points: float

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class RiskEngine:
    def __init__(self, event_log: EventLog):
        self.log = event_log

    def position_risk(self, *, connection_id: str, symbol: str,
                      direction: str, volume: float, entry_price: float,
                      stop_loss: float, atr: float = 0.0,
                      point_value: float = 1.0, point: float = 0.01,
                      sl_price: Optional[float] = None) -> PositionRisk:
        """USD stop-risk of one position from actual geometry.

        point_value is USD per *point* (tick) per lot. The price distance
        must be converted to points (÷ point) before multiplying, so the
        USD exposure is correct when point_value is quoted per tick.
        """
        stop = sl_price if sl_price is not None else stop_loss
        stop_dist = abs(entry_price - stop)
        stop_dist_points = stop_dist / point if point else 0.0
        # Use point distance (not raw price distance) so USD risk is correct
        # when point_value is per-tick. Falls back to raw distance only when
        # point is 0 (no tick info available).
        if point and point > 0:
            stop_risk_usd = volume * stop_dist_points * point_value
        else:
            stop_risk_usd = volume * stop_dist * point_value
        return PositionRisk(
            connection_id=connection_id, candidate_id="", symbol=symbol,
            direction=direction, volume=volume, entry_price=entry_price,
            stop_loss=stop, take_profit=0.0,
            stop_risk_usd=stop_risk_usd, atr=atr, point_value=point_value,
            stop_dist_points=stop_dist_points)

    def aggregate_owner_risk(self, owner_id: str) -> Dict[str, Any]:
        """Sum stop-risk across all linked open positions for an owner.

        Includes ALL accounts for the owner (connected and disconnected),
        since disconnected MT5 accounts may still hold open positions with
        outstanding broker exposure. Uses actual contract point values from
        the SYMBOL_REGISTRY rather than a fixed proxy.
        """
        total_risk = 0.0
        total_balance = 0.0
        unknown: List[str] = []
        # Import the symbol registry lazily to avoid circular imports.
        try:
            from ..apex.eligibility import SYMBOL_REGISTRY
        except Exception:
            SYMBOL_REGISTRY = {}
        for acct in engine_db.list_accounts(owner_id=owner_id):
            cid = acct["connection_id"]
            balance = float(acct.get("equity", 0.0) or 0.0)
            total_balance += balance
            positions = engine_db.recent_open_positions(cid)
            conn_risk = 0.0
            for p in positions:
                # stop risk recomputed from native SL and fill price, using
                # the actual contract point value (not a fixed proxy).
                entry = float(p.get("fill_price", 0.0) or 0.0)
                sl = float(p.get("stop_loss", 0.0) or 0.0)
                vol = float(p.get("volume", 0.0) or 0.0)
                sym = p.get("symbol", "")
                profile = SYMBOL_REGISTRY.get(sym)
                if profile is not None:
                    point = float(profile.contract.point or 0.0)
                    point_value = float(profile.contract.point_value or 1.0)
                else:
                    point = 0.0
                    point_value = 1.0
                if entry and sl:
                    stop_dist = abs(entry - sl)
                    if point and point > 0:
                        stop_dist_pts = stop_dist / point
                        conn_risk += vol * stop_dist_pts * point_value
                    else:
                        conn_risk += vol * stop_dist * point_value
            if not positions:
                unknown.append(cid)
            total_risk += conn_risk
        cap = DEFAULT_PORTFOLIO_RISK_CAP_PCT * total_balance
        return {
            "owner_id": owner_id,
            "aggregate_stop_risk_usd": total_risk,
            "aggregate_balance_usd": total_balance,
            "cap_pct": DEFAULT_PORTFOLIO_RISK_CAP_PCT,
            "cap_usd": cap,
            "headroom_usd": cap - total_risk,
            "cap_exceeded": total_risk > cap,
            "unknown_exposure_connections": unknown,
        }

    def trailing_stop(self, *, connection_id: str, candidate_id: str,
                      symbol: str, direction: str, current_price: float,
                      entry_price: float, current_stop: float,
                      atr: float, point: float = 0.01,
                      point_value: float = 1.0,
                      native_ticket: int = 0) -> Dict[str, Any]:
        """Tighten the stop in the trade's favor (never widen).

        A trail is only recorded as PROTECTED when a valid native_ticket
        (MT5-confirmed stop modification) is provided. Without broker
        confirmation the trail is recorded as PENDING — never falsely
        marked protected.
        """
        trail_dist = DEFAULT_TRAILING_ATR_MULT * atr
        if direction == "BUY":
            new_stop = current_price - trail_dist
            if new_stop > current_stop:
                self._apply_trail(connection_id, candidate_id, symbol,
                                  direction, new_stop, current_stop,
                                  native_ticket)
                return {"tightened": True, "new_stop": new_stop,
                        "old_stop": current_stop,
                        "confirmed": native_ticket > 0}
        else:
            new_stop = current_price + trail_dist
            if new_stop < current_stop:
                self._apply_trail(connection_id, candidate_id, symbol,
                                  direction, new_stop, current_stop,
                                  native_ticket)
                return {"tightened": True, "new_stop": new_stop,
                        "old_stop": current_stop,
                        "confirmed": native_ticket > 0}
        return {"tightened": False, "new_stop": current_stop,
                "old_stop": current_stop, "confirmed": False}

    def _apply_trail(self, connection_id, candidate_id, symbol, direction,
                     new_stop, old_stop, native_ticket: int = 0) -> None:
        """Record a trailing-stop event.

        Only marks PROTECTED when MT5 has confirmed the stop modification
        (native_ticket > 0). Without broker confirmation the event is
        PENDING — a trail is not complete until reconciliation observes
        the updated broker stop.
        """
        confirmed = native_ticket > 0
        engine_db.add_ledger({
            "connection_id": connection_id,
            "owner_id": "", "native_ticket": native_ticket,
            "event": "TRAIL", "symbol": symbol, "direction": direction,
            "volume": 0.0, "price": new_stop, "realized_pnl": 0.0,
            "commission": 0.0, "swap": 0.0,
            "stop_loss": new_stop, "take_profit": 0.0,
            "balance": 0.0, "observed_at": utc_now(),
            "source": "native"})
        self.log.append(make_event(
            "risk.trailing_stop", connection_id=connection_id,
            symbol=symbol,
            execution_state="PROTECTED" if confirmed else "PENDING",
            payload={"new_stop": new_stop, "old_stop": old_stop,
                     "native_ticket": native_ticket,
                     "confirmed": confirmed}))
