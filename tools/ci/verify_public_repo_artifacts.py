#!/usr/bin/env python3
"""Reject host-private evidence bundles accidentally staged in public Git trees.

This checks the tracked Git index, not just working-tree ignore patterns.
It never reads or prints private file contents.
"""
from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
import subprocess
import sys

FORBIDDEN_GLOBS = (
    "issue25-*.tgz",
    "*.hcc",
    "*.sqlite3",
    "*.sqlite3-*",
    "ReportHistory-*.xlsx",
    "*broker-history*.xlsx",
    "*.private.json",
)
FORBIDDEN_DIRS = (
    "host_evidence",
    "private_evidence",
    "runtime_evidence",
    "var/backups",
)


def forbidden_reason(path: str) -> str | None:
    posix = path.replace("\\", "/").lstrip("/")
    parts = PurePosixPath(posix).parts
    if any(part in ("..", "") for part in parts):
        return "unexpected path traversal"
    if any(posix == name or posix.startswith(name + "/") for name in FORBIDDEN_DIRS):
        return "private host directory"
    if any(fnmatch.fnmatch(PurePosixPath(posix).name, pattern) for pattern in FORBIDDEN_GLOBS):
        return "private data or archive format"
    return None


def check_paths(paths: list[str]) -> list[tuple[str, str]]:
    return [(p, reason) for p in paths if (reason := forbidden_reason(p)) is not None]


def main() -> int:
    proc = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    paths = [part.decode("utf-8", "surrogateescape") for part in proc.stdout.split(b"\x00") if part]
    errors = check_paths(paths)
    if errors:
        print("FAIL: private host evidence is tracked in Git. Remove from the index and review history.")
        for path, reason in errors:
            print(f"  - {path}: {reason}")
        print(".gitignore does not untrack committed files or erase their Git history.")
        return 1
    print(f"PASS: {len(paths)} tracked paths contain no prohibited host evidence files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
