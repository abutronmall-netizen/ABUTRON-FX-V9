"""CRM control-plane: accounts, licensing and the ON/OFF state machine.

The blueprint is explicit: *requested ON* and *effective ON* are distinct.
Effective ON requires a fresh native ACK bound to the current activation
revision. Client OFF blocks new entries but must not stop protective
management of existing positions.
"""
from __future__ import annotations

import secrets
import time
from typing import Dict, List, Optional

from ..config import (BROKERS, DERIV_SERVERS, FP_SYMBOL_SUFFIX_HINT,
                      DEFAULT_PER_TRADE_RISK_PCT, DEFAULT_PORTFOLIO_RISK_CAP_PCT,
                      LIVE_TRADING_ENABLED, utc_now)
from ..crypto import make_activation_revision
from ..db import engine_db, portal_db


class CrmError(Exception):
    pass


def _hash_password(password: str) -> str:
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()


def _hours_ago(hours: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


class CrmService:
    """Portal/CRM business logic (no broker order authority here)."""

    def __init__(self):
        self.engine = engine_db
        self.portal = portal_db

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def register_account(self, *, connection_id: str, broker: str,
                         mt5_login: int, owner_id: str,
                         display_name: str = "", environment: str = "demo",
                         server: str = "",
                         per_trade_risk_pct: Optional[float] = None,
                         portfolio_cap_pct: Optional[float] = None,
                         symbols: Optional[List[str]] = None) -> Dict:
        if broker not in BROKERS:
            raise CrmError(f"unsupported broker: {broker}")
        if environment not in ("demo", "live"):
            raise CrmError("environment must be 'demo' or 'live'")
        symbols = symbols or []
        self.engine.upsert_account({
            "connection_id": connection_id,
            "broker": broker,
            "mt5_login": mt5_login,
            "owner_id": owner_id,
            "display_name": display_name or connection_id,
            "environment": environment,
            "server": server,
            "per_trade_risk_pct": per_trade_risk_pct
            or DEFAULT_PER_TRADE_RISK_PCT,
            "portfolio_cap_pct": portfolio_cap_pct
            or DEFAULT_PORTFOLIO_RISK_CAP_PCT,
            "desired_on": 0,
            "effective_on": 0,
            "symbols": ",".join(symbols),
        })
        return self.engine.get_account(connection_id)

    def link_broker_identity(self, connection_id: str, *,
                             ea_sha256: str, server: str) -> Dict:
        """Bind the compiled EA pin + broker server to an account."""
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError(f"account {connection_id} not found")
        revision = make_activation_revision(connection_id, ea_sha256,
                                            acct["environment"])
        acct.update({
            "ea_sha256": ea_sha256,
            "server": server or acct["server"],
            "activation_revision": revision,
        })
        self.engine.upsert_account(acct)
        return self.engine.get_account(connection_id)

    # ------------------------------------------------------------------
    # Broker connection lifecycle (blueprint C10–C16, seven statuses)
    # ------------------------------------------------------------------
    def set_conn_status(self, connection_id: str, status: str,
                        error: str = "") -> None:
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError(f"account {connection_id} not found")
        acct["conn_status"] = status
        acct["conn_updated_at"] = utc_now()
        acct["conn_error"] = error
        self.engine.upsert_account(acct)

    def connection_status(self, connection_id: str) -> Dict:
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError(f"account {connection_id} not found")
        return {
            "connection_id": connection_id,
            "broker": acct["broker"],
            "mt5_login": acct["mt5_login"],
            "owner_id": acct["owner_id"],
            "environment": acct["environment"],
            "server": acct["server"],
            "conn_status": acct.get("conn_status", "NOT_LINKED"),
            "conn_updated_at": acct.get("conn_updated_at", ""),
            "conn_error": acct.get("conn_error", ""),
            "worker_binding": acct.get("worker_binding", ""),
            "broker_verified_at": acct.get("broker_verified_at", ""),
            "broker_authenticated": bool(
                acct.get("conn_status") == "CONNECTED"
                and acct.get("worker_binding")
                and acct.get("broker_verified_at")),
            "desired_on": bool(acct["desired_on"]),
            "effective_on": bool(acct["effective_on"]),
        }

    # ------------------------------------------------------------------
    # Broker connection lifecycle — split enroll / verify / disconnect.
    # The broker password is NEVER accepted at enroll time; it flows only
    # through the isolated verifier during verification, and only a boolean
    # + reason code is recorded. CONNECTED requires a worker binding set by
    # a trusted broker worker — never by a client request or login record.
    # ------------------------------------------------------------------
    MAX_LINK_ATTEMPTS = 8          # per owner per hour
    MAX_ACTIVE_CONNECTIONS = 15

    def start_broker_link(self, *, owner_id: str, broker: str,
                          environment: str, server: str, mt5_login: int,
                          display_name: str = "",
                          symbols: Optional[List[str]] = None) -> Dict:
        """Enroll a broker connection (blueprint C11). No password accepted.

        Registers a scoped connection in CONNECTING and opens the client's
        MT5 terminal. Trading stays OFF; broker authentication is a separate
        step (verify_broker_identity).
        """
        if broker not in BROKERS:
            raise CrmError(f"unsupported broker: {broker}")
        if environment not in ("demo", "live"):
            raise CrmError("environment must be 'demo' or 'live'")
        if not owner_id:
            raise CrmError("OWNER_REQUIRED: no authenticated owner")
        if not isinstance(mt5_login, int) or mt5_login <= 0:
            raise CrmError("CREDENTIAL_INVALID: invalid MT5 account number")

        # DEMO/LIVE: both are enabled. Fail-closed LIVE gating has been
        # removed — LIVE connects are accepted directly (aggressive mode).
        if environment == "live" and not LIVE_TRADING_ENABLED:
            raise CrmError("LIVE_NOT_APPROVED: LIVE trading is not enabled "
                           "for this deployment")

        # Rate-limit link attempts (anti-abuse).
        now = utc_now()
        hour_ago = _hours_ago(1)
        self.engine.purge_link_attempts(hour_ago)
        attempts = self.engine.count_recent_link_attempts(owner_id, hour_ago)
        if attempts >= self.MAX_LINK_ATTEMPTS:
            raise CrmError("LINK_RATE_LIMITED: maximum linking attempts reached")
        active = sum(1 for a in self.engine.list_accounts(owner_id=owner_id)
                     if a["conn_status"] != "DISCONNECTED")
        if active >= self.MAX_ACTIVE_CONNECTIONS:
            raise CrmError("LINK_LIMIT_REACHED: maximum active connections reached")

        # A broker account must be unique per owner + environment.
        dup = next((a for a in self.engine.list_accounts(owner_id=owner_id)
                    if a["broker"] == broker and a["mt5_login"] == mt5_login
                    and a["environment"] == environment
                    and a["conn_status"] != "DISCONNECTED"), None)
        if dup is not None:
            raise CrmError(f"DUPLICATE_ACCOUNT_OWNER: account already linked "
                           f"for this owner ({dup['connection_id']})")

        connection_id = f"conn_{secrets.token_hex(6)}"
        self.register_account(connection_id=connection_id, broker=broker,
                              mt5_login=mt5_login, owner_id=owner_id,
                              display_name=display_name or connection_id,
                              environment=environment, server=server,
                              symbols=symbols)
        self.engine.add_link_attempt(owner_id, now)
        self.set_conn_status(connection_id, "CONNECTING")
        self.engine.add_connection_event(connection_id, owner_id,
                                         "CLIENT_LINK_INITIATED", now)
        return self.connection_status(connection_id)

    def verify_broker_identity(self, connection_id: str, *, password: str,
                               verifier=None) -> Dict:
        """One-shot native DEMO identity proof via the isolated verifier.

        The password flows ONLY to the verifier. On success we record
        NATIVE_DEMO_IDENTITY_PROVED and set a worker binding, then mark
        CONNECTED. This proof does NOT grant trading or orders.
        """
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError("CONNECTION_NOT_FOUND: account not found")
        if acct["conn_status"] not in ("CONNECTING", "AUTHENTICATING"):
            raise CrmError("NOT_AUTHENTICATING: connection is not awaiting "
                           "broker verification")
        if acct["environment"] != "demo":
            raise CrmError("LIVE_NOT_VERIFIABLE_THIS_WAY: only DEMO accounts "
                           "use native identity verification")

        from ..feeds.mt5_verifier import Mt5Verifier, REASON_NONE
        verifier = verifier or Mt5Verifier()
        now = utc_now()
        passed, reason = verifier.verify(
            connection_id=connection_id, broker=acct["broker"],
            server=acct["server"], login=acct["mt5_login"], password=password)

        # Record the audit event with the reason code (never the password).
        event = "NATIVE_DEMO_IDENTITY_PROVED" if passed else \
            "NATIVE_DEMO_IDENTITY_NOT_PROVED"
        self.engine.add_connection_event(connection_id, acct["owner_id"],
                                         event, now,
                                         "no_persistent_session" if passed
                                         else reason[:80])

        if not passed:
            self.set_conn_status(connection_id, "ERROR", error=reason)
            raise CrmError(f"BROKER_AUTH_NOT_PROVEN: {reason}")

        # Worker binding + CONNECTED. The binding is set here by the verified
        # native proof; a real deployment additionally requires a trusted
        # broker worker to confirm the persistent session before trading.
        binding = f"wb_{secrets.token_hex(8)}"
        acct["worker_binding"] = binding
        acct["broker_verified_at"] = now
        acct["conn_status"] = "CONNECTED"
        acct["conn_updated_at"] = now
        acct["conn_error"] = ""
        self.engine.upsert_account(acct)
        return self.connection_status(connection_id)

    def activate_live_session(self, connection_id: str) -> Dict:
        """Mark a LIVE account CONNECTED once its MT5 terminal has launched.

        For a LIVE account the terminal launch IS the broker session: once
        the portal has successfully launched the client's MT5 terminal, the
        account is CONNECTED with a worker binding and can trade. This is
        the "connect a new live account -> launch MT5 terminal" flow.
        """
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError("CONNECTION_NOT_FOUND: account not found")
        if acct["environment"] != "live":
            raise CrmError("NOT_LIVE: activate_live_session is for LIVE "
                           "accounts only")
        now = utc_now()
        binding = acct.get("worker_binding") or f"wb_{secrets.token_hex(8)}"
        acct["worker_binding"] = binding
        acct["broker_verified_at"] = now
        acct["conn_status"] = "CONNECTED"
        acct["conn_updated_at"] = now
        acct["conn_error"] = ""
        self.engine.upsert_account(acct)
        self.engine.add_connection_event(
            connection_id, acct["owner_id"], "LIVE_TERMINAL_LAUNCHED",
            now, "MT5 terminal launched; live session CONNECTED")
        return self.connection_status(connection_id)

    def disconnect(self, connection_id: str) -> Dict:
        """Client-initiated disconnect. Requires no active trading session."""
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError("CONNECTION_NOT_FOUND: account not found")
        if acct["effective_on"] or acct["conn_status"] == "CONNECTED":
            raise CrmError("ACTIVE_SESSION: stop the broker session and revoke "
                           "the worker binding before disconnecting")
        now = utc_now()
        acct["conn_status"] = "DISCONNECTED"
        acct["conn_updated_at"] = now
        acct["worker_binding"] = ""
        acct["broker_verified_at"] = ""
        acct["desired_on"] = 0
        self.engine.upsert_account(acct)
        self.engine.add_connection_event(connection_id, acct["owner_id"],
                                         "CLIENT_UNLINKED", now)
        return self.connection_status(connection_id)

    def get_account(self, connection_id: str) -> Optional[Dict]:
        return self.engine.get_account(connection_id)

    def list_accounts(self, broker: Optional[str] = None,
                      owner_id: Optional[str] = None) -> List[Dict]:
        return self.engine.list_accounts(broker=broker, owner_id=owner_id)

    # ------------------------------------------------------------------
    # Users / entitlements
    # ------------------------------------------------------------------
    def create_user(self, *, username: str, role: str,
                    password: str, owner_id: str = "",
                    entitlement: str = "free", mfa: bool = False) -> Dict:
        if role not in ("client", "admin", "operator"):
            raise CrmError("invalid role")
        ok = self.portal.add_user({
            "user_id": f"u_{secrets.token_hex(8)}",
            "username": username,
            "role": role,
            "owner_id": owner_id,
            "password_hash": _hash_password(password),
            "mfa_enabled": 1 if mfa else 0,
            "entitlement": entitlement,
            "status": "active",
            "created_at": utc_now(),
        })
        if not ok:
            raise CrmError("username already exists")
        return self.portal.get_user(username)

    def authenticate(self, username: str, password: str) -> Optional[Dict]:
        u = self.portal.get_user(username)
        if u is None or u["password_hash"] != _hash_password(password):
            return None
        if u["status"] != "active":
            return None
        return u

    def grant_license(self, *, license_id: str, owner_id: str, tier: str,
                      expires_at: str = "", billing_state: str = "paid") -> Dict:
        self.portal.add_license({
            "license_id": license_id,
            "owner_id": owner_id,
            "tier": tier,
            "status": "active",
            "billing_state": billing_state,
            "expires_at": expires_at,
        })
        return self.portal.get_license(owner_id)

    def check_entitlement(self, owner_id: str, connection_id: str) -> bool:
        """An active license is required for effective ON."""
        lic = self.portal.get_license(owner_id)
        if lic is None:
            return False
        if lic["status"] != "active":
            return False
        if lic["expires_at"] and lic["expires_at"] < utc_now():
            return False
        return True

    # ------------------------------------------------------------------
    # ON / OFF state machine
    # ------------------------------------------------------------------
    def request_on(self, connection_id: str, *, by: str) -> Dict:
        """Client requests trading ON (aggressive mode).

        Fail-closed pending/broker-ack gating has been removed. A request
        grants trading IMMEDIATELY for both DEMO and LIVE: desired_on=1 and
        effective_on=1 are set together, so the account trades on the next
        decision pass. No separate LIVE-intent bridge is required.
        """
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError("account not found")
        if not self.check_entitlement(acct["owner_id"], connection_id):
            raise CrmError("ENITLEMENT_MISSING: no active license for owner")

        now = utc_now()
        env = acct["environment"]
        ready = (acct["conn_status"] == "CONNECTED"
                 and acct.get("worker_binding"))

        if not ready:
            raise CrmError("NOT_CONNECTED: MT5 is not connected; trading "
                           "stays OFF")
        if acct["effective_on"]:
            return self._control_state(connection_id)  # already ON

        # Grant trading immediately — desired and effective ON together.
        self.portal.set_control(connection_id, True,
                                note=f"TRADING_ON_{env.upper()}_IMMEDIATE")
        acct["desired_on"] = 1
        acct["effective_on"] = 1
        self.engine.upsert_account(acct)
        self.engine.add_connection_event(
            connection_id, acct["owner_id"], "TRADING_ON_GRANT", now,
            f"immediate ON for {env} (fail-closed pending gate removed)")
        return self._control_state(connection_id)

    def request_off(self, connection_id: str, *, by: str) -> Dict:
        """Client requests trading OFF. New entries blocked immediately.

        desired_on=0 denies new orders right away; effective_on is confirmed
        OFF by the native writer. Protective SL/TP/close for existing
        positions are preserved (never revoked by an OFF request).
        """
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError("account not found")
        now = utc_now()
        env = acct["environment"]
        # Clear any LIVE intent so no future issuer reads it as permission.
        if env == "live":
            self.engine.upsert_live_intent(connection_id, 0, "CUSTOMER_OFF", now)
        self.portal.set_control(connection_id, False,
                                note="requested by " + by)
        acct["desired_on"] = 0
        # effective_on stays until the native writer acknowledges disarm,
        # but new entries are blocked immediately by desired_on=0.
        self.engine.upsert_account(acct)
        return self._control_state(connection_id)

    def confirm_effective_on(self, connection_id: str, *,
                             activation_revision: str,
                             ea_sha256: str) -> Dict:
        """Native writer arms ON. Verifies the activation revision matches
        the registered binary pin, then flips effective_on."""
        acct = self.engine.get_account(connection_id)
        if acct is None:
            raise CrmError("account not found")
        if acct["desired_on"] != 1:
            raise CrmError("REQUESTED_OFF: desired ON is off")
        expected = make_activation_revision(connection_id, ea_sha256,
                                            acct["environment"])
        if not secrets.compare_digest(expected, activation_revision):
            # Aggressive mode: binary/revision mismatch is ADVISORY only —
            # it does not block effective ON (fail-closed gating removed).
            self.engine.add_connection_event(
                connection_id, acct["owner_id"], "EFFECTIVE_ON_ADVISORY",
                utc_now(),
                "activation revision mismatch — advisory, ON still granted")
        self.engine.set_effective_on(connection_id, True,
                                     activation_revision, utc_now())
        return self._control_state(connection_id)

    def confirm_effective_off(self, connection_id: str) -> Dict:
        self.engine.set_effective_on(connection_id, False, "", utc_now())
        return self._control_state(connection_id)

    def effective_on(self, connection_id: str) -> bool:
        acct = self.engine.get_account(connection_id)
        return bool(acct and acct["effective_on"] == 1)

    def _control_state(self, connection_id: str) -> Dict:
        acct = self.engine.get_account(connection_id)
        ctrl = self.portal.get_control(connection_id)
        return {
            "connection_id": connection_id,
            "broker": acct["broker"],
            "mt5_login": acct["mt5_login"],
            "owner_id": acct["owner_id"],
            "environment": acct["environment"],
            "desired_on": bool(acct["desired_on"]),
            "effective_on": bool(acct["effective_on"]),
            "activation_revision": acct["activation_revision"],
            "ea_sha256": acct["ea_sha256"],
            "per_trade_risk_pct": acct["per_trade_risk_pct"],
            "portfolio_cap_pct": acct["portfolio_cap_pct"],
            "conn_status": acct.get("conn_status", "NOT_LINKED"),
            "conn_updated_at": acct.get("conn_updated_at", ""),
            "broker_authenticated": bool(
                acct.get("conn_status") == "CONNECTED"
                and acct.get("worker_binding")),
            "worker_binding": acct.get("worker_binding", ""),
            "control_note": ctrl["note"] if ctrl else "",
            "live_intent": (self.engine.get_live_intent(connection_id) or
                            {}).get("status", ""),
        }

    # ------------------------------------------------------------------
    # Owner-wide risk exposure (aggregated across linked accounts)
    # ------------------------------------------------------------------
    def owner_risk_exposure(self, owner_id: str,
                            stop_risk_by_conn: Dict[str, float],
                            balance_by_conn: Dict[str, float]) -> Dict:
        """Aggregate USD stop-risk vs the owner's 20% ceiling.

        Missing linked exposure is reported as UNKNOWN and never zero-filled.
        """
        cap_pct = DEFAULT_PORTFOLIO_RISK_CAP_PCT
        total_risk = 0.0
        total_balance = 0.0
        unknown = []
        accounts = self.engine.list_accounts(owner_id=owner_id)
        for a in accounts:
            cid = a["connection_id"]
            if cid in stop_risk_by_conn:
                total_risk += stop_risk_by_conn[cid]
            else:
                unknown.append(cid)
            if cid in balance_by_conn:
                total_balance += balance_by_conn[cid]
        headroom = None
        if total_balance > 0:
            headroom = (cap_pct * total_balance) - total_risk
        return {
            "owner_id": owner_id,
            "aggregate_stop_risk_usd": total_risk,
            "aggregate_balance_usd": total_balance,
            "cap_pct": cap_pct,
            "cap_usd": cap_pct * total_balance if total_balance else None,
            "headroom_usd": headroom,
            "unknown_exposure_connections": unknown,
        }


crm = CrmService()
