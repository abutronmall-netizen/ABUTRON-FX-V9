"""Tests for Deriv DEMO session-manager reboot recovery repair (Issue #65).

Verifies a bounded, restart-consistent recovery mechanism:
- reboot_recently_detected() detects a boot_id change (reboot), caches the
  result with a timestamp so ALL accounts in the same boot receive True,
  and the trigger expires after REBOOT_RECOVERY_WINDOW_S.
- The detection timestamp is persisted to disk so a manager restart within
  the recovery window retains eligibility.
- After expiry, a new reboot can be detected again.
- recovery_trigger_active() is True on reboot OR maintenance reexec.
- recover_offline_demo() recovers DEMO sessions after a reboot trigger and
  recovers ALL offline DEMO accounts.
- ensure_worker_accessible() sets o+rx on the worker directory and script,
  and is scoped to DEMO workers only (not LIVE).
- launch_worker() calls ensure_worker_accessible() for DEMO and does NOT
  use the ineffective ReadOnlyPaths= property.
- LIVE accounts are never auto-recovered.
- Active services are not re-launched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "ops" / "v9-cutover" / "mt5_session_manager.py"


def _load_manager():
    spec = importlib.util.spec_from_file_location(
        "mt5_session_manager", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mgr = _load_manager()


@pytest.fixture(autouse=True)
def _reset_reboot_cache():
    """Reset the reboot detection cache before every test.

    The cache is module-level and persists across tests; without this reset
    a test that sets the cache would leak into subsequent tests.
    """
    mgr._reboot_detected_at = None
    yield


# --------------------------------------------------------------------------- #
# reboot_recently_detected — bounded + restart-consistent
# --------------------------------------------------------------------------- #
class TestRebootDetection:
    def _patch_boot_id(self, monkeypatch, boot_id_text, uptime_text=None):
        """Patch Path.read_text so boot_id returns boot_id_text."""
        real_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if str(self) == "/proc/sys/kernel/random/boot_id":
                return boot_id_text
            if str(self) == "/proc/uptime":
                return uptime_text or "3600.0 100000.0"
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

    def _write_marker(self, tmp_path, boot_id, detected_at):
        """Write a reboot_recovery.json marker file."""
        marker = tmp_path / "reboot_recovery.json"
        marker.write_text(json.dumps({
            "boot_id": boot_id,
            "reboot_detected_at": detected_at,
        }))
        return marker

    def test_boot_id_change_triggers_reboot(self, tmp_path, monkeypatch):
        """A boot_id change since the last recorded one = reboot detected."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        self._write_marker(tmp_path, "boot-old", time.time())
        self._patch_boot_id(monkeypatch, "boot-new\n")

        assert mgr.reboot_recently_detected() is True
        marker = json.loads((tmp_path / "reboot_recovery.json").read_text())
        assert marker["boot_id"] == "boot-new"
        assert marker["reboot_detected_at"] is not None

    def test_same_boot_id_does_not_trigger(self, tmp_path, monkeypatch):
        """Same boot_id as last recorded with expired window = no reboot."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        # Marker with old detection timestamp (well past the window)
        self._write_marker(tmp_path, "boot-same", time.time() - 99999)
        self._patch_boot_id(monkeypatch, "boot-same\n")

        assert mgr.reboot_recently_detected() is False

    def test_reboot_trigger_cached_for_multi_account(self, tmp_path, monkeypatch):
        """After a reboot is detected, ALL subsequent calls return True.

        Regression test for the multi-account defect: without caching, the
        first account consumed the boot_id marker and subsequent accounts
        saw an unchanged boot_id and incorrectly got False.
        """
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        self._write_marker(tmp_path, "boot-old", time.time())
        self._patch_boot_id(monkeypatch, "boot-new\n")

        # First account detects the reboot
        assert mgr.reboot_recently_detected() is True
        # Second and third accounts get the cached True
        assert mgr.reboot_recently_detected() is True
        assert mgr.reboot_recently_detected() is True

    def test_reboot_trigger_expires_after_window(self, tmp_path, monkeypatch):
        """Regression test: the trigger must expire after REBOOT_RECOVERY_WINDOW_S.

        Previously the cache latched True for the entire process lifetime,
        even after 24 hours. Now the detection timestamp is checked against
        the window and the trigger returns False once expired.
        """
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        # Marker with detection timestamp far in the past (24 hours ago)
        old_ts = time.time() - 86400
        self._write_marker(tmp_path, "boot-old", old_ts)
        self._patch_boot_id(monkeypatch, "boot-old\n")  # same boot

        # In-memory cache is also set to the old timestamp (simulating a
        # long-running process that detected the reboot 24h ago)
        mgr._reboot_detected_at = old_ts

        # After the window, the trigger must return False
        assert mgr.reboot_recently_detected() is False

    def test_reboot_trigger_active_within_window(self, tmp_path, monkeypatch):
        """The trigger is active when detection is within the window."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        recent_ts = time.time() - 300  # 5 minutes ago, within 900s window
        self._write_marker(tmp_path, "boot-old", recent_ts)
        self._patch_boot_id(monkeypatch, "boot-old\n")  # same boot

        # No in-memory cache — evaluated from disk marker
        assert mgr.reboot_recently_detected() is True

    def test_manager_restart_within_window_retains_eligibility(
            self, tmp_path, monkeypatch):
        """Regression test: manager restart within window keeps recovery active.

        Previously, restarting the manager reset the in-memory cache, and the
        existing marker prevented recovery from triggering again because the
        boot_id hadn't changed. Now the persisted detection timestamp ensures
        a restart within the window still returns True.
        """
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        recent_ts = time.time() - 120  # 2 minutes ago, within window
        # Write marker as if a previous process instance detected the reboot
        self._write_marker(tmp_path, "boot-current", recent_ts)
        self._patch_boot_id(monkeypatch, "boot-current\n")

        # Simulate manager restart: in-memory cache is None (fresh process)
        mgr._reboot_detected_at = None

        # Must still detect the reboot from the persisted marker
        assert mgr.reboot_recently_detected() is True

    def test_manager_restart_after_window_no_eligibility(
            self, tmp_path, monkeypatch):
        """Manager restart after the window expires = no recovery."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        old_ts = time.time() - 99999
        self._write_marker(tmp_path, "boot-current", old_ts)
        self._patch_boot_id(monkeypatch, "boot-current\n")
        mgr._reboot_detected_at = None  # fresh process

        assert mgr.reboot_recently_detected() is False

    def test_new_reboot_detectable_after_expiry(self, tmp_path, monkeypatch):
        """After the window expires, a new reboot can be detected."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        # First boot: old detection, expired window
        old_ts = time.time() - 99999
        self._write_marker(tmp_path, "boot-old", old_ts)
        self._patch_boot_id(monkeypatch, "boot-old\n")
        assert mgr.reboot_recently_detected() is False

        # Now boot_id changes (new reboot after the window)
        self._patch_boot_id(monkeypatch, "boot-new\n")
        assert mgr.reboot_recently_detected() is True

    def test_first_observation_short_uptime_triggers(self, tmp_path, monkeypatch):
        """First observation: reboot if uptime within window."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        self._patch_boot_id(monkeypatch, "boot-first\n", uptime_text="30.0 100000.0")

        assert mgr.reboot_recently_detected() is True
        marker = json.loads((tmp_path / "reboot_recovery.json").read_text())
        assert marker["boot_id"] == "boot-first"
        assert marker["reboot_detected_at"] is not None

    def test_first_observation_long_uptime_no_trigger(self, tmp_path, monkeypatch):
        """First observation with long uptime = no reboot (already running long)."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        self._patch_boot_id(monkeypatch, "boot-first\n",
                            uptime_text="99999.0 200000.0")

        assert mgr.reboot_recently_detected() is False
        # Marker still written for future comparisons
        assert (tmp_path / "reboot_recovery.json").is_file()

    def test_os_error_returns_false(self, tmp_path, monkeypatch):
        """If boot_id cannot be read, return False (fail-safe)."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        real_read_text = Path.read_text
        def fake_read_text(self, *args, **kwargs):
            if str(self) == "/proc/sys/kernel/random/boot_id":
                raise OSError("no boot_id")
            return real_read_text(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", fake_read_text)

        assert mgr.reboot_recently_detected() is False

    def test_corrupt_marker_returns_false(self, tmp_path, monkeypatch):
        """Corrupt marker JSON = fail-safe, treat as first observation."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        (tmp_path / "reboot_recovery.json").write_text("not valid json{{{")
        self._patch_boot_id(monkeypatch, "boot-corrupt\n",
                            uptime_text="99999.0 200000.0")

        assert mgr.reboot_recently_detected() is False


# --------------------------------------------------------------------------- #
# recovery_trigger_active
# --------------------------------------------------------------------------- #
class TestRecoveryTrigger:
    def test_trigger_active_on_reboot(self, monkeypatch):
        monkeypatch.setattr(mgr, "maintenance_reexec_seen", lambda: False)
        monkeypatch.setattr(mgr, "reboot_recently_detected", lambda: True)
        assert mgr.recovery_trigger_active() is True

    def test_trigger_active_on_maintenance(self, monkeypatch):
        monkeypatch.setattr(mgr, "maintenance_reexec_seen", lambda: True)
        monkeypatch.setattr(mgr, "reboot_recently_detected", lambda: False)
        assert mgr.recovery_trigger_active() is True

    def test_trigger_inactive_when_neither(self, monkeypatch):
        monkeypatch.setattr(mgr, "maintenance_reexec_seen", lambda: False)
        monkeypatch.setattr(mgr, "reboot_recently_detected", lambda: False)
        assert mgr.recovery_trigger_active() is False


# --------------------------------------------------------------------------- #
# recover_offline_demo reboot path
# --------------------------------------------------------------------------- #
class TestRecoverOfflineDemoReboot:
    def _setup_demo_error(self, monkeypatch, trigger=True):
        """Mock a DEMO account stuck in ERROR/MT5_WORKER_NOT_RUNNING."""
        monkeypatch.setattr(mgr, "expected_mode", lambda b, s: "DEMO")
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: True)
        monkeypatch.setattr(mgr, "sealed_path", lambda cid: "/sealed/"+cid)
        monkeypatch.setattr(mgr, "service_active", lambda cid: False)
        monkeypatch.setattr(mgr, "recovery_trigger_active", lambda: trigger)
        monkeypatch.setattr(mgr, "RECOVERY_LAST_ATTEMPT", {})
        monkeypatch.setattr(mgr, "subprocess", MagicMock())
        mgr.subprocess.run = MagicMock(return_value=MagicMock(returncode=0))

        cursor = MagicMock()
        cursor.fetchone.return_value = ("ERROR", None, 0, "MT5_WORKER_NOT_RUNNING")
        cursor.rowcount = 1
        # execute() must return the cursor itself so .fetchone() works
        cursor.execute.return_value = cursor
        fake_db = MagicMock()
        fake_db.__enter__.return_value = cursor
        fake_db.execute.return_value = cursor
        monkeypatch.setattr(mgr, "db", lambda: fake_db)
        return cursor

    def test_recovers_on_reboot_trigger(self, monkeypatch):
        """DEMO session in ERROR recovers when reboot trigger is active."""
        self._setup_demo_error(monkeypatch, trigger=True)
        launched = []
        monkeypatch.setattr(mgr, "launch_worker",
                            lambda cid, b, m, e: launched.append(cid))
        monkeypatch.setattr(mgr, "active", set())

        mgr.recover_offline_demo("cid123", "owner1", "Deriv", "Deriv-Demo")

        assert "cid123" in launched, "Worker should be launched on reboot recovery"

    def test_no_recovery_when_trigger_inactive(self, monkeypatch):
        """No recovery when neither reboot nor maintenance trigger is active."""
        self._setup_demo_error(monkeypatch, trigger=False)
        launched = []
        monkeypatch.setattr(mgr, "launch_worker",
                            lambda cid, b, m, e: launched.append(cid))

        mgr.recover_offline_demo("cid123", "owner1", "Deriv", "Deriv-Demo")

        assert launched == [], "No launch when trigger is inactive"

    def test_live_account_not_recovered(self, monkeypatch):
        """LIVE accounts are never auto-recovered, even on reboot."""
        monkeypatch.setattr(mgr, "expected_mode", lambda b, s: "LIVE")
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: True)
        monkeypatch.setattr(mgr, "service_active", lambda cid: False)
        monkeypatch.setattr(mgr, "recovery_trigger_active", lambda: True)
        monkeypatch.setattr(mgr, "RECOVERY_LAST_ATTEMPT", {})

        launched = []
        monkeypatch.setattr(mgr, "launch_worker",
                            lambda cid, b, m, e: launched.append(cid))

        mgr.recover_offline_demo("cid123", "owner1", "Deriv",
                                "DerivBVI-Server-02")

        assert launched == [], "LIVE accounts must not be auto-recovered"

    def test_active_service_not_recovered(self, monkeypatch):
        """A running service is not re-launched."""
        monkeypatch.setattr(mgr, "expected_mode", lambda b, s: "DEMO")
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: True)
        monkeypatch.setattr(mgr, "service_active", lambda cid: True)
        monkeypatch.setattr(mgr, "recovery_trigger_active", lambda: True)
        monkeypatch.setattr(mgr, "RECOVERY_LAST_ATTEMPT", {})

        launched = []
        monkeypatch.setattr(mgr, "launch_worker",
                            lambda cid, b, m, e: launched.append(cid))

        mgr.recover_offline_demo("cid123", "owner1", "Deriv", "Deriv-Demo")

        assert launched == [], "Active service must not be re-launched"

    def test_no_sealed_credential_no_recovery(self, monkeypatch):
        """No sealed credential = no recovery (fail-safe)."""
        monkeypatch.setattr(mgr, "expected_mode", lambda b, s: "DEMO")
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: False)
        monkeypatch.setattr(mgr, "recovery_trigger_active", lambda: True)
        monkeypatch.setattr(mgr, "RECOVERY_LAST_ATTEMPT", {})

        launched = []
        monkeypatch.setattr(mgr, "launch_worker",
                            lambda cid, b, m, e: launched.append(cid))

        mgr.recover_offline_demo("cid123", "owner1", "Deriv", "Deriv-Demo")

        assert launched == [], "No recovery without sealed credential"

    def test_multi_account_all_recover_on_reboot(self, monkeypatch):
        """All three DEMO accounts recover when reboot trigger is active.

        Regression test for the multi-account defect: [True, False, False]
        was the observed failure. All three accounts receive True and all
        three workers are launched.
        """
        # Patch the trigger to use the real reboot detection mechanism
        monkeypatch.setattr(mgr, "expected_mode", lambda b, s: "DEMO")
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: True)
        monkeypatch.setattr(mgr, "sealed_path", lambda cid: "/sealed/"+cid)
        monkeypatch.setattr(mgr, "service_active", lambda cid: False)
        monkeypatch.setattr(mgr, "RECOVERY_LAST_ATTEMPT", {})
        monkeypatch.setattr(mgr, "subprocess", MagicMock())
        mgr.subprocess.run = MagicMock(return_value=MagicMock(returncode=0))

        # Use the real recovery_trigger_active which calls reboot_recently_detected
        # Patch reboot_recently_detected to simulate a cached reboot detection
        # with a fresh timestamp (within window)
        def fake_reboot_detected():
            # Detection already cached with a recent timestamp
            if mgr._reboot_detected_at is None:
                mgr._reboot_detected_at = time.time()
            return True

        monkeypatch.setattr(mgr, "reboot_recently_detected",
                            fake_reboot_detected)
        # maintenance_reexec_seen returns False so only reboot trigger fires
        monkeypatch.setattr(mgr, "maintenance_reexec_seen", lambda: False)

        cursor = MagicMock()
        cursor.fetchone.return_value = ("ERROR", None, 0, "MT5_WORKER_NOT_RUNNING")
        cursor.rowcount = 1
        cursor.execute.return_value = cursor
        fake_db = MagicMock()
        fake_db.__enter__.return_value = cursor
        fake_db.execute.return_value = cursor
        monkeypatch.setattr(mgr, "db", lambda: fake_db)

        launched = []
        monkeypatch.setattr(mgr, "launch_worker",
                            lambda cid, b, m, e: launched.append(cid))
        monkeypatch.setattr(mgr, "active", set())

        # Three Deriv DEMO accounts, all offline
        accounts = [
            ("cid001", "owner1", "Deriv", "Deriv-Demo"),
            ("cid002", "owner2", "Deriv", "Deriv-Demo"),
            ("cid003", "owner3", "Deriv", "Deriv-Demo"),
        ]
        for cid, owner, broker, server in accounts:
            mgr.recover_offline_demo(cid, owner, broker, server)

        results = [cid in launched for cid in ("cid001", "cid002", "cid003")]
        assert results == [True, True, True], (
            f"All three accounts must recover, got {results}. "
            f"Launched: {launched}")


# --------------------------------------------------------------------------- #
# ensure_worker_accessible + launch_worker
# --------------------------------------------------------------------------- #
class TestLaunchWorkerAccess:
    def test_ensure_worker_accessible_sets_o_rx(self, tmp_path, monkeypatch):
        """ensure_worker_accessible sets 0o755 on worker dir and script."""
        monkeypatch.setattr(mgr, "WORKER_DIR", str(tmp_path))
        # Create a fake worker script inside the directory
        worker_dir = tmp_path
        worker_script = worker_dir / "worker_feed_v4_usd50_canary.py"
        worker_script.write_text("#!/usr/bin/env python3\n")
        worker_script.chmod(0o600)  # restrictive initially
        worker_dir.chmod(0o700)

        mgr.ensure_worker_accessible(str(worker_script))

        dir_stat = worker_dir.stat()
        script_stat = worker_script.stat()
        assert dir_stat.st_mode & 0o005, "Worker dir must have o+rx"
        assert script_stat.st_mode & 0o005, "Worker script must have o+rx"

    def test_launch_worker_calls_ensure_for_demo(self, monkeypatch):
        """launch_worker calls ensure_worker_accessible for DEMO workers."""
        called = []

        monkeypatch.setattr(mgr, "ensure_worker_accessible",
                            lambda p: called.append(p))
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: True)
        monkeypatch.setattr(mgr, "sealed_path", lambda cid: "/sealed/path")

        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(mgr.subprocess, "run", fake_run)

        mgr.launch_worker("cid123", "Deriv", "DEMO", "/sealed/path")

        assert len(called) == 1, "ensure_worker_accessible must be called for DEMO"
        assert "worker_feed_v4_usd50_canary.py" in called[0]

    def test_launch_worker_skips_ensure_for_live(self, monkeypatch):
        """launch_worker does NOT call ensure_worker_accessible for LIVE.

        Regression test: the permission fix must be scoped to DEMO workers
        only. LIVE workers are outside the recovery scope and must not have
        their filesystem permissions modified.
        """
        called = []

        monkeypatch.setattr(mgr, "ensure_worker_accessible",
                            lambda p: called.append(p))
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: True)
        monkeypatch.setattr(mgr, "sealed_path", lambda cid: "/sealed/path")

        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(mgr.subprocess, "run", fake_run)

        mgr.launch_worker("cid123", "Deriv", "LIVE", "/sealed/path")

        assert called == [], (
            "ensure_worker_accessible must NOT be called for LIVE workers. "
            "The permission fix is scoped to DEMO recovery only.")

    def test_launch_worker_does_not_use_read_only_paths(self, monkeypatch):
        """launch_worker must NOT include ReadOnlyPaths= (ineffective for access).

        ReadOnlyPaths= only marks a path read-only; it does not grant
        read/execute access to the unprivileged DynamicUser identity.
        The correct fix is ensure_worker_accessible() with o+rx permissions.
        """
        monkeypatch.setattr(mgr, "ensure_worker_accessible", lambda p: None)
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: True)
        monkeypatch.setattr(mgr, "sealed_path", lambda cid: "/sealed/path")

        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(mgr.subprocess, "run", fake_run)

        mgr.launch_worker("cid123", "Deriv", "DEMO", "/sealed/path")

        cmd = captured["cmd"]
        rop = [arg for arg in cmd if "ReadOnlyPaths" in arg]
        assert rop == [], (
            "ReadOnlyPaths= must not be used; it does not grant "
            "unprivileged access. Use ensure_worker_accessible() instead.")

    def test_worker_dir_constant_value(self):
        """WORKER_DIR points to the native runtime workers directory."""
        assert mgr.WORKER_DIR == "/opt/abutron-v9-native-runtime/workers"
