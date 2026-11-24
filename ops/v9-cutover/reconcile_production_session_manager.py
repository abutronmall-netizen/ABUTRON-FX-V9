#!/usr/bin/env python3
"""Surgical reconciliation: inject the Deriv worker-access fix into the
production mt5_session_manager.py while preserving ALL FP-specific behavior.

This script patches ONLY the worker-access fix (ensure_worker_accessible +
its call site in launch_worker) into the production manager at
/opt/abutron-v9-native-runtime/mt5_session_manager.py.

It does NOT touch:
  - FP_WORKER constant or any FP worker path/SHA-256
  - FP-specific reconnect logic
  - FP-specific systemd properties (SupplementaryGroups etc.)
  - Any other production behavior

The fix is derived from the worker path itself (not a WORKER_DIR constant)
so it is portable across different manager versions.

Usage:
  python3 reconcile_production_session_manager.py [--manager-path PATH] [--dry-run]

Idempotent: safe to run multiple times. Checks for an existing patch marker.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

# Patch marker — used for idempotency check
PATCH_MARKER = "# ABUTRON-ISSUE65-WORKER-ACCESS-RECONCILED"

# The ensure_worker_accessible function to inject.
# Derives the worker directory from the worker path itself, so it does not
# depend on a WORKER_DIR constant that may not exist in the production manager.
ENSURE_WORKER_ACCESSIBLE_FUNC = '''
def ensure_worker_accessible(worker_path):
 # ABUTRON-ISSUE65-WORKER-ACCESS-RECONCILED
 # ProtectSystem=strict + DynamicUser=yes gives the service an unprivileged
 # dynamic UID. ReadOnlyPaths= only marks a path read-only; it does NOT
 # grant read/execute access to unprivileged users. The worker directory
 # and script must be world-readable+executable so the dynamic service
 # identity can open and exec the worker script after a reboot.
 # Scoped to DEMO workers: the Issue #65 defect affects DEMO session
 # recovery. LIVE workers are outside the recovery scope and must not have
 # their filesystem permissions modified by this recovery path.
 wd=Path(worker_path).parent
 wd.chmod(0o755)
 wp=Path(worker_path)
 wp.chmod(0o755)
'''

# The call site to inject after the chosen= assignment in launch_worker.
# Scoped to DEMO mode only.
ENSURE_WORKER_ACCESSIBLE_CALL = ''' if mode=="DEMO":
  ensure_worker_accessible(chosen)
'''


def find_launch_worker(text):
    """Find the launch_worker function definition and return (start, end) char offsets."""
    pattern = re.compile(r'^def launch_worker\(cid,broker,mode,encrypted\):', re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None, None
    start = match.start()
    # Find the next top-level def or end of file
    next_def = re.compile(r'^def ', re.MULTILINE)
    after = text[match.end():]
    next_match = next_def.search(after)
    if next_match:
        end = match.end() + next_match.start()
    else:
        end = len(text)
    return start, end


def find_chosen_assignment(text):
    """Find the chosen=... assignment inside launch_worker.

    Uses a flexible pattern that matches the chosen=( ... ) assignment
    regardless of exact whitespace, since production and GitHub versions
    may have different formatting.
    """
    # Match chosen=( ... ) where the content includes FP_WORKER and WORKER
    # Use a greedy match that captures from chosen= to the closing paren
    pattern = re.compile(
        r'chosen=\(.*?WORKER\)',
        re.DOTALL
    )
    match = pattern.search(text)
    if match:
        return match.end()  # position right after the closing paren
    return None


def is_already_patched(text):
    """Check if the production manager already has the reconciliation patch."""
    return PATCH_MARKER in text


def patch_manager(text):
    """Apply the surgical patch to the manager text. Returns (patched_text, changes_list)."""
    changes = []

    if is_already_patched(text):
        return text, ["Already patched (idempotent — no changes needed)"]

    # Step 1: Inject ensure_worker_accessible function before launch_worker
    launch_start, launch_end = find_launch_worker(text)
    if launch_start is None:
        return text, ["ERROR: could not find launch_worker() function"]

    if PATCH_MARKER not in text:
        func_text = ENSURE_WORKER_ACCESSIBLE_FUNC
        # Insert the function before launch_worker
        text = text[:launch_start] + func_text + "\n" + text[launch_start:]
        changes.append("Injected ensure_worker_accessible() before launch_worker()")

    # Re-find launch_worker in the modified text (offset shifted)
    launch_start, launch_end = find_launch_worker(text)

    # Step 2: Inject the call site after chosen= assignment
    chosen_end = find_chosen_assignment(text)
    if chosen_end is None:
        return text, ["ERROR: could not find chosen= assignment in launch_worker()"]

    # Check if the call site already exists after chosen=
    after_chosen = text[chosen_end:chosen_end + 200]
    if "ensure_worker_accessible(chosen)" in after_chosen:
        changes.append("Call site already present (idempotent)")
    else:
        # Insert the call right after the chosen= assignment
        call_text = ENSURE_WORKER_ACCESSIBLE_CALL
        text = text[:chosen_end] + "\n" + call_text + text[chosen_end:]
        changes.append("Injected ensure_worker_accessible(chosen) call after chosen= assignment")

    return text, changes


def verify_patch(text, original_text):
    """Verify the patch was applied correctly and FP-specific code is preserved."""
    errors = []

    # Check 1: ensure_worker_accessible function exists
    if "def ensure_worker_accessible" not in text:
        errors.append("ensure_worker_accessible function not found after patch")

    # Check 2: call site exists in launch_worker
    launch_start, launch_end = find_launch_worker(text)
    if launch_start is not None:
        launch_body = text[launch_start:launch_end]
        if "ensure_worker_accessible(chosen)" not in launch_body:
            errors.append("ensure_worker_accessible(chosen) call not found in launch_worker")
        if 'if mode=="DEMO":' not in launch_body:
            errors.append("DEMO scope guard not found in launch_worker")

    # Check 3: FP worker constant unchanged
    fp_pattern = re.compile(r'FP_WORKER\s*=\s*"([^"]+)"')
    fp_original = fp_pattern.search(original_text)
    fp_patched = fp_pattern.search(text)
    if fp_original and fp_patched:
        if fp_original.group(1) != fp_patched.group(1):
            errors.append(f"FP_WORKER path changed: {fp_original.group(1)} -> {fp_patched.group(1)}")
    elif fp_original and not fp_patched:
        errors.append("FP_WORKER constant missing after patch")

    # Check 4: FP-specific SupplementaryGroups preserved
    if "SupplementaryGroups=abutron-fp-boot-assets" in original_text:
        if "SupplementaryGroups=abutron-fp-boot-assets" not in text:
            errors.append("FP SupplementaryGroups property removed")

    # Check 5: compile check
    try:
        compile(text, "<patched_session_manager>", "exec")
    except SyntaxError as e:
        errors.append(f"Syntax error after patch: {e}")

    return errors


def main():
    parser = argparse.ArgumentParser(
        description="Surgically patch the production session manager with the Deriv worker-access fix."
    )
    parser.add_argument(
        "--manager-path",
        default="/opt/abutron-v9-native-runtime/mt5_session_manager.py",
        help="Path to the production mt5_session_manager.py (default: /opt/abutron-v9-native-runtime/mt5_session_manager.py)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without writing to disk",
    )
    args = parser.parse_args()

    manager_path = Path(args.manager_path)

    if not manager_path.is_file():
        print(f"ERROR: Manager file not found: {manager_path}")
        print(f"       Expected production path: {manager_path}")
        sys.exit(1)

    print(f"Reconciliation target: {manager_path}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    original_text = manager_path.read_text()
    original_lines = len(original_text.splitlines())

    if is_already_patched(original_text):
        print("STATUS: Already reconciled (idempotent). No changes needed.")
        sys.exit(0)

    patched_text, changes = patch_manager(original_text)

    print("Changes to apply:")
    for change in changes:
        print(f"  - {change}")
    print()

    if not changes or any(c.startswith("ERROR") for c in changes):
        print("PATCH FAILED. No changes applied.")
        sys.exit(1)

    # Verify
    errors = verify_patch(patched_text, original_text)
    if errors:
        print("VERIFICATION FAILED:")
        for error in errors:
            print(f"  - {error}")
        print()
        print("No changes written.")
        sys.exit(1)

    print("Verification passed:")
    print(f"  - ensure_worker_accessible() function present")
    print(f"  - Call site scoped to DEMO mode in launch_worker()")
    print(f"  - FP_WORKER constant unchanged")
    print(f"  - FP SupplementaryGroups preserved")
    print(f"  - File compiles without syntax errors")
    print()

    patched_lines = len(patched_text.splitlines())
    print(f"Lines: {original_lines} -> {patched_lines} (+{patched_lines - original_lines})")

    if args.dry_run:
        print("DRY RUN: No changes written to disk.")
        # Show the diff summary
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
    backup_path = manager_path.with_suffix(f".py.backup-issue65-{__import__('time').strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(manager_path, backup_path)
    print(f"Backup created: {backup_path}")

    manager_path.write_text(patched_text)
    print(f"Patched file written: {manager_path}")
    print()
    print("RECONCILIATION COMPLETE.")
    print("Next steps:")
    print("  1. Review the patched file for correctness")
    print("  2. Restart the session manager: sudo systemctl restart abutron-client-mt5-manager.service")
    print("  3. Verify worker directory permissions: ls -ld /opt/abutron-v9-native-runtime/workers")
    print("  4. Use the normal authenticated DEMO reconnect path for Deriv 6207, 7593, 9244")


if __name__ == "__main__":
    main()
