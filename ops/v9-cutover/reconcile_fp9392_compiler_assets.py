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
  - The pinned stock compiler cache
    /usr/local/lib/abutron-fp6364-stock-mt5-90000002-v1 contains 64
    verified files (32 MQ5 + 32 EX5), all checksum-verified, covering
    ExpertMACD.ex5 and 32 of the 47 missing compiled files. The
    remaining 15 are unit-test scripts (also present in working DEMO
    installations).
  - stage_fp6364_stock_compiler_assets() (the verified stock compiler
    asset staging function) is FP6364-only. The 9392 LIVE read-only
    account receives no stock compiler assets at all, so MT5 starts
    with incomplete compiled EX5 coverage, triggers a full recompilation,
    and the recompilation is interrupted by the exit-0/10053 handoff
    before EX5 files are written. Every retry repeats the cycle.

CORRECTION:

1. EXTEND PINNED STOCK COMPILER ASSETS TO 9392 READ-ONLY:
   Extend the account gate in stage_fp6364_stock_compiler_assets() to
   also accept the 9392 LIVE read-only account. The pinned manifest
   cache contains only verified stock MT5 Expert examples (Advisors,
   Free Robots) — no customer charts, EAs, or broker state. Staging
   these verified stock compiler assets for 9392 prevents the repeated
   first-run full recompilation that interrupts startup before compiled
   EX5 files exist.

   This correction does NOT hardcode any account identifiers. The
   FP6364 identity is extracted from the existing gate in the file being
   patched, and the 9392 identity is extracted from the existing gate in
   stage_fp_live_9392_verified_readonly_history() in the same file. The
   patch is therefore identifier-agnostic and matches any production
   variant regardless of anonymization.

2. PRESERVE ACCOUNT ISOLATION, AUTHENTICATION AND LIVE OFF:
   No correction changes broker credentials, account permissions, risk
   settings, or LIVE state. The stock compiler assets are read-only
   MT5 Expert examples. The 9392 named read-only chart profile
   (ABUTRON9392_READONLY) and its history indicator staging remain
   unchanged. No retry-count increase is applied — the observed results
   do not justify one.

3. ATOMIC FILE REPLACEMENT WITH OWNERSHIP PRESERVATION:
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


def _extract_gate_tuple(func_text):
    """Extract the (cid,login,server,mode) tuple from an account gate.

    Matches the pattern:
        if (cid,login,server,mode)!=(<tuple>):
          return "SKIP_..."
    Returns the tuple string (including quotes and commas) or None.
    """
    pattern = re.compile(
        r'if \(cid,login,server,mode\)!=\((.*?)\):\s*\n\s*return "SKIP_',
        re.DOTALL
    )
    match = pattern.search(func_text)
    if match:
        return match.group(1).strip()
    return None


def is_already_patched(text):
    return PATCH_MARKER in text


def patch_worker(text):
    """Apply all corrections. Returns (patched_text, changes)."""
    changes = []

    if is_already_patched(text):
        return text, ["Already patched (idempotent — no changes needed)"]

    # --- Correction 1: Extend pinned stock compiler assets to 9392 ---
    # Find the FP6364 stock compiler assets function and extract its
    # existing gate tuple (the FP6364 identity — no hardcoded values).
    fn_start, fn_end = find_function(text, "stage_fp6364_stock_compiler_assets")
    if fn_start is None:
        return text, ["ERROR: could not find stage_fp6364_stock_compiler_assets()"]

    fn_text = text[fn_start:fn_end]
    fp6364_tuple = _extract_gate_tuple(fn_text)
    if fp6364_tuple is None:
        return text, ["ERROR: could not extract FP6364 account gate from stage_fp6364_stock_compiler_assets()"]

    # Find the 9392 read-only staging function and extract its gate tuple
    # (the 9392 identity — no hardcoded values).
    fn9392_start, fn9392_end = find_function(text, "stage_fp_live_9392_verified_readonly_history")
    if fn9392_start is None:
        return text, ["ERROR: could not find stage_fp_live_9392_verified_readonly_history()"]

    fn9392_text = text[fn9392_start:fn9392_end]
    fp9392_tuple = _extract_gate_tuple(fn9392_text)
    if fp9392_tuple is None:
        return text, ["ERROR: could not extract 9392 account gate from stage_fp_live_9392_verified_readonly_history()"]

    # Build the new gate: accept both identities via `not in (...)`.
    # Format the tuples with consistent indentation.
    fp6364_lines = fp6364_tuple.replace("\n", " ").split(",")
    fp9392_lines = fp9392_tuple.replace("\n", " ").split(",")

    # Reformat each tuple as a single line for the `not in` expression
    fp6364_single = ", ".join(s.strip() for s in fp6364_lines)
    fp9392_single = ", ".join(s.strip() for s in fp9392_lines)

    gate_new = (
        f'if (cid,login,server,mode) not in (({fp6364_single}),\n'
        f'                                  ({fp9392_single})):\n'
        f'  return "SKIP_UNRELATED_ACCOUNT"'
    )

    # Find and replace the old gate in the function text
    gate_pattern = re.compile(
        r'if \(cid,login,server,mode\)!=\(.*?\):\s*\n\s*return "SKIP_UNRELATED_ACCOUNT"',
        re.DOTALL
    )
    gate_match = gate_pattern.search(fn_text)
    if gate_match is None:
        return text, ["ERROR: could not locate FP6364 account gate to replace"]

    old_gate = gate_match.group(0)
    new_fn_text = fn_text.replace(old_gate, gate_new, 1)
    text = text[:fn_start] + new_fn_text + text[fn_end:]
    changes.append("Extended pinned stock compiler assets to FP 9392 LIVE read-only account")

    # Insert the patch marker at the top of the file (after the module
    # header comment block, before the first import or code line).
    marker_line = f"\n{PATCH_MARKER}\n"
    if PATCH_MARKER not in text:
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

    # Check 2: Stock compiler assets gate extended to 9392
    # The gate must now use `not in ((...),(...))` with two identities.
    fn_start, fn_end = find_function(text, "stage_fp6364_stock_compiler_assets")
    if fn_start is None:
        errors.append("stage_fp6364_stock_compiler_assets() not found after patch")
    else:
        fn_text = text[fn_start:fn_end]
        if "not in ((" not in fn_text:
            errors.append("Stock compiler assets gate not extended (missing 'not in ((')")
        # Count identity tuples in the gate (should be 2)
        gate_matches = re.findall(r'\("[^"]*",\s*"[^"]*",\s*"[^"]*",\s*"[^"]*"\)', fn_text)
        if len(gate_matches) < 2:
            errors.append("Stock compiler assets gate does not accept two account identities")
        # Verify the old `!=` gate is gone
        if re.search(r'if \(cid,login,server,mode\)!=\(', fn_text):
            errors.append("Old FP6364-only `!=` gate still present in stock compiler assets function")

    # Check 3: FP6364 identity preserved (extracted from original, not hardcoded)
    # The original file's FP6364 identity must appear in the patched gate.
    orig_fn_start, orig_fn_end = find_function(original_text, "stage_fp6364_stock_compiler_assets")
    if orig_fn_start is not None:
        orig_fn_text = original_text[orig_fn_start:orig_fn_end]
        orig_tuple = _extract_gate_tuple(orig_fn_text)
        if orig_tuple:
            # The first identity in the new gate must match the original FP6364 tuple
            new_fn_text = text[fn_start:fn_end]
            gate_matches = re.findall(r'\("[^"]*",\s*"[^"]*",\s*"[^"]*",\s*"[^"]*"\)', new_fn_text)
            if gate_matches:
                orig_cid = re.search(r'"([^"]+)"', orig_tuple)
                new_cid = re.search(r'"([^"]+)"', gate_matches[0])
                if orig_cid and new_cid and orig_cid.group(1) != new_cid.group(1):
                    errors.append("FP6364 identity changed in stock compiler assets gate — regression")

    # Check 4: 9392 identity present in the extended gate
    orig_9392_start, orig_9392_end = find_function(original_text, "stage_fp_live_9392_verified_readonly_history")
    if orig_9392_start is not None:
        orig_9392_text = original_text[orig_9392_start:orig_9392_end]
        orig_9392_tuple = _extract_gate_tuple(orig_9392_text)
        if orig_9392_tuple:
            orig_9392_cid = re.search(r'"([^"]+)"', orig_9392_tuple)
            if orig_9392_cid:
                new_fn_text = text[fn_start:fn_end]
                if orig_9392_cid.group(1) not in new_fn_text:
                    errors.append("9392 read-only identity not present in stock compiler assets gate")

    # Check 5: No retry-count increase
    if "while attempt < 3:" not in text:
        errors.append("Launch loop retry bound changed — must remain attempt < 3")
    if "attempt<2 and time.monotonic()-started<90" not in text:
        errors.append("Exit-69 retry bound changed — must remain attempt<2 and <90s")

    # Check 6: LIVE OFF and authentication guards preserved
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

    # Check 7: 9392 named read-only profile preparation preserved
    if "ABUTRON9392_READONLY" not in text:
        errors.append("ABUTRON9392_READONLY named profile preparation lost")

    # Check 8: Stock compiler assets manifest verification preserved
    if "FP6364_STOCK_ASSET_MANIFEST_UNTRUSTED" not in text:
        errors.append("Stock compiler assets manifest verification lost")
    if "FP6364_STOCK_SHA256_REJECTED" not in text:
        errors.append("Stock compiler assets SHA-256 verification lost")

    # Check 9: Warm-cache copy loop unchanged (no no-op directory additions)
    # The warm cache does not contain Experts/Advisors or Experts/Free
    # Robots; adding them would be a no-op. The pinned stock compiler
    # cache is the correct asset source.
    if '"Experts/Advisors"' in text and 'for name in ("Include"' in text:
        # Check if the warm-cache loop was modified to include nonexistent dirs
        warm_loop_line = None
        for line in text.splitlines():
            if 'for name in ("Include"' in line:
                warm_loop_line = line
                break
        if warm_loop_line and "Experts/Advisors" in warm_loop_line:
            errors.append("Warm-cache copy loop modified with nonexistent directories — pinned stock compiler cache is the correct asset source")

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
    print(f"  - Pinned stock compiler assets extended to FP 9392 LIVE read-only account")
    print(f"  - Account identities extracted from file (no hardcoded identifiers)")
    print(f"  - FP6364 stock compiler assets gate preserved (no regression)")
    print(f"  - 9392 read-only identity present in extended gate")
    print(f"  - Retry bounds unchanged (attempt < 3, exit-69 attempt<2 and <90s)")
    print(f"  - LIVE OFF guards preserved (hold_zero_exit, fp6364_invalid_account_journal,")
    print(f"    stage_fp_live_6364_preserved_profile, fp6364_compiler_exit69)")
    print(f"  - 9392 read-only history staging and named profile preserved")
    print(f"  - Stock compiler assets manifest and SHA-256 verification preserved")
    print(f"  - Warm-cache copy loop unchanged (pinned stock compiler cache is the asset source)")
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
