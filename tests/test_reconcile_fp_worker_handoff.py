"""Tests for reconcile_fp_worker_handoff.py — four-priority FP worker corrections.

Tests use the anonymized production worker from the review branch as the
fixture, verifying each correction applies, is idempotent, and preserves
all LIVE OFF guards.
"""
import ast
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ops", "v9-cutover"))
from reconcile_fp_worker_handoff import (  # noqa: E402
    PATCH_MARKER,
    BUILD6295_FUNC,
    FP_DEMO_EXIT69_CHECK,
    FP9392_RECOVERY_CHECK,
    FP9392_STAGING_OLD,
    FP9392_STAGING_NEW,
    BREAK_CONDITION_OLD,
    BREAK_CONDITION_NEW,
    DECISION_OLD,
    DECISION_NEW,
    find_function,
    is_already_patched,
    patch_worker,
    verify_patch,
)

# The anonymized production worker from the review branch
WORKER_SOURCE = """\
#!/usr/bin/python3
\"\"\"ABUTRON client MT5 worker: isolated UID, broker-native feed plus gated order bridge.\"\"\"
import os,sys,json,subprocess,time,shutil,secrets,uuid,signal
from pathlib import Path

def handoff_event(journal_bytes):
    \"\"\"An MT5 updater handoff in exactly the newly appended journal range.\"\"\"
    if not journal_bytes: return False
    return False

def fp1622_mql_recompile_startup_handoff(journal_bytes):
    \"\"\"Recognize exactly the current FP1622 MT5 compiler startup handoff.\"\"\"
    if not journal_bytes:return False
    return (\"compiler\\tfull recompilation has been started\" in text
            and \"terminal\\texit with code 0\" in text
            and \"terminal\\tstopped with 10053\" in text)


def fp6364_invalid_account_journal(data):
 \"\"\"Recognize this account's broker rejection, never infer an authenticated session.\"\"\"
 if not data:return False
 return False

def cgroup_terminal_running():
    return False

def hold_zero_exit_for_native_proof(returncode,exact_fresh_heartbeat,seconds_since_exit):
    if returncode != 0 or seconds_since_exit < 0:
        return False
    if exact_fresh_heartbeat:
        return True
    return seconds_since_exit < 18.0

def exit_action(returncode,was_connected,attached,handoff,attempt,total_elapsed):
    if attached:return \"MONITOR_CHILD\"
    if was_connected:return \"STOP\"
    if returncode==0 and handoff and attempt<3 and total_elapsed<220:
        return \"RETRY_UPDATE\"
    return \"STOP\"

def stage_fp_live_6364_preserved_profile(terminal,cid,login,server,mode):
 return \"SKIP\"

def stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid):
 \"\"\"PINNED LIVE9392 broker-history only, no order calls or LIVE enablement.\"\"\"
 return \"SKIP_OTHER_ACCOUNT\"

def new_challenge():
 return (\"nonce\",\"owner\")

def stage_fp_demo_history(terminal,files,nonce,challenge_owner,login,server,mode):
 return \"SKIP\"

def stage_fp_live_6364_history(terminal,files,nonce,challenge_owner,login,server,mode,cid):
 return \"SKIP\"

def main():
 nonce,challenge_owner=new_challenge()
 # Staged history indicator remains read-only and cannot alter native feed EA.
 try:
  stage_fp_demo_history(terminal,files,nonce,challenge_owner,login,server,mode)
  stage_fp_live_6364_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)
  stage_fp_live_6364_preserved_profile(terminal,cid,login,server,mode)
  stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)
 except (OSError,ValueError,UnicodeError,RuntimeError):
  print(\"FP_HISTORY_READONLY_ADDON_NOT_STAGED\",file=sys.stderr)
 attempt=-1
 while attempt < 3:
  attempt+=1
  try:
   data=journal.read_bytes()
   tail=data[previous_bytes:] if len(data)>previous_bytes else b\"\"
  except OSError:
   tail=b\"\"
   # FP6364 LIVE's Wine launcher reports 69 while its signed-session MT5
   fp6364_compiler_exit69=(
       cid==\"f0000000000000000000000000000005\"
       and broker==\"FP Markets\" and server==\"FPMarketsSC-Live\"
       and login==\"90000010\" and mode==\"LIVE\"
       and process.returncode==69 and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and fp1622_mql_recompile_startup_handoff(tail))
   if process.returncode!=0 and not fp6364_compiler_exit69:
    break
   trusted_fp6364_compiler_relaunch=(
       cid==\"f0000000000000000000000000000005\"
       and broker==\"FP Markets\" and server==\"FPMarketsSC-Live\"
       and login==\"90000010\" and mode==\"LIVE\"
       and process.returncode in (0,69) and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and fp1622_mql_recompile_startup_handoff(tail))
   decision=exit_action(0 if fp6364_compiler_exit69 else process.returncode,was_connected,
              cgroup_terminal_running(),
              handoff_event(tail) or trusted_fp6364_compiler_relaunch,
              attempt,time.monotonic()-started)
   if decision==\"STOP\":
    break
 return
"""


class TestPatchWorker:
    """Tests for patch_worker() applying all four corrections."""

    def test_all_corrections_applied(self):
        patched, changes = patch_worker(WORKER_SOURCE)
        assert len(changes) >= 4
        assert "mt5_build_6295_detected" in patched
        assert "fp1622_mql_recompile_startup_handoff_6295" in patched
        assert "fp_demo_compiler_exit69" in patched
        assert "fp9392_error_recovery" in patched

    def test_build_6295_functions_present(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "def mt5_build_6295_detected(journal_bytes):" in patched
        assert "def fp1622_mql_recompile_startup_handoff_6295(journal_bytes):" in patched

    def test_fp_demo_exit69_check_present(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp_demo_compiler_exit69=(" in patched
        assert "process.returncode==69" in patched
        assert "not was_connected" in patched
        assert "attempt<2" in patched
        assert "fp1622_mql_recompile_startup_handoff(tail)" in patched

    def test_break_condition_updated(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp6364_compiler_exit69 or fp_demo_compiler_exit69" in patched
        assert "if process.returncode!=0 and not (fp6364_compiler_exit69 or fp_demo_compiler_exit69):" in patched

    def test_decision_exit_action_updated(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "0 if fp6364_compiler_exit69 or fp_demo_compiler_exit69 else process.returncode" in patched

    def test_9392_recovery_check_present(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp9392_error_recovery=(" in patched
        assert "cid==\"f0000000000000000000000000000008\"" in patched
        assert "broker==\"FP Markets\"" in patched
        assert "server==\"FPMarketsSC-Live\"" in patched
        assert "login==\"90000008\"" in patched
        assert "mode==\"LIVE\"" in patched
        assert "not was_connected" in patched

    def test_9392_staging_call_guarded(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "if cid==\"f0000000000000000000000000000008\" or fp9392_error_recovery:" in patched
        assert "stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)" in patched

    def test_patch_marker_present(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert PATCH_MARKER in patched

    def test_compiles_after_patch(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        compile(patched, "<patched>", "exec")  # raises SyntaxError if broken


class TestLiveOffPreservation:
    """Verify all LIVE OFF guards are preserved after patching."""

    def test_fp6364_compiler_exit69_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp6364_compiler_exit69=(" in patched
        assert "cid==\"f0000000000000000000000000000005\"" in patched

    def test_hold_zero_exit_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "def hold_zero_exit_for_native_proof" in patched

    def test_fp6364_invalid_account_journal_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "def fp6364_invalid_account_journal" in patched

    def test_stage_fp_live_6364_preserved_profile_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "def stage_fp_live_6364_preserved_profile" in patched

    def test_9392_staging_function_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "def stage_fp_live_9392_verified_readonly_history" in patched

    def test_trusted_fp6364_compiler_relaunch_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "trusted_fp6364_compiler_relaunch" in patched
        assert "process.returncode in (0,69)" in patched

    def test_exit_action_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "def exit_action" in patched
        assert "MONITOR_CHILD" in patched
        assert "RETRY_UPDATE" in patched


class TestIdempotency:
    """Verify the patch is idempotent."""

    def test_second_patch_no_changes(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched2, changes2 = patch_worker(patched)
        assert patched2 == patched
        assert "Already patched" in changes2[0]

    def test_is_already_patched(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert is_already_patched(patched)
        assert not is_already_patched(WORKER_SOURCE)


class TestVerifyPatch:
    """Tests for verify_patch()."""

    def test_verify_passes_on_correctly_patched(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert errors == []

    def test_verify_fails_on_unpatched(self):
        errors = verify_patch(WORKER_SOURCE, WORKER_SOURCE)
        assert len(errors) > 0
        assert any("PATCH_MARKER" in e for e in errors)

    def test_verify_detects_missing_fp_demo_exit69(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        # Remove the fp_demo_compiler_exit69 check
        patched = patched.replace("fp_demo_compiler_exit69", "fp_demo_removed_69")
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("fp_demo_compiler_exit69" in e for e in errors)

    def test_verify_detects_missing_9392_recovery(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("fp9392_error_recovery", "fp9392_removed")
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("fp9392_error_recovery" in e for e in errors)

    def test_verify_detects_missing_build_6295(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("mt5_build_6295_detected", "mt5_build_removed")
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("mt5_build_6295_detected" in e for e in errors)


class TestFindFunction:
    """Tests for find_function()."""

    def test_find_existing_function(self):
        start, end = find_function(WORKER_SOURCE, "handoff_event")
        assert start is not None
        assert end is not None
        assert "def handoff_event" in WORKER_SOURCE[start:end]

    def test_find_nonexistent_function(self):
        start, end = find_function(WORKER_SOURCE, "nonexistent_function")
        assert start is None
        assert end is None


class TestDryRun:
    """Tests for CLI dry-run mode."""

    def test_dry_run_no_file_write(self, tmp_path):
        worker = tmp_path / "worker_feed_v8_fp_handoff.py"
        worker.write_text(WORKER_SOURCE)
        from reconcile_fp_worker_handoff import main as cli_main

        import argparse
        sys.argv = [
            "reconcile_fp_worker_handoff.py",
            "--worker-path", str(worker),
            "--dry-run",
        ]
        # Capture exit — should be 0 for dry run with valid patch
        try:
            cli_main()
        except SystemExit as e:
            assert e.code == 0
        # File should be unchanged (dry run)
        assert worker.read_text() == WORKER_SOURCE

    def test_missing_file_exits_error(self, tmp_path):
        from reconcile_fp_worker_handoff import main as cli_main

        sys.argv = [
            "reconcile_fp_worker_handoff.py",
            "--worker-path", str(tmp_path / "nonexistent.py"),
        ]
        try:
            cli_main()
            assert False, "Should have exited"
        except SystemExit as e:
            assert e.code == 1


class TestPatchContent:
    """Verify the exact content of inserted corrections."""

    def test_fp_demo_exit69_bounded(self):
        """The FP DEMO exit 69 check must be bounded and DEMO-scoped."""
        assert "mode==\"DEMO\"" in FP_DEMO_EXIT69_CHECK
        assert "process.returncode==69" in FP_DEMO_EXIT69_CHECK
        assert "attempt<2" in FP_DEMO_EXIT69_CHECK
        assert "time.monotonic()-started<90" in FP_DEMO_EXIT69_CHECK
        assert "fp1622_mql_recompile_startup_handoff(tail)" in FP_DEMO_EXIT69_CHECK
        assert "not was_connected" in FP_DEMO_EXIT69_CHECK

    def test_fp_demo_exit69_never_infers_auth(self):
        """The exit 69 check must never infer authentication."""
        assert "Never infer broker authentication from exit code 69" in FP_DEMO_EXIT69_CHECK

    def test_build_6295_read_only(self):
        """Build 6295 detection must be read-only (no broker behavior changes)."""
        assert "read-only" in BUILD6295_FUNC
        assert "without" in BUILD6295_FUNC
        assert "inferring any broker authentication" in BUILD6295_FUNC

    def test_9392_recovery_scoped_to_live_readonly(self):
        """9392 recovery must be scoped to LIVE read-only, no order calls."""
        assert "9392 LIVE in ERROR" in FP9392_RECOVERY_CHECK
        assert "read-only" in FP9392_RECOVERY_CHECK
        assert "no order calls" in FP9392_RECOVERY_CHECK
        assert "no LIVE" in FP9392_RECOVERY_CHECK
        assert "enablement" in FP9392_RECOVERY_CHECK

    def test_staging_old_and_new_consistent(self):
        """The staging OLD pattern must be a substring of the call site."""
        assert "stage_fp_live_9392_verified_readonly_history" in FP9392_STAGING_OLD
        assert "stage_fp_live_9392_verified_readonly_history" in FP9392_STAGING_NEW
        assert "fp9392_error_recovery" in FP9392_STAGING_NEW
