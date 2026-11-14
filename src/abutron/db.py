"""SQLite persistence for the engine + portal.

Kept deliberately simple: SQLAlchemy 2.0 core-style with small repositories.
Durable ledger, candidate reservations, account registry and decision
journals all live here so state survives restarts (blueprint: recoverability
must return to a reconciled state without duplicate execution).
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import (Column, Float, Integer, String, Text, DateTime,
                        UniqueConstraint, create_engine, func, select)
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                            scoped_session, sessionmaker)

from .config import ENGINE_DB, PORTAL_DB

_lock = threading.RLock()


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Engine schema
# ---------------------------------------------------------------------------
class AccountRow(Base):
    __tablename__ = "accounts"
    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker: Mapped[str] = mapped_column(String(32), index=True)
    mt5_login: Mapped[int] = mapped_column(Integer, index=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    environment: Mapped[str] = mapped_column(String(16), default="demo")
    server: Mapped[str] = mapped_column(String(128), default="")
    per_trade_risk_pct: Mapped[float] = mapped_column(Float, default=0.15)
    portfolio_cap_pct: Mapped[float] = mapped_column(Float, default=0.20)
    desired_on: Mapped[int] = mapped_column(Integer, default=1)
    effective_on: Mapped[int] = mapped_column(Integer, default=0)
    activation_revision: Mapped[str] = mapped_column(String(128), default="")
    ea_sha256: Mapped[str] = mapped_column(String(128), default="")
    symbols: Mapped[str] = mapped_column(Text, default="")
    # Broker connection lifecycle (blueprint seven statuses):
    # NOT_LINKED/CONNECTING/AUTHENTICATING/CONNECTED/DEGRADED/DISCONNECTED/REVOKED/ERROR
    conn_status: Mapped[str] = mapped_column(String(32), default="NOT_LINKED")
    conn_updated_at: Mapped[str] = mapped_column(String(32), default="")
    conn_error: Mapped[str] = mapped_column(String(256), default="")
    # Broker-native proof: a one-shot identity proof is recorded separately
    # from persistent CONNECTED. CONNECTED requires worker_binding set by a
    # trusted broker worker (never by a client request or a login record).
    worker_binding: Mapped[str] = mapped_column(String(64), default="")
    broker_verified_at: Mapped[str] = mapped_column(String(32), default="")


class LinkAttemptRow(Base):
    """Rate-limited broker-link attempts per owner (anti-abuse)."""
    __tablename__ = "link_attempts"
    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(32), index=True)


class ConnectionEventRow(Base):
    """Audit trail of broker connection lifecycle events."""
    __tablename__ = "connection_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str] = mapped_column(String(256), default="")


class LiveIntentRow(Base):
    """Separate LIVE-trading intent record.

    A client may REQUEST LIVE ON, but that intent lives outside the trading
    controls and NEVER grants order permission: desired_on/effective_on stay
    0 until a verified LIVE broker bridge + risk authorization activate it.
    """
    __tablename__ = "live_intents"
    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requested_enabled: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")


class CandidateRow(Base):
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("replay_key", name="uq_replay_key"),)
    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    broker: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    bar_open: Mapped[str] = mapped_column(String(32))
    replay_key: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(8))
    conviction: Mapped[float] = mapped_column(Float, default=0.0)
    regime: Mapped[str] = mapped_column(String(32), default="")
    agent_votes: Mapped[str] = mapped_column(Text, default="{}")
    risk_fraction: Mapped[float] = mapped_column(Float, default=0.0)
    stop_risk_usd: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    entry_type: Mapped[str] = mapped_column(String(8), default="")
    trigger_price: Mapped[float] = mapped_column(Float, default=0.0)
    invalidation: Mapped[float] = mapped_column(Float, default=0.0)
    entry_quality: Mapped[float] = mapped_column(Float, default=0.0)
    sniper_plan: Mapped[str] = mapped_column(Text, default="{}")
    trail_k: Mapped[float] = mapped_column(Float, default=0.0)
    trail_stop: Mapped[float] = mapped_column(Float, default=0.0)
    trail_plan: Mapped[str] = mapped_column(Text, default="{}")
    execution_state: Mapped[str] = mapped_column(String(32), default="RESERVED")
    reason_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")


class OrderReceiptRow(Base):
    __tablename__ = "order_receipts"
    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    mt5_login: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    native_ticket: Mapped[int] = mapped_column(Integer, default=0)
    broker_deal_id: Mapped[str] = mapped_column(String(64), default="")
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    fill_price: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    swap: Mapped[float] = mapped_column(Float, default=0.0)
    execution_state: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    reconciled_state: Mapped[str] = mapped_column(String(32), default="PENDING")
    received_at: Mapped[str] = mapped_column(String(32), default="")
    reason_code: Mapped[str] = mapped_column(String(64), default="")


class LedgerRow(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    native_ticket: Mapped[int] = mapped_column(Integer, default=0)
    event: Mapped[str] = mapped_column(String(32))        # OPEN/TRAIL/CLOSE/CASH
    symbol: Mapped[str] = mapped_column(String(64), default="")
    direction: Mapped[str] = mapped_column(String(8), default="")
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    swap: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit: Mapped[float] = mapped_column(Float, default=0.0)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    observed_at: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(32), default="")  # native/crm


# ---------------------------------------------------------------------------
# Portal schema (CRM)
# ---------------------------------------------------------------------------
class PortalUserRow(Base):
    __tablename__ = "portal_users"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16))   # client | admin | operator
    owner_id: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(128), default="")
    mfa_enabled: Mapped[int] = mapped_column(Integer, default=0)
    entitlement: Mapped[str] = mapped_column(String(32), default="free")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class LicenseRow(Base):
    __tablename__ = "licenses"
    license_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    tier: Mapped[str] = mapped_column(String(32), default="standard")
    status: Mapped[str] = mapped_column(String(16), default="active")
    billing_state: Mapped[str] = mapped_column(String(16), default="paid")
    expires_at: Mapped[str] = mapped_column(String(32), default="")


class ControlRow(Base):
    """Per-connection requested/effective ON state machine (CRM controls)."""
    __tablename__ = "controls"
    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requested_on: Mapped[int] = mapped_column(Integer, default=0)
    effective_on: Mapped[int] = mapped_column(Integer, default=0)
    activation_revision: Mapped[str] = mapped_column(String(128), default="")
    last_ack_at: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(String(256), default="")


class SessionRow(Base):
    """Portal session — secure HttpOnly cookie auth (blueprint Identity layer).

    Only the token hash is stored; the raw token is returned to the client
    once and lives in an HttpOnly cookie. Sessions are tenant-scoped by
    user_id + owner_id and can be revoked.
    """
    __tablename__ = "sessions"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="client")
    owner_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    token_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    expires_at: Mapped[str] = mapped_column(String(32), default="")
    revoked: Mapped[int] = mapped_column(Integer, default=0)


class RunRow(Base):
    """Structured run/trace record — one decision/candidate cycle per row.

    Lets an operator trace a signal end-to-end (APEX decision → review →
    candidate → dispatch → receipt) via a single run_id, the AgenticTrading
    run/trace pattern.
    """
    __tablename__ = "runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    symbol: Mapped[str] = mapped_column(String(64), default="", index=True)
    signal_id: Mapped[str] = mapped_column(String(128), default="")
    decision: Mapped[str] = mapped_column(String(16), default="")   # BUY/SELL/HOLD/REJECTED/BLOCKED
    status: Mapped[str] = mapped_column(String(16), default="")     # REJECTED/HOLD/READY/BLOCKED
    regime: Mapped[str] = mapped_column(String(32), default="")
    conviction: Mapped[float] = mapped_column(Float, default=0.0)
    gemma_signal: Mapped[str] = mapped_column(String(16), default="")
    gemma_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    riskguard: Mapped[str] = mapped_column(String(32), default="")  # approved / RISKGUARD_*
    blockers: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class ModelProviderRow(Base):
    """LLM model-provider registry entry (non-secret config only).

    Stores provider metadata (provider, base_url, model, enabled). The actual
    credential/secret is injected via environment variables — never stored in
    the repo or DB (blueprint security rule).
    """
    __tablename__ = "model_providers"
    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="")   # gemma/ollama/etc
    base_url: Mapped[str] = mapped_column(String(256), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[int] = mapped_column(Integer, default=0)
    env_secret_var: Mapped[str] = mapped_column(String(64), default="")  # name of env var holding the secret
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


def _migrate_columns(engine, table: str, columns: Dict[str, str]) -> None:
    """Idempotently add missing columns to an existing SQLite table.

    `create_all` creates tables but never alters them; this bridges schema
    additions across existing deployments (e.g. conn_status on accounts).
    """
    from sqlalchemy import inspect as sa_inspect, text
    insp = sa_inspect(engine)
    if not insp.has_table(table):
        return
    existing = {c["name"] for c in insp.get_columns(table)}
    with engine.begin() as conn:
        for name, coltype in columns.items():
            if name in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))


class EngineDB:
    def __init__(self, path: str = ENGINE_DB):
        self._engine = create_engine(f"sqlite:///{path}", future=True)
        # A registry provides one persistent Session per thread; the old
        # single mutable Session slot leaked pooled connections whenever
        # requests and engine/dispatch workers alternated threads.
        self._session_factory = scoped_session(
            sessionmaker(bind=self._engine, expire_on_commit=False))
        with _lock:
            Base.metadata.create_all(self._engine)
            _migrate_columns(self._engine, "accounts", {
                "conn_status": "VARCHAR(32) DEFAULT 'NOT_LINKED'",
                "conn_updated_at": "VARCHAR(32) DEFAULT ''",
                "conn_error": "VARCHAR(256) DEFAULT ''",
                "worker_binding": "VARCHAR(64) DEFAULT ''",
                "broker_verified_at": "VARCHAR(32) DEFAULT ''",
            })
            _migrate_columns(self._engine, "candidates", {
                "price": "FLOAT DEFAULT 0",
                "entry_type": "VARCHAR(8) DEFAULT ''",
                "trigger_price": "FLOAT DEFAULT 0",
                "invalidation": "FLOAT DEFAULT 0",
                "entry_quality": "FLOAT DEFAULT 0",
                "sniper_plan": "TEXT DEFAULT '{}'",
                "trail_k": "FLOAT DEFAULT 0",
                "trail_stop": "FLOAT DEFAULT 0",
                "trail_plan": "TEXT DEFAULT '{}'",
            })

    @property
    def session(self) -> Session:
        return self._session_factory()

    def _reset(self) -> None:
        self._session_factory.remove()

    # -- generic helpers -------------------------------------------------
    def upsert_account(self, acct: Dict[str, Any]) -> None:
        with _lock:
            s = self.session
            row = s.get(AccountRow, acct["connection_id"])
            if row is None:
                row = AccountRow(connection_id=acct["connection_id"])
                s.add(row)
            for k in ("broker", "mt5_login", "owner_id", "display_name",
                      "environment", "server", "per_trade_risk_pct",
                      "portfolio_cap_pct", "desired_on", "effective_on",
                      "activation_revision", "ea_sha256", "symbols",
                      "conn_status", "conn_updated_at", "conn_error",
                      "worker_binding", "broker_verified_at"):
                if k in acct:
                    setattr(row, k, acct[k])
            s.commit()

    def get_account(self, connection_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.get(AccountRow, connection_id)
        return _row_dict(row) if row else None

    def list_accounts(self, broker: Optional[str] = None,
                      owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        stmt = select(AccountRow)
        if broker:
            stmt = stmt.where(AccountRow.broker == broker)
        if owner_id:
            stmt = stmt.where(AccountRow.owner_id == owner_id)
        return [_row_dict(r) for r in self.session.scalars(stmt)]

    def add_candidate(self, cand: Dict[str, Any]) -> bool:
        """Insert candidate if replay_key is free (at-most-once)."""
        with _lock:
            s = self.session
            if s.scalar(select(CandidateRow).where(
                    CandidateRow.replay_key == cand["replay_key"])):
                return False
            s.add(CandidateRow(**cand))
            s.commit()
            return True

    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.get(CandidateRow, candidate_id)
        return _row_dict(row) if row else None

    def set_candidate_state(self, candidate_id: str, state: str,
                            reason_code: str = "") -> None:
        with _lock:
            row = self.session.get(CandidateRow, candidate_id)
            if row:
                row.execution_state = state
                if reason_code:
                    row.reason_code = reason_code
                self.session.commit()

    def list_candidates(self, connection_id: Optional[str] = None,
                        limit: int = 200) -> List[Dict[str, Any]]:
        stmt = select(CandidateRow)
        if connection_id:
            stmt = stmt.where(CandidateRow.connection_id == connection_id)
        stmt = stmt.order_by(CandidateRow.created_at.desc()).limit(limit)
        return [_row_dict(r) for r in self.session.scalars(stmt)]

    def add_receipt(self, r: Dict[str, Any]) -> None:
        with _lock:
            self.session.add(OrderReceiptRow(**r))
            self.session.commit()

    def update_receipt(self, receipt_id: str, **kw) -> None:
        with _lock:
            row = self.session.get(OrderReceiptRow, receipt_id)
            if row:
                for k, v in kw.items():
                    setattr(row, k, v)
                self.session.commit()

    def list_receipts(self, connection_id: Optional[str] = None,
                      limit: int = 100) -> List[Dict[str, Any]]:
        stmt = select(OrderReceiptRow).order_by(OrderReceiptRow.received_at.desc()).limit(limit)
        if connection_id:
            stmt = stmt.where(OrderReceiptRow.connection_id == connection_id)
        return [_row_dict(r) for r in self.session.scalars(stmt)]

    def add_ledger(self, row: Dict[str, Any]) -> None:
        with _lock:
            self.session.add(LedgerRow(**row))
            self.session.commit()

    def list_ledger(self, connection_id: Optional[str] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
        stmt = select(LedgerRow).order_by(LedgerRow.id.desc()).limit(limit)
        if connection_id:
            stmt = stmt.where(LedgerRow.connection_id == connection_id)
        return [_row_dict(r) for r in self.session.scalars(stmt)]

    def recent_open_positions(self, connection_id: str) -> List[Dict[str, Any]]:
        """Open positions = receipts with execution_state OPEN/PROTECTED."""
        stmt = select(OrderReceiptRow).where(
            OrderReceiptRow.connection_id == connection_id,
            OrderReceiptRow.execution_state.in_(("OPEN", "PROTECTED")))
        return [_row_dict(r) for r in self.session.scalars(stmt)]

    def set_effective_on(self, connection_id: str, on: bool,
                         revision: str, ack_at: str) -> None:
        with _lock:
            acct = self.session.get(AccountRow, connection_id)
            if acct:
                acct.effective_on = 1 if on else 0
                acct.activation_revision = revision
            ctrl = self.session.get(ControlRow, connection_id)
            if ctrl is None:
                ctrl = ControlRow(connection_id=connection_id)
                self.session.add(ctrl)
            ctrl.effective_on = 1 if on else 0
            ctrl.activation_revision = revision
            ctrl.last_ack_at = ack_at
            self.session.commit()

    # -- broker link attempts (rate limiting) -------------------------
    def count_recent_link_attempts(self, owner_id: str,
                                   since_iso: str) -> int:
        stmt = select(LinkAttemptRow).where(
            LinkAttemptRow.owner_id == owner_id,
            LinkAttemptRow.created_at >= since_iso)
        return len(self.session.scalars(stmt).all())

    def add_link_attempt(self, owner_id: str, created_at: str) -> None:
        with _lock:
            self.session.add(LinkAttemptRow(
                attempt_id=f"la_{secrets.token_hex(8)}",
                owner_id=owner_id, created_at=created_at))
            self.session.commit()

    def purge_link_attempts(self, before_iso: str) -> None:
        with _lock:
            s = self.session
            s.query(LinkAttemptRow).filter(
                LinkAttemptRow.created_at < before_iso).delete()
            s.commit()

    # -- connection events (audit) --------------------------------------
    def add_connection_event(self, connection_id: str, owner_id: str,
                             event: str, occurred_at: str,
                             detail: str = "") -> None:
        with _lock:
            self.session.add(ConnectionEventRow(
                event_id=f"ce_{secrets.token_hex(8)}",
                connection_id=connection_id, owner_id=owner_id,
                event=event, occurred_at=occurred_at, detail=detail))
            self.session.commit()

    def list_connection_events(self, connection_id: str,
                               limit: int = 50) -> List[Dict[str, Any]]:
        stmt = (select(ConnectionEventRow)
                .where(ConnectionEventRow.connection_id == connection_id)
                .order_by(ConnectionEventRow.occurred_at.desc()).limit(limit))
        return [_row_dict(r) for r in self.session.scalars(stmt)]

    # -- live trading intents -------------------------------------------
    def upsert_live_intent(self, connection_id: str, requested: int,
                           status: str, updated_at: str) -> None:
        with _lock:
            s = self.session
            row = s.get(LiveIntentRow, connection_id)
            if row is None:
                row = LiveIntentRow(connection_id=connection_id)
                s.add(row)
            row.requested_enabled = requested
            row.status = status
            row.updated_at = updated_at
            s.commit()

    def get_live_intent(self, connection_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.get(LiveIntentRow, connection_id)
        return _row_dict(row) if row else None

    # -- runs / traces --------------------------------------------------
    def add_run(self, run: Dict[str, Any]) -> None:
        with _lock:
            s = self.session
            s.add(RunRow(**{k: v for k, v in run.items()
                            if k in RunRow.__table__.columns.keys()}))
            s.commit()

    def list_runs(self, connection_id: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        stmt = select(RunRow)
        if connection_id:
            stmt = stmt.where(RunRow.connection_id == connection_id)
        stmt = stmt.order_by(RunRow.created_at.desc()).limit(limit)
        return [_row_dict(r) for r in self.session.scalars(stmt)]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.get(RunRow, run_id)
        return _row_dict(row) if row else None

    # -- model providers (non-secret config) ---------------------------
    def upsert_model_provider(self, prov: Dict[str, Any]) -> None:
        with _lock:
            s = self.session
            pid = prov["provider_id"]
            row = s.get(ModelProviderRow, pid)
            if row is None:
                row = ModelProviderRow(provider_id=pid)
                s.add(row)
            for k in ("provider", "base_url", "model", "enabled",
                      "env_secret_var", "note"):
                if k in prov:
                    setattr(row, k, prov[k])
            s.commit()

    def list_model_providers(self) -> List[Dict[str, Any]]:
        return [_row_dict(r) for r in self.session.scalars(
            select(ModelProviderRow))]

    def delete_model_provider(self, provider_id: str) -> bool:
        with _lock:
            s = self.session
            row = s.get(ModelProviderRow, provider_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True


class PortalDB:
    def __init__(self, path: str = PORTAL_DB):
        self._engine = create_engine(f"sqlite:///{path}", future=True)
        with _lock:
            Base.metadata.create_all(self._engine)
        self._session_factory = scoped_session(
            sessionmaker(bind=self._engine, expire_on_commit=False))

    @property
    def session(self) -> Session:
        return self._session_factory()

    def add_user(self, u: Dict[str, Any]) -> bool:
        with _lock:
            s = self.session
            if s.scalar(select(PortalUserRow).where(
                    PortalUserRow.username == u["username"])):
                return False
            s.add(PortalUserRow(**u))
            s.commit()
            return True

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        row = self.session.scalar(select(PortalUserRow).where(
            PortalUserRow.username == username))
        return _row_dict(row) if row else None

    def set_control(self, connection_id: str, requested_on: bool,
                    note: str = "") -> None:
        with _lock:
            s = self.session
            row = s.get(ControlRow, connection_id)
            if row is None:
                row = ControlRow(connection_id=connection_id)
                s.add(row)
            row.requested_on = 1 if requested_on else 0
            if note:
                row.note = note
            s.commit()

    def get_control(self, connection_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.get(ControlRow, connection_id)
        return _row_dict(row) if row else None

    def add_license(self, lic: Dict[str, Any]) -> None:
        """Idempotent license upsert (re-grants update in place)."""
        with _lock:
            s = self.session
            row = s.get(LicenseRow, lic["license_id"])
            if row is None:
                s.add(LicenseRow(**lic))
            else:
                for k in ("owner_id", "tier", "status", "billing_state",
                          "expires_at"):
                    if k in lic:
                        setattr(row, k, lic[k])
            s.commit()

    def get_license(self, owner_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.scalar(select(LicenseRow).where(
            LicenseRow.owner_id == owner_id).order_by(LicenseRow.expires_at.desc()))
        return _row_dict(row) if row else None

    # -- sessions -------------------------------------------------------
    def create_session(self, sess: Dict[str, Any]) -> None:
        with _lock:
            s = self.session
            s.add(SessionRow(**sess))
            s.commit()

    def get_session_by_token(self, token_hash: str) -> Optional[Dict[str, Any]]:
        row = self.session.scalar(select(SessionRow).where(
            SessionRow.token_hash == token_hash))
        return _row_dict(row) if row else None

    def revoke_session(self, session_id: str) -> bool:
        with _lock:
            s = self.session
            row = s.get(SessionRow, session_id)
            if row is None:
                return False
            row.revoked = 1
            s.commit()
            return True

    def list_sessions(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        stmt = select(SessionRow)
        if user_id:
            stmt = stmt.where(SessionRow.user_id == user_id)
        return [_row_dict(r) for r in self.session.scalars(stmt)]


def _row_dict(row) -> Dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


engine_db = EngineDB()
portal_db = PortalDB()
