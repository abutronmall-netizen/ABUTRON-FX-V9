"""Non-disruptive V9 activation and session-manager migration checks."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "ops" / "v9-cutover"


def load(name):
    path = ROOT / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def activation(monkeypatch, tmp_path):
    m = load("activation_6207.py")
    crm = tmp_path / "crm.sqlite3"
    crm.touch()
    monkeypatch.setattr(m, "CRM", str(crm))
    row = ["1" * 32, "2" * 32, "12346207", "Deriv-Demo",
           "CONNECTED", "abutron-client-mt5-synthetic", "approved",
           1, 1, 7, 1, 1, "CONNECTED"]
    monkeypatch.setattr(m, "crm_row", lambda db: tuple(row))
    binary = tmp_path / "ApexNativeWriter.ex5"
    binary.write_bytes(b"synthetic-static-qa-ea")
    monkeypatch.setattr(m, "EA", binary)
    monkeypatch.setattr(m, "EA_SHA", hashlib.sha256(binary.read_bytes()).hexdigest())

    def source(path, maximum_age):
        if str(path).endswith("issue38-health.json"):
            return {
                "status": "SIGNED_NATIVE_EXPOSURE_VERIFIED",
                "signature_verified": True,
                "automated_trading_configured": True,
                "native_probe_sha256": m.EA_SHA,
            }
        return {
            "account_suffix": "6207",
            "environment": "DEMO",
            "native_bid_ask_and_ordercalcprofit_verified": True,
        }

    monkeypatch.setattr(m, "recent_json", source)
    monkeypatch.setattr(m.SingleWriterGateway, "read_existing_secret",
                        lambda path: "synthetic-only")
    monkeypatch.setattr(
        m, "signed_native_exposure",
        lambda *args, **kwargs: ({"position_count": 0}, "synthetic-source-sha"))
    monkeypatch.setattr(
        m, "CurrentBrokerOwnerRisk",
        lambda **kwargs: SimpleNamespace(current_owner=lambda cid: {
            "owner_current_native_reconciled": True,
        }))
    return m, row


def test_signed_native_demo_binding_ready(activation):
    module, _ = activation
    result = module.inspect()
    assert result["ready"] is True
    assert result["position_count"] == 0
    assert result["revision"] == 7


def test_client_off_does_not_authorize(activation):
    module, row = activation
    row[7] = 0
    row[10] = 0
    result = module.inspect()
    assert result["ready"] is False
    assert result["reason"] == "CLIENT_REQUESTED_OFF"


def test_broker_disconnected_does_not_authorize(activation):
    module, row = activation
    row[4] = "CONNECTING"
    row[12] = "CONNECTING"
    result = module.inspect()
    assert result["ready"] is False
    assert result["reason"] == "BROKER_CONNECTION_NOT_CURRENT"


def test_mismatched_native_writer_fails(activation):
    module, _ = activation
    module.EA_SHA = "0" * 64
    with pytest.raises(ValueError, match="BROKER_NATIVE_EA_IDENTITY_NOT_AUTHENTICATED"):
        module.inspect()


def test_missing_native_protection_fails(activation, monkeypatch):
    module, _ = activation

    def no_exposure(*args, **kwargs):
        raise ValueError("BROKER_NATIVE_SIGNED_EXPOSURE_MISSING")

    monkeypatch.setattr(module, "signed_native_exposure", no_exposure)
    with pytest.raises(ValueError, match="SIGNED_EXPOSURE_MISSING"):
        module.inspect()


def test_manager_preserves_portal_socket_and_demo_modes():
    module = load("mt5_session_manager.py")
    assert str(module.SOCK) == "/run/abutron-client-mt5-manager/manager.sock"
    assert module.expected_mode("Deriv", "Deriv-Demo") == "DEMO"
    assert module.expected_mode("FP Markets", "FPMarketsSC-Demo") == "DEMO"
    assert module.expected_mode("FP Markets", "FPMarketsSC-Live") == "LIVE"
    assert module.unit("c" * 32) == "abutron-client-mt5-" + "c" * 32


def test_systemd_unit_paths_not_legacy():
    units = ROOT / "systemd"
    assert "/opt/abutron-v9-native-runtime/activation_6207.py" in (
        units / "abutron-v9-native-activation-6207.service").read_text()
    manager = (units / "abutron-v9-mt5-session-manager.service").read_text()
    assert "ExecStart=/usr/bin/python3 -B /opt/abutron-v9-native-runtime/mt5_session_manager.py" in manager
    assert "Conflicts=abutron-client-mt5-manager.service" in manager
    assert "EnvironmentFile=/etc/abutron-v9-native-runtime/manager-identities.env" in manager
    assert "PrivateNetwork=yes" in (
        units / "abutron-v9-native-activation-6207.service").read_text()
