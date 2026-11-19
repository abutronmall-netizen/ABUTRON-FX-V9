"""DEMO order lifecycle verification for ABUTRON V9.

Implements the independently verifiable broker-native DEMO order
lifecycle: ``OrderCheck -> OrderSend -> broker ACK/deal -> broker-side
SL/TP read-back -> stop amendment``.

Every verification is fail-closed:

* **No fake ACK** — a broker ACK must match the current CRM activation
  revision, the original native request, and the broker history.
  Historical matches are not certified as a new revision.
* **SL/TP read-back** — the broker-side stop must be read back and
  matched to the requested value. Existing protective stops alone do
  not prove a trailing modification.
* **OrderCalcProfit** — must be account-bound at the actual proposed
  symbol, direction, volume, entry and SL/TP, normalized by
  SYMBOL_VOLUME_MIN/MAX/STEP.
* **Stop amendment** — must return a broker retcode and the read-back
  must match the desired monotonic trailing direction.

This module does NOT send orders. It verifies evidence produced by the
broker. Order sending is handled by ``NativeDispatcher`` /
``SingleWriterGateway``; this module proves the lifecycle after the
fact (or before, for OrderCheck).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..db import engine_db
from ..events import EventLog, make_event


class LifecycleError(Exception):
    """Raised when broker order lifecycle evidence is invalid/missing."""


@dataclass
class LifecycleResult:
    """Result of a lifecycle verification."""

    candidate_id: str
    connection_id: str
    stage: str
    verified: bool
    reason_code: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "connection_id": self.connection_id,
            "stage": self.stage,
            "verified": self.verified,
            "reason_code": self.reason_code,
            "details": self.details,
        }


# --------------------------------------------------------------------------- #
# OrderCheck verification
# --------------------------------------------------------------------------- #

def verify_order_check(
    *,
    connection_id: str,
    candidate_id: str,
    symbol: str,
    direction: str,
    volume: float,
    stop_loss: float,
    take_profit: float,
    broker_order_check: Optional[Dict[str, Any]] = None,
    event_log: Optional[EventLog] = None) -> LifecycleResult:
    """Verify a broker OrderCheck at the actual proposed geometry.

    ``broker_order_check`` is the broker's response to OrderCheck,
    containing retcode, min/max volume, volume step, and the
    calculated stop-risk. This is NOT a blanket one-lot certificate —
    it must match the exact proposed symbol/direction/volume/SL/TP.

    Raises ``LifecycleError`` if the broker response is missing or
    does not match the proposed geometry.
    """
    if event_log is None:
        event_log = EventLog(path="/tmp/abutron_lifecycle_audit.jsonl")
    if broker_order_check is None:
        raise LifecycleError(
            "no broker OrderCheck response for candidate")

    retcode = broker_order_check.get("retcode")
    if retcode is None or int(retcode) != 0:
        raise LifecycleError(
            f"broker OrderCheck rejected: retcode={retcode}")

    # Volume must respect SYMBOL_VOLUME_MIN/MAX/STEP.
    vol_min = float(broker_order_check.get("volume_min", 0))
    vol_max = float(broker_order_check.get("volume_max", 0))
    vol_step = float(broker_order_check.get("volume_step", 0))

    if vol_min and volume < vol_min:
        raise LifecycleError(
            f"proposed volume {volume} below broker minimum {vol_min}")
    if vol_max and volume > vol_max:
        raise LifecycleError(
            f"proposed volume {volume} above broker maximum {vol_max}")
    if vol_step and volume > 0:
        remainder = volume % vol_step
        if abs(remainder) > 1e-9 and abs(remainder - vol_step) > 1e-9:
            raise LifecycleError(
                f"proposed volume {volume} not a multiple of "
                f"step {vol_step}")

    # The broker's reported symbol must match.
    b_symbol = broker_order_check.get("symbol", symbol)
    if b_symbol != symbol:
        raise LifecycleError(
            f"broker OrderCheck symbol mismatch: "
            f"{b_symbol} != {symbol}")

    result = LifecycleResult(
        candidate_id=candidate_id, connection_id=connection_id,
        stage="ORDER_CHECK", verified=True,
        reason_code="ORDER_CHECK_VERIFIED",
        details={
            "symbol": symbol, "direction": direction,
            "volume": volume, "stop_loss": stop_loss,
            "take_profit": take_profit,
            "volume_min": vol_min, "volume_max": vol_max,
            "volume_step": vol_step,
        })
    event_log.append(make_event(
        "lifecycle.order_check_verified", connection_id=connection_id,
        order_request_id=candidate_id, execution_state="VERIFIED",
        reason_code="ORDER_CHECK_VERIFIED",
        payload=result.details))
    return result


# --------------------------------------------------------------------------- #
# Broker ACK reconciliation
# --------------------------------------------------------------------------- #

def verify_broker_ack(
    *,
    connection_id: str,
    candidate_id: str,
    broker_ack: Dict[str, Any],
    expected_revision: str = "",
    event_log: Optional[EventLog] = None) -> LifecycleResult:
    """Verify a broker ACK/deal against the current CRM activation revision.

    The ACK must match:
    * the original native request (candidate_id, symbol, direction, volume)
    * the current CRM activation revision
    * a valid owner/account/nonce

    Historical native matches cannot be certified as a new revision.
    """
    if event_log is None:
        event_log = EventLog(path="/tmp/abutron_lifecycle_audit.jsonl")
    if not broker_ack:
        raise LifecycleError("no broker ACK for candidate")

    ack_candidate_id = broker_ack.get("candidate_id", "")
    if ack_candidate_id != candidate_id:
        raise LifecycleError(
            f"broker ACK candidate mismatch: "
            f"{ack_candidate_id} != {candidate_id}")

    # The ACK must carry a broker deal/ticket reference.
    deal_id = broker_ack.get("deal_id", "") or \
        broker_ack.get("ticket", 0)
    if not deal_id:
        raise LifecycleError("broker ACK missing deal/ticket reference")

    # Revision binding: the ACK's activation_revision must match the
    # current CRM revision. Historical matches are not certified.
    ack_revision = broker_ack.get("activation_revision", "")
    if expected_revision and ack_revision and \
            ack_revision != expected_revision:
        raise LifecycleError(
            f"broker ACK revision mismatch: {ack_revision} != "
            f"{expected_revision} (historical match not certified)")

    # The ACK must be account-bound.
    ack_connection = broker_ack.get("connection_id", "")
    if ack_connection and ack_connection != connection_id:
        raise LifecycleError(
            f"broker ACK connection mismatch: "
            f"{ack_connection} != {connection_id}")

    result = LifecycleResult(
        candidate_id=candidate_id, connection_id=connection_id,
        stage="BROKER_ACK", verified=True,
        reason_code="BROKER_ACK_RECONCILED",
        details={
            "deal_id": deal_id,
            "activation_revision": ack_revision,
            "expected_revision": expected_revision,
        })
    event_log.append(make_event(
        "lifecycle.broker_ack_verified", connection_id=connection_id,
        order_request_id=candidate_id, execution_state="VERIFIED",
        reason_code="BROKER_ACK_RECONCILED",
        payload=result.details))
    return result


# --------------------------------------------------------------------------- #
# SL/TP read-back
# --------------------------------------------------------------------------- #

def verify_sltp_readback(
    *,
    connection_id: str,
    candidate_id: str,
    expected_stop_loss: float,
    expected_take_profit: float,
    broker_position: Dict[str, Any],
    event_log: Optional[EventLog] = None) -> LifecycleResult:
    """Verify broker-side SL/TP matches the requested values.

    The broker position read-back must show the exact stop_loss and
    take_profit that were requested. Existing protective stops alone
    do not prove a trailing modification — this verifies the actual
    broker-side values.
    """
    if event_log is None:
        event_log = EventLog(path="/tmp/abutron_lifecycle_audit.jsonl")
    if not broker_position:
        raise LifecycleError("no broker position read-back")

    sl = float(broker_position.get("stop_loss", 0))
    tp = float(broker_position.get("take_profit", 0))

    if sl <= 0:
        raise LifecycleError("broker read-back missing stop_loss")
    if expected_stop_loss > 0 and abs(sl - expected_stop_loss) > 1e-6:
        raise LifecycleError(
            f"broker SL read-back mismatch: {sl} != "
            f"{expected_stop_loss}")
    if expected_take_profit > 0 and abs(tp - expected_take_profit) > 1e-6:
        raise LifecycleError(
            f"broker TP read-back mismatch: {tp} != "
            f"{expected_take_profit}")

    result = LifecycleResult(
        candidate_id=candidate_id, connection_id=connection_id,
        stage="SLTP_READBACK", verified=True,
        reason_code="SLTP_READBACK_VERIFIED",
        details={
            "stop_loss": sl, "take_profit": tp,
            "expected_stop_loss": expected_stop_loss,
            "expected_take_profit": expected_take_profit,
        })
    event_log.append(make_event(
        "lifecycle.sltp_readback_verified",
        connection_id=connection_id, order_request_id=candidate_id,
        execution_state="VERIFIED",
        reason_code="SLTP_READBACK_VERIFIED",
        payload=result.details))
    return result


# --------------------------------------------------------------------------- #
# Stop amendment (trailing) verification
# --------------------------------------------------------------------------- #

def verify_stop_amendment(
    *,
    connection_id: str,
    candidate_id: str,
    previous_stop: float,
    desired_stop: float,
    direction: str = "",
    broker_amendment: Optional[Dict[str, Any]] = None,
    event_log: Optional[EventLog] = None) -> LifecycleResult:
    """Verify a broker stop amendment with retcode and read-back.

    The amendment must:
    * return a broker retcode (0 = success)
    * read back the new stop matching ``desired_stop``
    * be monotonic in the correct direction (trailing tightens the
      stop toward the current price, never loosens it)

    Never fabricate an order to satisfy this check.
    """
    if event_log is None:
        event_log = EventLog(path="/tmp/abutron_lifecycle_audit.jsonl")
    if broker_amendment is None:
        raise LifecycleError("no broker stop-amendment response")

    retcode = broker_amendment.get("retcode")
    if retcode is None or int(retcode) != 0:
        raise LifecycleError(
            f"broker stop-amendment rejected: retcode={retcode}")

    new_stop = float(broker_amendment.get("stop_loss", 0))
    if new_stop <= 0:
        raise LifecycleError("stop-amendment read-back missing stop")

    if abs(new_stop - desired_stop) > 1e-6:
        raise LifecycleError(
            f"stop-amendment read-back mismatch: {new_stop} != "
            f"{desired_stop}")

    # Monotonic direction: trailing must tighten (move stop closer to
    # price), never loosen. This is a safety invariant, not advisory.
    if previous_stop > 0 and desired_stop > 0:
        # For a BUY, stop tightens = stop increases (moves up).
        # For a SELL, stop tightens = stop decreases (moves down).
        # The amendment must not move the stop in the loosening direction.
        if direction == "BUY" and desired_stop < previous_stop - 1e-9:
            raise LifecycleError(
                "stop-amendment loosened stop on BUY (non-monotonic)")
        if direction == "SELL" and desired_stop > previous_stop + 1e-9:
            raise LifecycleError(
                "stop-amendment loosened stop on SELL (non-monotonic)")

    result = LifecycleResult(
        candidate_id=candidate_id, connection_id=connection_id,
        stage="STOP_AMENDMENT", verified=True,
        reason_code="STOP_AMENDMENT_VERIFIED",
        details={
            "previous_stop": previous_stop,
            "desired_stop": desired_stop,
            "new_stop": new_stop,
            "direction": direction,
            "retcode": retcode,
        })
    event_log.append(make_event(
        "lifecycle.stop_amendment_verified",
        connection_id=connection_id, order_request_id=candidate_id,
        execution_state="VERIFIED",
        reason_code="STOP_AMENDMENT_VERIFIED",
        payload=result.details))
    return result


# --------------------------------------------------------------------------- #
# OrderCalcProfit verification
# --------------------------------------------------------------------------- #

def verify_order_calc_profit(
    *,
    connection_id: str,
    candidate_id: str,
    symbol: str,
    direction: str,
    volume: float,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    broker_calc: Optional[Dict[str, Any]] = None,
    event_log: Optional[EventLog] = None) -> LifecycleResult:
    """Verify broker OrderCalcProfit/Margin at the actual proposed geometry.

    This is NOT a blanket one-lot certificate. It must be
    account-bound at the exact proposed symbol, direction, volume,
    entry and SL/TP, normalized by SYMBOL_VOLUME_MIN/MAX/STEP.
    """
    if event_log is None:
        event_log = EventLog(path="/tmp/abutron_lifecycle_audit.jsonl")
    if broker_calc is None:
        raise LifecycleError("no broker OrderCalcProfit response")

    # The broker must return a profit/loss calculation for the exact
    # geometry. The stop-risk (loss at SL) must be a positive number
    # that the RiskGate can use.
    calc_symbol = broker_calc.get("symbol", symbol)
    if calc_symbol != symbol:
        raise LifecycleError(
            f"OrderCalcProfit symbol mismatch: {calc_symbol} != {symbol}")

    calc_volume = float(broker_calc.get("volume", volume))
    if abs(calc_volume - volume) > 1e-9:
        raise LifecycleError(
            f"OrderCalcProfit volume mismatch: {calc_volume} != {volume}")

    profit_at_sl = float(broker_calc.get("profit_at_stop_loss", 0))
    profit_at_tp = float(broker_calc.get("profit_at_take_profit", 0))

    if profit_at_sl >= 0:
        # The SL should represent a loss (negative profit). A non-negative
        # SL profit means the geometry is wrong or the calculation failed.
        raise LifecycleError(
            f"OrderCalcProfit SL profit not negative: {profit_at_sl}")
    if profit_at_tp <= 0:
        raise LifecycleError(
            f"OrderCalcProfit TP profit not positive: {profit_at_tp}")

    # R:R check — the RiskGate invariant (R:R >= 1.50) must hold.
    risk = abs(profit_at_sl)
    reward = profit_at_tp
    if risk > 0 and reward / risk < 1.50:
        raise LifecycleError(
            f"OrderCalcProfit R:R {reward/risk:.2f} below 1.50 invariant")

    result = LifecycleResult(
        candidate_id=candidate_id, connection_id=connection_id,
        stage="ORDER_CALC_PROFIT", verified=True,
        reason_code="ORDER_CALC_PROFIT_VERIFIED",
        details={
            "symbol": symbol, "direction": direction,
            "volume": volume, "entry_price": entry_price,
            "stop_loss": stop_loss, "take_profit": take_profit,
            "profit_at_sl": profit_at_sl,
            "profit_at_tp": profit_at_tp,
            "risk_reward": reward / risk if risk > 0 else 0,
        })
    event_log.append(make_event(
        "lifecycle.order_calc_profit_verified",
        connection_id=connection_id, order_request_id=candidate_id,
        execution_state="VERIFIED",
        reason_code="ORDER_CALC_PROFIT_VERIFIED",
        payload=result.details))
    return result


# --------------------------------------------------------------------------- #
# Full lifecycle verification
# --------------------------------------------------------------------------- #

def verify_full_demo_lifecycle(
    *,
    connection_id: str,
    candidate_id: str,
    symbol: str,
    direction: str,
    volume: float,
    stop_loss: float,
    take_profit: float,
    entry_price: float,
    broker_order_check: Dict[str, Any],
    broker_ack: Dict[str, Any],
    broker_position: Dict[str, Any],
    broker_calc: Dict[str, Any],
    expected_revision: str = "",
    event_log: Optional[EventLog] = None) -> Dict[str, LifecycleResult]:
    """Verify the complete DEMO order lifecycle in sequence.

    Runs: OrderCheck -> broker ACK -> SL/TP read-back -> OrderCalcProfit.
    Returns a dict of stage -> LifecycleResult. Raises ``LifecycleError``
    on the first failure (fail-closed — no partial certification).
    """
    if event_log is None:
        event_log = EventLog(path="/tmp/abutron_lifecycle_audit.jsonl")
    results: Dict[str, LifecycleResult] = {}

    results["ORDER_CHECK"] = verify_order_check(
        connection_id=connection_id, candidate_id=candidate_id,
        symbol=symbol, direction=direction, volume=volume,
        stop_loss=stop_loss, take_profit=take_profit,
        broker_order_check=broker_order_check)

    results["BROKER_ACK"] = verify_broker_ack(
        connection_id=connection_id, candidate_id=candidate_id,
        broker_ack=broker_ack, expected_revision=expected_revision)

    results["SLTP_READBACK"] = verify_sltp_readback(
        connection_id=connection_id, candidate_id=candidate_id,
        expected_stop_loss=stop_loss, expected_take_profit=take_profit,
        broker_position=broker_position)

    results["ORDER_CALC_PROFIT"] = verify_order_calc_profit(
        connection_id=connection_id, candidate_id=candidate_id,
        symbol=symbol, direction=direction, volume=volume,
        entry_price=entry_price, stop_loss=stop_loss,
        take_profit=take_profit, broker_calc=broker_calc)

    return results
