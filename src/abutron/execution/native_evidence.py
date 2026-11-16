"""Broker-native evidence inventory for order authority.

Five evidence categories must ALL be present before a candidate may be
sent to the wire. Any gap yields reason ``BLOCKED_INCOMPLETE_EXPOSURE``
with the exact missing categories — the dispatcher never sends blind,
and the candidate-review gates are filled from this assessment instead
of hardcoded values.

Categories (see CATEGORY_DOCS):
  1. broker_session      — CRM CONNECTED + worker binding + verified proof
  2. ea_attestation      — writer armed + SHA pin + activation revision
  3. terminal_liveness   — fresh native account export, login-bound
  4. positions_inventory — no unresolved UNKNOWN; open mirror broker-anchored
  5. exposure_join       — every live sibling account satisfies 1-4

Known limitation (documented, not hidden): broker-side positions opened
outside the platform (manual trading) cannot be seen until a native
position export exists; DISCONNECTED accounts are excluded from the join
and the supervisor must verify them flat before disconnect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import utc_now
from .. import db as dbmod
from ..events import EventLog, make_event
from .terminal_handoff import check_terminal_liveness


NATIVE_EVIDENCE_CATEGORIES: Tuple[str, ...] = (
    'broker_session',
    'ea_attestation',
    'terminal_liveness',
    'positions_inventory',
    'exposure_join',
)

CATEGORY_DOCS: Dict[str, str] = {
    'broker_session': 'CRM CONNECTED + worker binding + native verified proof',
    'ea_attestation': 'single writer armed + EA SHA pin + activation revision',
    'terminal_liveness': 'fresh login-bound native account export from MT5',
    'positions_inventory': 'no unresolved UNKNOWN; open mirror broker-anchored',
    'exposure_join': 'every live sibling account satisfies categories 1-4',
}

REASON_BLOCKED_INCOMPLETE_EXPOSURE = 'BLOCKED_INCOMPLETE_EXPOSURE'
RECONCILED_OK = ('MATCHED', 'SETTLED')
_INERT_STATUSES = ('DISCONNECTED', 'NOT_LINKED')


@dataclass
class NativeEvidence:
    connection_id: str
    owner_id: str
    environment: str
    present: Dict[str, bool] = field(default_factory=dict)
    reasons: Dict[str, str] = field(default_factory=dict)
    checked_at: str = ''
    unresolved_unknowns: int = 0
    verified_stop_risk_usd: float = 0.0
    stop_risk_approx: bool = False

    @property
    def complete(self) -> bool:
        return all(self.present.get(c, False)
                   for c in NATIVE_EVIDENCE_CATEGORIES)

    @property
    def missing(self) -> List[str]:
        return [c for c in NATIVE_EVIDENCE_CATEGORIES
                if not self.present.get(c, False)]

    def to_dict(self) -> Dict[str, Any]:
        return {'connection_id': self.connection_id,
                'owner_id': self.owner_id, 'environment': self.environment,
                'complete': self.complete, 'missing': self.missing,
                'present': dict(self.present), 'reasons': dict(self.reasons),
                'checked_at': self.checked_at,
                'unresolved_unknowns': self.unresolved_unknowns,
                'verified_stop_risk_usd': round(self.verified_stop_risk_usd, 2),
                'stop_risk_approx': self.stop_risk_approx}


def _counted_in_join(acct: Dict[str, Any]) -> bool:
    """Live siblings count toward the owner exposure join.

    Inert accounts (never linked, disconnected, switches off) carry no
    platform-driven risk and are excluded; everything else must show
    complete core evidence or the join is missing.
    """
    if acct.get('conn_status') not in _INERT_STATUSES:
        return True
    if acct.get('desired_on') or acct.get('effective_on'):
        return True
    return False


def _core_checks(connection_id: str, account: Dict[str, Any], *,
                 gateway: Any = None,
                 mt5_root: Optional[str] = None,
                 stale_seconds: Optional[float] = None,
                 now_ts: Optional[float] = None) -> Tuple[Dict[str, bool],
                                                          Dict[str, str],
                                                          int]:
    present: Dict[str, bool] = {}
    reasons: Dict[str, str] = {}

    # 1) broker session ------------------------------------------------
    if (account.get('conn_status') == 'CONNECTED'
            and account.get('worker_binding')
            and account.get('broker_verified_at')):
        present['broker_session'] = True
        reasons['broker_session'] = 'OK'
    else:
        present['broker_session'] = False
        reasons['broker_session'] = (
            'NOT_CONNECTED: conn_status=%s binding=%s verified=%s'
            % (account.get('conn_status', 'NONE'),
               bool(account.get('worker_binding')),
               bool(account.get('broker_verified_at'))))

    # 2) EA attestation --------------------------------------------------
    if gateway is None:
        present['ea_attestation'] = False
        reasons['ea_attestation'] = 'GATEWAY_NOT_WIRED'
    else:
        try:
            status = gateway.status(connection_id)
        except Exception:
            status = None
        armed = bool(status and status.armed)
        rev_ok = bool(status and status.revision_ok)
        pinned = str(account.get('activation_revision', '') or '')
        rev_match = (not pinned or
                     (status is not None
                      and str(status.activation_revision or '') == pinned))
        if armed and rev_ok and rev_match:
            present['ea_attestation'] = True
            reasons['ea_attestation'] = 'OK'
        else:
            present['ea_attestation'] = False
            reasons['ea_attestation'] = (
                'EA_NOT_ATTESTED: armed=%s revision_ok=%s revision_match=%s'
                % (armed, rev_ok, rev_match))

    # 3) terminal liveness ------------------------------------------------
    live, live_reason, _ident = check_terminal_liveness(
        connection_id, account, mt5_root=mt5_root,
        stale_seconds=stale_seconds, now_ts=now_ts)
    present['terminal_liveness'] = live
    reasons['terminal_liveness'] = live_reason

    # 4) positions inventory -----------------------------------------------
    receipts = dbmod.engine_db.list_receipts(connection_id, limit=500)
    unknowns = [r for r in receipts
                if r.get('execution_state') == 'UNKNOWN'
                and r.get('reconciled_state') not in RECONCILED_OK]
    bad_open = 0
    for r in dbmod.engine_db.recent_open_positions(connection_id):
        anchored = r.get('reconciled_state') in RECONCILED_OK
        try:
            stopped = float(r.get('stop_loss', 0.0) or 0.0) != 0.0
        except (TypeError, ValueError):
            stopped = False
        if not (anchored and stopped):
            bad_open += 1
    if unknowns or bad_open:
        present['positions_inventory'] = False
        reasons['positions_inventory'] = (
            'INVENTORY_UNCERTAIN: unresolved_unknown=%d unanchored_open=%d'
            % (len(unknowns), bad_open))
    else:
        present['positions_inventory'] = True
        reasons['positions_inventory'] = 'OK'
    return present, reasons, len(unknowns)


def assess_native_evidence(connection_id: str, *, gateway: Any = None,
                           mt5_root: Optional[str] = None,
                           stale_seconds: Optional[float] = None,
                           now_ts: Optional[float] = None,
                           event_log: Optional[EventLog] = None
                           ) -> NativeEvidence:
    """Assess all five native-evidence categories for one connection."""
    account = dbmod.engine_db.get_account(connection_id)
    if account is None:
        return NativeEvidence(
            connection_id=connection_id, owner_id='', environment='',
            present={c: False for c in NATIVE_EVIDENCE_CATEGORIES},
            reasons={c: 'ACCOUNT_NOT_FOUND'
                     for c in NATIVE_EVIDENCE_CATEGORIES},
            checked_at=utc_now())
    present, reasons, unknowns = _core_checks(
        connection_id, account, gateway=gateway, mt5_root=mt5_root,
        stale_seconds=stale_seconds, now_ts=now_ts)

    # 5) owner exposure join -------------------------------------------------
    owner_id = str(account.get('owner_id', '') or '')
    blind: List[str] = []
    for sib in dbmod.engine_db.list_accounts(owner_id=owner_id or None):
        if sib.get('connection_id') == connection_id:
            continue  # own gaps are categories 1-4; the join is siblings
        if not _counted_in_join(sib):
            continue
        sib_present, _sib_reasons, _sib_unk = _core_checks(
            sib['connection_id'], sib, gateway=gateway, mt5_root=mt5_root,
            stale_seconds=stale_seconds, now_ts=now_ts)
        if not all(sib_present.get(c, False)
                   for c in NATIVE_EVIDENCE_CATEGORIES[:4]):
            blind.append(sib['connection_id'])
    if blind:
        present['exposure_join'] = False
        reasons['exposure_join'] = (
            'BLIND_SIBLINGS: %s' % ','.join(sorted(blind)))
    else:
        present['exposure_join'] = True
        reasons['exposure_join'] = 'OK'

    stop_risk, approx = verified_stop_risk_usd(connection_id)
    ev = NativeEvidence(
        connection_id=connection_id, owner_id=owner_id,
        environment=str(account.get('environment', '') or ''),
        present=present, reasons=reasons, checked_at=utc_now(),
        unresolved_unknowns=unknowns,
        verified_stop_risk_usd=stop_risk, stop_risk_approx=approx)
    if event_log is not None and not ev.complete:
        try:
            event_log.append(make_event(
                'evidence.incomplete', connection_id=connection_id,
                broker=account.get('broker'),
                mt5_login=account.get('mt5_login'),
                execution_state='BLOCKED',
                reason_code=REASON_BLOCKED_INCOMPLETE_EXPOSURE,
                payload=ev.to_dict()))
        except Exception:
            pass
    return ev


def verified_stop_risk_usd(connection_id: str) -> Tuple[float, bool]:
    """Recompute open stop-risk from anchored fills (approx point values).

    Uses the eligibility registry's contract point value when the symbol
    is known, else 1.0 (flagged approximate). Only anchored receipts
    (MATCHED/SETTLED) contribute; anything else is not verified risk.
    """
    try:
        from ..apex.eligibility import SymbolEligibility
        registry = SymbolEligibility().registry
    except Exception:
        registry = {}
    total = 0.0
    approx = False
    for r in dbmod.engine_db.recent_open_positions(connection_id):
        if r.get('reconciled_state') not in RECONCILED_OK:
            continue
        try:
            entry = float(r.get('fill_price', 0.0) or 0.0)
            stop = float(r.get('stop_loss', 0.0) or 0.0)
            vol = float(r.get('volume', 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if not (entry and stop and vol):
            continue
        prof = registry.get(r.get('symbol', '')) if registry else None
        point_value = 1.0
        if prof is not None and prof.contract.point_value:
            point_value = float(prof.contract.point_value)
        else:
            approx = True
        total += vol * abs(entry - stop) * point_value
    return total, approx


def gate_flags_from_evidence(evidence: NativeEvidence, *,
                             account: Dict[str, Any],
                             equity: float,
                             entitlement_approved: bool) -> Dict[str, Any]:
    """Map an evidence assessment onto candidate-review gate overrides."""
    present = evidence.present
    return {
        'account_bound': True,
        'worker_nonce_bound': bool(present.get('broker_session', False)),
        'fresh_broker_session': bool(present.get('broker_session', False)
                                     and present.get('terminal_liveness',
                                                     False)),
        'native_ea_account_match': bool(present.get('ea_attestation', False)),
        'broker_trade_allowed': bool(account.get('effective_on')),
        'terminal_trade_allowed': bool(present.get('ea_attestation', False)
                                       and present.get('terminal_liveness',
                                                       False)),
        'crm_entitlement_approved': bool(entitlement_approved),
        'single_writer_exclusive': bool(present.get('ea_attestation', False)),
        'broker_positions_protected': bool(
            present.get('positions_inventory', False)),
        'broker_exposure_reconciled': bool(
            present.get('exposure_join', False)),
        'account_equity_usd': float(equity or 0.0),
        'current_verified_stop_risk_usd': float(
            evidence.verified_stop_risk_usd),
        'order_history_uncertain': bool(evidence.unresolved_unknowns > 0),
        'kill_switch_halted': False,
    }
