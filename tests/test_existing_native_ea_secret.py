"""Authenticated already-attached native EA: use its file secret unchanged."""
import hashlib
import hmac
import os
import pytest

from abutron.events import EventLog
from abutron.execution.writer_gateway import (
    SingleWriterGateway, _canonical_order)


def setup_native(tmp_path):
    folder = tmp_path / "MQL5" / "Files"
    folder.mkdir(parents=True, mode=0o700)
    folder.chmod(0o700)
    key = folder / "ABUTRON_ACTIVATION_SECRET.txt"
    key.write_text("local-test-secret-123456789abcdef\n")
    key.chmod(0o600)
    ea = tmp_path / "native.ex5"
    ea.write_bytes(b"synthetic-qa-binary")
    sha = hashlib.sha256(ea.read_bytes()).hexdigest()
    gw = SingleWriterGateway(EventLog(str(tmp_path / "log.jsonl")))
    return folder, key, ea, sha, gw


def test_existing_secret_arms_without_replacing_or_exposing(monkeypatch, tmp_path):
    folder, key, ea, sha, gw = setup_native(tmp_path)
    monkeypatch.delenv("ABUTRON_ACTIVATION_SECRET", raising=False)
    original = key.read_bytes()
    state = gw.arm(connection_id="demo01", ea_path=str(ea),
                   expected_sha256=sha, activation_revision="rev1",
                   broker="Deriv", mql5_files_dir=str(folder),
                   existing_secret=True)
    assert state.armed and state.revision_ok
    assert key.read_bytes() == original
    captured = {}
    def fake_deliver(request, timeout):
        captured.update(request)
        return "UNKNOWN", None
    gw._deliver_to_ea = fake_deliver
    candidate = dict(candidate_id="candidate-1", symbol="EURUSD",
                     direction="BUY", volume=0.01, stop_loss=1.1,
                     take_profit=1.3, price=1.2)
    result = gw.send_order(connection_id="demo01",
                           candidate=candidate,
                           account={"mt5_login":123}, broker="Deriv")
    assert result["outcome"] == "UNKNOWN"
    canonical = _canonical_order(captured["request_id"],
                                 candidate,"demo01")
    expected = hmac.new(original.strip(), canonical.encode(),
                        hashlib.sha256).hexdigest()
    assert captured["signed"] == expected
    assert "local-test-secret" not in (tmp_path / "log.jsonl").read_text()


def test_insecure_secret_permissions_disarm(tmp_path):
    folder, key, ea, sha, gw = setup_native(tmp_path)
    key.chmod(0o644)
    state = gw.arm(connection_id="demo01", ea_path=str(ea),
                   expected_sha256=sha, activation_revision="rev1",
                   broker="Deriv", mql5_files_dir=str(folder),
                   existing_secret=True)
    assert not state.armed


def test_secret_symlink_rejected(tmp_path):
    folder, key, ea, sha, gw = setup_native(tmp_path)
    key.unlink()
    another = tmp_path / "outside.txt"
    another.write_text("local-test-secret-123456789abcdef")
    another.chmod(0o600)
    key.symlink_to(another)
    with pytest.raises((OSError, ValueError)):
        gw.read_existing_secret(str(folder))
