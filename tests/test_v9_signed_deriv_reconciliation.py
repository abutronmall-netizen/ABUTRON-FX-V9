"""Synthetic protection-source matching tests; no trading or CRM writes."""
from __future__ import annotations
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "ops/v9-cutover/native_deriv_reconciliation.py"

def source():
    spec = importlib.util.spec_from_file_location("v9_deriv_reconcile_test", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture
def broker_native(monkeypatch):
    import abutron.reconciliation.native_crm_6207 as crm
    import abutron.execution.writer_gateway as gw
    p = {
        "ticket": 88441235, "symbol": "Volatility 50 Index.0",
        "direction": "BUY", "volume": 25.5, "entry": 80.25,
        "sl": 80.10, "tp": 80.55, "stop_risk_usd": 3.75,
        "ordercalcprofit_verified": True,
    }
    payload = {"position_count": 1, "positions": [p]}
    monkeypatch.setattr(gw.SingleWriterGateway, "read_existing_secret",
                        lambda _: "synthetic-unit-test-only")
    monkeypatch.setattr(crm, "signed_native_exposure",
                        lambda *args, **kwargs: (payload, "synthetic-sha"))
    return p, payload

def invoke(m, **overrides):
    args = dict(ticket="88441235", symbol="Volatility 50 Index.0",
                side="0", lots=25.5, entry=80.25, sl=80.10, tp=80.55,
                positions=1)
    args.update(overrides)
    return m.v9_signed_protection("a" * 32, "12346207", "Deriv-Demo", **args)

def test_exact_broker_signed_position_matches(broker_native):
    assert invoke(source()) == 3.75

def test_wrong_broker_ticket_rejected(broker_native):
    with pytest.raises(ValueError, match="POSITION_MISMATCH"):
        invoke(source(), ticket="99999111")

def test_broker_position_count_disagreement_rejected(broker_native):
    with pytest.raises(ValueError, match="COUNT_DISAGREEMENT"):
        invoke(source(), positions=2)

def test_native_stops_must_match(broker_native):
    with pytest.raises(ValueError, match="POSITION_MISMATCH"):
        invoke(source(), sl=80.09)

def test_ordercalcprofit_verification_required(broker_native):
    p,_ = broker_native
    p["ordercalcprofit_verified"] = False
    with pytest.raises(ValueError, match="POSITION_MISMATCH"):
        invoke(source())
