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

Repeat runs verify the helper and guarded call. The original all-DEMO guard
from a02d2d0 is recognized and narrowed on upgrade.
"""
import argparse
import ast
import os
import tempfile
import shutil
import sys
import time
from pathlib import Path

# Patch marker — used for idempotency check
PATCH_MARKER = "# ABUTRON-ISSUE65-WORKER-ACCESS-RECONCILED"

# The ensure_worker_accessible function to inject.
# Derives the worker directory from the worker path itself, so it does not
# depend on a WORKER_DIR constant that may not exist in the production manager.
ENSURE_WORKER_ACCESSIBLE_FUNC = '''
def ensure_worker_accessible(worker_path):
 # ABUTRON-ISSUE65-WORKER-ACCESS-RECONCILED
 # The session manager sees /opt read-only under ProtectSystem=strict.
 # Deployment sets these modes; avoid issuing chmod on already-correct paths.
 for path in (Path(worker_path).parent, Path(worker_path)):
  if (path.stat().st_mode & 0o7777) != 0o755:
   path.chmod(0o755)
'''

# Exact previous helper shape, also used by the original broad DEMO patch.
LEGACY_HELPER = '''def ensure_worker_accessible(worker_path):
 wd=Path(worker_path).parent
 wd.chmod(0o755)
 wp=Path(worker_path)
 wp.chmod(0o755)
'''

# Only the Deriv DEMO selection uses this repair. Other launches keep their
# production permissions and paths. Recognize the exact previous injection
# so a manager already patched by a02d2d0 can be upgraded.
GUARD = 'if (broker, mode) == ("Deriv", "DEMO"):'
OLD_GUARD = 'if mode=="DEMO":'


def patched_source(source):
    """Return reconciled source, or reject an ambiguous or partial patch."""
    module = ast.parse(source)
    launch, = [node for node in module.body
               if isinstance(node, ast.FunctionDef) and node.name == "launch_worker"]
    selection, = [node for node in launch.body if isinstance(node, ast.Assign)
                  and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id == "chosen"]
    lines = source.splitlines(keepends=True)
    indent = lines[selection.lineno - 1][:selection.col_offset]
    call = "ensure_worker_accessible(chosen)"
    helpers = [node for node in module.body if isinstance(node, ast.FunctionDef)
               and node.name == "ensure_worker_accessible"]
    uses = [node for node in ast.walk(module) if isinstance(node, ast.Name)
            and node.id == "ensure_worker_accessible"]
    if helpers or uses or PATCH_MARKER in source:
        old_helper = ast.dump(ast.parse(LEGACY_HELPER).body[0])
        current_helper = ast.dump(ast.parse(ENSURE_WORKER_ACCESSIBLE_FUNC).body[0])
        if (len(helpers) != 1 or len(uses) != 1 or source.count(PATCH_MARKER) != 1
                or ast.dump(helpers[0]) not in (old_helper, current_helper)):
            raise ValueError("Unrecognized pre-existing worker access helper or marker")
        index = launch.body.index(selection) + 1
        if index >= len(launch.body):
            raise ValueError("Missing guarded call after worker selection")
        existing = launch.body[index]
        for guard in (GUARD, OLD_GUARD):
            expected = ast.parse(guard + "\n " + call).body[0]
            if ast.dump(existing) == ast.dump(expected):
                if guard == OLD_GUARD:
                    lines[existing.lineno - 1:existing.end_lineno] = [
                        indent + GUARD + "\n", indent + " " + call + "\n"]
                break
        else:
            raise ValueError("Unrecognized worker access call")
        helper = helpers[0]
        if ast.dump(helper) == old_helper:
            if helper.end_lineno >= launch.lineno:
                raise ValueError("Unexpected legacy helper location")
            lines[helper.lineno - 1:helper.end_lineno] = [ENSURE_WORKER_ACCESSIBLE_FUNC.lstrip("\n")]
    else:
        lines[selection.end_lineno:selection.end_lineno] = [
            indent + GUARD + "\n", indent + " " + call + "\n"]
        lines[launch.lineno - 1:launch.lineno - 1] = [ENSURE_WORKER_ACCESSIBLE_FUNC + "\n"]
    result = "".join(lines)
    compile(result, "<patched manager>", "exec")
    return result


def replace_manager(path, original, patched):
    # Stage the complete file on the same filesystem; preserve owner and mode.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        stage = Path(staged.name)
    try:
        info = path.stat()
        stage.write_bytes(patched)
        os.chown(stage, info.st_uid, info.st_gid)
        shutil.copystat(path, stage)
        if path.read_bytes() != original:
            raise ValueError("Manager changed during reconciliation")
        backup = path.with_name(f"{path.name}.backup-issue65-{time.time_ns()}")
        os.link(path, backup)
        os.replace(stage, path)
        return backup
    finally:
        stage.unlink(missing_ok=True)


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

    if manager_path.is_symlink():
        sys.exit("ERROR: Expected a regular manager file, found a symlink")
    # Serialize invocations across the atomic rename of the target inode.
    import fcntl
    directory = os.open(manager_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        original = manager_path.read_bytes()
        source = original.decode("utf-8")
        patched = patched_source(source)
        if patched == source:
            print("Already reconciled: verified Deriv DEMO guard and helper.")
            return
        print("Verification passed: Deriv DEMO worker-access reconciliation")
        if args.dry_run:
            import difflib
            sys.stdout.writelines(difflib.unified_diff(
                source.splitlines(True), patched.splitlines(True),
                fromfile="production", tofile="patched"))
            return
        backup = replace_manager(manager_path, original, patched.encode("utf-8"))
        print(f"Backup created: {backup}")
        print("RECONCILIATION COMPLETE")
    except (OSError, ValueError, SyntaxError) as exc:
        sys.exit(f"ERROR: {exc}")
    finally:
        os.close(directory)


if __name__ == "__main__":
    main()
