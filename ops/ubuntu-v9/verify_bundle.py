#!/usr/bin/env python3
"""Non-executing integrity and privacy lint of the Ubuntu V9 source export."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
BAD = [
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\b[a-f0-9]{32}\b"),
    re.compile(r"\b(?:32456207|7410512|7411636|7411622|7412582|86076364)\b"),
]
py_files = []
errors = []
for path in sorted(ROOT.rglob("*")):
    if not path.is_file() or path.name == "verify_bundle.py":
        continue
    if path.suffix in (".db", ".sqlite", ".ex5", ".pem", ".key"):
        errors.append(f"forbidden binary/private file: {path.relative_to(ROOT)}")
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError:
        errors.append(f"non-text source: {path.relative_to(ROOT)}")
        continue
    for pattern in BAD:
        if pattern.search(text):
            errors.append(f"sensitive literal: {path.relative_to(ROOT)}")
            break
    if path.suffix == ".py" or (path.parent.name == "scripts" and text.startswith("#!") and "python" in text.splitlines()[0]):
        py_files.append(path)
        try:
            ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            errors.append(f"Python syntax: {path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
print(f"migration files checked: {len([p for p in ROOT.rglob('*') if p.is_file()])}")
print(f"Python files checked: {len(py_files)}")
for error in errors:
    print("FAIL:", error)
print("ABUTRON_V9_MIGRATION_BUNDLE_"+("FAIL" if errors else "SOURCE_LINT_PASS"))
sys.exit(1 if errors else 0)
