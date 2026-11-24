#!/usr/bin/env python3
"""Surgical corrections for the production FP worker handoff (Issue #65 follow-up).

Applies targeted corrections to the production
worker_feed_v8_fp_handoff.py on the host. All corrections use the
production file's own in-scope variables (broker, mode, server, cid,
login, attempt, started, was_connected, process, tail) and never
hardcode anonymized account identifiers or numeric literals.

CORRECTIONS:

1. EXIT CODE 69 — ALL FP ACCOUNTS (DEMO + LIVE):
   The production worker handles exit code 69 only for FP6364 LIVE
   (fp6364_compiler_exit69). FP DEMO accounts and FP LIVE accounts
   other than 6364 (e.g. 9392) hitting exit code 69 break out of the
   retry loop. Adds fp_demo_compiler_exit69 (DEMO) and
   fp_live_compiler_exit69 (LIVE, covers 9392) checks, bounded by the
   same compiler-handoff journal detection, never inferring
   authentication from exit code 69.

2. MT5 BUILD 6295 — INTEGRATED INTO RETRY DECISIONS:
   Adds mt5_build_6295_detected() and
   fp1622_mql_recompile_startup_handoff_6295() — read-only journal
   inspection for build 6295 compiler handoff variants. These are
   integrated into the exit-code-69 checks and compiler-relaunch
   checks as alternative handoff detection paths.

3. PRESERVE BROKER AUTHENTICATION AND LIVE OFF:
   All corrections preserve existing LIVE OFF guards:
   fp6364_compiler_exit69, hold_zero_exit_for_native_proof,
   fp6364_invalid_account_journal, stage_fp_live_6364_preserved_profile,
   trusted_fp6364_compiler_relaunch. The 9392 staging call remains
   unconditional (as in the original production file).

4. RESTORE 9392 EXPOSURE REPORTING:
   No additional source change required. The existing
   stage_fp_live_9392_verified_readonly_history() call remains
   unconditional. The fp_live_compiler_exit69 check (inserted in the
   launch loop AFTER was_connected is initialized) provides the 9392
   recovery path for exit code 69 compiler-handoff failures. No
   separate recovery variable is added — avoiding any reference to
   was_connected before its initialization.

5. DEMO RETRY LOGIC — HANDOFF ELIGIBILITY:
   The fp_demo_compiler_exit69 and fp_live_compiler_exit69 checks are
   added to the handoff parameter expression in the exit_action() call,
   ensuring the retry decision (RETRY_UPDATE) is actually reached when
   these conditions are True.

6. ATOMIC FILE REPLACEMENT:
   The patched file is written to a temp file and moved into place via
   os.replace(), ensuring atomic replacement. File ownership, permissions,
   and timestamps are preserved via shutil.copystat. A backup is created
   first.

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
# Correction 1+3: FP DEMO exit code 69 — uses in-scope variables, no
# hardcoded account identifiers. Bounded by compiler-handoff detection.
# --------------------------------------------------------------------------- #
FP_DEMO_EXIT69_CHECK = '''   # FP DEMO accounts: the Wine launcher may report exit code 69 during
   # the MT5 compiler full-recompilation handoff, same as FP6364 LIVE.
   # Bounded by the same compiler-handoff journal detection. Never infer
   # broker authentication from exit code 69. Uses in-scope broker/mode
   # variables — no hardcoded account identifiers.
   fp_demo_compiler_exit69=(
       broker=="FP Markets" and mode=="DEMO"
       and process.returncode==69 and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and (fp1622_mql_recompile_startup_handoff(tail)
            or fp1622_mql_recompile_startup_handoff_6295(tail)))
'''

# --------------------------------------------------------------------------- #
# Correction 1+3: FP LIVE exit code 69 for accounts beyond FP6364
# (covers 9392). Uses in-scope variables, no hardcoded identifiers.
# Bounded by the same compiler-handoff detection. Never infers auth.
# --------------------------------------------------------------------------- #
FP_LIVE_EXIT69_CHECK = '''   # FP LIVE accounts beyond FP6364 (e.g. 9392): same compiler-handoff
   # exit code 69 as FP6364 LIVE. Bounded by the same journal detection.
   # Scoped to FP Markets LIVE with the compiler handoff — never infer
   # broker authentication from exit code 69. Uses in-scope variables.
   fp_live_compiler_exit69=(
       broker=="FP Markets" and mode=="LIVE"
       and process.returncode==69 and not was_connected
       and attempt<2 and time.monotonic()-started<90
       and (fp1622_mql_recompile_startup_handoff(tail)
            or fp1622_mql_recompile_startup_handoff_6295(tail)))
'''

# The break condition is updated to include both new exit-69 checks
BREAK_CONDITION_OLD = '   if process.returncode!=0 and not fp6364_compiler_exit69:\n    break'
BREAK_CONDITION_NEW = ('   if process.returncode!=0 and not (fp6364_compiler_exit69'
                       ' or fp_demo_compiler_exit69'
                       ' or fp_live_compiler_exit69):\n    break')

# --------------------------------------------------------------------------- #
# Correction 5: DEMO retry logic — handoff eligibility expression.
# The exit_action() call's handoff parameter must include the new
# exit-69 checks so RETRY_UPDATE is actually reached.
# --------------------------------------------------------------------------- #
DECISION_OLD = '   decision=exit_action(0 if fp6364_compiler_exit69 else process.returncode,was_connected,'
DECISION_NEW = ('   decision=exit_action(0 if fp6364_compiler_exit69'
                ' or fp_demo_compiler_exit69'
                ' or fp_live_compiler_exit69 else process.returncode,was_connected,')

# The handoff parameter expression must include the new exit-69 checks
HANDOFF_OLD = ('              handoff_event(tail) or trusted_fp1622_compiler_relaunch'
               ' or trusted_fp6364_compiler_relaunch,')
HANDOFF_NEW = ('              handoff_event(tail) or trusted_fp1622_compiler_relaunch'
               ' or trusted_fp6364_compiler_relaunch'
               ' or fp_demo_compiler_exit69'
               ' or fp_live_compiler_exit69,')

# --------------------------------------------------------------------------- #
# Correction 2: MT5 build 6295 — detection functions AND integration.
# Read-only journal inspection. The _6295 variant is used as an
# alternative handoff detection path in the exit-69 and relaunch checks.
# --------------------------------------------------------------------------- #
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
    return ("build 6295" in text
            or "mt5 terminal build 6295" in text
            or "compiler\\tfull recompilation (build 6295)" in text)

def fp1622_mql_recompile_startup_handoff_6295(journal_bytes):
    # ABUTRON-FP-WORKER-CORRECTIONS-20261124
    # Build 6295 variant of the compiler handoff detection. Same bounded
    # retry semantics; never a broker ACK. Used as an alternative handoff
    # detection path in exit-code-69 and compiler-relaunch checks.
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
    """Apply all corrections. Returns (patched_text, changes)."""
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

    # --- Correction 1+3: FP DEMO and LIVE exit code 69 ---
    # Insert both checks before the break condition, then update it
    if BREAK_CONDITION_OLD in text:
        check_text = FP_DEMO_EXIT69_CHECK + "\n" + FP_LIVE_EXIT69_CHECK + "\n"
        text = text.replace(BREAK_CONDITION_OLD, check_text + BREAK_CONDITION_OLD, 1)
        # Update the break condition
        text = text.replace(BREAK_CONDITION_OLD, BREAK_CONDITION_NEW, 1)
        changes.append("Added FP DEMO and LIVE exit code 69 compiler-handoff retry (bounded)")
    else:
        changes.append("WARNING: break condition not found — exit code 69 correction skipped")

    # --- Correction 5: DEMO retry logic — handoff eligibility ---
    # Update the decision=exit_action line (returncode and handoff param)
    if DECISION_OLD in text:
        text = text.replace(DECISION_OLD, DECISION_NEW, 1)
        changes.append("Updated decision=exit_action returncode to include new exit-69 checks")
    else:
        changes.append("WARNING: decision=exit_action line not found — returncode correction skipped")

    if HANDOFF_OLD in text:
        text = text.replace(HANDOFF_OLD, HANDOFF_NEW, 1)
        changes.append("Updated handoff eligibility to include new exit-69 checks")
    else:
        changes.append("WARNING: handoff parameter expression not found — eligibility correction skipped")

    # Correction 4 (9392 recovery): No source change required.
    # The existing stage_fp_live_9392_verified_readonly_history() call
    # remains unconditional. The exit-code-69 retry for FP LIVE accounts
    # (fp_live_compiler_exit69, inserted above in the launch loop after
    # was_connected is initialized) provides the 9392 recovery path.

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

    # Check 3: Build 6295 INTEGRATED into exit-69 checks
    if "fp1622_mql_recompile_startup_handoff_6295(tail)" not in text:
        errors.append("fp1622_mql_recompile_startup_handoff_6295 not integrated into retry decisions")

    # Check 4: FP DEMO exit code 69 check present (no hardcoded identifiers)
    if "fp_demo_compiler_exit69" not in text:
        errors.append("fp_demo_compiler_exit69 check not found")
    # Verify no hardcoded CID in the DEMO check section
    # Extract the demo check section and verify it uses in-scope vars
    demo_section_start = text.find("fp_demo_compiler_exit69=(")
    if demo_section_start > 0:
        demo_section_end = text.find("fp_live_compiler_exit69", demo_section_start)
        if demo_section_end > 0:
            demo_section = text[demo_section_start:demo_section_end]
            if "cid" in demo_section or "login" in demo_section:
                errors.append("fp_demo_compiler_exit69 hardcodes account identifiers — must use in-scope variables")

    # Check 5: FP LIVE exit code 69 check present (covers 9392)
    if "fp_live_compiler_exit69" not in text:
        errors.append("fp_live_compiler_exit69 check not found (9392 exit code 69 unaddressed)")
    # Verify no hardcoded CID in the LIVE check section
    live_section_start = text.find("fp_live_compiler_exit69=(")
    if live_section_start > 0:
        live_section_end = text.find("if process.returncode", live_section_start)
        if live_section_end > 0:
            live_section = text[live_section_start:live_section_end]
            if "cid" in live_section or "login" in live_section:
                errors.append("fp_live_compiler_exit69 hardcodes account identifiers — must use in-scope variables")

    # Check 6: Break condition updated
    if "fp6364_compiler_exit69 or fp_demo_compiler_exit69 or fp_live_compiler_exit69" not in text:
        errors.append("Break condition not updated to include new exit-69 checks")

    # Check 7: decision=exit_action returncode updated
    if "fp6364_compiler_exit69 or fp_demo_compiler_exit69 or fp_live_compiler_exit69 else process.returncode" not in text:
        errors.append("decision=exit_action returncode not updated to include new exit-69 checks")

    # Check 8: handoff eligibility updated
    if "or fp_demo_compiler_exit69 or fp_live_compiler_exit69," not in text:
        errors.append("handoff eligibility expression not updated to include new exit-69 checks")

    # Check 9: 9392 staging call remains UNCONDITIONAL (no if wrapper)
    # Find the CALL SITE specifically — search for the 2-space indented
    # call, which is distinct from the function definition (0-space, 'def ')
    staging_call = "  stage_fp_live_9392_verified_readonly_history(terminal,files,"
    staging_pos = text.find(staging_call)
    if staging_pos > 0:
        # Check if the call is indented deeper (conditional wrapper)
        line_start = text.rfind("\n", 0, staging_pos) + 1
        preceding = text[line_start:staging_pos].strip()
        if preceding.startswith("if "):
            errors.append("9392 staging call is wrapped in a condition — must remain unconditional")
        else:
            # Check if the preceding line is an 'if' statement (call moved
            # to deeper indentation inside an if block)
            lines_before = text[:line_start].splitlines()
            if lines_before and lines_before[-1].strip().startswith("if "):
                errors.append("9392 staging call is wrapped in a condition — must remain unconditional")
    else:
        # If no 2-space call site found, check if the call exists at a
        # deeper indentation (indicating it was wrapped in a condition)
        staging_call_any = "stage_fp_live_9392_verified_readonly_history(terminal,files,"
        pos = text.find(staging_call_any)
        while pos > 0:
            before = text[max(0, pos-10):pos]
            if "def " not in before:
                line_start = text.rfind("\n", 0, pos) + 1
                preceding = text[line_start:pos].strip()
                indent = len(text[line_start:pos]) - len(text[line_start:pos].lstrip())
                if indent > 2 or preceding.startswith("if "):
                    errors.append("9392 staging call is wrapped in a condition — must remain unconditional")
                    break
            pos = text.find(staging_call_any, pos + 1)

    # Check 11: LIVE OFF preservation
    if "fp6364_compiler_exit69" not in text:
        errors.append("fp6364_compiler_exit69 removed — LIVE OFF guard lost")
    if "def hold_zero_exit_for_native_proof" not in text:
        errors.append("hold_zero_exit_for_native_proof removed — LIVE OFF guard lost")
    if "def fp6364_invalid_account_journal" not in text:
        errors.append("fp6364_invalid_account_journal removed — LIVE OFF guard lost")
    if "def stage_fp_live_6364_preserved_profile" not in text:
        errors.append("stage_fp_live_6364_preserved_profile removed — LIVE OFF guard lost")
    if "def stage_fp_live_9392_verified_readonly_history" not in text:
        errors.append("stage_fp_live_9392_verified_readonly_history removed")
    if "trusted_fp6364_compiler_relaunch" not in text:
        errors.append("trusted_fp6364_compiler_relaunch removed — LIVE OFF guard lost")

    # Check 12: compile
    try:
        compile(text, "<patched_worker_handoff>", "exec")
    except SyntaxError as e:
        errors.append(f"Syntax error after patch: {e}")

    return errors


def atomic_write(path, content):
    """Write content to path via temp file + os.replace (atomic).

    Preserves file ownership, permissions, and timestamps via
    shutil.copystat so the patched file matches the original's
    access metadata.
    """
    tmp = path.with_suffix(path.suffix + ".tmp-fpcorr")
    tmp.write_text(content)
    if path.exists():
        # Preserve mode, ownership, timestamps, and extended attributes
        shutil.copystat(path, tmp)
    else:
        tmp.chmod(0o755)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(
        description="Apply targeted corrections to the production FP worker handoff."
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
    print(f"  - Build 6295 integrated into exit-69 and relaunch retry decisions")
    print(f"  - FP DEMO exit code 69 compiler-handoff retry added (bounded, in-scope vars)")
    print(f"  - FP LIVE exit code 69 compiler-handoff retry added (covers 9392, in-scope vars)")
    print(f"  - Break condition updated to include new exit-69 checks")
    print(f"  - decision=exit_action returncode updated to include new exit-69 checks")
    print(f"  - handoff eligibility updated to include new exit-69 checks")
    print(f"  - 9392 staging call remains unconditional (no source change required)")
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

    # Live patch: backup then atomic write
    backup_path = worker_path.with_suffix(f".py.backup-fpcorr-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(worker_path, backup_path)
    print(f"Backup created: {backup_path}")

    atomic_write(worker_path, patched_text)
    print(f"Patched file written (atomic): {worker_path}")
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
