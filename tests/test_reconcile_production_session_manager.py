"""Tests for the production session-manager reconciliation script.

Verifies that the reconciliation script:
- Injects ensure_worker_accessible() before launch_worker()
- Injects the DEMO-scoped call site after chosen= assignment
- Preserves FP_WORKER constant unchanged
- Preserves FP SupplementaryGroups property
- Is idempotent (safe to run twice)
- Fails safely when launch_worker is not found
- Handles different production FP worker paths
"""
from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECONCILE_PATH = ROOT / "ops" / "v9-cutover" / "reconcile_production_session_manager.py"

# A minimal production manager fixture with FP-specific differences
PRODUCTION_MANAGER = '''#!/usr/bin/env python3
import os,json,socket,struct,sqlite3,pwd,grp,re,threading,time,subprocess,secrets,stat
from pathlib import Path
ROOT=Path("/run/abutron-client-mt5-manager")
SOCK=ROOT/"manager.sock"
SECRETS=ROOT/"secrets"
SEALED=Path("/var/lib/abutron-client-mt5-manager/credentials")
RECOVERY_LAST_ATTEMPT={}
DB="/var/lib/apex-signup/accounts.sqlite3"
WORKER="/opt/abutron-v9-native-runtime/workers/worker_feed_v4.py"
FP_WORKER="/opt/abutron-v9-native-runtime/workers/fp_worker_prod_v2.py"
DERIV_50_100_WORKER="/opt/abutron-v9-native-runtime/workers/worker_feed_v4_usd50_canary.py"
DERIV_LIVE_READONLY_WORKER="/opt/abutron-v9-native-runtime/workers/worker_feed_v4_deriv_live_readonly.py"
TEMPLATE=Path("/opt/abutron/client-mt5-template-v3")
FP_MODES={"FPMarketsSC-Demo":"DEMO"}
DERIV_LIVE_MODES={"DerivBVI-Server-02":"LIVE"}
ALLOWED={("Deriv","Deriv-Demo")} | {("FP Markets",s) for s in FP_MODES} | {("Deriv",s) for s in DERIV_LIVE_MODES}
def expected_mode(broker,server):
 return (FP_MODES.get(server) if broker=="FP Markets" else
         DERIV_LIVE_MODES.get(server) if broker=="Deriv" and server in DERIV_LIVE_MODES else
         "DEMO" if (broker,server) in ALLOWED else None)
CID=re.compile(r"^[a-f0-9]{32}$")
LOGIN=re.compile(r"^[0-9]{4,20}$")
g_lock=threading.RLock()
active=set()
def db():
 c=sqlite3.connect(DB,isolation_level=None,timeout=8)
 return c
def unit(cid):return "abutron-client-mt5-"+cid
def service_active(cid):
 return False
def sealed_path(cid):
 return SEALED/(cid+".cred")
def sealed_available(cid):
 return True
def fp_reconnect_logic(cid,broker,server):
 # PRODUCTION FP-SPECIFIC RECONNECT LOGIC — must be preserved
 return True
def launch_worker(cid,broker,mode,encrypted):
 if (broker not in ("Deriv","FP Markets","IC Markets")
     or mode not in ("DEMO","LIVE")):
  raise ValueError("Unverified approved broker encrypted credential")
 chosen=(FP_WORKER if broker=="FP Markets" else
         DERIV_LIVE_READONLY_WORKER if broker=="Deriv" and mode=="LIVE" else
         DERIV_50_100_WORKER if broker=="Deriv" else WORKER)
 subprocess.run(["systemctl","reset-failed",unit(cid)+".service"],timeout=6,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 subprocess.run(["systemd-run","--collect","--unit="+unit(cid),
     "--property=Type=exec","--property=DynamicUser=yes",
     "--property=ProtectSystem=strict","--property=ProtectHome=yes",
     "--property=PrivateTmp=yes","--property=PrivateDevices=yes",
     "--property=NoNewPrivileges=yes",
     "--property=MemoryMax=1800M","--property=TasksMax=170",
     "--property=UMask=0077",
     *(["--property=SupplementaryGroups=abutron-fp-boot-assets"]
       if (broker,mode)==("FP Markets","DEMO") else []),
     "/usr/bin/python3","-B",chosen],check=True,timeout=20,
     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def maintenance_reexec_seen():
 return False
def recover_offline_demo(cid,owner,broker,server):
 if expected_mode(broker,server)!="DEMO" or not sealed_available(cid):return
 if service_active(cid):return
 if not maintenance_reexec_seen():
  return
'''


def _run_reconcile(manager_path, *extra_args):
    """Run the reconciliation script and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, str(RECONCILE_PATH), "--manager-path", str(manager_path), *extra_args],
        capture_output=True, text=True, timeout=15,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def production_manager(tmp_path):
    """Write the production manager fixture to a temp file."""
    path = tmp_path / "mt5_session_manager.py"
    path.write_text(PRODUCTION_MANAGER)
    return path


class TestReconciliationPatch:
    def test_dry_run_shows_changes_without_writing(self, production_manager):
        """Dry-run reports changes but does not modify the file."""
        original = production_manager.read_text()
        rc, out, err = _run_reconcile(production_manager, "--dry-run")

        assert rc == 0
        assert "+def ensure_worker_accessible(" in out
        assert "+  ensure_worker_accessible(chosen)" in out
        assert "Verification passed" in out
        # File unchanged after dry-run
        assert production_manager.read_text() == original

    def test_live_patch_injects_function_and_call(self, production_manager):
        """Live patch writes the function and call site to the file."""
        original = production_manager.read_text()
        rc, out, err = _run_reconcile(production_manager)

        assert rc == 0
        assert "RECONCILIATION COMPLETE" in out
        assert "Backup created" in out

        patched = production_manager.read_text()
        # Function injected
        assert "def ensure_worker_accessible" in patched
        assert "ABUTRON-ISSUE65-WORKER-ACCESS-RECONCILED" in patched
        # Call site injected, scoped to DEMO
        assert 'if (broker, mode) == ("Deriv", "DEMO"):' in patched
        assert "ensure_worker_accessible(chosen)" in patched
        # Original code preserved
        assert "def fp_reconnect_logic" in patched
        assert "def launch_worker" in patched
        assert "def recover_offline_demo" in patched
        # File changed
        assert patched != original

    def test_fp_worker_constant_unchanged(self, production_manager):
        """FP_WORKER path is not modified by the reconciliation."""
        original_fp = "fp_worker_prod_v2.py"
        assert original_fp in production_manager.read_text()

        rc, out, err = _run_reconcile(production_manager)
        assert rc == 0

        patched = production_manager.read_text()
        assert original_fp in patched
        assert "FP_WORKER" in patched

    def test_fp_supplementary_groups_preserved(self, production_manager):
        """FP SupplementaryGroups property is not removed."""
        assert "SupplementaryGroups=abutron-fp-boot-assets" in production_manager.read_text()

        rc, out, err = _run_reconcile(production_manager)
        assert rc == 0

        patched = production_manager.read_text()
        assert "SupplementaryGroups=abutron-fp-boot-assets" in patched

    def test_fp_reconnect_logic_preserved(self, production_manager):
        """Production FP-specific reconnect logic is not touched."""
        assert "PRODUCTION FP-SPECIFIC RECONNECT LOGIC" in production_manager.read_text()

        rc, out, err = _run_reconcile(production_manager)
        assert rc == 0

        patched = production_manager.read_text()
        assert "PRODUCTION FP-SPECIFIC RECONNECT LOGIC" in patched
        assert "def fp_reconnect_logic" in patched

    def test_idempotent(self, production_manager):
        """Running the script twice does not double-patch."""
        rc1, out1, err1 = _run_reconcile(production_manager)
        assert rc1 == 0
        assert "RECONCILIATION COMPLETE" in out1

        patched_after_first = production_manager.read_text()
        count_func = patched_after_first.count("def ensure_worker_accessible")
        count_call = patched_after_first.count("ensure_worker_accessible(chosen)")

        rc2, out2, err2 = _run_reconcile(production_manager)
        assert rc2 == 0
        assert "Already reconciled" in out2

        patched_after_second = production_manager.read_text()
        assert patched_after_second == patched_after_first
        assert patched_after_second.count("def ensure_worker_accessible") == count_func
        assert patched_after_second.count("ensure_worker_accessible(chosen)") == count_call

    def test_backup_created(self, production_manager):
        """A backup file is created before patching."""
        before_files = set(production_manager.parent.iterdir())

        rc, out, err = _run_reconcile(production_manager)
        assert rc == 0

        after_files = set(production_manager.parent.iterdir())
        new_files = after_files - before_files
        backup_files = [f for f in new_files if ".backup-issue65" in f.name]
        assert len(backup_files) == 1, "Exactly one backup file should be created"

    def test_patched_file_compiles(self, production_manager):
        """The patched file has valid Python syntax."""
        rc, out, err = _run_reconcile(production_manager)
        assert rc == 0

        # Verify compile
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(production_manager)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"Compile failed: {result.stderr}"

    def test_fails_when_launch_worker_not_found(self, tmp_path):
        """Script fails gracefully when launch_worker is absent."""
        bad_manager = tmp_path / "bad_manager.py"
        bad_manager.write_text("# No launch_worker here\nimport os\n")

        rc, out, err = _run_reconcile(bad_manager)
        assert rc == 1
        assert "ERROR" in out or "ERROR" in err

    def test_fails_when_file_not_found(self, tmp_path):
        """Script fails gracefully when the manager file does not exist."""
        missing = tmp_path / "nonexistent.py"
        rc, out, err = _run_reconcile(missing)
        assert rc == 1
        assert "not found" in out.lower() or "not found" in err.lower()

    def test_call_site_scoped_to_deriv_demo_only(self, production_manager):
        """The call site is inside an Deriv DEMO guard, not unconditional."""
        rc, out, err = _run_reconcile(production_manager)
        assert rc == 0

        patched = production_manager.read_text()
        # Find the call site context
        idx = patched.index("ensure_worker_accessible(chosen)")
        context_before = patched[max(0, idx - 50):idx]
        assert 'if (broker, mode) == ("Deriv", "DEMO"):' in context_before, (
            "ensure_worker_accessible requires the Deriv DEMO guard"
        )

    def test_ensure_function_derives_dir_from_path(self, production_manager):
        """ensure_worker_accessible derives the worker dir from the worker path.

        This makes it portable across manager versions that may not have
        a WORKER_DIR constant.
        """
        rc, out, err = _run_reconcile(production_manager)
        assert rc == 0

        patched = production_manager.read_text()
        func_start = patched.index("def ensure_worker_accessible")
        func_end = patched.index("def launch_worker")
        func_body = patched[func_start:func_end]
        # Should use Path(worker_path).parent, not a WORKER_DIR constant
        assert "Path(worker_path).parent" in func_body
        assert "WORKER_DIR" not in func_body

@pytest.mark.parametrize("broker,mode,selected,expect_access", [
    ("Deriv", "DEMO", "DERIV_50_100_WORKER", True),
    ("Deriv", "LIVE", "DERIV_LIVE_READONLY_WORKER", False),
    ("FP Markets", "DEMO", "FP_WORKER", False),
    ("FP Markets", "LIVE", "FP_WORKER", False),
    ("IC Markets", "DEMO", "WORKER", False),
])
def test_launch_effects(production_manager, tmp_path, broker, mode, selected, expect_access):
    assert _run_reconcile(production_manager)[0] == 0
    ns = runpy.run_path(str(production_manager))["launch_worker"].__globals__
    mock_runner = Mock()
    ns["subprocess"] = mock_runner
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    script = worker_dir / "example.py"
    script.touch(mode=0o600)
    ns[selected] = str(script)
    ns["launch_worker"]("cid", broker, mode, ns["sealed_path"]("cid"))
    assert script.stat().st_mode & 0o777 == (0o755 if expect_access else 0o600)
    assert worker_dir.stat().st_mode & 0o777 == (0o755 if expect_access else 0o700)
    argv = mock_runner.run.call_args.args[0]
    assert argv[-1] == str(script)
    fp_group = "--property=SupplementaryGroups=abutron-fp-boot-assets"
    assert (fp_group in argv) == ((broker, mode) == ("FP Markets", "DEMO"))


def test_upgrade_previous_guard(production_manager):
    assert _run_reconcile(production_manager)[0] == 0
    corrected = production_manager.read_text()
    old = corrected.replace('if (broker, mode) == ("Deriv", "DEMO"):',
                            'if mode=="DEMO":')
    assert corrected != old
    production_manager.write_text(old)
    assert _run_reconcile(production_manager)[0] == 0
    assert production_manager.read_text() == corrected


def test_marker_without_valid_patch_is_rejected(production_manager):
    text = "# ABUTRON-ISSUE65-WORKER-ACCESS-RECONCILED\n" + PRODUCTION_MANAGER
    production_manager.write_text(text)
    assert _run_reconcile(production_manager)[0] != 0
    assert production_manager.read_text() == text


def test_other_chosen_assignments_untouched(production_manager):
    decoy = "def another():\n chosen=(WORKER)\n return chosen\n"
    production_manager.write_text(decoy + PRODUCTION_MANAGER)
    assert _run_reconcile(production_manager)[0] == 0
    assert production_manager.read_text().startswith(decoy)

def test_prepared_read_only_worker_paths(production_manager, tmp_path):
    assert _run_reconcile(production_manager)[0] == 0
    helper = runpy.run_path(str(production_manager))["ensure_worker_accessible"]
    worker = tmp_path / "workers" / "worker.py"
    worker.parent.mkdir()
    worker.touch()
    worker.parent.chmod(0o755)
    worker.chmod(0o755)
    with patch.object(Path, "chmod", side_effect=OSError(30, "Read-only file system")):
        helper(str(worker))


def test_upgrade_unconditional_chmod(production_manager):
    assert _run_reconcile(production_manager)[0] == 0
    current = production_manager.read_text()
    start = current.index("def ensure_worker_accessible")
    end = current.index("def launch_worker", start)
    old_helper = """def ensure_worker_accessible(worker_path):
 # ABUTRON-ISSUE65-WORKER-ACCESS-RECONCILED
 wd=Path(worker_path).parent
 wd.chmod(0o755)
 wp=Path(worker_path)
 wp.chmod(0o755)

"""
    production_manager.write_text(current[:start] + old_helper + current[end:])
    assert _run_reconcile(production_manager)[0] == 0
    assert production_manager.read_text() == current
