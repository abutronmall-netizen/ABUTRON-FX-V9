"""Multi-user per-client broker connect flow (blueprint C01–C16, C25).

Covers session auth, tenant isolation, the Connect Trading Account lifecycle
(seven statuses), DEMO/LIVE gating, and fail-closed error surfacing.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from abutron.crm.controls import CrmError


@pytest.fixture
def env(tmp_path, monkeypatch):
    from abutron import db as dbmod
    from abutron.crm import controls as crmmod
    from abutron.crm import sessions as sessions_mod
    from abutron.events import EventLog

    engine_db = dbmod.EngineDB(str(tmp_path / "engine.db"))
    portal_db = dbmod.PortalDB(str(tmp_path / "portal.db"))
    dbmod.engine_db = engine_db
    dbmod.portal_db = portal_db
    crmmod.engine_db = engine_db
    crmmod.portal_db = portal_db
    sessions_mod.portal_db = portal_db
    return {"engine": engine_db, "portal": portal_db,
            "log": EventLog(dbmod.engine_db)}


@pytest.fixture
def crm_svc(env):
    # CrmService captures engine/portal at construction; rebind the
    # instance to the tmp DBs the `env` fixture installed.
    from abutron.crm.controls import crm
    crm.engine = env["engine"]
    crm.portal = env["portal"]
    return crm


@pytest.fixture
def client(env, monkeypatch):
    # Avoid launching a real Wine/MT5 terminal in tests — a clean launch
    # yields the CONNECTED lifecycle status.
    from abutron.feeds.mt5_terminal import Mt5TerminalManager
    monkeypatch.setattr(Mt5TerminalManager, "launch",
                        lambda self, **kw: None, raising=True)
    from fastapi.testclient import TestClient
    from abutron.portal.app import create_app
    app = create_app(env["log"])
    return TestClient(app)


def _seed_user(crm_svc, username, owner_id, password="secret", role="client"):
    user = crm_svc.create_user(username=username, role=role,
                               password=password, owner_id=owner_id)
    return user


# -- auth -----------------------------------------------------------------

def test_login_and_me(client, crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    r = client.post("/api/client/login",
                    json={"username": "alice", "password": "pw123"})
    assert r.status_code == 200
    assert r.json()["owner_id"] == "owner_a"
    assert "abutron_session" in r.cookies

    me = client.get("/api/client/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


def test_login_bad_credentials(client, crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    r = client.post("/api/client/login",
                    json={"username": "alice", "password": "wrong"})
    assert r.status_code == 401
    assert "CREDENTIAL_INVALID" in r.json()["detail"]


def test_client_endpoints_require_auth(client):
    assert client.get("/api/client/accounts").status_code == 401
    assert client.get("/api/client/me").status_code == 401


# -- connect lifecycle ----------------------------------------------------

def test_connect_demo_account_enrolls(client, crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    client.post("/api/client/login", json={"username": "alice", "password": "pw123"})

    # Enroll only — no password accepted at this step.
    r = client.post("/api/client/connect-account", json={
        "broker": "deriv", "environment": "demo", "server": "Deriv-Demo",
        "mt5_login": 123456})
    assert r.status_code == 200
    body = r.json()
    assert body["conn_status"] == "CONNECTING"
    assert body["broker"] == "deriv"
    assert body["mt5_login"] == 123456
    assert "password" not in body
    assert body["broker_authenticated"] is False

    status = client.get(f"/api/client/accounts/{body['connection_id']}/status")
    assert status.status_code == 200
    assert status.json()["conn_status"] == "CONNECTING"


def test_connect_requires_auth(client, crm_svc):
    r = client.post("/api/client/connect-account", json={
        "broker": "deriv", "environment": "demo", "server": "Deriv-Demo",
        "mt5_login": 1})
    assert r.status_code == 401


def test_connect_invalid_broker(client, crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    client.post("/api/client/login", json={"username": "alice", "password": "pw123"})
    r = client.post("/api/client/connect-account", json={
        "broker": "bogus", "environment": "demo", "server": "S",
        "mt5_login": 1})
    assert r.status_code == 400
    assert "unsupported broker" in r.json()["detail"]


def test_connect_duplicate_account(client, crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    client.post("/api/client/login", json={"username": "alice", "password": "pw123"})
    body = {"broker": "deriv", "environment": "demo", "server": "Deriv-Demo",
            "mt5_login": 999}
    assert client.post("/api/client/connect-account", json=body).status_code == 200
    r = client.post("/api/client/connect-account", json=body)
    assert r.status_code == 400
    assert "DUPLICATE_ACCOUNT_OWNER" in r.json()["detail"]


def test_live_disabled_by_default(client, crm_svc, monkeypatch):
    """LIVE must remain opt-in until the owner enables it explicitly."""
    from abutron import config as cfgmod
    from abutron.crm import controls as crmmod
    monkeypatch.setattr(cfgmod, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(crmmod, "LIVE_TRADING_ENABLED", False)
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    client.post("/api/client/login", json={"username": "alice", "password": "pw123"})
    r = client.post("/api/client/connect-account", json={
        "broker": "deriv", "environment": "live", "server": "Deriv-Live",
        "mt5_login": 555})
    assert r.status_code == 400
    assert "LIVE_NOT_APPROVED" in r.json()["detail"]


# -- tenant isolation -----------------------------------------------------

def test_tenant_isolation(client, crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    _seed_user(crm_svc, "bob", "owner_b", "pw456")
    client.post("/api/client/login", json={"username": "alice", "password": "pw123"})
    conn = client.post("/api/client/connect-account", json={
        "broker": "deriv", "environment": "demo", "server": "Deriv-Demo",
        "mt5_login": 777}).json()

    # Alice sees her account; Bob (new session) does not.
    assert client.get("/api/client/accounts").status_code == 200
    alice_ids = [a["connection_id"] for a in client.get("/api/client/accounts").json()]
    assert conn["connection_id"] in alice_ids

    client.post("/api/client/logout")
    client.post("/api/client/login", json={"username": "bob", "password": "pw456"})
    bob_ids = [a["connection_id"] for a in client.get("/api/client/accounts").json()]
    assert conn["connection_id"] not in bob_ids

    # Bob cannot reach Alice's connection by ID (no ID guessing).
    r = client.get(f"/api/client/accounts/{conn['connection_id']}/status")
    assert r.status_code == 404
    r = client.post(f"/api/client/accounts/{conn['connection_id']}/on")
    assert r.status_code == 404


def test_verify_sets_connected_with_worker_binding(crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    status = crm_svc.start_broker_link(
        owner_id="owner_a", broker="deriv", environment="demo",
        server="Deriv-Demo", mt5_login=4242)
    assert status["conn_status"] == "CONNECTING"
    assert status["broker_authenticated"] is False

    # A successful native identity proof sets a worker binding + CONNECTED.
    class _OkVerifier:
        def verify(self, **kw):
            return True, "NONE"

    verified = crm_svc.verify_broker_identity(
        status["connection_id"], password="brkpw", verifier=_OkVerifier())
    assert verified["conn_status"] == "CONNECTED"
    assert verified["broker_authenticated"] is True
    assert verified["worker_binding"]

    # CONNECTED but trading still OFF until a worker flips effective_on.
    assert verified["effective_on"] is False


def test_verify_failure_sets_error_without_leak(crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    status = crm_svc.start_broker_link(
        owner_id="owner_a", broker="deriv", environment="demo",
        server="Deriv-Demo", mt5_login=4242)

    class _BadVerifier:
        def verify(self, **kw):
            return False, "BROKER_AUTH_REJECTED"

    with pytest.raises(CrmError, match="BROKER_AUTH_NOT_PROVEN"):
        crm_svc.verify_broker_identity(
            status["connection_id"], password="brkpw", verifier=_BadVerifier())

    acct = crm_svc.get_account(status["connection_id"])
    assert acct["conn_status"] == "ERROR"
    assert "BROKER_AUTH_REJECTED" in acct["conn_error"]
    # The password must never be persisted on the account.
    assert "brkpw" not in str(acct)


def test_enroll_is_rate_limited(crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    for i in range(crm_svc.MAX_LINK_ATTEMPTS):
        crm_svc.start_broker_link(
            owner_id="owner_a", broker="deriv", environment="demo",
            server="Deriv-Demo", mt5_login=1000 + i)
    with pytest.raises(CrmError, match="LINK_RATE_LIMITED"):
        crm_svc.start_broker_link(
            owner_id="owner_a", broker="deriv", environment="demo",
            server="Deriv-Demo", mt5_login=9999)


class _OkVerifier:
    def verify(self, **kw):
        return True, "NONE"


def _connect_demo(crm_svc, owner="owner_a", login=500):
    _seed_user(crm_svc, "alice", owner, "pw123")
    s = crm_svc.start_broker_link(
        owner_id=owner, broker="deriv", environment="demo",
        server="Deriv-Demo", mt5_login=login)
    crm_svc.grant_license(license_id=f"lic_{login}", owner_id=owner,
                          tier="standard")
    crm_svc.verify_broker_identity(s["connection_id"], password="x",
                                   verifier=_OkVerifier())
    return s["connection_id"]


def test_demo_on_grants_immediately(crm_svc):
    cid = _connect_demo(crm_svc)
    state = crm_svc.request_on(cid, by="client")
    # Aggressive mode: DEMO ON grants trading immediately.
    assert state["desired_on"] is True
    assert state["effective_on"] is True


def test_demo_on_requires_connected(crm_svc):
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    s = crm_svc.start_broker_link(
        owner_id="owner_a", broker="deriv", environment="demo",
        server="Deriv-Demo", mt5_login=601)
    crm_svc.grant_license(license_id="lic_601", owner_id="owner_a",
                          tier="standard")
    # Not verified/connected -> ON request denied.
    with pytest.raises(CrmError, match="NOT_CONNECTED"):
        crm_svc.request_on(s["connection_id"], by="client")


def test_live_on_grants_immediately(crm_svc):
    """Aggressive mode: a LIVE ON request grants trading controls ON
    immediately — no separate intent bridge required."""
    from abutron import config as cfgmod
    from abutron.crm import controls as crmmod
    old_cfg = cfgmod.LIVE_TRADING_ENABLED
    old_ctrl = crmmod.LIVE_TRADING_ENABLED
    cfgmod.LIVE_TRADING_ENABLED = True
    crmmod.LIVE_TRADING_ENABLED = True
    try:
        _seed_user(crm_svc, "alice", "owner_a", "pw123")
        crm_svc.grant_license(license_id="lic_live", owner_id="owner_a",
                              tier="standard")
        s = crm_svc.start_broker_link(
            owner_id="owner_a", broker="deriv", environment="live",
            server="Deriv-Live", mt5_login=700)
        # Mark connected manually (LIVE identity is a separate broker path).
        acct = crm_svc.get_account(s["connection_id"])
        acct["conn_status"] = "CONNECTED"
        acct["worker_binding"] = "wb_live"
        crm_svc.engine.upsert_account(acct)
        state = crm_svc.request_on(s["connection_id"], by="client")
        # LIVE grants order permission immediately.
        assert state["desired_on"] is True
        assert state["effective_on"] is True
    finally:
        cfgmod.LIVE_TRADING_ENABLED = old_cfg
        crmmod.LIVE_TRADING_ENABLED = old_ctrl


def test_live_connect_launches_terminal_and_connects(client, crm_svc, monkeypatch):
    """Explicit opt-in permits LIVE linking; it still does not trade."""
    from abutron import config as cfgmod
    from abutron.crm import controls as crmmod
    monkeypatch.setattr(cfgmod, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(crmmod, "LIVE_TRADING_ENABLED", True)
    _seed_user(crm_svc, "alice", "owner_a", "pw123")
    client.post("/api/client/login",
                json={"username": "alice", "password": "pw123"})
    r = client.post("/api/client/connect-account", json={
        "broker": "deriv", "environment": "live", "server": "Deriv-Live",
        "mt5_login": 99001})
    assert r.status_code == 200
    body = r.json()
    assert body["environment"] == "live"
    # terminal launch (mocked) succeeded -> live session CONNECTED
    assert body["conn_status"] == "CONNECTED"
    assert body["broker_authenticated"] is True
    assert body["worker_binding"]
