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

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

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
       if (broker,mode)==(FP_MARKETS_DEMO) else []),
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
        assert "Injected ensure_worker_accessible()" in out
        assert "Injected ensure_worker_accessible(chosen) call" in out
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
        assert 'if mode=="DEMO":' in patched
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

    def test_call_site_scoped_to_demo_only(self, production_manager):
        """The call site is inside an if mode=="DEMO" guard, not unconditional."""
        rc, out, err = _run_reconcile(production_manager)
        assert rc == 0

        patched = production_manager.read_text()
        # Find the call site context
        idx = patched.index("ensure_worker_accessible(chosen)")
        context_before = patched[max(0, idx - 50):idx]
        assert 'if mode=="DEMO":' in context_before, (
            "ensure_worker_accessible must be guarded by if mode==DEMO"
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
