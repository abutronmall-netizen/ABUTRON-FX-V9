"""Native MT5 exposure publisher (Issue #36).

Binds exposure certification to AUTHENTICATED broker-native evidence
instead of a manually supplied flag.

The MT5 terminal (ApexNativeWriter.mq5) writes an
``ABUTRON_NATIVE_EXPOSURE_V2_*.txt`` snapshot on each export cycle: the
account's open positions, equity, balance and currency, authenticated by
the native export nonce. This module reads that snapshot from the terminal
and feeds it to ``crm.certify_exposure``, which derives
``native_positions_verified`` from the real broker evidence.

Before this path exists, exposure certification could be set with a blind
``native_positions_verified=True`` boolean — the defect tracked in V9
Issue #36. There is now no way to certify exposure without a broker
snapshot carrying a valid nonce.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..config import DATA_ROOT, utc_now
from ..crypto import parse_native_nonce
from ..crm.controls import CrmError, crm
from ..events import EventLog, make_event
from ..feeds.mt5_terminal import Mt5TerminalManager, terminal_manager


def publish_exposure(connection_id: str,
                     log: Optional[EventLog] = None,
                     manager: Optional[Mt5TerminalManager] = None) -> Dict:
    """Read the broker-native exposure snapshot and certify it.

    Called by the MT5 worker after each native export cycle. Reads the
    terminal's ``ABUTRON_NATIVE_EXPOSURE_V2`` snapshot, validates the nonce,
    and calls ``crm.certify_exposure`` with the real open-positions list +
    equity. Certification is therefore derived from broker-native evidence,
    not a manually supplied flag.

    Raises ``CrmError`` if the snapshot is missing, the nonce is invalid, or
    the account is unknown — the caller should log and NOT certify (the
    dispatcher then blocks on BLOCKED_INCOMPLETE_EXPOSURE, fail-closed).
    """
    mgr = manager or terminal_manager
    snapshot = mgr.read_native_exposure(connection_id)
    if snapshot is None:
        raise CrmError(
            f"no broker-native exposure snapshot for {connection_id}: "
            "cannot certify exposure without authenticated broker evidence")

    nonce = snapshot.get("nonce", "")
    if parse_native_nonce(nonce) is None:
        raise CrmError(
            f"invalid exposure snapshot nonce for {connection_id}: "
            "broker evidence is not authenticated")

    equity = snapshot.get("equity")
    # Equity may arrive as a JSON number or a quoted numeric string (MQL5
    # DoubleToString); accept both and parse to float.
    if isinstance(equity, str):
        try:
            equity = float(equity)
        except ValueError:
            raise CrmError(
                f"exposure snapshot for {connection_id} has non-numeric "
                "equity")
    if not isinstance(equity, (int, float)):
        raise CrmError(
            f"exposure snapshot for {connection_id} has no numeric equity")
    positions: List[Dict] = snapshot.get("positions")
    if not isinstance(positions, list):
        raise CrmError(
            f"exposure snapshot for {connection_id} has malformed positions")

    # certify_exposure verifies account-binding (connection_id + login),
    # the nonce, equity, and reconciles every native position into stop-risk.
    certified = crm.certify_exposure(connection_id, snapshot=snapshot)

    if log is not None:
        log.append(make_event(
            "exposure.published_native",
            connection_id=connection_id,
            nonce=nonce,
            equity=float(equity),
            account_currency=snapshot.get("currency", ""),
            position_count=len(positions),
            exposure_snapshot_digest=certified.get(
                "exposure_snapshot_digest", ""),
            execution_state="CERTIFIED",
            reason_code="BROKER_NATIVE_SNAPSHOT",
            ts=utc_now(),
        ))
    return certified
