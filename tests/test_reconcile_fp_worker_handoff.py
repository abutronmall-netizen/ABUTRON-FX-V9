"""Tests for reconcile_fp_worker_handoff.py — four-priority FP worker corrections.

Tests use a fixture that mirrors the production worker's structure and
indentation. All corrections must use in-scope variables (broker, mode,
server) — never hardcoded account identifiers or numeric literals.
"""
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
    FP_LIVE_EXIT69_CHECK,
    FP9392_RECOVERY_CHECK,
    BREAK_CONDITION_OLD,
    BREAK_CONDITION_NEW,
    DECISION_OLD,
    DECISION_NEW,
    HANDOFF_OLD,
    HANDOFF_NEW,
    find_function,
    is_already_patched,
    patch_worker,
    verify_patch,
    atomic_write,
)

# Fixture mirrors the production worker's indentation:
# main() body at 1-space, try: at 1-space, body at 2-space,
# while at 2-space, body at 3-space.
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
              handoff_event(tail) or trusted_fp1622_compiler_relaunch or trusted_fp6364_compiler_relaunch,
              attempt,time.monotonic()-started)
   if decision==\"STOP\":
    break
 return
"""


class TestPatchWorker:
    """Tests for patch_worker() applying all corrections."""

    def test_all_corrections_applied(self):
        patched, changes = patch_worker(WORKER_SOURCE)
        assert len(changes) >= 5
        assert "mt5_build_6295_detected" in patched
        assert "fp1622_mql_recompile_startup_handoff_6295" in patched
        assert "fp_demo_compiler_exit69" in patched
        assert "fp_live_compiler_exit69" in patched
        assert "fp_live_error_recovery" in patched

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

    def test_fp_live_exit69_check_present(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp_live_compiler_exit69=(" in patched
        assert "process.returncode==69" in patched
        assert "not was_connected" in patched
        assert "attempt<2" in patched

    def test_build_6295_integrated_into_retry(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        # The _6295 variant must appear in the exit-69 checks
        assert "fp1622_mql_recompile_startup_handoff_6295(tail)" in patched

    def test_break_condition_updated(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp6364_compiler_exit69 or fp_demo_compiler_exit69 or fp_live_compiler_exit69" in patched

    def test_decision_returncode_updated(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "0 if fp6364_compiler_exit69 or fp_demo_compiler_exit69 or fp_live_compiler_exit69 else process.returncode" in patched

    def test_handoff_eligibility_updated(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "or fp_demo_compiler_exit69 or fp_live_compiler_exit69," in patched

    def test_9392_recovery_check_present(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp_live_error_recovery=(" in patched
        assert "broker==\"FP Markets\"" in patched
        assert "mode==\"LIVE\"" in patched
        assert "not was_connected" in patched

    def test_9392_staging_call_unconditional(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        # The 9392 staging call must NOT be wrapped in a condition
        assert "  stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)" in patched
        # No if-wrapper around the staging call
        assert 'if cid=="f0000000000000000000000000000008"' not in patched

    def test_patch_marker_present(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert PATCH_MARKER in patched

    def test_compiles_after_patch(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        compile(patched, "<patched>", "exec")


class TestNoAnonymizedIdentifiers:
    """Verify no hardcoded account identifiers in inserted code."""

    def test_fp_demo_exit69_no_hardcoded_cid(self):
        """fp_demo_compiler_exit69 must use broker/mode, not specific CIDs."""
        assert "broker==\"FP Markets\"" in FP_DEMO_EXIT69_CHECK
        assert "mode==\"DEMO\"" in FP_DEMO_EXIT69_CHECK
        assert "cid" not in FP_DEMO_EXIT69_CHECK
        assert "login" not in FP_DEMO_EXIT69_CHECK

    def test_fp_live_exit69_no_hardcoded_cid(self):
        """fp_live_compiler_exit69 must use broker/mode, not specific CIDs."""
        assert "broker==\"FP Markets\"" in FP_LIVE_EXIT69_CHECK
        assert "mode==\"LIVE\"" in FP_LIVE_EXIT69_CHECK
        assert "cid" not in FP_LIVE_EXIT69_CHECK
        assert "login" not in FP_LIVE_EXIT69_CHECK

    def test_9392_recovery_no_hardcoded_cid(self):
        """fp_live_error_recovery must use broker/mode, not specific CIDs."""
        assert "broker==\"FP Markets\"" in FP9392_RECOVERY_CHECK
        assert "mode==\"LIVE\"" in FP9392_RECOVERY_CHECK
        assert "cid" not in FP9392_RECOVERY_CHECK
        assert "login" not in FP9392_RECOVERY_CHECK

    def test_patched_no_anonymized_cids(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        # The inserted code sections must not contain anonymized CIDs
        # (the fixture's existing fp6364_compiler_exit69 has them, but
        # the NEW inserted checks must not)
        demo_section = patched[patched.find("fp_demo_compiler_exit69"):]
        demo_section = demo_section[:demo_section.find("fp_live_compiler_exit69")]
        assert "f0000000000000000000000000000001" not in demo_section

        live_section = patched[patched.find("fp_live_compiler_exit69"):]
        live_section = live_section[:live_section.find("if process.returncode")]
        assert "f0000000000000000000000000000008" not in live_section


class TestLiveOffPreservation:
    """Verify all LIVE OFF guards are preserved after patching."""

    def test_fp6364_compiler_exit69_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp6364_compiler_exit69=(" in patched

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

    def test_exit_action_preserved(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "def exit_action" in patched


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
        patched = patched.replace("fp_demo_compiler_exit69", "fp_demo_removed_69")
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("fp_demo_compiler_exit69" in e for e in errors)

    def test_verify_detects_missing_fp_live_exit69(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("fp_live_compiler_exit69", "fp_live_removed_69")
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("fp_live_compiler_exit69" in e for e in errors)

    def test_verify_detects_missing_build_6295_integration(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("fp1622_mql_recompile_startup_handoff_6295(tail)", "NOT_FOUND")
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("not integrated" in e for e in errors)

    def test_verify_detects_missing_handoff_eligibility(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        patched = patched.replace("or fp_demo_compiler_exit69 or fp_live_compiler_exit69,", ",")
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("handoff eligibility" in e for e in errors)

    def test_verify_detects_hardcoded_cid_in_demo_check(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        # Inject a hardcoded CID into the demo check
        old = '       broker=="FP Markets" and mode=="DEMO"'
        new = '       cid=="f0000000000000000000000000000001"\n       and broker=="FP Markets" and mode=="DEMO"'
        patched = patched.replace(old, new, 1)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("hardcodes account identifiers" in e for e in errors)

    def test_verify_detects_conditional_9392_staging(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        # Wrap the 9392 staging call in a condition (indent the call deeper)
        old = '  stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)'
        new = '  if cid=="f0000000000000000000000000000008":\n    stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)'
        patched = patched.replace(old, new)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("wrapped in a condition" in e for e in errors)


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


class TestAtomicWrite:
    """Tests for atomic_write()."""

    def test_atomic_write_creates_file(self, tmp_path):
        target = tmp_path / "test.py"
        atomic_write(target, "print('hello')")
        assert target.is_file()
        assert target.read_text() == "print('hello')"

    def test_atomic_write_replaces_existing(self, tmp_path):
        target = tmp_path / "test.py"
        target.write_text("old content")
        atomic_write(target, "new content")
        assert target.read_text() == "new content"
        # No temp files left behind
        leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert len(leftovers) == 0


class TestDryRun:
    """Tests for CLI dry-run mode."""

    def test_dry_run_no_file_write(self, tmp_path):
        worker = tmp_path / "worker_feed_v8_fp_handoff.py"
        worker.write_text(WORKER_SOURCE)
        from reconcile_fp_worker_handoff import main as cli_main

        sys.argv = [
            "reconcile_fp_worker_handoff.py",
            "--worker-path", str(worker),
            "--dry-run",
        ]
        try:
            cli_main()
        except SystemExit as e:
            assert e.code == 0
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
        assert "mode==\"DEMO\"" in FP_DEMO_EXIT69_CHECK
        assert "process.returncode==69" in FP_DEMO_EXIT69_CHECK
        assert "attempt<2" in FP_DEMO_EXIT69_CHECK
        assert "time.monotonic()-started<90" in FP_DEMO_EXIT69_CHECK
        assert "not was_connected" in FP_DEMO_EXIT69_CHECK

    def test_fp_demo_exit69_never_infers_auth(self):
        assert "Never infer" in FP_DEMO_EXIT69_CHECK

    def test_fp_demo_exit69_uses_in_scope_vars(self):
        assert "broker==\"FP Markets\"" in FP_DEMO_EXIT69_CHECK
        assert "mode==\"DEMO\"" in FP_DEMO_EXIT69_CHECK
        assert "cid" not in FP_DEMO_EXIT69_CHECK

    def test_fp_live_exit69_bounded(self):
        assert "mode==\"LIVE\"" in FP_LIVE_EXIT69_CHECK
        assert "process.returncode==69" in FP_LIVE_EXIT69_CHECK
        assert "attempt<2" in FP_LIVE_EXIT69_CHECK
        assert "time.monotonic()-started<90" in FP_LIVE_EXIT69_CHECK
        assert "not was_connected" in FP_LIVE_EXIT69_CHECK

    def test_fp_live_exit69_uses_in_scope_vars(self):
        assert "broker==\"FP Markets\"" in FP_LIVE_EXIT69_CHECK
        assert "mode==\"LIVE\"" in FP_LIVE_EXIT69_CHECK
        assert "cid" not in FP_LIVE_EXIT69_CHECK

    def test_build_6295_read_only(self):
        assert "read-only" in BUILD6295_FUNC
        assert "inferring any broker authentication" in BUILD6295_FUNC

    def test_build_6295_integrated_description(self):
        assert "alternative handoff" in BUILD6295_FUNC

    def test_9392_recovery_scoped_to_live_readonly(self):
        assert "FP Markets LIVE" in FP9392_RECOVERY_CHECK
        assert "read-only" in FP9392_RECOVERY_CHECK
        assert "no order calls" in FP9392_RECOVERY_CHECK
        assert "no LIVE" in FP9392_RECOVERY_CHECK
        assert "enablement" in FP9392_RECOVERY_CHECK

    def test_9392_recovery_uses_in_scope_vars(self):
        assert "broker==\"FP Markets\"" in FP9392_RECOVERY_CHECK
        assert "mode==\"LIVE\"" in FP9392_RECOVERY_CHECK
        assert "cid" not in FP9392_RECOVERY_CHECK
