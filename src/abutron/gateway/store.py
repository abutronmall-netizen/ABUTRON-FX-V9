"""Gateway persistence (SQLite, SQLAlchemy 2.0).

Scope is deliberately narrow: tenant/key metadata, connected-account
metadata, the last account snapshot pushed by each bridge, the trade
command queue, idempotency records, webhook subscriptions/deliveries and
an append-only audit trail. Market data and positions are *not* duplicated
here — the terminal (or the simulator) is the source of truth, and the
snapshot table exists so a restart can serve a last-known state instead of
an empty one.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from sqlalchemy import (Float, Integer, String, Text, create_engine, event,
                        select, update)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM
# ---------------------------------------------------------------------------
class TenantRow(Base):
    __tablename__ = "gw_tenants"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class ApiKeyRow(Base):
    __tablename__ = "gw_api_keys"
    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    prefix: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(96), default="")
    scopes: Mapped[str] = mapped_column(String(128), default="read,trade,manage")
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=600)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    last_used_at: Mapped[str] = mapped_column(String(32), default="")
    revoked_at: Mapped[str] = mapped_column(String(32), default="")


class AccountRow(Base):
    __tablename__ = "gw_accounts"
    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(96), default="")
    broker: Mapped[str] = mapped_column(String(48), default="")
    server: Mapped[str] = mapped_column(String(128), default="")
    login: Mapped[int] = mapped_column(Integer, index=True)
    platform: Mapped[str] = mapped_column(String(16), default="mt5")
    environment: Mapped[str] = mapped_column(String(16), default="demo")
    control_mode: Mapped[str] = mapped_column(String(16), default="api")
    status: Mapped[str] = mapped_column(String(24), default="PROVISIONING")
    trading_enabled: Mapped[int] = mapped_column(Integer, default=1)
    market_data_mode: Mapped[str] = mapped_column(String(16), default="simulated")
    # Set only for accounts mirrored read-only from the APEX engine registry.
    engine_connection_id: Mapped[str] = mapped_column(
        String(64), default="", index=True)
    symbols: Mapped[str] = mapped_column(Text, default="")
    max_lot: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_open_positions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    daily_loss_limit_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    symbol_allowlist: Mapped[str] = mapped_column(Text, default="")
    bridge_salt: Mapped[str] = mapped_column(String(64), default="")
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    state_sequence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")
    last_seen_at: Mapped[str] = mapped_column(String(32), default="")
    last_error: Mapped[str] = mapped_column(String(256), default="")


class TradeCommandRow(Base):
    __tablename__ = "gw_trade_commands"
    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", index=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[str] = mapped_column(String(32), default="")
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)


class IdempotencyRow(Base):
    __tablename__ = "gw_idempotency"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idem_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(96), default="")
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    response_json: Mapped[str] = mapped_column(Text, default="{}")
    command_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class WebhookRow(Base):
    __tablename__ = "gw_webhooks"
    webhook_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(1024))
    events: Mapped[str] = mapped_column(Text, default="*")
    description: Mapped[str] = mapped_column(String(200), default="")
    secret: Mapped[str] = mapped_column(String(128), default="")
    active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(32), default="")


class DeliveryRow(Base):
    __tablename__ = "gw_webhook_deliveries"
    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    webhook_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    last_error: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


class AuditRow(Base):
    __tablename__ = "gw_audit"
    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    key_id: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------
class GatewayStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        if path != ":memory:":
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        self._Session: sessionmaker[Session] = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True)

    def init(self) -> None:
        Base.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        SQLite's CREATE TABLE IF NOT EXISTS does not alter an existing
        table, and deployments may already have a gateway.db from an
        earlier build. Migrations are additive only.
        """
        with self.engine.begin() as conn:
            columns = {row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info(gw_accounts)")}
            if "engine_connection_id" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE gw_accounts ADD COLUMN "
                    "engine_connection_id VARCHAR(64) DEFAULT ''")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_gw_accounts_engine_cid "
                "ON gw_accounts (engine_connection_id)")

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self):
        s = self._Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ---------------- tenants & keys ----------------
    def create_tenant(self, name: str = "") -> str:
        tenant_id = new_id("tnt")
        with self.session() as s:
            s.add(TenantRow(tenant_id=tenant_id, name=name, created_at=utc_now()))
        return tenant_id

    def ensure_tenant(self, tenant_id: str, name: str = "") -> str:
        with self.session() as s:
            row = s.get(TenantRow, tenant_id)
            if row is None:
                s.add(TenantRow(tenant_id=tenant_id, name=name,
                                created_at=utc_now()))
        return tenant_id

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            row = s.get(TenantRow, tenant_id)
            return None if row is None else {
                "tenant_id": row.tenant_id, "name": row.name,
                "status": row.status, "created_at": row.created_at}

    def list_tenants(self) -> List[Dict[str, Any]]:
        with self.session() as s:
            tenants = s.execute(select(TenantRow)).scalars().all()
            keys = s.execute(select(ApiKeyRow)).scalars().all()
            accounts = s.execute(select(AccountRow)).scalars().all()
            out = []
            for tenant in tenants:
                out.append({
                    "tenant_id": tenant.tenant_id, "name": tenant.name,
                    "status": tenant.status, "created_at": tenant.created_at,
                    "accounts": sum(
                        1 for a in accounts
                        if a.tenant_id == tenant.tenant_id
                        and a.status != "DELETED"),
                    "keys": [{
                        "key_id": k.key_id, "prefix": k.prefix,
                        "label": k.label, "revoked": bool(k.revoked_at),
                    } for k in keys if k.tenant_id == tenant.tenant_id],
                })
            return out

    def create_api_key(self, *, tenant_id: str, key_id: str, prefix: str,
                       key_hash: str, label: str, scopes: List[str],
                       rate_limit_per_min: int) -> None:
        with self.session() as s:
            s.add(ApiKeyRow(
                key_id=key_id, tenant_id=tenant_id, prefix=prefix,
                key_hash=key_hash, label=label, scopes=",".join(scopes),
                rate_limit_per_min=rate_limit_per_min, created_at=utc_now()))

    def get_api_key_by_prefix(self, prefix: str) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            row = s.execute(select(ApiKeyRow).where(
                ApiKeyRow.prefix == prefix)).scalar_one_or_none()
            if row is None:
                return None
            return {
                "key_id": row.key_id, "tenant_id": row.tenant_id,
                "prefix": row.prefix, "key_hash": row.key_hash,
                "label": row.label,
                "scopes": [x for x in (row.scopes or "").split(",") if x],
                "rate_limit_per_min": row.rate_limit_per_min,
                "revoked_at": row.revoked_at,
                "created_at": row.created_at}

    def touch_api_key(self, key_id: str) -> None:
        with self.session() as s:
            s.execute(update(ApiKeyRow).where(ApiKeyRow.key_id == key_id)
                      .values(last_used_at=utc_now()))

    # ---------------- accounts ----------------
    def create_account(self, **fields) -> Dict[str, Any]:
        fields.setdefault("created_at", utc_now())
        fields.setdefault("updated_at", utc_now())
        with self.session() as s:
            s.add(AccountRow(**fields))
        return self.get_account_any(fields["connection_id"])  # type: ignore[return-value]

    def get_account_any(self, connection_id: str) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            row = s.get(AccountRow, connection_id)
            return None if row is None else _account_dict(row)

    def get_account(self, tenant_id: str, connection_id: str) -> Optional[Dict[str, Any]]:
        account = self.get_account_any(connection_id)
        if account is None or account["tenant_id"] != tenant_id:
            return None
        return account

    def list_accounts(self, tenant_id: Optional[str] = None,
                      include_deleted: bool = False) -> List[Dict[str, Any]]:
        stmt = select(AccountRow)
        if tenant_id:
            stmt = stmt.where(AccountRow.tenant_id == tenant_id)
        if not include_deleted:
            stmt = stmt.where(AccountRow.status != "DELETED")
        stmt = stmt.order_by(AccountRow.created_at)
        with self.session() as s:
            return [_account_dict(r) for r in s.execute(stmt).scalars()]

    def update_account(self, connection_id: str, **fields) -> Optional[Dict[str, Any]]:
        fields["updated_at"] = utc_now()
        with self.session() as s:
            s.execute(update(AccountRow)
                      .where(AccountRow.connection_id == connection_id)
                      .values(**fields))
        return self.get_account_any(connection_id)

    def save_state(self, connection_id: str, state: Dict[str, Any],
                   sequence: int, *, status: Optional[str] = None,
                   last_error: str = "") -> None:
        fields: Dict[str, Any] = {
            "state_json": json.dumps(state, sort_keys=True, default=str),
            "state_sequence": sequence,
            "last_seen_at": utc_now(),
            "updated_at": utc_now(),
        }
        if status:
            fields["status"] = status
        if last_error:
            fields["last_error"] = last_error[:256]
        with self.session() as s:
            s.execute(update(AccountRow)
                      .where(AccountRow.connection_id == connection_id)
                      .values(**fields))

    def delete_account(self, connection_id: str) -> None:
        with self.session() as s:
            s.execute(update(AccountRow)
                      .where(AccountRow.connection_id == connection_id)
                      .values(status="DELETED", trading_enabled=0,
                              updated_at=utc_now()))

    def get_account_by_engine_id(self, engine_connection_id: str
                                 ) -> Optional[Dict[str, Any]]:
        stmt = select(AccountRow).where(
            AccountRow.engine_connection_id == engine_connection_id,
            AccountRow.status != "DELETED")
        with self.session() as s:
            row = s.execute(stmt).scalars().first()
            return None if row is None else _account_dict(row)

    def count_accounts(self, tenant_id: str) -> int:
        with self.session() as s:
            return len(s.execute(select(AccountRow.connection_id).where(
                AccountRow.tenant_id == tenant_id,
                AccountRow.status != "DELETED")).all())

    # ---------------- trade commands ----------------
    def create_command(self, *, tenant_id: str, connection_id: str, kind: str,
                       payload: Dict[str, Any], ttl_s: float) -> Dict[str, Any]:
        command_id = new_id("cmd")
        now = time.time()
        with self.session() as s:
            s.add(TradeCommandRow(
                command_id=command_id, tenant_id=tenant_id,
                connection_id=connection_id, kind=kind,
                payload_json=json.dumps(payload, default=str),
                status="QUEUED", created_at=utc_now(), updated_at=utc_now(),
                expires_at=now + ttl_s))
        return self.get_command(connection_id, command_id)  # type: ignore[return-value]

    def get_command(self, connection_id: str, command_id: str) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            row = s.get(TradeCommandRow, command_id)
            if row is None or row.connection_id != connection_id:
                return None
            return _command_dict(row)

    def pull_commands(self, connection_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Return QUEUED commands and mark them SENT (at-most-once delivery).

        At-most-once is intentional: re-delivering a market order after a
        bridge reconnect could duplicate a real broker position. A bridge
        that dies mid-command leaves the row SENT, and the API surfaces
        that as an unknown outcome for reconciliation.
        """
        now = time.time()
        out: List[Dict[str, Any]] = []
        with self.session() as s:
            rows = s.execute(
                select(TradeCommandRow).where(
                    TradeCommandRow.connection_id == connection_id,
                    TradeCommandRow.status == "QUEUED",
                    TradeCommandRow.expires_at > now,
                ).order_by(TradeCommandRow.created_at).limit(limit)
            ).scalars().all()
            for row in rows:
                row.status = "SENT"
                row.updated_at = utc_now()
                out.append(_command_dict(row))
        return out

    def complete_command(self, connection_id: str, command_id: str,
                         status: str, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            row = s.get(TradeCommandRow, command_id)
            if row is None or row.connection_id != connection_id:
                return None
            if row.status in ("DONE", "REJECTED", "FAILED"):
                return _command_dict(row)  # idempotent completion
            row.status = status
            row.result_json = json.dumps(result, default=str)
            row.updated_at = utc_now()
        return self.get_command(connection_id, command_id)

    def expire_commands(self) -> int:
        now = time.time()
        with self.session() as s:
            rows = s.execute(select(TradeCommandRow).where(
                TradeCommandRow.status.in_(("QUEUED", "SENT")),
                TradeCommandRow.expires_at <= now)).scalars().all()
            for row in rows:
                row.status = "TIMEOUT"
                row.updated_at = utc_now()
                row.result_json = json.dumps({"error": "COMMAND_TIMEOUT"})
            return len(rows)

    # ---------------- idempotency ----------------
    def get_idempotency(self, tenant_id: str, key: str) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            row = s.get(IdempotencyRow, (tenant_id, key))
            if row is None:
                return None
            return {
                "tenant_id": row.tenant_id, "key": row.idem_key,
                "endpoint": row.endpoint, "request_hash": row.request_hash,
                "status_code": row.status_code,
                "response": json.loads(row.response_json or "{}"),
                "command_id": row.command_id}

    def put_idempotency(self, *, tenant_id: str, key: str, endpoint: str,
                        request_hash: str, status_code: int,
                        response: Dict[str, Any], command_id: str = "") -> None:
        try:
            with self.session() as s:
                s.add(IdempotencyRow(
                    tenant_id=tenant_id, idem_key=key, endpoint=endpoint,
                    request_hash=request_hash, status_code=status_code,
                    response_json=json.dumps(response, default=str),
                    command_id=command_id, created_at=utc_now()))
        except IntegrityError:
            # A concurrent duplicate won the race; keeping the first answer
            # is the correct idempotent behaviour.
            pass

    # ---------------- webhooks ----------------
    def create_webhook(self, *, tenant_id: str, url: str, events: List[str],
                       description: str, secret: str) -> Dict[str, Any]:
        webhook_id = new_id("whk")
        with self.session() as s:
            s.add(WebhookRow(
                webhook_id=webhook_id, tenant_id=tenant_id, url=url,
                events=",".join(events), description=description,
                secret=secret, created_at=utc_now()))
        return self.get_webhook(tenant_id, webhook_id)  # type: ignore[return-value]

    def get_webhook(self, tenant_id: str, webhook_id: str) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            row = s.get(WebhookRow, webhook_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _webhook_dict(row)

    def list_webhooks(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(select(WebhookRow).where(
                WebhookRow.tenant_id == tenant_id)).scalars().all()
            return [_webhook_dict(r) for r in rows]

    def webhooks_for_event(self, tenant_id: str, event_type: str) -> List[Dict[str, Any]]:
        out = []
        for hook in self.list_webhooks(tenant_id):
            if not hook["active"]:
                continue
            events = hook["events"]
            if "*" in events or event_type in events:
                out.append(hook)
        return out

    def delete_webhook(self, tenant_id: str, webhook_id: str) -> bool:
        with self.session() as s:
            row = s.get(WebhookRow, webhook_id)
            if row is None or row.tenant_id != tenant_id:
                return False
            row.active = 0
            return True

    def create_delivery(self, *, webhook_id: str, tenant_id: str,
                        event_type: str, payload: Dict[str, Any],
                        next_attempt_at: float) -> str:
        delivery_id = new_id("dlv")
        with self.session() as s:
            s.add(DeliveryRow(
                delivery_id=delivery_id, webhook_id=webhook_id,
                tenant_id=tenant_id, event_type=event_type,
                payload_json=json.dumps(payload, default=str),
                next_attempt_at=next_attempt_at, created_at=utc_now()))
        return delivery_id

    def due_deliveries(self, limit: int = 20) -> List[Dict[str, Any]]:
        now = time.time()
        with self.session() as s:
            rows = s.execute(select(DeliveryRow).where(
                DeliveryRow.status == "PENDING",
                DeliveryRow.next_attempt_at <= now,
            ).order_by(DeliveryRow.next_attempt_at).limit(limit)).scalars().all()
            return [{
                "delivery_id": r.delivery_id, "webhook_id": r.webhook_id,
                "tenant_id": r.tenant_id, "event_type": r.event_type,
                "payload": json.loads(r.payload_json or "{}"),
                "attempts": r.attempts, "status": r.status,
                "last_error": r.last_error} for r in rows]

    def finish_delivery(self, delivery_id: str, *, status: str,
                        attempts: int, next_attempt_at: Optional[float] = None,
                        last_error: str = "") -> None:
        with self.session() as s:
            row = s.get(DeliveryRow, delivery_id)
            if row is None:
                return
            row.status = status
            row.attempts = attempts
            row.last_error = (last_error or "")[:512]
            if next_attempt_at is not None:
                row.next_attempt_at = next_attempt_at

    def list_deliveries(self, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(select(DeliveryRow).where(
                DeliveryRow.tenant_id == tenant_id).order_by(
                    DeliveryRow.created_at.desc()).limit(limit)).scalars().all()
            return [{
                "delivery_id": r.delivery_id, "event_type": r.event_type,
                "status": r.status, "attempts": r.attempts,
                "last_error": r.last_error, "created_at": r.created_at}
                for r in rows]

    # ---------------- audit ----------------
    def audit(self, *, tenant_id: str, key_id: str, action: str,
              account_id: str = "", detail: Optional[Dict[str, Any]] = None,
              ip: str = "") -> str:
        audit_id = new_id("aud")
        with self.session() as s:
            s.add(AuditRow(
                audit_id=audit_id, tenant_id=tenant_id, key_id=key_id,
                action=action, account_id=account_id,
                detail_json=json.dumps(detail or {}, default=str),
                ip=ip, created_at=utc_now()))
        return audit_id

    def list_audit(self, tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(select(AuditRow).where(
                AuditRow.tenant_id == tenant_id).order_by(
                    AuditRow.created_at.desc()).limit(limit)).scalars().all()
            return [{
                "audit_id": r.audit_id, "action": r.action,
                "account_id": r.account_id, "key_id": r.key_id,
                "detail": json.loads(r.detail_json or "{}"),
                "ip": r.ip, "created_at": r.created_at} for r in rows]


# ---------------------------------------------------------------------------
# Row -> dict
# ---------------------------------------------------------------------------
def _csv(value: str) -> List[str]:
    return [x for x in (value or "").split(",") if x]


def _account_dict(row: AccountRow) -> Dict[str, Any]:
    return {
        "connection_id": row.connection_id, "tenant_id": row.tenant_id,
        "name": row.name, "broker": row.broker, "server": row.server,
        "login": row.login, "platform": row.platform,
        "environment": row.environment, "control_mode": row.control_mode,
        "status": row.status, "trading_enabled": bool(row.trading_enabled),
        "market_data_mode": row.market_data_mode,
        "engine_connection_id": row.engine_connection_id or "",
        "symbols": _csv(row.symbols),
        "max_lot": row.max_lot,
        "max_open_positions": row.max_open_positions,
        "daily_loss_limit_usd": row.daily_loss_limit_usd,
        "symbol_allowlist": _csv(row.symbol_allowlist) or None,
        "bridge_salt": row.bridge_salt,
        "state_json": row.state_json, "state_sequence": row.state_sequence,
        "created_at": row.created_at, "updated_at": row.updated_at,
        "last_seen_at": row.last_seen_at, "last_error": row.last_error}


def _command_dict(row: TradeCommandRow) -> Dict[str, Any]:
    return {
        "command_id": row.command_id, "connection_id": row.connection_id,
        "tenant_id": row.tenant_id, "kind": row.kind,
        "payload": json.loads(row.payload_json or "{}"),
        "status": row.status, "result": json.loads(row.result_json or "{}"),
        "created_at": row.created_at, "updated_at": row.updated_at}


def _webhook_dict(row: WebhookRow) -> Dict[str, Any]:
    return {
        "webhook_id": row.webhook_id, "tenant_id": row.tenant_id,
        "url": row.url, "events": _csv(row.events),
        "description": row.description, "secret": row.secret,
        "active": bool(row.active), "created_at": row.created_at}
