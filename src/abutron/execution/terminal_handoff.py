"""Native EA file handoff — genuine broker order transport.

The manager socket ``order`` / ``order_handoff`` handlers proxy signed order
requests to the account's MT5 terminal through the MQL5/Files drop zone
(``ABUTRON_REQ_PENDING.json`` in, ``ABUTRON_NATIVE_RECEIPTS_V2_*.txt`` out,
as implemented by ``mql5/ApexNativeWriter.mq5``) and return ONLY
broker-written receipts.

No terminal, no receipt, or an unverifiable receipt yields unknown/reject —
never a fabricated ACK. The pending file is created with O_EXCL so it also
acts as a cross-process single-writer mutex: a second submit while one is
in flight is rejected WRITER_BUSY instead of overwriting the request.
"""
from __future__ import annotations

import glob
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import HEARTBEAT_STALE_SECONDS, utc_now
from .. import db as dbmod
from ..events import EventLog, make_event
from ..feeds.mt5_terminal import terminal_export_dir


REQ_PENDING_FILE = 'ABUTRON_REQ_PENDING.json'
RECEIPT_GLOB = 'ABUTRON_NATIVE_RECEIPTS_V2_*.txt'
IDENTITY_GLOB = 'ABUTRON_NATIVE_ACCOUNT_V2_*.txt'
CONSUMED_SUFFIX = '.consumed'
RECEIPT_SWEEP_SECONDS = 3600.0


def _latest(pattern: str) -> Optional[str]:
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (ValueError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _file_age_s(path: str, now: float) -> float:
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return float('inf')


def check_terminal_liveness(connection_id: str, account: Dict[str, Any], *,
                            mt5_root: Optional[str] = None,
                            stale_seconds: Optional[float] = None,
                            now_ts: Optional[float] = None
                            ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Verify the account terminal is alive and bound to this account.

    Returns (ok, reason, identity). ``ok`` requires a fresh native account
    export whose login + connection match the registered account.
    """
    now = now_ts if now_ts is not None else time.time()
    stale = (stale_seconds if stale_seconds is not None
             else float(HEARTBEAT_STALE_SECONDS))
    export_dir = (terminal_export_dir(connection_id)
                  if mt5_root is None
                  else terminal_export_dir(connection_id, root=mt5_root))
    ident_path = _latest(os.path.join(export_dir, IDENTITY_GLOB))
    if ident_path is None:
        return False, 'TERMINAL_EXPORT_MISSING', None
    if _file_age_s(ident_path, now) > stale:
        return False, 'NATIVE_EXPORT_STALE', None
    ident = _read_json(ident_path)
    if ident is None:
        return False, 'IDENTITY_UNPARSEABLE', None
    try:
        login_ok = int(ident.get('login', 0)) == int(account.get('mt5_login', -1))
    except (TypeError, ValueError):
        login_ok = False
    if not login_ok:
        return False, 'NATIVE_LOGIN_MISMATCH', ident
    if str(ident.get('connection_id', '')) != str(connection_id):
        return False, 'NATIVE_CONNECTION_MISMATCH', ident
    return True, 'OK', ident


def _consume(path: str) -> None:
    try:
        os.rename(path, path + CONSUMED_SUFFIX)
    except OSError:
        pass


def _sweep_old_receipts(export_dir: str, now: float) -> None:
    for path in glob.glob(os.path.join(export_dir, RECEIPT_GLOB)):
        if _file_age_s(path, now) > RECEIPT_SWEEP_SECONDS:
            _consume(path)


class TerminalOrderProxy:
    """Proxies manager-socket order requests to the native EA file handoff."""

    def __init__(self, event_log: Optional[EventLog] = None, *,
                 mt5_root: Optional[str] = None,
                 stale_seconds: Optional[float] = None,
                 poll_interval: float = 0.25):
        self.log = event_log
        self.mt5_root = mt5_root
        self.stale_seconds = stale_seconds
        self.poll_interval = poll_interval

    # -- public ------------------------------------------------------
    def submit(self, request: Dict[str, Any],
               timeout: float = 10.0) -> Dict[str, Any]:
        """Submit one signed order request, return the genuine EA response.

        Response contract: ``status`` is 'ack' | 'reject' | 'unknown' |
        'error'. An 'ack' ALWAYS carries a broker-written ticket + deal;
        anything unverifiable degrades to 'unknown', never 'ack'.
        """
        try:
            return self._submit(request, timeout)
        except Exception as exc:  # never raise out of the socket handler
            self._emit('handoff.error', request,
                       reason='HANDOFF_ERROR:' + type(exc).__name__)
            return {'status': 'unknown', 'reason': 'HANDOFF_ERROR',
                    'request_id': str(request.get('request_id', ''))}

    # -- internals ---------------------------------------------------
    def _submit(self, request: Dict[str, Any],
                timeout: float) -> Dict[str, Any]:
        cid = str(request.get('connection_id', '') or '')
        req_id = str(request.get('request_id', '') or '')
        if not cid:
            return {'status': 'error', 'error': 'MISSING_CONNECTION'}
        if not req_id:
            return {'status': 'error', 'error': 'MISSING_REQUEST_ID'}
        account = dbmod.engine_db.get_account(cid)
        if account is None:
            return {'status': 'reject', 'reason': 'ACCOUNT_NOT_FOUND',
                    'request_id': req_id}

        # 1) account binding: the request must name this account's login.
        try:
            login_ok = (int(request.get('mt5_login', 0))
                        == int(account.get('mt5_login', -1)))
        except (TypeError, ValueError):
            login_ok = False
        if not login_ok:
            self._emit('handoff.rejected', request, connection_id=cid,
                       reason='LOGIN_MISMATCH')
            return {'status': 'reject', 'reason': 'LOGIN_MISMATCH',
                    'request_id': req_id}

        # 2) activation binding: when the account has a linked revision the
        # request must carry it (the EA re-checks against its own seed).
        pinned = str(account.get('activation_revision', '') or '')
        if pinned and str(request.get('activation_revision', '')) != pinned:
            self._emit('handoff.rejected', request, connection_id=cid,
                       reason='ACTIVATION_REVISION_MISMATCH')
            return {'status': 'reject',
                    'reason': 'ACTIVATION_REVISION_MISMATCH',
                    'request_id': req_id}

        # 3) terminal liveness: never queue a request to a dead terminal.
        live, live_reason, _ident = check_terminal_liveness(
            cid, account, mt5_root=self.mt5_root,
            stale_seconds=self.stale_seconds)
        if not live:
            self._emit('handoff.rejected', request, connection_id=cid,
                       reason=live_reason)
            return {'status': 'unknown', 'reason': live_reason,
                    'request_id': req_id}

        export_dir = self._export_dir(cid)

        # 4) claim the cross-process single-writer slot (O_EXCL create).
        pending = os.path.join(export_dir, REQ_PENDING_FILE)
        try:
            fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._emit('handoff.rejected', request, connection_id=cid,
                       reason='WRITER_BUSY')
            return {'status': 'reject', 'reason': 'WRITER_BUSY',
                    'request_id': req_id}
        except OSError:
            self._emit('handoff.rejected', request, connection_id=cid,
                       reason='HANDOFF_IO_ERROR')
            return {'status': 'unknown', 'reason': 'HANDOFF_IO_ERROR',
                    'request_id': req_id}
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(request, fh, separators=(',', ':'))
        except OSError:
            try:
                os.unlink(pending)
            except OSError:
                pass
            return {'status': 'unknown', 'reason': 'HANDOFF_IO_ERROR',
                    'request_id': req_id}
        self._emit('handoff.submitted', request, connection_id=cid,
                   reason='OK')

        # 5) wait for the EA-written receipt naming our request_id.
        _sweep_old_receipts(export_dir, time.time())
        deadline = time.monotonic() + max(0.5, float(timeout))
        while time.monotonic() < deadline:
            hit = self._poll_receipts(export_dir, req_id, account, cid)
            if hit is not None:
                return hit
            time.sleep(self.poll_interval)
        self._emit('handoff.timeout', request, connection_id=cid,
                   reason='EA_RECEIPT_TIMEOUT')
        # The pending file is left for the EA; the reconciler resolves the
        # ambiguous outcome against native history. Never invent a receipt.
        return {'status': 'unknown', 'reason': 'EA_RECEIPT_TIMEOUT',
                'request_id': req_id}

    def _poll_receipts(self, export_dir: str, req_id: str,
                       account: Dict[str, Any],
                       cid: str) -> Optional[Dict[str, Any]]:
        for path in sorted(glob.glob(os.path.join(export_dir, RECEIPT_GLOB))):
            body = _read_json(path)
            if body is None or str(body.get('request_id', '')) != req_id:
                continue
            return self._settle_receipt(path, body, account, cid, req_id)
        return None

    def _settle_receipt(self, path: str, body: Dict[str, Any],
                        account: Dict[str, Any], cid: str,
                        req_id: str) -> Optional[Dict[str, Any]]:
        """Validate one EA receipt naming our request; consume + map it."""
        try:
            receipt_login = int(body.get('login', 0))
        except (TypeError, ValueError):
            receipt_login = 0
        try:
            ticket = int(body.get('ticket', 0) or 0)
        except (TypeError, ValueError):
            ticket = 0
        deal = str(body.get('deal', '') or '')
        try:
            deal_no = int(deal)
        except (TypeError, ValueError):
            deal_no = 0
        state = str(body.get('state', '') or '').upper()

        def _ack() -> Dict[str, Any]:
            def _f(key: str) -> float:
                try:
                    return float(body.get(key, 0.0) or 0.0)
                except (TypeError, ValueError):
                    return 0.0
            return {'status': 'ack', 'ticket': ticket, 'deal': deal,
                    'request_id': req_id, 'login': receipt_login,
                    'price': _f('price'), 'volume': _f('volume'),
                    'sl': _f('sl'), 'tp': _f('tp'),
                    'ts': str(body.get('ts', '') or '')}

        # A receipt bound to another login is never ours — consume it so it
        # cannot confuse a later poll, and keep waiting for the genuine one.
        try:
            acct_login = int(account.get('mt5_login', -1))
        except (TypeError, ValueError):
            acct_login = -1
        if receipt_login != acct_login:
            _consume(path)
            self._emit('handoff.receipt_login_mismatch', {}, request_id=req_id,
                       connection_id=cid, reason='RECEIPT_LOGIN_MISMATCH')
            return None

        if state == 'ACK' and ticket > 0 and deal_no > 0:
            _consume(path)
            self._emit('handoff.receipt', {}, request_id=req_id,
                       connection_id=cid, reason='NATIVE_ACK')
            return _ack()
        if state == 'REJECTED':
            _consume(path)
            reason = str(body.get('reason', '') or 'EA_REJECTED')
            self._emit('handoff.receipt', {}, request_id=req_id,
                       connection_id=cid, reason=reason)
            return {'status': 'reject', 'reason': reason, 'request_id': req_id}
        if state == 'UNKNOWN':
            _consume(path)
            reason = str(body.get('reason', '') or 'SEND_UNKNOWN')
            self._emit('handoff.receipt', {}, request_id=req_id,
                       connection_id=cid, reason=reason)
            return {'status': 'unknown', 'reason': reason,
                    'request_id': req_id}
        # Weak/foreign ACK shape (ticket 0, empty deal, unknown state):
        # consume it and keep waiting rather than accept a weak ACK.
        _consume(path)
        self._emit('handoff.receipt_unverifiable', {}, request_id=req_id,
                   connection_id=cid, reason='RECEIPT_UNVERIFIABLE')
        return None

    def _export_dir(self, connection_id: str) -> str:
        if self.mt5_root is None:
            return terminal_export_dir(connection_id)
        return terminal_export_dir(connection_id, root=self.mt5_root)

    def _emit(self, event_type: str, request: Dict[str, Any], **kw: Any) -> None:
        if self.log is None:
            return
        payload = {'request_id': kw.pop('request_id',
                                        request.get('request_id', '')),
                   'candidate_id': request.get('candidate_id', ''),
                   'symbol': request.get('symbol', '')}
        try:
            self.log.append(make_event(event_type, payload=payload, **kw))
        except Exception:
            pass
