"""Unit checks using an isolated synthetic EA receipt; never broker orders."""
import json
import threading
import time

import pytest
from abutron.execution.native_file_handoff import NativeFileHandoff


def setup(tmp_path):
    path = tmp_path / "MQL5" / "Files"
    path.mkdir(parents=True)
    bridge = NativeFileHandoff(str(path), connection_id="demo-a", mt5_login=123)
    request = dict(request_id="r1", candidate_id="c1", connection_id="demo-a",
                   mt5_login=123, activation_revision="rev1", signed="test",
                   symbol="SYMBOL", direction="BUY", volume=0.1,
                   trigger_price=11, stop_loss=10, take_profit=13)
    return path, bridge, request


def test_unknown_is_not_retried_or_overwritten(tmp_path):
    path, bridge, request = setup(tmp_path)
    state, proof = bridge.send(request, timeout=0)
    assert state == "UNKNOWN" and proof["reason"] == "NATIVE_RECEIPT_TIMEOUT_NO_RETRY"
    assert json.loads((path / "ABUTRON_REQ_PENDING.json").read_text())["request_id"] == "r1"
    state, proof = bridge.send(dict(request, request_id="r2"), timeout=0)
    assert state == "REJECTED" and proof["reason"] == "NATIVE_PENDING_REQUEST_BUSY"


def test_wrong_identity_or_geometry_never_submitted(tmp_path):
    path, bridge, request = setup(tmp_path)
    for invalid in (dict(request, mt5_login=456), dict(request, signed=""),
                    dict(request, stop_loss=0), dict(request, direction="HOLD")):
        with pytest.raises(ValueError):
            bridge.send(invalid, timeout=0)
    assert not (path / "ABUTRON_REQ_PENDING.json").exists()


def test_native_ea_receipt_account_bound(tmp_path):
    path, bridge, request = setup(tmp_path)
    def emulate_ea():
        pending = path / "ABUTRON_REQ_PENDING.json"
        for _ in range(100):
            if pending.exists():
                assert json.loads(pending.read_text())["request_id"] == "r1"
                (path / "ABUTRON_NATIVE_RECEIPTS_V2_test.txt").write_text(
                    json.dumps(dict(request_id="r1", login=123,
                                    state="ACK", ticket=901, deal="900")))
                pending.unlink()
                return
            time.sleep(0.01)
    thread = threading.Thread(target=emulate_ea)
    thread.start()
    status, proof = bridge.send(request, timeout=2)
    thread.join(timeout=2)
    assert status == "ACK" and proof["ticket"] == 901
