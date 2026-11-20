"""Current broker exposure is execution evidence; lifetime audit is telemetry."""
import datetime as dt
import json
import sqlite3

import pytest

from abutron.risk.owner_native_authority import (
    CurrentBrokerOwnerRisk, NativeOwnerRiskError)


CONNECTION = "f" * 32


def sources(tmp_path):
    crm = tmp_path / "crm.sqlite3"
    conn = sqlite3.connect(crm)
    conn.execute("""CREATE TABLE apex_mt5_connections
        (id TEXT, account_id TEXT, broker TEXT, environment TEXT,
         account_reference TEXT, status TEXT, worker_binding TEXT)""")
    for cid,broker,environment,login in [
        (CONNECTION,"Deriv","DEMO","32456207"),
        ("a"*32,"FP Markets","DEMO","7410512"),
        ("b"*32,"FP Markets","LIVE","86079392")]:
        conn.execute("INSERT INTO apex_mt5_connections VALUES (?,?,?,?,?,?,?)",
                     (cid,"owner-1",broker,environment,login,"CONNECTED","worker"))
    conn.commit()
    conn.close()
    connections = []
    for broker,environment,suffix,equity,risk,count in [
        ("Deriv","DEMO","6207",9306.74,0,0),
        ("FP Markets","DEMO","0512",101490.7,165.62,2),
        ("FP Markets","LIVE","9392",0,0,0)]:
        connections.append({
            "account_masked":"XXXX"+suffix,
            "broker":broker,"environment":environment,
            "snapshot":{"equity_usd":equity,"positions_count":count,
                        "pending_orders_count":0}})
    report = {
        "checked_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
        "native_current_open_exposure_reconciled":True,
        "unknown_connected_position_sets":0,
        "status":"BLOCKED_INCOMPLETE_EXPOSURE",
        "historical_audit_status":"INCOMPLETE_BROKER_LIFETIME_EQUITY_ACK_PROOF",
        "risk_caps_policy":{"aggregate_open_pct":2.0,"max_drawdown_pct":20.0},
        "owners":{"owner-hash":{"current_open_exposure_reconciled":True,
                 "unknown_connected_broker_position_sets":0,
                 "disconnected_or_error_accounts":0,
                 "current_open_stop_risk_usd":165.62,
                 "connections":connections}}}
    path = tmp_path / "native-report.json"
    path.write_text(json.dumps(report))
    authority = CurrentBrokerOwnerRisk(
        crm_db=str(crm), native_risk_report=str(path),
        require_root_owned=False, max_age_seconds=90)
    return authority, path, report


def test_current_native_authority_ignores_unrelated_lifetime_audit(tmp_path):
    authority,path,report = sources(tmp_path)
    result=authority.current_owner(CONNECTION)
    assert result["owner_current_native_reconciled"] is True
    assert result["owner_risk_certified"] is True
    assert result["connection_count"]==3
    assert result["aggregate_stop_risk_usd"]==165.62
    assert result["cap_pct"]==0.02
    assert result["cap_exceeded"] is False


@pytest.mark.parametrize("modification", [
    lambda j:j.update(unknown_connected_position_sets=1),
    lambda j:j.update(native_current_open_exposure_reconciled=False),
    lambda j:j["owners"]["owner-hash"]["connections"].pop(),
    lambda j:j["owners"]["owner-hash"]["connections"][0]["snapshot"].update(
        pending_orders_count=1),
    lambda j:j["owners"]["owner-hash"].update(
        current_open_exposure_reconciled=False),
    lambda j:j["owners"]["owner-hash"]["connections"][1]["snapshot"].update(
        equity_usd=-1),
    lambda j:j.update(checked_utc="2020-01-01T00:00:00+00:00"),
])
def test_missing_or_stale_current_native_risk_rejected(tmp_path,modification):
    authority,path,report=sources(tmp_path)
    modification(report)
    path.write_text(json.dumps(report))
    with pytest.raises(NativeOwnerRiskError):
        authority.current_owner(CONNECTION)


def test_live_connection_never_authorized_by_demo_provider(tmp_path):
    authority,_,_=sources(tmp_path)
    with pytest.raises(NativeOwnerRiskError):
        authority.current_owner("b"*32)
