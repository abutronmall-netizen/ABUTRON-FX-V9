"""Isolated native MT5 identity verifier.

The broker MT5 password is submitted ONLY to this verifier — never to the
HTTP request path, never to the CRM, never logged. The verifier performs a
one-shot DEMO login check against the client's isolated terminal and returns
a boolean + a reason code. A successful identity proof does NOT establish a
persistent trading connection or grant orders.

In production the verifier runs as an isolated, disposable subprocess (the
reference uses a private Unix socket at /run/apex-mt5-private/verify.sock).
This class wraps the per-account terminal manager and is the seam where that
subprocess is wired; the contract (bool + reason code, no password persistence)
is what the CRM relies on.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from .mt5_terminal import Mt5Error, Mt5TerminalManager

log = logging.getLogger("abutron.mt5_verifier")

# Reason codes surfaced to the client (never the password or raw logs).
REASON_NONE = "NONE"
REASON_BROKER_AUTH_REJECTED = "BROKER_AUTH_REJECTED"
REASON_VERIFIER_UNAVAILABLE = "VERIFIER_UNAVAILABLE"
REASON_VERIFIER_TIMEOUT = "VERIFIER_TIMEOUT"
REASON_NATIVE_PROOF_MISSING = "NATIVE_PROOF_MISSING"
REASON_NATIVE_VERIFICATION_INCONCLUSIVE = "NATIVE_VERIFICATION_INCONCLUSIVE"
REASON_GATEWAY_ERROR = "GATEWAY_OR_NATIVE_ERROR"


class Mt5Verifier:
    """One-shot native DEMO login verification (password-isolated)."""

    def __init__(self, terminal_mgr: Optional[Mt5TerminalManager] = None):
        self._mgr = terminal_mgr or Mt5TerminalManager()

    def verify(self, *, connection_id: str, broker: str, server: str,
               login: int, password: str) -> Tuple[bool, str]:
        """Attempt a one-shot native DEMO login.

        Returns (broker_authenticated, reason_code). The password is held in
        memory for the duration of this call only and is never returned or
        persisted by this class.
        """
        if not password:
            return False, REASON_BROKER_AUTH_REJECTED
        try:
            term = self._mgr.get(connection_id)
        except Mt5Error:
            term = None
        if term is None or not term.is_running():
            # No live isolated terminal to verify against — this is a verifier
            # availability issue, not a proof that the credentials are wrong.
            return False, REASON_VERIFIER_UNAVAILABLE
        try:
            ok, reason = self._native_login(term, broker, server, login, password)
            return ok, reason
        except Mt5Error as exc:
            log.warning("mt5 verifier runtime conflict for %s: %s",
                        connection_id, type(exc).__name__)
            return False, REASON_VERIFIER_UNAVAILABLE
        except Exception:  # noqa: BLE001 — never leak details to the client
            log.exception("mt5 verifier unexpected failure for %s", connection_id)
            return False, REASON_GATEWAY_ERROR
        finally:
            # Best-effort: drop the reference so the password is not retained.
            del password

    def _native_login(self, term, broker: str, server: str, login: int,
                      password: str) -> Tuple[bool, str]:
        """Perform the native login check via the terminal's RPC/export.

        Production wires a dedicated native verifier subprocess here (the
        reference's private-socket verifier). The terminal's native export
        channel is the transport; only the boolean outcome is returned.
        """
        # The terminal's native export is the broker-native evidence channel.
        # A real deployment stages a one-shot login-probe EA into MQL5/Files
        # and reads the result back. Without that probe wired, we cannot
        # claim a proof — report inconclusive rather than guessing.
        native = self._mgr.read_native_export(term.connection_id)
        if not native:
            return False, REASON_NATIVE_PROOF_MISSING
        identity = native.get("identity") or {}
        if identity.get("login") != login:
            return False, REASON_BROKER_AUTH_REJECTED
        # The export contains account claims, not an authenticated broker
        # challenge/response. Until the isolated native login probe is wired,
        # never promote a matching login number into authenticated status.
        return False, REASON_NATIVE_VERIFICATION_INCONCLUSIVE
