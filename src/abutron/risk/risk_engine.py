"""Portfolio risk engine.

Computes per-trade USD stop-risk, aggregate owner exposure against the
20% ceiling, and manages trailing stops for open positions. Broker-native
SL/TP is authority; this engine computes risk from actual fill geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
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
                      point_value: Optional[float] = None,
                      point: Optional[float] = None,
                      sl_price: Optional[float] = None) -> PositionRisk:
        """USD stop-risk of one position from actual geometry.

        point_value is USD per *point* (tick) per lot. The price distance
        must be converted to points (÷ point) before multiplying, so the
        USD exposure is correct when point_value is quoted per tick.
        """
        stop = sl_price if sl_price is not None else stop_loss
        # Contract geometry is broker-specific. Missing tick geometry MUST
        # never produce a small, plausible-looking USD risk estimate.
        vals = (volume, entry_price, stop, point, point_value)
        if (not all(type(v) in (int, float) and math.isfinite(v) and v > 0
                    for v in vals)
                or direction not in ("BUY", "SELL")
                or (direction == "BUY" and stop >= entry_price)
                or (direction == "SELL" and stop <= entry_price)):
            raise ValueError("BROKER_NATIVE_STOP_RISK_GEOMETRY_UNVERIFIED")
        stop_dist = abs(entry_price - stop)
        stop_dist_points = stop_dist / point
        stop_risk_usd = volume * stop_dist_points * point_value
        return PositionRisk(
            connection_id=connection_id, candidate_id="", symbol=symbol,
            direction=direction, volume=volume, entry_price=entry_price,
            stop_loss=stop, take_profit=0.0,
            stop_risk_usd=stop_risk_usd, atr=atr, point_value=point_value,
            stop_dist_points=stop_dist_points)

    def aggregate_owner_risk(self, owner_id: str) -> Dict[str, Any]:
        """Aggregate only verified broker-native USD stop-risk across owner accounts.

        A static SYMBOL_REGISTRY price/tick profile is never proof of an
        account's real MT5 OrderCalcProfit loss. Flat means that an
        authenticated, current native positions snapshot explicitly says
        there are zero positions; empty database rows alone cannot imply it.
        """
        total_risk = 0.0
        total_equity = 0.0
        unknown: List[str] = []
        unverified_symbols = set()
        accounts = engine_db.list_accounts(owner_id=owner_id)
        complete = bool(accounts)
        for acct in accounts:
            cid = acct.get("connection_id", "")
            if not cid:
                complete = False
                continue
            equity = acct.get("equity")
            if (type(equity) not in (int, float)
                    or not math.isfinite(equity) or equity <= 0
                    or acct.get("account_currency") != "USD"
                    or acct.get("native_positions_verified") is not True):
                complete = False
                unknown.append(cid)
            else:
                total_equity += float(equity)
            positions = engine_db.recent_open_positions(cid)
            if not isinstance(positions, list):
                complete = False
                if cid not in unknown:
                    unknown.append(cid)
                continue
            for row in positions:
                sym = row.get("symbol", "")
                native_loss = row.get("broker_native_stop_risk_usd")
                # These fields must originate from a broker-reconciled
                # native position and actual entry-to-SL OrderCalcProfit.
                verified = row.get("broker_native_ordercalcprofit_verified") is True
                entry = row.get("fill_price")
                stop = row.get("stop_loss")
                volume = row.get("volume")
                direction = row.get("direction")
                valid_geometry = (
                    all(type(v) in (int, float) and math.isfinite(v) and v > 0
                        for v in (entry, stop, volume))
                    and direction in ("BUY", "SELL")
                    and ((direction == "BUY" and stop < entry)
                         or (direction == "SELL" and stop > entry))
                )
                if (not verified or not valid_geometry
                        or type(native_loss) not in (int, float)
                        or not math.isfinite(native_loss) or native_loss < 0):
                    complete = False
                    if cid not in unknown:
                        unknown.append(cid)
                    if isinstance(sym, str) and sym:
                        unverified_symbols.add(sym)
                    continue
                total_risk += float(native_loss)
        cap_fraction = DEFAULT_PORTFOLIO_RISK_CAP_PCT
        if not (type(cap_fraction) in (int, float)
                and math.isfinite(cap_fraction) and 0 < cap_fraction <= 0.20):
            complete = False
        cap = cap_fraction * total_equity if complete else None
        return {
            "owner_id": owner_id,
            "aggregate_stop_risk_usd": total_risk if complete else None,
            "observed_verified_stop_risk_usd": total_risk,
            "aggregate_balance_usd": total_equity if complete else None,
            "cap_pct": cap_fraction,
            "cap_usd": cap,
            "headroom_usd": cap - total_risk if complete else None,
            "cap_exceeded": total_risk > cap if complete else None,
            "owner_risk_certified": complete,
            "risk_usd_is_partial_observed_only": not complete,
            "risk_usd_is_lower_bound_only": not complete,
            "unknown_exposure_connections": unknown,
            "unknown_symbols": sorted(unverified_symbols),
        }

    def trailing_stop(self, *, connection_id: str, candidate_id: str,
                      symbol: str, direction: str, current_price: float,
                      entry_price: float, current_stop: float,
                      atr: float, point: float = 0.01,
                      point_value: float = 1.0,
                      native_ticket: int = 0,
                      broker_sl: Optional[float] = None,
                      broker_reconciliation_verified: bool = False) -> Dict[str, Any]:
        """Tighten the stop in the trade's favor (never widen).

        A trail is marked PROTECTED only when the account-bound broker
        reconciliation was independently verified, AND the native ticket
        and matching broker SL prove the actual amendment. Otherwise it
        remains PENDING and cannot write a synthetic broker ledger event.
        """
        trail_dist = DEFAULT_TRAILING_ATR_MULT * atr
        if direction == "BUY":
            new_stop = current_price - trail_dist
            if new_stop > current_stop:
                confirmed = self._broker_confirms_sl(
                    native_ticket, broker_sl, new_stop, current_stop,
                    direction, broker_reconciliation_verified)
                self._apply_trail(connection_id, candidate_id, symbol,
                                  direction, new_stop, current_stop,
                                  native_ticket, confirmed)
                return {"tightened": confirmed, "proposed": True,
                        "new_stop": new_stop, "old_stop": current_stop,
                        "confirmed": confirmed}
        else:
            new_stop = current_price + trail_dist
            if new_stop < current_stop:
                confirmed = self._broker_confirms_sl(
                    native_ticket, broker_sl, new_stop, current_stop,
                    direction, broker_reconciliation_verified)
                self._apply_trail(connection_id, candidate_id, symbol,
                                  direction, new_stop, current_stop,
                                  native_ticket, confirmed)
                return {"tightened": confirmed, "proposed": True,
                        "new_stop": new_stop, "old_stop": current_stop,
                        "confirmed": confirmed}
        return {"tightened": False, "proposed": False,
                "new_stop": current_stop, "old_stop": current_stop,
                "confirmed": False}

    def _broker_confirms_sl(self, native_ticket: int, broker_sl: Optional[float],
                           new_stop: float, current_stop: float,
                           direction: str,
                           broker_reconciliation_verified: bool = False) -> bool:
        """Check that the broker actually accepted the modified stop.

        Requires BOTH a positive native ticket AND the broker's reported
        SL to match the intended new_stop. A ticket alone is not proof —
        the broker may have rejected the SL modification or applied a
        different value. The broker_sl must be reconciled against actual
        MT5 position data.
        """
        if (broker_reconciliation_verified is not True
                or type(native_ticket) is not int or native_ticket <= 0
                or direction not in ("BUY", "SELL")
                or type(broker_sl) not in (int, float)
                or not math.isfinite(broker_sl)
                or broker_sl <= 0):
            return False
        # Broker-side reconciliation must already bind ticket, symbol,
        # account and actual SL/TP. A bare value match is never enough.
        tol = 1e-5
        return abs(broker_sl - new_stop) <= tol

    def _apply_trail(self, connection_id, candidate_id, symbol, direction,
                     new_stop, old_stop, native_ticket: int = 0,
                     confirmed: bool = False) -> None:
        """Record a trailing-stop event.

        Only marks PROTECTED when BOTH a native ticket and the broker's
        confirmed SL price are provided. Without full broker SL
        reconciliation the event is PENDING.
        """
        # An intended stop change is NOT a broker stop modification. Never
        # inject a synthetic TRAIL into the native ledger before MT5 confirms
        # the exact SL on the account-bound open position.
        if confirmed:
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


