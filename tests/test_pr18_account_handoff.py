"""PR18 account-scoped file handoff — no MT5, no broker orders."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from abutron.crypto import hmac_sign
from abutron.events import EventLog
from abutron.execution.writer_gateway import (
    SingleWriterGateway, _canonical_order,
)


def _fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", "private-test-fixture-only")
    folder = tmp_path / "MQL5" / "Files"
    folder.mkdir(parents=True)
    ea = tmp_path / "writer.ex5"
    ea.write_bytes(b"fixtures-not-native-writer")
    gw = SingleWriterGateway(EventLog(str(tmp_path / "events.jsonl")))
    cid = "0123456789abcdef0123456789abcdef"
    status = gw.arm(
        connection_id=cid,
        ea_path=str(ea),
        expected_sha256=hashlib.sha256(ea.read_bytes()).hexdigest(),
        activation_revision="revision-42", broker="fpmarkets",
        mql5_files_dir=str(folder))
    assert status.armed
    return gw, folder, cid


def test_gateway_file_handoff_simulated_native_receipt(tmp_path, monkeypatch):
    gw, folder, cid = _fixture(tmp_path, monkeypatch)
    captured = []

    def simulated_native():
        pending = folder / "ABUTRON_REQ_PENDING.json"
        for _ in range(150):
            if pending.exists():
                break
            time.sleep(.02)
        else:
            return
        data = json.loads(pending.read_text())
        captured.append(data)
        assert data["connection_id"] == cid
        assert data["mt5_login"] == 7410512
        assert isinstance(int(data["expires_at"]), int)
        cand = {k: data[k] for k in (
            "candidate_id", "symbol", "direction", "volume",
            "stop_loss", "take_profit", "entry_type",
            "trigger_price", "trail_k", "trail_stop")}
        expected = hmac_sign(
            "private-test-fixture-only",
            _canonical_order(data["request_id"], cand, cid,
                             expires_at=data["expires_at"]))
        assert expected == data["signed"]
        receipt = {
            "state": "ACK", "request_id": data["request_id"],
            "login": 7410512, "ticket": 4051, "deal": "7159",
            "price": 1.0850, "volume": 0.01, "sl": 1.0840,
            "tp": 1.0880,
        }
        name = "ABUTRON_NATIVE_RECEIPT_" + data["request_id"] + ".json"
        (folder / name).write_text(json.dumps(receipt))
        pending.unlink()

    thread = threading.Thread(target=simulated_native)
    thread.start()
    cand = {
        "candidate_id": "demo-candidate-0512",
        "symbol": "EURUSD.r", "direction": "BUY", "volume": 0.01,
        "stop_loss": 1.084, "take_profit": 1.088,
        "entry_type": "CONFIRM", "trigger_price": 1.085,
    }
    res = gw.send_order(connection_id=cid, candidate=cand,
                        account={"mt5_login": 7410512},
                        broker="fpmarkets", request_timeout=3)
    thread.join(4)
    assert not thread.is_alive()
    assert len(captured) == 1
    assert res["outcome"] == "ACK"
    assert res["receipt_ref"]["ticket"] == 4051


def test_missing_native_receipt_stays_unknown(tmp_path, monkeypatch):
    gw, folder, cid = _fixture(tmp_path, monkeypatch)
    req = {"request_id": "ABUTRON_REQ_TEST_TIMOUT_00",
           "connection_id": cid, "mt5_login": 7410512, "volume": 0.01}
    result, details = gw._deliver_to_ea(req, .2)
    assert result == "UNKNOWN"
    assert details["reason"] == "BROKER_RECEIPT_TIMEOUT"
    assert (folder / "ABUTRON_REQ_PENDING.json").exists()
    # A second call must not overwrite unresolved broker intent.
    second, data = gw._deliver_to_ea(
        {**req, "request_id": "ABUTRON_REQ_DIFFERENT_01"}, .1)
    assert second == "REJECTED"
    assert data["reason"] == "UNRECONCILED_NATIVE_HANDOFF"


def test_wrong_account_receipt_never_acked(tmp_path, monkeypatch):
    gw, folder, cid = _fixture(tmp_path, monkeypatch)
    req = {"request_id": "ABUTRON_REQ_TEST_WRONG_01",
           "connection_id": cid, "mt5_login": 7410512, "volume": 0.01}
    receipt = folder / ("ABUTRON_NATIVE_RECEIPT_" +
                        req["request_id"] + ".json")

    def malicious_native():
        pending = folder / "ABUTRON_REQ_PENDING.json"
        for _ in range(100):
            if pending.exists():
                break
            time.sleep(.01)
        receipt.write_text(json.dumps({
            "request_id": req["request_id"],
            "login": 7411636, "ticket": 100, "deal": "200",
            "state": "ACK", "price": 1.0, "volume": .01
        }))
        pending.unlink()

    t = threading.Thread(target=malicious_native)
    t.start()
    status, details = gw._deliver_to_ea(req, 2)
    t.join(4)
    assert status == "UNKNOWN"
    assert details["reason"] == "RECEIPT_ACCOUNT_MISMATCH"


def test_expiry_bound_into_hmac(monkeypatch):
    cand = {
        "candidate_id": "c", "symbol": "EURUSD.r", "direction": "SELL",
        "volume": 0.01, "stop_loss": 1.09, "take_profit": 1.08,
    }
    a = _canonical_order("ABUTRON_REQ_X", cand, "conn",
                         expires_at="1800000000")
    b = _canonical_order("ABUTRON_REQ_X", cand, "conn",
                         expires_at="1800000001")
    assert a != b
    assert hmac_sign("random-test-key", a) != hmac_sign("random-test-key", b)


def test_mql5_is_fail_closed_for_ambiguous_accounts_and_replay():
    source = (Path(__file__).resolve().parents[1] /
              "mql5/ApexNativeWriter.mq5").read_text()
    assert "SafeRequestId(reqId)" in source
    assert "FileIsExist(RequestReceiptFile(reqId))" in source
    assert "SIGNED_REQUEST_EXPIRED" in source
    assert "NATIVE_ACCOUNT_IDENTITY_MISMATCH" in source
    assert "ABUTRON_NATIVE_RECEIPT_" in source
    assert "TimeGMT()" in source
