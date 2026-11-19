"""Issue #38: DEMO-only native writer staging and authenticated MT5 source binding.

All fixtures are temporary directories and synthetic broker data. No real
terminal, credentials, chart attachment or orders are used.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

import pytest

from abutron.crm.controls import CrmError, crm
from abutron.crypto import build_exposure_canonical, hmac_sign
from abutron.exposure.publisher import publish_exposure
from abutron.feeds.mt5_terminal import Mt5Error, Mt5TerminalManager


QA_SECRET = "ABUTRON_TEST_ONLY_DO_NOT_USE_ON_REAL_TERMINALS_2026"
LOGIN = 7410512
CID = "issue38-synthetic-demo-0512"
SERVER = "Synthetic-Demo"


def terminal_dir(tmp_path):
    root = tmp_path / "DEMO_TerminalData"
    (root / "MQL5" / "Files").mkdir(parents=True)
    (root / "MQL5" / "Experts").mkdir()
    return root


def qa_bin(tmp_path):
    ex5 = tmp_path / "ApexNativeWriter.ex5"
    ex5.write_bytes(b"\x4d\x5a" + b"synthetic-QA-EX5-NOT_EXECUTABLE")
    digest = hashlib.sha256(ex5.read_bytes()).hexdigest()
    return ex5, digest


def manager(tmp_path):
    return Mt5TerminalManager(root=str(tmp_path / "isolated-manager"))


def stage(mgr, d, ea, digest, **kw):
    defaults = dict(connection_id=CID, broker="fpmarkets", login=LOGIN,
                    server=SERVER, environment="demo",
                    data_dir=str(d), compiled_ex5=str(ea),
                    expected_sha256=digest)
    defaults.update(kw)
    return mgr.stage_demo_writer_qa(**defaults)


def test_demo_qa_stages_pinned_ex5_only_in_experts(tmp_path):
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    files = d / "MQL5" / "Files"
    original = set(files.iterdir())
    staged = Path(stage(mgr, d, ea, digest))
    assert staged.relative_to(d).as_posix() == \
        "MQL5/Experts/ABUTRON-PR37-QA/ApexNativeWriter.ex5"
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == digest
    assert set(files.iterdir()) == original, "must not inject order requests or secrets"
    assert not (files / "ABUTRON_REQ_PENDING.json").exists()
    assert not (files / "ABUTRON_ACTIVATION_SECRET.txt").exists()
    assert not (d / "MQL5" / "Profiles").exists()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o640
    assert stage(mgr, d, ea, digest) == str(staged)  # idempotent


@pytest.mark.parametrize("environment", ["live", "LIVE"])
def test_live_account_never_stages_writer(tmp_path, environment):
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    with pytest.raises(Mt5Error, match="DEMO-only"):
        stage(manager(tmp_path), d, ea, digest, environment=environment)
    assert not list((d / "MQL5" / "Experts").rglob("*.ex5"))


def test_stage_refuses_wrong_hash(tmp_path):
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    with pytest.raises(Mt5Error, match="pin mismatch"):
        stage(manager(tmp_path), d, ea, "0" * 64)
    assert not list((d / "MQL5" / "Experts").rglob("*.ex5"))


def test_stage_refuses_overwriting_different_qa_binary(tmp_path):
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    staged = Path(stage(mgr, d, ea, digest))
    ea.write_bytes(b"MZother-SYNTHETIC-binary")
    new_digest = hashlib.sha256(ea.read_bytes()).hexdigest()
    with pytest.raises(Mt5Error, match="refuse overwrite"):
        stage(mgr, d, ea, new_digest)
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == digest


def test_stage_allow_replace_backs_up_prior_binary(tmp_path):
    """allow_replace=True backs up the current binary before replacing."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    staged = Path(stage(mgr, d, ea, digest))
    old_digest = digest
    # New binary, different content.
    ea.write_bytes(b"MZnew-SYNTHETIC-binary-v2")
    new_digest = hashlib.sha256(ea.read_bytes()).hexdigest()
    assert new_digest != old_digest
    staged2 = Path(stage(mgr, d, ea, new_digest, allow_replace=True))
    assert hashlib.sha256(staged2.read_bytes()).hexdigest() == new_digest
    # A timestamped backup of the prior binary must exist.
    backup_dir = d / "MQL5" / "Experts" / "ABUTRON-PR37-QA" / ".backup"
    backups = list(backup_dir.glob("ApexNativeWriter.ex5.*.bak"))
    assert backups, "prior binary must be backed up before replace"
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == old_digest


def test_rollback_restored_last_backup(tmp_path):
    """rollback_demo_writer_qa restores the most recent backup."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    staged = Path(stage(mgr, d, ea, digest))
    old_digest = digest
    ea.write_bytes(b"MZnew-SYNTHETIC-binary-v2")
    new_digest = hashlib.sha256(ea.read_bytes()).hexdigest()
    stage(mgr, d, ea, new_digest, allow_replace=True)
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == new_digest
    restored = Path(mgr.rollback_demo_writer_qa(data_dir=str(d)))
    assert restored == staged
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == old_digest


def test_rollback_fails_closed_when_no_backup(tmp_path):
    """No backup present -> rollback raises (fail-closed)."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    stage(mgr, d, ea, digest)
    with pytest.raises(Mt5Error, match="no backup"):
        mgr.rollback_demo_writer_qa(data_dir=str(d))


def test_stage_rejects_symlinked_backup_dir(tmp_path):
    """A symlinked .backup directory is rejected (no write outside staging)."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    stage(mgr, d, ea, digest)
    # First replacement creates the real .backup directory.
    ea.write_bytes(b"MZnew-SYNTHETIC-binary-v2")
    new_digest = hashlib.sha256(ea.read_bytes()).hexdigest()
    stage(mgr, d, ea, new_digest, allow_replace=True)
    # Replace .backup with a symlink pointing outside the staging dir.
    backup_dir = d / "MQL5" / "Experts" / "ABUTRON-PR37-QA" / ".backup"
    outside = tmp_path / "outside_backup"
    outside.mkdir()
    for f in backup_dir.iterdir():
        f.unlink()
    backup_dir.rmdir()
    backup_dir.symlink_to(outside, target_is_directory=True)
    # A further replacement must reject the symlinked backup dir.
    ea.write_bytes(b"MZnew-SYNTHETIC-binary-v3")
    new_digest2 = hashlib.sha256(ea.read_bytes()).hexdigest()
    with pytest.raises(Mt5Error, match="cannot be a symlink"):
        stage(mgr, d, ea, new_digest2, allow_replace=True)
    assert not list(outside.iterdir()), "must not write through symlink"


def test_rollback_rejects_symlinked_backup_file(tmp_path):
    """A symlinked .bak file is rejected; rollback fails closed."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    stage(mgr, d, ea, digest)
    ea.write_bytes(b"MZnew-SYNTHETIC-binary-v2")
    new_digest = hashlib.sha256(ea.read_bytes()).hexdigest()
    stage(mgr, d, ea, new_digest, allow_replace=True)
    # Replace the backup with a symlink to an unrelated file.
    backup_dir = d / "MQL5" / "Experts" / "ABUTRON-PR37-QA" / ".backup"
    bak = next(backup_dir.glob("*.bak"))
    elsewhere = tmp_path / "elsewhere.bin"
    elsewhere.write_bytes(b"EVIL")
    bak.unlink()
    bak.symlink_to(elsewhere)
    with pytest.raises(Mt5Error, match="symlink"):
        mgr.rollback_demo_writer_qa(data_dir=str(d))


def test_rollback_rejects_corrupt_backup(tmp_path):
    """A backup whose content does not match its SHA name is rejected."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    stage(mgr, d, ea, digest)
    ea.write_bytes(b"MZnew-SYNTHETIC-binary-v2")
    new_digest = hashlib.sha256(ea.read_bytes()).hexdigest()
    stage(mgr, d, ea, new_digest, allow_replace=True)
    # Tamper with the backup content so it no longer matches its name.
    backup_dir = d / "MQL5" / "Experts" / "ABUTRON-PR37-QA" / ".backup"
    bak = next(backup_dir.glob("*.bak"))
    bak.write_bytes(b"CORRUPTED_BYTES")
    with pytest.raises(Mt5Error, match="does not match"):
        mgr.rollback_demo_writer_qa(data_dir=str(d))


def test_backup_names_are_content_sha_bound_no_collision(tmp_path):
    """Repeated replacements produce distinct content-SHA-bound backup names."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    stage(mgr, d, ea, digest)
    backup_dir = d / "MQL5" / "Experts" / "ABUTRON-PR37-QA" / ".backup"
    # Two distinct replacements -> two distinct backup names.
    for i in range(2):
        ea.write_bytes(f"MZversion-{i}-SYNTHETIC".encode())
        dig = hashlib.sha256(ea.read_bytes()).hexdigest()
        stage(mgr, d, ea, dig, allow_replace=True)
    backups = sorted(backup_dir.glob("*.bak"))
    assert len(backups) == 2, "each distinct content gets its own backup name"
    names = {b.name for b in backups}
    assert len(names) == 2, "backup names must be unique per content"



def test_rollback_rejects_unrelated_sha_named_file(tmp_path):
    """SHA validity alone must not authorize an unrelated file as QA backup."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    staged = Path(stage(mgr, d, ea, digest))
    expected_old = staged.read_bytes()
    ea.write_bytes(b"MZnew-QA-version")
    stage(mgr, d, ea, hashlib.sha256(ea.read_bytes()).hexdigest(),
          allow_replace=True)
    bdir = staged.parent / ".backup"
    unrelated = b"MZunrelated-not-a-prior-writer"
    bad_sha = hashlib.sha256(unrelated).hexdigest()
    fake = bdir / f"NOT_APEX_WRITER.{bad_sha}.bak"
    fake.write_bytes(unrelated)
    with pytest.raises(Mt5Error, match="not an exact QA transaction"):
        mgr.rollback_demo_writer_qa(data_dir=str(d))
    assert staged.read_bytes() != unrelated
    fake.unlink()
    assert Path(mgr.rollback_demo_writer_qa(data_dir=str(d))).read_bytes() == expected_old


def test_rollback_repeated_contents_restores_immediate_predecessor(tmp_path):
    """A -> B -> A -> C MUST restore A, not B by older file mtime."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    versions = [ea.read_bytes(), b"MZversion-B-QA", ea.read_bytes(),
                b"MZversion-C-QA"]
    staged = Path(stage(mgr, d, ea, digest))
    for new_bytes in versions[1:]:
        ea.write_bytes(new_bytes)
        stage(mgr, d, ea, hashlib.sha256(new_bytes).hexdigest(),
              allow_replace=True)
    backup_dir = staged.parent / ".backup"
    assert len(list(backup_dir.glob("*.bak"))) == 3
    restored = Path(mgr.rollback_demo_writer_qa(data_dir=str(d)))
    assert restored.read_bytes() == versions[2]
    assert restored.read_bytes() != versions[1]


def test_rollback_rejects_current_binary_with_no_committed_transition(tmp_path):
    """An unknown current digest must never select unrelated old backups."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    staged = Path(stage(mgr, d, ea, digest))
    ea.write_bytes(b"MZnext-QA-binary")
    stage(mgr, d, ea, hashlib.sha256(ea.read_bytes()).hexdigest(),
          allow_replace=True)
    staged.write_bytes(b"MZunknown-current-not-from-staging")
    with pytest.raises(Mt5Error, match="no committed rollback"):
        mgr.rollback_demo_writer_qa(data_dir=str(d))
    assert staged.read_bytes() == b"MZunknown-current-not-from-staging"


def test_rollback_ignores_uncommitted_backup_even_with_matching_target(tmp_path):
    """An interrupted deployment's pending backup cannot be treated as history."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    staged = Path(stage(mgr, d, ea, digest))
    first = staged.read_bytes()
    ea.write_bytes(b"MZcommitted-new-QA")
    new_sha = hashlib.sha256(ea.read_bytes()).hexdigest()
    stage(mgr, d, ea, new_sha, allow_replace=True)
    bdir = staged.parent / ".backup"
    fake_old = b"MZuncommitted-not-current-old"
    import time as time_module
    pending = bdir / (
        f"ApexNativeWriter.ex5.{time_module.time_ns()}.1234567890abcdef."
        f"{hashlib.sha256(fake_old).hexdigest()}.{new_sha}.bak")
    pending.write_bytes(fake_old)
    assert not (bdir / (pending.name + ".committed")).exists()
    assert Path(mgr.rollback_demo_writer_qa(data_dir=str(d))).read_bytes() == first


def test_failed_commit_rolls_back_without_silently_accepting_new_binary(
        tmp_path, monkeypatch):
    """Failure to persist the transaction marker restores prior bytes."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    staged = Path(stage(mgr, d, ea, digest))
    original = staged.read_bytes()
    ea.write_bytes(b"MZnew-uncommitted-QA")
    new_sha = hashlib.sha256(ea.read_bytes()).hexdigest()

    def fail_commit(*args):
        raise OSError("synthetic journal fsync failure")

    monkeypatch.setattr(mgr, "_commit_backup", fail_commit)
    with pytest.raises(OSError, match="journal fsync failure"):
        stage(mgr, d, ea, new_sha, allow_replace=True)
    assert staged.read_bytes() == original
    assert not list((staged.parent / ".backup").glob("*.committed"))


def test_rollback_rejects_symlinked_commit_marker(tmp_path):
    """A substituted commit marker is not authentic proof of a transition."""
    d = terminal_dir(tmp_path)
    ea, digest = qa_bin(tmp_path)
    mgr = manager(tmp_path)
    staged = Path(stage(mgr, d, ea, digest))
    ea.write_bytes(b"MZnew-stage-with-marker")
    stage(mgr, d, ea, hashlib.sha256(ea.read_bytes()).hexdigest(),
          allow_replace=True)
    marker = next((staged.parent / ".backup").glob("*.committed"))
    other = tmp_path / "external-marker"
    other.write_bytes(marker.read_bytes())
    marker.unlink()
    marker.symlink_to(other)
    with pytest.raises(Mt5Error, match="marker invalid"):
        mgr.rollback_demo_writer_qa(data_dir=str(d))


@pytest.mark.parametrize("missing", ["Files", "Experts"])
def test_missing_mt5_data_folder_is_not_created(tmp_path, missing):
    d = terminal_dir(tmp_path)
    (d / "MQL5" / missing).rmdir()
    ea, digest = qa_bin(tmp_path)
    with pytest.raises(Mt5Error, match="must exist"):
        stage(manager(tmp_path), d, ea, digest)
    assert not (d / "MQL5" / missing).exists()


def test_symlinked_experts_path_rejected(tmp_path):
    d = terminal_dir(tmp_path)
    experts = d / "MQL5" / "Experts"
    experts.rmdir()
    actual = tmp_path / "somewhere_else"
    actual.mkdir()
    experts.symlink_to(actual, target_is_directory=True)
    ea, digest = qa_bin(tmp_path)
    with pytest.raises(Mt5Error, match="must exist"):
        stage(manager(tmp_path), d, ea, digest)
    assert not list(actual.iterdir())


def test_unverified_process_liveness_not_broker_connected(tmp_path):
    mgr = manager(tmp_path)
    from abutron.feeds.mt5_terminal import Terminal

    class Alive:
        def poll(self):
            return None

    mgr._terminals[CID] = Terminal(
        CID, "fpmarkets", LOGIN, SERVER, "fake-prefix", "/tmp/FAKE", proc=Alive()
    )
    assert mgr.health(CID)["status"] == "PROCESS_RUNNING_UNVERIFIED"
    with pytest.raises(Mt5Error):
        mgr.mark_native_authenticated(CID, {"native_positions_verified": False})


class MemoryDB:
    def __init__(self):
        self.account = {
            "connection_id": CID, "owner_id": "synthetic-owner",
            "broker": "fpmarkets", "mt5_login": LOGIN, "server": SERVER,
            "equity": 0, "native_positions_verified": 0, "environment": "demo",
        }

    def get_account(self, connection_id):
        return dict(self.account) if connection_id == CID else None

    def upsert_account(self, account):
        self.account.update(account)

    def recent_open_positions(self, connection_id):
        return []


def signed_flat_snapshot():
    nonce = f"ABUTRON_NATIVE_{LOGIN}_{int(time.time())}"
    result = {
        "nonce": nonce,
        "challenge": "synthetic-challenge",
        "connection_id": CID,
        "login": str(LOGIN),
        "server": SERVER,
        "equity": 10000.00,
        "balance": 10000.00,
        "currency": "USD",
        "position_count": 0,
        "positions": [],
    }
    result["sig"] = hmac_sign(
        QA_SECRET, build_exposure_canonical(
            CID, str(LOGIN), 10000.0, nonce, [],
            server=SERVER, currency="USD", balance=10000.0,
            challenge="synthetic-challenge",
        ),
    )
    return result


def test_external_demo_source_starts_unverified_then_signed_exposure(tmp_path, monkeypatch):
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", QA_SECRET)
    db = MemoryDB()
    monkeypatch.setattr(crm, "engine", db)
    root = terminal_dir(tmp_path)
    mgr = manager(tmp_path)
    term = mgr.bind_demo_data_source(
        connection_id=CID, broker="fpmarkets", login=LOGIN,
        server=SERVER, data_dir=str(root), environment="demo",
    )
    assert term.external_evidence_only
    assert term.proc is None, "binding must not launch a competing MT5 terminal"
    assert mgr.health(CID)["status"] == "SOURCE_BOUND_UNVERIFIED"
    path = root / "MQL5" / "Files" / "ABUTRON_NATIVE_EXPOSURE_V2_SYNTHETIC.txt"
    path.write_text(json.dumps(signed_flat_snapshot()))
    account = publish_exposure(CID, manager=mgr)
    assert account["native_positions_verified"] == 1
    mgr.mark_native_authenticated(CID, account)
    assert mgr.health(CID)["status"] == "CONNECTED"
    mgr.invalidate_native_authentication(CID)
    crm.invalidate_exposure(CID)
    assert mgr.health(CID)["status"] == "SOURCE_BOUND_UNVERIFIED"
    assert db.account["native_positions_verified"] == 0


def test_wrong_broker_server_cannot_authenticate_bound_source(tmp_path, monkeypatch):
    monkeypatch.setenv("ABUTRON_ACTIVATION_SECRET", QA_SECRET)
    db = MemoryDB()
    monkeypatch.setattr(crm, "engine", db)
    root = terminal_dir(tmp_path)
    mgr = manager(tmp_path)
    mgr.bind_demo_data_source(
        connection_id=CID, broker="fpmarkets", login=LOGIN,
        server="NotTheRegisteredServer", data_dir=str(root), environment="demo",
    )
    path = root / "MQL5" / "Files" / "ABUTRON_NATIVE_EXPOSURE_V2_SYNTHETIC.txt"
    path.write_text(json.dumps(signed_flat_snapshot()))
    account = publish_exposure(CID, manager=mgr)
    with pytest.raises(Mt5Error, match="server"):
        mgr.mark_native_authenticated(CID, account)
    assert mgr.health(CID)["status"] == "SOURCE_BOUND_UNVERIFIED"


def test_missing_evidence_not_interpreted_as_flat_broker(tmp_path):
    root = terminal_dir(tmp_path)
    mgr = manager(tmp_path)
    mgr.bind_demo_data_source(
        connection_id=CID, broker="fpmarkets", login=LOGIN,
        server=SERVER, data_dir=str(root), environment="demo",
    )
    assert mgr.read_native_exposure(CID) is None
    assert mgr.health(CID)["status"] == "SOURCE_BOUND_UNVERIFIED"


def test_untrusted_symlink_native_export_refused(tmp_path):
    root = terminal_dir(tmp_path)
    mgr = manager(tmp_path)
    mgr.bind_demo_data_source(
        connection_id=CID, broker="fpmarkets", login=LOGIN,
        server=SERVER, data_dir=str(root), environment="demo",
    )
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text(json.dumps(signed_flat_snapshot()))
    (root / "MQL5" / "Files" / "ABUTRON_NATIVE_EXPOSURE_V2_FAKE.txt").symlink_to(elsewhere)
    assert mgr.read_native_exposure(CID) is None


def test_bind_live_environment_refused(tmp_path):
    root = terminal_dir(tmp_path)
    mgr = manager(tmp_path)
    with pytest.raises(Mt5Error, match="DEMO-only"):
        mgr.bind_demo_data_source(
            connection_id=CID, broker="fpmarkets", login=LOGIN,
            server=SERVER, data_dir=str(root), environment="live",
        )


def test_compiled_writer_exposure_only_is_default_before_order_send():
    source = (Path(__file__).resolve().parents[1] /
              "mql5" / "ApexNativeWriter.mq5").read_text()
    assert "input bool    InpExposureOnly   = true;" in source
    assert "if(!InpExposureOnly)" in source
    assert "ProcessOrderRequests();" in source

def test_native_hmac_known_answer_and_utf8_export():
    source = (Path(__file__).resolve().parents[1] /
              "mql5" / "ApexNativeWriter.mq5").read_text()
    assert "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8" in source
    assert "FileWriteString(h, content)" in source
    assert "FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8" in source
    assert 'H[i] & 0xFF' in source
    assert "ACCOUNT_TRADE_MODE_DEMO" in source
    fragment = source.split("string expJson = StringFormat(", 1)[1].split(
        "expJson += posJson", 1)[0]
    assert (chr(92) * 3 + '"') not in fragment
    assert fragment.count(chr(92) + '"') >= 20


def test_generated_probe_has_no_order_authority():
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from generate_issue38_exposure_probe import make_probe
    generated = make_probe((root / "mql5" / "ApexNativeWriter.mq5").read_text())
    assert "OrderSend(" not in generated
    assert "ProcessOrderRequests(" not in generated
    assert "TRADE_ACTION_DEAL" not in generated
    assert "ABUTRON_REQ_PENDING" not in generated
    assert "WriteReceipt(" not in generated
    assert "void ExportNativeData()" in generated
    assert "FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8" in generated
    assert "ACCOUNT_TRADE_MODE_DEMO" in generated

def test_native_hmac_padding_explicit_zeroes():
    source = (Path(__file__).resolve().parents[1] /
              "mql5" / "ApexNativeWriter.mq5").read_text()
    assert "for(int i = inputLen + 1; i < totalLen - 8; i++)" in source
    assert "bytes[i] = 0;" in source
    assert "ISSUE38_INIT_BLOCK_HMAC_KAT" in source
    assert "ISSUE38_INIT_BLOCK_SECRET_NOT_PROVISIONED" in source
    assert "ISSUE38_INIT_EXPOSURE_QA_READY_NO_ORDERS" in source
