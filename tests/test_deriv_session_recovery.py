"""Tests for Deriv DEMO session-manager reboot recovery repair (Issue #65).

Verifies:
- reboot_recently_detected() detects a boot_id change (reboot) and does not
  fire repeatedly within the same boot.
- recovery_trigger_active() is True on reboot OR maintenance reexec.
- recover_offline_demo() recovers DEMO sessions after a reboot trigger, not
  only after apt-daily-upgrade reexec.
- launch_worker() grants ReadOnlyPaths to the worker directory so the
  DynamicUser service identity can open the worker script.
- LIVE accounts are never auto-recovered.
- Active services are not re-launched.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "ops" / "v9-cutover" / "mt5_session_manager.py"


def _load_manager():
    spec = importlib.util.spec_from_file_location(
        "mt5_session_manager", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mgr = _load_manager()


# --------------------------------------------------------------------------- #
# reboot_recently_detected
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

    def test_boot_id_change_triggers_reboot(self, tmp_path, monkeypatch):
        """A boot_id change since the last recorded one = reboot detected."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        marker = tmp_path / "last_boot_id"
        marker.write_text("boot-old")
        self._patch_boot_id(monkeypatch, "boot-new\n")

        assert mgr.reboot_recently_detected() is True
        assert marker.read_text().strip() == "boot-new"

    def test_same_boot_id_does_not_trigger(self, tmp_path, monkeypatch):
        """Same boot_id as last recorded = no reboot."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        marker = tmp_path / "last_boot_id"
        marker.write_text("boot-same")
        self._patch_boot_id(monkeypatch, "boot-same\n")

        assert mgr.reboot_recently_detected() is False

    def test_first_observation_short_uptime_triggers(self, tmp_path, monkeypatch):
        """First observation: reboot if uptime within window."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        self._patch_boot_id(monkeypatch, "boot-first\n", uptime_text="30.0 100000.0")

        assert mgr.reboot_recently_detected() is True
        assert (tmp_path / "last_boot_id").is_file()

    def test_first_observation_long_uptime_no_trigger(self, tmp_path, monkeypatch):
        """First observation with long uptime = no reboot (already running long)."""
        monkeypatch.setattr(mgr, "ROOT", tmp_path)
        self._patch_boot_id(monkeypatch, "boot-first\n",
                            uptime_text="99999.0 200000.0")

        assert mgr.reboot_recently_detected() is False
        # Marker still written for future comparisons
        assert (tmp_path / "last_boot_id").is_file()

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


# --------------------------------------------------------------------------- #
# launch_worker ReadOnlyPaths
# --------------------------------------------------------------------------- #
class TestLaunchWorkerAccess:
    def test_read_only_paths_granted_to_worker_dir(self, monkeypatch):
        """launch_worker includes ReadOnlyPaths for the worker directory."""
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(mgr.subprocess, "run", fake_run)
        monkeypatch.setattr(mgr, "sealed_available", lambda cid: True)
        monkeypatch.setattr(mgr, "sealed_path", lambda cid: "/sealed/path")

        mgr.launch_worker("cid123", "Deriv", "DEMO", "/sealed/path")

        cmd = captured["cmd"]
        assert "--property=ReadOnlyPaths=" + mgr.WORKER_DIR in cmd, (
            "launch_worker must grant ReadOnlyPaths to the worker directory")

    def test_worker_dir_constant_value(self):
        """WORKER_DIR points to the native runtime workers directory."""
        assert mgr.WORKER_DIR == "/opt/abutron-v9-native-runtime/workers"
