"""Bootstrap UI smoke tests — all three portal surfaces render the rebuilt
design system and bind to their real API endpoints."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def portal(tmp_path, monkeypatch):
    from abutron import db as dbmod
    from abutron.crm import controls as crmmod
    from abutron.events import EventLog
    from abutron.feeds.mt5_terminal import Mt5TerminalManager

    engine_db = dbmod.EngineDB(str(tmp_path / "engine.db"))
    portal_db = dbmod.PortalDB(str(tmp_path / "portal.db"))
    dbmod.engine_db = engine_db
    dbmod.portal_db = portal_db
    crmmod.engine_db = engine_db
    crmmod.portal_db = portal_db
    # Service modules capture engine_db/portal_db at their own import time.
    # Rebind the references everywhere they were already imported so the
    # portal app's services (health, receipts, dispatcher, ops, ...) use
    # the tmp DBs rather than a stale singleton from an earlier test.
    import sys as _sys
    for _mod in list(_sys.modules.values()):
        if _mod is None or not getattr(_mod, "__name__", "").startswith("abutron"):
            continue
        if getattr(_mod, "engine_db", None) is not None and _mod.engine_db is not engine_db:
            try:
                _mod.engine_db = engine_db
            except (AttributeError, TypeError):
                pass
        if getattr(_mod, "portal_db", None) is not None and _mod.portal_db is not portal_db:
            try:
                _mod.portal_db = portal_db
            except (AttributeError, TypeError):
                pass
    # No real terminal in tests.
    monkeypatch.setattr(Mt5TerminalManager, "launch", lambda self, **kw: None)

    from abutron.crm.controls import crm
    crm.engine = engine_db
    crm.portal = portal_db
    crm.create_user(username="demo_user", role="client", password="pw",
                    owner_id="owner_demo")
    crm.create_user(username="admin_user", role="admin", password="pw",
                    owner_id="")
    crm.register_account(connection_id="conn_demo01", broker="deriv",
                         mt5_login=101, owner_id="owner_demo",
                         display_name="Demo FX", environment="demo",
                         server="Deriv-Demo")

    from fastapi.testclient import TestClient
    from abutron.portal.app import create_app
    log = EventLog(str(tmp_path / "journal.jsonl"))
    return TestClient(create_app(log))


def test_client_portal_renders_bootstrap(portal):
    r = portal.get("/client")
    assert r.status_code == 200
    html = r.text
    assert "ABUTRON" in html
    assert "CLIENT WORKSPACE" in html
    assert "Sign in to ABUTRON" in html  # unauthed -> sign-in gate


def test_admin_portal_renders_bootstrap(portal):
    r = portal.get("/admin")
    assert r.status_code == 200
    html = r.text
    assert "ADMIN CRM" in html
    assert "New account" in html
    assert "Entitlement management" in html


def test_command_portal_renders_bootstrap(portal):
    r = portal.get("/command")
    assert r.status_code == 200
    html = r.text
    assert "COMMAND CENTER" in html
    for view in ("Overview", "Accounts", "Analytics", "Model Providers"):
        assert view in html
    assert "Run simulation" in html


def test_design_system_shared_across_platforms(portal):
    """All three surfaces extend the same base design system."""
    for path in ("/client", "/admin", "/command"):
        html = portal.get(path).text
        assert "ABUTRON" in html and "FX V9" in html
        assert "topbar" in html and "brand" in html


def test_client_authed_sees_connect_flow(portal):
    portal.post("/api/client/login", json={"username": "demo_user", "password": "pw"})
    html = portal.get("/client").text
    assert "Connect a trading account" in html
    assert "Your accounts" in html


def test_admin_api_returns_accounts_for_table(portal):
    portal.post("/api/admin/login", json={"username": "admin_user",
                                          "password": "pw"})
    rows = portal.get("/api/admin/accounts").json()
    assert len(rows) == 1
    assert rows[0]["connection_id"] == "conn_demo01"
    assert "conn_status" in rows[0]


def test_admin_api_requires_admin_auth(portal):
    # No session -> 401.
    assert portal.get("/api/admin/accounts").status_code == 401
    # A client session -> still 401 (client cannot reach admin CRM).
    portal.post("/api/client/login", json={"username": "demo_user",
                                           "password": "pw"})
    assert portal.get("/api/admin/accounts").status_code == 401
    # Admin session -> 200.
    portal.post("/api/admin/login", json={"username": "admin_user",
                                          "password": "pw"})
    assert portal.get("/api/admin/accounts").status_code == 200
    # Client login must reject a non-client user.
    r = portal.post("/api/client/login",
                    json={"username": "admin_user", "password": "pw"})
    assert r.status_code == 403


def test_command_endpoints_match_ui_bindings(portal):
    h = portal.get("/api/command/health").json()
    assert h and "overall" in h[0] and "faults" in h[0]
    sk = portal.get("/api/command/selflearn").json()
    assert {"model_updates", "observations", "skill_cells", "execution_authority"} <= set(sk)
    rg = portal.get("/api/command/riskguard").json()
    assert "limits" in rg and "max_risk_per_trade_pct" in rg["limits"]
    an = portal.get("/api/command/analytics").json()
    assert "net_pnl_usd" in an and "win_rate_pct" in an
    sim = portal.post("/api/command/simulate", json={"bars": 20}).json()
    assert {"final_equity", "return_pct", "trades", "rejected"} <= set(sim)
