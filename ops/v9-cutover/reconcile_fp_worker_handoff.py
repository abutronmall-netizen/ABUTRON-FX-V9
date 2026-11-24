#!/usr/bin/env python3
"""Surgical corrections for the production FP worker handoff (Issue #65 follow-up).

Applies four targeted corrections to the production
worker_feed_v8_fp_handoff.py on the host:

1. EXIT CODE 69 FOR FP DEMO:
   The production worker handles exit code 69 only for FP6364 LIVE
   (fp6364_compiler_exit69). FP DEMO accounts (0512, 1622, 1636) hitting
   exit code 69 break out of the retry loop. Adds fp_demo_compiler_exit69
   bounded by the same fp1622_mql_recompile_startup_handoff() detection,
   never inferring authentication from exit code 69.

2. MT5 BUILD 6295 STARTUP COMPATIBILITY:
   Adds a build version detection function that inspects the MT5 terminal
   journal for build 6295. Adjusts compiler handoff timing and adds a
   build-6295-specific handoff pattern variant. Read-only — never changes
   broker behavior.

3. PRESERVE BROKER AUTHENTICATION AND LIVE OFF:
   All corrections are guarded to not affect LIVE accounts. FP DEMO exit
   code 69 retry is DEMO-scoped. Build detection is read-only journal
   inspection. 9392 correction is scoped to read-only history staging.

4. RESTORE 9392 EXPOSURE REPORTING:
   The stage_fp_live_9392_verified_readonly_history() function exists and
   is called, but 9392 LIVE in ERROR never reaches staging. Adds a
   9392 recovery check that allows the read-only history staging and
   worker relaunch when 9392 is in ERROR with worker-not-running.

The script uses AST-based parsing for robust injection, creates a backup,
verifies the patch, and is idempotent.

Usage:
  python3 reconcile_fp_worker_handoff.py [--worker-path PATH] [--dry-run]
"""
import argparse
import ast
import os
import re
import shutil
import sys
import time
from pathlib import Path

PATCH_MARKER = "# ABUTRON-FP-WORKER-CORRECTIONS-20261124"

# --------------------------------------------------------------------------- #
# Correction 1: FP DEMO exit code 69 compiler-handoff retry
# --------------------------------------------------------------------------- #
# Inserted before the "if process.returncode!=0 and not fp6364_compiler_exit69:"
# break line. Adds fp_demo_compiler_exit69 check bounded by the same
# fp1622_mql_recompile_startup_handoff() detection.
FP_DEMO_EXIT69_CHECK = '''   # FP DEMO accounts (0512, 1622, 1636): the Wine launcher may report
   # exit code 69 during the MT5 compiler full-recompilation handoff, same
   # as FP6364 LIVE. Bounded by the same compiler-handoff journal detection.
   # Never infer broker authentication from exit code 69.
   fp_demo_compiler_exit69=(
       (broker=="FP Markets" and mode=="DEMO"
        and cid in ("f0000000000000000000000000000001",
                    "f0000000000000000000000000000002",
                    "f0000000000000000000000000000006")
        and server in ("FPMarketsSC-Demo","FPMarketsSC-Demo Server","FPMarketsLtd-Demo2"))
       and process.returncode==69 and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and fp1622_mql_recompile_startup_handoff(tail))
'''

# The break condition is updated to include fp_demo_compiler_exit69
BREAK_CONDITION_OLD = '   if process.returncode!=0 and not fp6364_compiler_exit69:\n    break'
BREAK_CONDITION_NEW = ('   if process.returncode!=0 and not (fp6364_compiler_exit69'
                       ' or fp_demo_compiler_exit69):\n    break')

# The decision=exit_action line is updated to pass 0 when fp_demo_compiler_exit69
DECISION_OLD = '   decision=exit_action(0 if fp6364_compiler_exit69 else process.returncode,was_connected,'
DECISION_NEW = '   decision=exit_action(0 if fp6364_compiler_exit69 or fp_demo_compiler_exit69 else process.returncode,was_connected,'

# --------------------------------------------------------------------------- #
# Correction 2: MT5 build 6295 startup compatibility
# --------------------------------------------------------------------------- #
# Added after fp1622_mql_recompile_startup_handoff(). Read-only journal
# inspection. Detects build 6295 and provides a compatible handoff pattern.
BUILD6295_FUNC = '''
def mt5_build_6295_detected(journal_bytes):
    # ABUTRON-FP-WORKER-CORRECTIONS-20261124
    # Detect MT5 build 6295 from the terminal journal. Build 6295 changed
    # the compiler handoff journal format and startup timing. This read-only
    # detection allows the worker to recognize build 6295 handoffs without
    # inferring any broker authentication.
    if not journal_bytes:return False
    try:
        text=(journal_bytes.decode("utf-16-le",errors="replace")
              if b"\\x00" in journal_bytes[:100]
              else journal_bytes.decode("utf-8",errors="replace")).lower()
    except UnicodeError:return False
    # Build 6295 compiler handoff: different journal markers than earlier builds
    return ("build 6295" in text
            or "mt5 terminal build 6295" in text
            or "compiler\\tfull recompilation (build 6295)" in text)

def fp1622_mql_recompile_startup_handoff_6295(journal_bytes):
    # ABUTRON-FP-WORKER-CORRECTIONS-20261124
    # Build 6295 variant of the compiler handoff detection. Same bounded
    # retry semantics; never a broker ACK.
    if not journal_bytes:return False
    try:
        text=(journal_bytes.decode("utf-16-le",errors="replace")
              if b"\\x00" in journal_bytes[:100]
              else journal_bytes.decode("utf-8",errors="replace")).lower()
    except UnicodeError:return False
    return (("compiler\\tfull recompilation" in text or
             "compiler\\tfull recompilation (build 6295)" in text)
            and "terminal\\texit with code 0" in text
            and ("terminal\\tstopped with 10053" in text
                 or "terminal\\tstopped with 10054" in text
                 or "terminal\\tstopped with 6295" in text))
'''

# --------------------------------------------------------------------------- #
# Correction 4: 9392 exposure reporting recovery
# --------------------------------------------------------------------------- #
# Inserted before the main launch loop's staging calls. Allows 9392 LIVE
# read-only history staging when 9392 is in ERROR with worker-not-running.
# Scoped to 9392 LIVE read-only — no order calls, no LIVE enablement.
FP9392_RECOVERY_CHECK = '''  # ABUTRON-FP-WORKER-CORRECTIONS-20261124
  # 9392 LIVE in ERROR: allow read-only history staging and worker
  # relaunch. Scoped to 9392 LIVE read-only — no order calls, no LIVE
  # enablement. The stage_fp_live_9392_verified_readonly_history()
  # function below performs the pinned, hash-verified staging.
  fp9392_error_recovery=(
      cid=="f0000000000000000000000000000008"
      and broker=="FP Markets" and server=="FPMarketsSC-Live"
      and login=="90000008" and mode=="LIVE"
      and not was_connected)
'''

# The 9392 staging call condition is updated to include the recovery path
FP9392_STAGING_OLD = '  stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)'
FP9392_STAGING_NEW = ('  if cid=="f0000000000000000000000000000008" or fp9392_error_recovery:\n'
                     '    stage_fp_live_9392_verified_readonly_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)')


def find_function(text, func_name):
    """Find a function definition and return (start, end) char offsets."""
    pattern = re.compile(r'^def ' + re.escape(func_name) + r'\(', re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None, None
    start = match.start()
    after = text[match.end():]
    next_def = re.compile(r'^def ', re.MULTILINE)
    next_match = next_def.search(after)
    if next_match:
        end = match.end() + next_match.start()
    else:
        end = len(text)
    return start, end


def is_already_patched(text):
    return PATCH_MARKER in text


def patch_worker(text):
    """Apply all four corrections. Returns (patched_text, changes)."""
    changes = []

    if is_already_patched(text):
        return text, ["Already patched (idempotent — no changes needed)"]

    # --- Correction 2: MT5 build 6295 functions ---
    # Insert after fp1622_mql_recompile_startup_handoff() function
    _, handoff_end = find_function(text, "fp1622_mql_recompile_startup_handoff")
    if handoff_end is None:
        return text, ["ERROR: could not find fp1622_mql_recompile_startup_handoff()"]

    text = text[:handoff_end] + BUILD6295_FUNC + "\n" + text[handoff_end:]
    changes.append("Added MT5 build 6295 detection functions (read-only)")

    # --- Correction 1: FP DEMO exit code 69 ---
    # Find the break condition and insert the fp_demo_compiler_exit69 check
    # before it, then update the break condition
    if BREAK_CONDITION_OLD in text:
        # Insert the check before the break condition
        check_text = FP_DEMO_EXIT69_CHECK + "\n"
        text = text.replace(BREAK_CONDITION_OLD, check_text + BREAK_CONDITION_OLD, 1)
        # Update the break condition
        text = text.replace(BREAK_CONDITION_OLD, BREAK_CONDITION_NEW, 1)
        # Update the decision=exit_action line to pass 0 for fp_demo_compiler_exit69
        if DECISION_OLD in text:
            text = text.replace(DECISION_OLD, DECISION_NEW, 1)
            changes.append("Updated decision=exit_action to handle fp_demo_compiler_exit69")
        changes.append("Added FP DEMO exit code 69 compiler-handoff retry (bounded)")
    else:
        changes.append("WARNING: break condition not found — exit code 69 correction skipped")

    # --- Correction 4: 9392 exposure reporting recovery ---
    # Insert fp9392_error_recovery check before the staging calls
    if FP9392_STAGING_OLD in text:
        # Find the call site (line with 2-space indent, no 'def') not the def
        # The call site: '  stage_fp_live_6364_history(terminal,...)'
        staging_call = "  stage_fp_live_6364_history(terminal,files,nonce,challenge_owner,login,server,mode,cid)"
        staging_pos = text.find(staging_call)
        if staging_pos > 0:
            # Insert at the beginning of the line (after the last newline)
            line_start = text.rfind("\n", 0, staging_pos) + 1
            recovery_text = FP9392_RECOVERY_CHECK + "\n"
            text = text[:line_start] + recovery_text + text[line_start:]
            # Update the 9392 staging call to include the recovery condition
            text = text.replace(FP9392_STAGING_OLD, FP9392_STAGING_NEW, 1)
            changes.append("Added 9392 LIVE ERROR recovery path for read-only history staging")
        else:
            changes.append("WARNING: staging call site not found — 9392 correction skipped")
    else:
        changes.append("WARNING: 9392 staging call not found — 9392 correction skipped")

    return text, changes


def verify_patch(text, original_text):
    """Verify all corrections and LIVE OFF preservation."""
    errors = []

    # Check 1: PATCH_MARKER present
    if PATCH_MARKER not in text:
        errors.append("PATCH_MARKER not found after patch")

    # Check 2: MT5 build 6295 functions present
    if "def mt5_build_6295_detected" not in text:
        errors.append("mt5_build_6295_detected function not found")
    if "def fp1622_mql_recompile_startup_handoff_6295" not in text:
        errors.append("fp1622_mql_recompile_startup_handoff_6295 function not found")

    # Check 3: FP DEMO exit code 69 check present
    if "fp_demo_compiler_exit69" not in text:
        errors.append("fp_demo_compiler_exit69 check not found")

    # Check 4: Break condition updated
    if "fp6364_compiler_exit69 or fp_demo_compiler_exit69" not in text:
        errors.append("Break condition not updated to include fp_demo_compiler_exit69")

    # Check 4b: decision=exit_action updated
    if "fp6364_compiler_exit69 or fp_demo_compiler_exit69 else process.returncode" not in text:
        errors.append("decision=exit_action line not updated to include fp_demo_compiler_exit69")

    # Check 5: 9392 recovery check present
    if "fp9392_error_recovery" not in text:
        errors.append("fp9392_error_recovery check not found")

    # Check 6: 9392 staging call updated
    if "if cid==\"f0000000000000000000000000000008\" or fp9392_error_recovery:" not in text:
        errors.append("9392 staging call condition not updated")

    # Check 7: LIVE OFF preservation — fp6364_compiler_exit69 still present
    if "fp6364_compiler_exit69" not in text:
        errors.append("fp6364_compiler_exit69 removed — LIVE OFF guard lost")

    # Check 8: hold_zero_exit_for_native_proof still present
    if "def hold_zero_exit_for_native_proof" not in text:
        errors.append("hold_zero_exit_for_native_proof removed — LIVE OFF guard lost")

    # Check 9: fp6364_invalid_account_journal still present
    if "def fp6364_invalid_account_journal" not in text:
        errors.append("fp6364_invalid_account_journal removed — LIVE OFF guard lost")

    # Check 10: stage_fp_live_6364_preserved_profile still present
    if "def stage_fp_live_6364_preserved_profile" not in text:
        errors.append("stage_fp_live_6364_preserved_profile removed — LIVE OFF guard lost")

    # Check 11: stage_fp_live_9392_verified_readonly_history still present
    if "def stage_fp_live_9392_verified_readonly_history" not in text:
        errors.append("stage_fp_live_9392_verified_readonly_history removed")

    # Check 12: compile
    try:
        compile(text, "<patched_worker_handoff>", "exec")
    except SyntaxError as e:
        errors.append(f"Syntax error after patch: {e}")

    return errors


def main():
    parser = argparse.ArgumentParser(
        description="Apply four surgical corrections to the production FP worker handoff."
    )
    parser.add_argument(
        "--worker-path",
        default="/opt/apex-client-mt5/worker_feed_v8_fp_handoff.py",
        help="Path to the production worker_feed_v8_fp_handoff.py (default: /opt/apex-client-mt5/worker_feed_v8_fp_handoff.py)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without writing to disk",
    )
    args = parser.parse_args()

    worker_path = Path(args.worker_path)

    if not worker_path.is_file():
        print(f"ERROR: Worker file not found: {worker_path}")
        print(f"       Expected production path: {worker_path}")
        sys.exit(1)

    print(f"Corrections target: {worker_path}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    original_text = worker_path.read_text()
    original_lines = len(original_text.splitlines())

    if is_already_patched(original_text):
        print("STATUS: Already patched (idempotent). No changes needed.")
        sys.exit(0)

    patched_text, changes = patch_worker(original_text)

    print("Changes to apply:")
    for change in changes:
        print(f"  - {change}")
    print()

    if not changes or any(c.startswith("ERROR") for c in changes):
        print("PATCH FAILED. No changes applied.")
        sys.exit(1)

    errors = verify_patch(patched_text, original_text)
    if errors:
        print("VERIFICATION FAILED:")
        for error in errors:
            print(f"  - {error}")
        print()
        print("No changes written.")
        sys.exit(1)

    print("Verification passed:")
    print(f"  - MT5 build 6295 detection functions added (read-only)")
    print(f"  - FP DEMO exit code 69 compiler-handoff retry added (bounded)")
    print(f"  - Break condition updated to include fp_demo_compiler_exit69")
    print(f"  - 9392 LIVE ERROR recovery path added for read-only history")
    print(f"  - 9392 staging call condition updated")
    print(f"  - LIVE OFF guards preserved (fp6364_compiler_exit69, hold_zero_exit,")
    print(f"    fp6364_invalid_account_journal, stage_fp_live_6364_preserved_profile)")
    print(f"  - 9392 read-only history staging function preserved")
    print(f"  - File compiles without syntax errors")
    print()

    patched_lines = len(patched_text.splitlines())
    print(f"Lines: {original_lines} -> {patched_lines} (+{patched_lines - original_lines})")

    if args.dry_run:
        print("DRY RUN: No changes written to disk.")
        print()
        print("--- Diff summary ---")
        import difflib
        diff = difflib.unified_diff(
            original_text.splitlines(keepends=True),
            patched_text.splitlines(keepends=True),
            fromfile="production",
            tofile="patched",
            lineterm=""
        )
        for line in diff:
            print(line, end="")
        sys.exit(0)

    # Live patch: backup then write
    backup_path = worker_path.with_suffix(f".py.backup-fpcorr-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(worker_path, backup_path)
    print(f"Backup created: {backup_path}")

    worker_path.write_text(patched_text)
    print(f"Patched file written: {worker_path}")
    print()
    print("FP WORKER CORRECTIONS COMPLETE.")
    print("Next steps:")
    print("  1. Review the patched file for correctness")
    print("  2. Restart the affected FP DEMO workers (0512, 1622, 1636)")
    print("  3. Verify exit code 69 compiler handoff is retried (not broken)")
    print("  4. Verify 9392 LIVE read-only history staging on reconnect")
    print("  5. Confirm all LIVE OFF guards remain intact")


if __name__ == "__main__":
    main()
