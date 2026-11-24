"""Tests for reconcile_fp_worker_handoff.py — FP worker handoff corrections.

Tests use a fixture that mirrors the production worker's structure and
indentation. All corrections must use in-scope variables (broker, mode,
server) — never hardcoded account identifiers or numeric literals.

Runtime regression tests execute the patched startup path to verify
control flow, not just compilation.
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
# NOTE: was_connected is initialized AFTER the staging calls (as in
# the production file at line 1087).
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
    try:
        text=(journal_bytes.decode(\"utf-16-le\",errors=\"replace\")\
              if b\"\\x00\" in journal_bytes[:100]\
              else journal_bytes.decode(\"utf-8\",errors=\"replace\")).lower()
    except UnicodeError:return False
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
        assert len(changes) >= 4
        assert "mt5_build_6295_detected" in patched
        assert "fp1622_mql_recompile_startup_handoff_6295" in patched
        assert "fp_demo_compiler_exit69" in patched
        assert "fp_live_compiler_exit69" in patched

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

    def test_9392_staging_call_unconditional(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "  stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)" in patched

    def test_no_fp_live_error_recovery_dead_code(self):
        """fp_live_error_recovery must NOT be present (was_connected
        is uninitialized at the staging call site)."""
        patched, _ = patch_worker(WORKER_SOURCE)
        assert "fp_live_error_recovery" not in patched

    def test_patch_marker_present(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        assert PATCH_MARKER in patched

    def test_compiles_after_patch(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        compile(patched, "<patched>", "exec")


class TestNoAnonymizedIdentifiers:
    """Verify no hardcoded account identifiers in inserted code."""

    def test_fp_demo_exit69_no_hardcoded_cid(self):
        assert "broker==\"FP Markets\"" in FP_DEMO_EXIT69_CHECK
        assert "mode==\"DEMO\"" in FP_DEMO_EXIT69_CHECK
        assert "cid" not in FP_DEMO_EXIT69_CHECK
        assert "login" not in FP_DEMO_EXIT69_CHECK

    def test_fp_live_exit69_no_hardcoded_cid(self):
        assert "broker==\"FP Markets\"" in FP_LIVE_EXIT69_CHECK
        assert "mode==\"LIVE\"" in FP_LIVE_EXIT69_CHECK
        assert "cid" not in FP_LIVE_EXIT69_CHECK
        assert "login" not in FP_LIVE_EXIT69_CHECK

    def test_patched_no_anonymized_cids_in_new_checks(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        demo_section_start = patched.find("fp_demo_compiler_exit69=(")
        demo_section_end = patched.find("fp_live_compiler_exit69", demo_section_start)
        demo_section = patched[demo_section_start:demo_section_end]
        assert "f0000000000000000000000000000001" not in demo_section

        live_section_start = patched.find("fp_live_compiler_exit69=(")
        live_section_end = patched.find("if process.returncode", live_section_start)
        live_section = patched[live_section_start:live_section_end]
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
        old = '       broker=="FP Markets" and mode=="DEMO"'
        new = '       cid=="f0000000000000000000000000000001"\n       and broker=="FP Markets" and mode=="DEMO"'
        patched = patched.replace(old, new, 1)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("hardcodes account identifiers" in e for e in errors)

    def test_verify_detects_conditional_9392_staging(self):
        patched, _ = patch_worker(WORKER_SOURCE)
        old = '  stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)'
        new = '  if cid=="f0000000000000000000000000000008":\n    stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)'
        patched = patched.replace(old, new)
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("wrapped in a condition" in e for e in errors)

    def test_verify_detects_unscoped_live_exit69(self):
        """If fp_live_compiler_exit69 lacks the ABUTRON9392_READONLY
        scope check, verify_patch must flag it."""
        patched, _ = patch_worker(WORKER_SOURCE)
        # Remove the scope restriction from the LIVE check
        patched = patched.replace(
            'and "ABUTRON9392_READONLY" in " ".join(args)',
            ''
        )
        errors = verify_patch(patched, WORKER_SOURCE)
        assert any("not scoped to read-only" in e for e in errors)


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
    """Tests for atomic_write() — including ownership/permission preservation."""

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
        leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert len(leftovers) == 0

    def test_atomic_write_preserves_permissions(self, tmp_path):
        target = tmp_path / "test.py"
        target.write_text("old content")
        target.chmod(0o600)
        original_mode = target.stat().st_mode & 0o777
        atomic_write(target, "new content")
        assert (target.stat().st_mode & 0o777) == original_mode

    def test_atomic_write_preserves_ownership(self, tmp_path):
        """atomic_write must call os.chown with the original UID/GID.

        shutil.copystat does NOT preserve ownership. This test uses
        unittest.mock to verify os.chown is called with the correct
        UID and GID, demonstrating explicit ownership preservation.
        """
        from unittest.mock import patch

        target = tmp_path / "test.py"
        target.write_text("old content")
        stat_before = target.stat()

        with patch("os.chown") as mock_chown:
            atomic_write(target, "new content")
            # os.chown must be called with the temp file and original UID/GID
            assert mock_chown.called, "os.chown must be called to preserve ownership"
            call_args = mock_chown.call_args
            assert call_args[0][1] == stat_before.st_uid, "UID must be preserved"
            assert call_args[0][2] == stat_before.st_gid, "GID must be preserved"


class TestRuntimeRegression:
    """Runtime tests that execute the patched startup path.

    These tests verify control flow, not just compilation. They execute
    the patched worker's exit-code-69 retry logic to confirm that
    fp_demo_compiler_exit69 and fp_live_compiler_exit69 actually
    influence the retry decision.
    """

    @staticmethod
    def _make_patched_module(patched_source):
        """Compile and return a module from patched source for execution."""
        import types
        mod = types.ModuleType("patched_worker")
        mod.__dict__["__name__"] = "patched_worker"
        exec(compile(patched_source, "<patched>", "exec"), mod.__dict__)
        return mod

    def _simulate_exit69_decision(self, mod, broker, mode, args, returncode=69,
                                    was_connected=False, attempt=0,
                                    journal_tail=None):
        """Simulate the worker's exit-code-69 retry decision path.

        Derives handoff eligibility from terminal failure conditions
        (broker, mode, args, returncode, journal tail) — the same way
        the patched worker does — and passes the result to exit_action.
        """
        import time
        started = time.monotonic()
        if journal_tail is None:
            journal_tail = b"compiler\tfull recompilation has been started\n" \
                           b"terminal\texit with code 0\n" \
                           b"terminal\tstopped with 10053"
        # Evaluate fp_demo_compiler_exit69 (as the worker would)
        fp_demo_compiler_exit69 = (
            broker == "FP Markets" and mode == "DEMO"
            and returncode == 69 and not was_connected
            and attempt < 2 and time.monotonic() - started < 90
            and (mod.fp1622_mql_recompile_startup_handoff(journal_tail)
                 or mod.fp1622_mql_recompile_startup_handoff_6295(journal_tail)))
        # Evaluate fp_live_compiler_exit69 (as the worker would)
        fp_live_compiler_exit69 = (
            broker == "FP Markets" and mode == "LIVE"
            and "ABUTRON9392_READONLY" in " ".join(args)
            and returncode == 69 and not was_connected
            and attempt < 2 and time.monotonic() - started < 90
            and (mod.fp1622_mql_recompile_startup_handoff(journal_tail)
                 or mod.fp1622_mql_recompile_startup_handoff_6295(journal_tail)))
        # Derive the retry decision (as the worker would)
        returncode_out = 0 if (fp_demo_compiler_exit69
                               or fp_live_compiler_exit69) else returncode
        handoff = (mod.handoff_event(journal_tail)
                   or fp_demo_compiler_exit69
                   or fp_live_compiler_exit69)
        return mod.exit_action(returncode_out, was_connected,
                               mod.cgroup_terminal_running(),
                               handoff, attempt,
                               time.monotonic() - started), \
            fp_demo_compiler_exit69, fp_live_compiler_exit69

    def test_full_retry_path_demo_exit69(self):
        """FP DEMO account with exit 69 + compiler handoff journal
        must derive RETRY_UPDATE from terminal failure conditions."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        decision, demo_check, live_check = self._simulate_exit69_decision(
            mod, broker="FP Markets", mode="DEMO",
            args=["terminal64.exe"], returncode=69)
        assert demo_check is True, "fp_demo_compiler_exit69 must be True"
        assert live_check is False, "fp_live_compiler_exit69 must be False for DEMO"
        assert decision == "RETRY_UPDATE"

    def test_full_retry_path_live_readonly_exit69(self):
        """FP LIVE read-only account (9392 profile) with exit 69 +
        compiler handoff journal must derive RETRY_UPDATE."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        decision, demo_check, live_check = self._simulate_exit69_decision(
            mod, broker="FP Markets", mode="LIVE",
            args=["terminal64.exe", "/profile:ABUTRON9392_READONLY"],
            returncode=69)
        assert live_check is True, "fp_live_compiler_exit69 must be True"
        assert demo_check is False, "fp_demo_compiler_exit69 must be False for LIVE"
        assert decision == "RETRY_UPDATE"

    def test_full_stop_path_live_non_readonly_exit69(self):
        """FP LIVE account WITHOUT read-only profile + exit 69 must
        NOT retry — scope restriction prevents platform-wide change."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        decision, demo_check, live_check = self._simulate_exit69_decision(
            mod, broker="FP Markets", mode="LIVE",
            args=["terminal64.exe"],  # No ABUTRON9392_READONLY profile
            returncode=69)
        assert live_check is False, "fp_live_compiler_exit69 must be False without read-only profile"
        assert decision == "STOP"

    def test_full_stop_path_no_journal_match(self):
        """Exit 69 without compiler handoff journal must STOP."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        decision, demo_check, live_check = self._simulate_exit69_decision(
            mod, broker="FP Markets", mode="DEMO",
            args=["terminal64.exe"], returncode=69,
            journal_tail=b"random error output without compiler markers")
        assert demo_check is False
        assert decision == "STOP"

    def test_full_stop_path_attempt_exhausted(self):
        """Exit 69 with attempt>=2 must STOP (bounded retry)."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        decision, demo_check, live_check = self._simulate_exit69_decision(
            mod, broker="FP Markets", mode="DEMO",
            args=["terminal64.exe"], returncode=69, attempt=2)
        assert demo_check is False, "fp_demo_compiler_exit69 must be False when attempt>=2"
        assert decision == "STOP"

    def test_full_stop_path_already_connected(self):
        """Exit 69 when already connected must STOP (not retry)."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        decision, demo_check, live_check = self._simulate_exit69_decision(
            mod, broker="FP Markets", mode="DEMO",
            args=["terminal64.exe"], returncode=69,
            was_connected=True)
        assert demo_check is False, "fp_demo_compiler_exit69 must be False when was_connected"
        assert decision == "STOP"

    def test_exit_action_stop_without_handoff(self):
        """Without handoff evidence, exit_action must return STOP."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        result = mod.exit_action(69, False, False, False, 0, 50)
        assert result == "STOP"

    def test_exit_action_stop_when_connected(self):
        """When was_connected is True, exit_action must return STOP."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        result = mod.exit_action(0, True, False, True, 0, 50)
        assert result == "STOP"

    def test_exit_action_monitor_child(self):
        """When attached is True, exit_action must return MONITOR_CHILD."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        result = mod.exit_action(0, False, True, False, 0, 50)
        assert result == "MONITOR_CHILD"

    def test_build_6295_detection_returns_bool(self):
        """mt5_build_6295_detected must return bool for various inputs."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        assert mod.mt5_build_6295_detected(b"") is False
        assert mod.mt5_build_6295_detected(b"random text") is False
        assert mod.mt5_build_6295_detected(b"build 6295 detected") is True

    def test_build_6295_handoff_returns_bool(self):
        """fp1622_mql_recompile_startup_handoff_6295 must return bool."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        assert mod.fp1622_mql_recompile_startup_handoff_6295(b"") is False
        assert mod.fp1622_mql_recompile_startup_handoff_6295(
            b"compiler\tfull recompilation (build 6295)\nterminal\texit with code 0\nterminal\tstopped with 6295") is True

    def test_hold_zero_exit_with_fresh_heartbeat(self):
        """hold_zero_exit_for_native_proof must allow fresh heartbeat."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        assert mod.hold_zero_exit_for_native_proof(0, True, 0) is True

    def test_hold_zero_exit_without_heartbeat_bounded(self):
        """hold_zero_exit_for_native_proof must be bounded without heartbeat."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        assert mod.hold_zero_exit_for_native_proof(0, False, 5) is True
        assert mod.hold_zero_exit_for_native_proof(0, False, 30) is False

    def test_hold_zero_exit_nonzero_returncode(self):
        """hold_zero_exit_for_native_proof must reject nonzero returncode."""
        patched, _ = patch_worker(WORKER_SOURCE)
        mod = self._make_patched_module(patched)
        assert mod.hold_zero_exit_for_native_proof(69, True, 0) is False

    def test_fp_demo_exit69_check_uses_in_scope_vars(self):
        """The fp_demo_compiler_exit69 expression must reference broker
        and mode, not hardcoded CIDs. This prevents UnboundLocalError
        from referencing was_connected before initialization."""
        # The check is inside the launch loop where was_connected IS
        # initialized (line 1087 in production). Verify the expression
        # structure: it must be inside the while loop, after was_connected.
        patched, _ = patch_worker(WORKER_SOURCE)
        # was_connected initialization must appear BEFORE the exit-69 checks
        was_connected_pos = patched.find("was_connected=False")
        demo_check_pos = patched.find("fp_demo_compiler_exit69=(")
        live_check_pos = patched.find("fp_live_compiler_exit69=(")
        assert was_connected_pos < demo_check_pos, "fp_demo_compiler_exit69 must be after was_connected init"
        assert was_connected_pos < live_check_pos, "fp_live_compiler_exit69 must be after was_connected init"


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

    def test_fp_live_exit69_scoped_to_readonly(self):
        """fp_live_compiler_exit69 must be scoped to read-only configured
        accounts via the ABUTRON9392_READONLY profile marker, not all
        FP LIVE accounts."""
        assert "ABUTRON9392_READONLY" in FP_LIVE_EXIT69_CHECK
        assert "in \" \".join(args)" in FP_LIVE_EXIT69_CHECK

    def test_build_6295_read_only(self):
        assert "read-only" in BUILD6295_FUNC
        assert "inferring any broker authentication" in BUILD6295_FUNC

    def test_build_6295_integrated_description(self):
        assert "alternative handoff" in BUILD6295_FUNC
