"""Independent, synthetic read-only contract tests for Issue #21 auditor."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "scripts/fp_issue21_receipt_auditor.py"


def module():
    spec=importlib.util.spec_from_file_location("issue21_readonly_test",SRC)
    m=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_missing_connection_binding_is_rejected(monkeypatch):
    m=module()
    monkeypatch.setattr(m,"ACCOUNT_ID","")
    monkeypatch.setattr(m,"EXPECTED_LOGIN","")
    with pytest.raises(RuntimeError, match="ACCOUNT_BOUND_CONNECTION_ID_MISSING"):
        m._validate_account_binding()


def test_live_mode_server_never_accepted(monkeypatch):
    m=module()
    monkeypatch.setattr(m,"ACCOUNT_ID","a"*32)
    monkeypatch.setattr(m,"EXPECTED_LOGIN","12345")  # synthetic fixture
    monkeypatch.setattr(m,"EXPECTED_SERVER","FPMarketsSC-Live")
    with pytest.raises(RuntimeError,match="BROKER_SERVER_NOT_FP_DEMO"):
        m._validate_account_binding()


def test_receipt_bad_name_fails_closed(tmp_path):
    m=module()
    (tmp_path/"unexpected.txt").write_text("non-native")
    with pytest.raises(ValueError,match="INVALID_RECEIPT_FILENAME"):
        m.list_receipt_names(tmp_path)


def test_auditor_rejects_symlink_native_receipt(tmp_path):
    m=module()
    target=tmp_path/"target";target.write_text("receipt")
    link=tmp_path/("a"*64+".txt");link.symlink_to(target)
    with pytest.raises(ValueError,match="NONREGULAR_INPUT"):
        m.list_receipt_names(tmp_path)


def test_systemd_unit_isolation():
    unit=(ROOT/"deploy/abutron-fp0512-issue21-receipt-audit.service").read_text()
    for literal in ("User=root","ProtectSystem=strict","NoNewPrivileges=yes",
                    "RestrictAddressFamilies=AF_UNIX",
                    "EnvironmentFile=/etc/abutron/fp-issue21-auditor.env"):
        assert literal in unit
    assert "ABUTRON_LIVE_TRADING_ENABLED=true" not in unit
    assert "ReadWritePaths=/var/lib/abutron-fp0512-issue21-scanner" in unit


def test_timer_bound_to_readonly_service():
    timer=(ROOT/"deploy/abutron-fp0512-issue21-receipt-audit.timer").read_text()
    assert "Unit=abutron-fp0512-issue21-receipt-audit.service" in timer
    assert "OnUnitInactiveSec=15s" in timer


def test_no_order_submitter_in_auditor():
    src=SRC.read_text()
    assert "OrderSend(" not in src
    assert "can_send_orders" in src
    assert 'EXPECTED_MODE = "DEMO"' in src