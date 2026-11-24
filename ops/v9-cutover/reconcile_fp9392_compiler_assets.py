#!/usr/bin/env python3
"""Surgical corrections for FP 9392 compiler asset preparation (24 Nov 2026).

Addresses the repeated MT5 build 6295 compiler shutdown documented in
COMPILER_FAILURE.md. The deployed worker's exit-code-69 retry patch is
working, but does not solve the root cause: incomplete MQL5 compiler
asset preparation.

ROOT CAUSE (from the 24 Nov 2026 diagnostic inventory):
  - Failed installation: 146 MQ5, 105 EX5, 47 MQ5 lacking same-path EX5.
    Stock ExpertMACD.ex5 missing.
  - Warm cache /opt/abutron/fpmarkets-mql-bootstrap-v1: 99 MQ5, 102 EX5,
    no MQ5 lacking counterpart — complete.
  - The worker's warm-cache copy loop copies only 4 subdirectories
    (Include, Indicators, Experts/Examples, Scripts/Examples), omitting
    Experts/Advisors and Experts/Free Robots.
  - stage_fp6364_stock_compiler_assets() (hash-verified pinned stock
    compiler assets: 32 MQ5 + 32 EX5) is FP6364-only. The 9392 LIVE
    read-only account receives no stock compiler assets at all.

  Result: MT5 starts with MQ5 sources lacking EX5 counterparts, triggers
  a full recompilation, and the recompilation is interrupted by the
  exit-0 / 10053 handoff before EX5 files are written. Every retry
  repeats the same cycle.

CORRECTIONS:

1. COMPLETE WARM-CACHE STOCK BOOTSTRAP COPY:
   Add Experts/Advisors and Experts/Free Robots to the warm-cache copy
   loop in main(). The warm cache is complete (no MQ5 lacking EX5
   counterpart); the worker's subdirectory selection was incomplete.
   This ensures the terminal receives the full stock MQL5 library set
   for every account.

2. EXTEND PINNED STOCK COMPILER ASSETS TO 9392 READ-ONLY:
   Extend the gate in stage_fp6364_stock_compiler_assets() to also
   accept the 9392 LIVE read-only account. The pinned manifest cache
   (/usr/local/lib/abutron-fp6364-stock-mt5-90000002-v1) contains only
   verified stock MT5 Expert examples (Advisors, Free Robots) — no
   customer charts, EAs, or broker state. Staging these verified stock
   compiler assets for 9392 prevents the repeated first-run full
   recompilation that interrupts startup before compiled EX5 files exist.

3. PRESERVE ACCOUNT ISOLATION, AUTHENTICATION AND LIVE OFF:
   No correction changes broker credentials, account permissions, risk
   settings, or LIVE state. The stock compiler assets are read-only
   MT5 Expert examples. The 9392 named read-only chart profile
   (ABUTRON9392_READONLY) and its history indicator staging remain
   unchanged. No retry-count increase is applied — the observed results
   do not justify one.

4. ATOMIC FILE REPLACEMENT WITH OWNERSHIP PRESERVATION:
   The patched file is written to a temp file and moved into place via
   os.replace(). File ownership (UID/GID via os.chown), permissions,
   and timestamps (shutil.copystat) are preserved. A backup is created
   first.

Usage:
  python3 reconcile_fp9392_compiler_assets.py [--worker-path PATH] [--dry-run]
"""
import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

PATCH_MARKER = "# ABUTRON-FP9392-COMPILER-ASSETS-20261124"

# --------------------------------------------------------------------------- #
# Correction 1: Complete warm-cache stock bootstrap copy.
# The deployed worker copies only 4 subdirectories; the warm cache is
# complete. Add the two omitted stock Expert subdirectories.
# --------------------------------------------------------------------------- #
WARM_LOOP_OLD = 'for name in ("Include","Indicators","Experts/Examples","Scripts/Examples"):'
WARM_LOOP_NEW = ('for name in ("Include","Indicators","Experts/Examples",'
                 '"Experts/Advisors","Experts/Free Robots","Scripts/Examples"):')

# --------------------------------------------------------------------------- #
# Correction 2: Extend pinned stock compiler assets to the 9392 LIVE
# read-only account. The gate currently accepts only FP6364 LIVE.
# --------------------------------------------------------------------------- #
# Old gate (FP6364-only):
GATE_OLD = ('if (cid,login,server,mode)!=("f0000000000000000000000000000005",\n'
            '                              "90000010","FPMarketsSC-Live","LIVE"):\n'
            '  return "SKIP_UNRELATED_ACCOUNT"')
# New gate (FP6364 LIVE + FP 9392 LIVE read-only):
GATE_NEW = ('if (cid,login,server,mode) not in (("f0000000000000000000000000000005",\n'
            '                                        "90000010","FPMarketsSC-Live","LIVE"),\n'
            '                                       ("f0000000000000000000000000000007",\n'
            '                                        "90000008","FPMarketsSC-Live","LIVE")):\n'
            '  return "SKIP_UNRELATED_ACCOUNT"')


def is_already_patched(text):
    return PATCH_MARKER in text


def patch_worker(text):
    """Apply all corrections. Returns (patched_text, changes)."""
    changes = []

    if is_already_patched(text):
        return text, ["Already patched (idempotent — no changes needed)"]

    # --- Correction 1: Complete warm-cache stock bootstrap copy ---
    if WARM_LOOP_OLD in text:
        text = text.replace(WARM_LOOP_OLD, WARM_LOOP_NEW, 1)
        changes.append("Completed warm-cache stock bootstrap copy (added Experts/Advisors, Experts/Free Robots)")
    else:
        changes.append("WARNING: warm-cache copy loop not found — correction 1 skipped")

    # --- Correction 2: Extend pinned stock compiler assets to 9392 ---
    if GATE_OLD in text:
        text = text.replace(GATE_OLD, GATE_NEW, 1)
        changes.append("Extended pinned stock compiler assets to FP 9392 LIVE read-only account")
    else:
        changes.append("WARNING: stock compiler assets gate not found — correction 2 skipped")

    # Insert the patch marker at the top of the file (after the module
    # header comment block, before the first import or code line).
    marker_line = f"\n{PATCH_MARKER}\n"
    if PATCH_MARKER not in text:
        # Insert after the last leading comment/shebang block
        lines = text.splitlines(keepends=True)
        insert_at = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith("#!/"):
                insert_at = i
                break
        lines.insert(insert_at, marker_line)
        text = "".join(lines)

    return text, changes


def verify_patch(text, original_text):
    """Verify all corrections and preservation of existing guards."""
    errors = []

    # Check 1: PATCH_MARKER present
    if PATCH_MARKER not in text:
        errors.append("PATCH_MARKER not found after patch")

    # Check 2: Warm-cache copy loop completed
    if WARM_LOOP_NEW not in text:
        errors.append("Warm-cache copy loop not completed (Experts/Advisors, Experts/Free Robots missing)")
    if WARM_LOOP_OLD in text:
        errors.append("Old warm-cache copy loop still present (correction 1 not applied)")

    # Check 3: Stock compiler assets gate extended to 9392
    if GATE_NEW not in text:
        errors.append("Stock compiler assets gate not extended to 9392 LIVE read-only")
    if GATE_OLD in text:
        errors.append("Old FP6364-only gate still present (correction 2 not applied)")

    # Check 4: FP6364 gate still accepted (no regression)
    if '"f0000000000000000000000000000005"' not in text or '"90000010"' not in text:
        errors.append("FP6364 stock compiler assets gate lost — regression")

    # Check 5: 9392 identity present in the extended gate
    if '"f0000000000000000000000000000007"' not in text or '"90000008"' not in text:
        errors.append("9392 read-only identity not present in stock compiler assets gate")

    # Check 6: No retry-count increase
    # The launch loop bound remains attempt < 3; exit-69 checks remain attempt<2.
    if "while attempt < 3:" not in text:
        errors.append("Launch loop retry bound changed — must remain attempt < 3")
    if "attempt<2 and time.monotonic()-started<90" not in text:
        errors.append("Exit-69 retry bound changed — must remain attempt<2 and <90s")

    # Check 7: LIVE OFF and authentication guards preserved
    if "def hold_zero_exit_for_native_proof" not in text:
        errors.append("hold_zero_exit_for_native_proof removed — LIVE OFF guard lost")
    if "def fp6364_invalid_account_journal" not in text:
        errors.append("fp6364_invalid_account_journal removed — LIVE OFF guard lost")
    if "def stage_fp_live_6364_preserved_profile" not in text:
        errors.append("stage_fp_live_6364_preserved_profile removed — LIVE OFF guard lost")
    if "def stage_fp_live_9392_verified_readonly_history" not in text:
        errors.append("stage_fp_live_9392_verified_readonly_history removed")
    if "fp6364_compiler_exit69" not in text:
        errors.append("fp6364_compiler_exit69 removed — LIVE OFF guard lost")

    # Check 8: 9392 named read-only profile preparation preserved
    if "ABUTRON9392_READONLY" not in text:
        errors.append("ABUTRON9392_READONLY named profile preparation lost")

    # Check 9: Stock compiler assets manifest verification preserved
    if "FP6364_STOCK_ASSET_MANIFEST_UNTRUSTED" not in text:
        errors.append("Stock compiler assets manifest verification lost")
    if "FP6364_STOCK_SHA256_REJECTED" not in text:
        errors.append("Stock compiler assets SHA-256 verification lost")

    # Check 10: compile
    try:
        compile(text, "<patched_fp9392_compiler_assets>", "exec")
    except SyntaxError as e:
        errors.append(f"Syntax error after patch: {e}")

    return errors


def atomic_write(path, content):
    """Write content to path via temp file + os.replace (atomic).

    Preserves file ownership (UID/GID via os.chown), permissions, and
    timestamps via shutil.copystat so the patched file matches the
    original's access metadata.
    """
    tmp = path.with_suffix(path.suffix + ".tmp-fp9392")
    tmp.write_text(content)
    if path.exists():
        stat = path.stat()
        # Preserve owner UID and group GID — shutil.copystat does NOT
        # copy ownership, so os.chown is required explicitly.
        os.chown(tmp, stat.st_uid, stat.st_gid)
        # Preserve mode, timestamps, and extended attributes
        shutil.copystat(path, tmp)
    else:
        tmp.chmod(0o755)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(
        description="Apply targeted corrections to the production FP worker for 9392 compiler asset preparation."
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
    print(f"  - Warm-cache stock bootstrap copy completed (Experts/Advisors, Experts/Free Robots)")
    print(f"  - Pinned stock compiler assets extended to FP 9392 LIVE read-only account")
    print(f"  - FP6364 stock compiler assets gate preserved (no regression)")
    print(f"  - 9392 read-only identity present in extended gate")
    print(f"  - Retry bounds unchanged (attempt < 3, exit-69 attempt<2 and <90s)")
    print(f"  - LIVE OFF guards preserved (hold_zero_exit, fp6364_invalid_account_journal,")
    print(f"    stage_fp_live_6364_preserved_profile, fp6364_compiler_exit69)")
    print(f"  - 9392 read-only history staging and named profile preserved")
    print(f"  - Stock compiler assets manifest and SHA-256 verification preserved")
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
    backup_path = worker_path.with_suffix(f".py.backup-fp9392-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(worker_path, backup_path)
    print(f"Backup created: {backup_path}")

    atomic_write(worker_path, patched_text)
    print(f"Patched file written (atomic): {worker_path}")
    print()
    print("FP 9392 COMPILER ASSET CORRECTIONS COMPLETE.")
    print("Next steps:")
    print("  1. Review the patched file for correctness")
    print("  2. Restart the affected FP LIVE read-only (9392) worker")
    print("  3. Verify MT5 does not trigger repeated full recompilation")
    print("  4. Verify compiled EX5 files exist after startup")
    print("  5. Confirm broker authentication and exposure reporting restore")
    print("  6. Confirm all LIVE OFF guards remain intact")


if __name__ == "__main__":
    main()
