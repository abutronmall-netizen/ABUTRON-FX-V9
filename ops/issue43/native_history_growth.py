"""Issue #43: safely plan an FP DEMO native history growth repair.

Pure code, never connects to MT5, modifies deployed sources, or sends orders.
The production router itself is managed outside this public repository.
This module documents the exact three-callsite patch and SHA allowlist update.
A release operator must separately verify the original source SHA, broker
account identity, signed EA pin, customer risk controls and rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

NATIVE_HISTORY_LIMIT_BYTES = 5_000_000
LEGACY_NATIVE_HISTORY_LIMIT_BYTES = 300_000
REQUIRED_HISTORY_LOAD_SITES = 3
RISK_POLICY_ANCHOR = "PORTFOLIO_CEILING=0.20"
ORIGINAL_HISTORY_LOAD = "load(HISTORY,300000)"
PATCHED_HISTORY_LOAD = "load(HISTORY,NATIVE_HISTORY_LIMIT_BYTES)"


class NativeHistoryPatchError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def propose_router_patch(original: str) -> str:
    """Plan a narrow fail-closed source patch; never touch deployed files."""
    if not isinstance(original, str):
        raise NativeHistoryPatchError("ROUTER_SOURCE_REQUIRED")
    if original.count(RISK_POLICY_ANCHOR) != 1:
        raise NativeHistoryPatchError("ORIGINAL_PORTFOLIO_CEILING_NOT_UNIQUE")
    if original.count(ORIGINAL_HISTORY_LOAD) != REQUIRED_HISTORY_LOAD_SITES:
        raise NativeHistoryPatchError("NATIVE_HISTORY_LOAD_CALLSITE_COUNT_CHANGED")
    if original.count("NATIVE_HISTORY_LIMIT_BYTES") != 0:
        raise NativeHistoryPatchError("REPAIR_ALREADY_APPLIED_OR_PARTIAL")
    patched = original.replace(
        RISK_POLICY_ANCHOR,
        RISK_POLICY_ANCHOR + "\n"
        "# Current broker-native history grows with broker fills. Never truncate.\n"
        "NATIVE_HISTORY_LIMIT_BYTES=5_000_000",
        1,
    ).replace(ORIGINAL_HISTORY_LOAD, PATCHED_HISTORY_LOAD)
    if patched.count(PATCHED_HISTORY_LOAD) != REQUIRED_HISTORY_LOAD_SITES:
        raise NativeHistoryPatchError("PATCH_NOT_APPLIED_TO_EVERY_HISTORY_READER")
    if patched.count(RISK_POLICY_ANCHOR) != 1:
        raise NativeHistoryPatchError("PORTFOLIO_POLICY_CHANGED")
    if "load(RECON,300000)" in original and "load(RECON,300000)" not in patched:
        raise NativeHistoryPatchError("UNRELATED_RECONCILIATION_BOUND_CHANGED")
    return patched


def propose_activation_source_allowlist(
    original: str, original_router_sha256: str, candidate_router_sha256: str
) -> str:
    """Add a *specific* source hash, leaving original/rollback hash approved.

    This changes source text only, not a running activation service or CRM.
    """
    if not isinstance(original, str) or not all(
        isinstance(v, str) and len(v) == 64
        and all(c in "0123456789abcdef" for c in v)
        for v in (original_router_sha256, candidate_router_sha256)
    ):
        raise NativeHistoryPatchError("INVALID_SHA_ALLOWLIST_REQUEST")
    if original_router_sha256 == candidate_router_sha256:
        raise NativeHistoryPatchError("NO_SOURCE_CHANGE")
    anchor = "'" + original_router_sha256 + "',"
    if original.count(anchor) != 1:
        raise NativeHistoryPatchError("OLD_SOURCE_PIN_MISSING_OR_DUPLICATE")
    if candidate_router_sha256 in original:
        raise NativeHistoryPatchError("CANDIDATE_ALREADY_APPROVED")
    return original.replace(
        anchor, anchor + "\n '" + candidate_router_sha256
        + "', # Issue43 original broker-history bound, same risk limits", 1
    )


def read_bounded_native_json(path: Path, max_bytes: int = NATIVE_HISTORY_LIMIT_BYTES):
    """Read a regular, stable native JSON source; never truncate or follow symlinks."""
    if not isinstance(path, Path) or type(max_bytes) is not int or not 1000 <= max_bytes <= NATIVE_HISTORY_LIMIT_BYTES:
        raise NativeHistoryPatchError("BAD_BOUNDED_NATIVE_READ_PARAMETERS")
    st = path.lstat()
    if (
        not stat.S_ISREG(st.st_mode)
        or path.is_symlink()
        or st.st_nlink != 1
        or not 1 <= st.st_size <= max_bytes
    ):
        raise NativeHistoryPatchError("UNSAFE_OR_OVERSIZED_NATIVE_SOURCE")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        raw = os.read(fd, max_bytes + 1)
        after = os.fstat(fd)
        metadata = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if len(raw) != before.st_size or metadata(before) != metadata(after):
            raise NativeHistoryPatchError("SOURCE_CHANGED_DURING_READ")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise NativeHistoryPatchError("NATIVE_DOCUMENT_NOT_OBJECT")
        return obj, sha256(raw)
    finally:
        os.close(fd)


def verify_hash_pinned_candidate(
    actual_original_bytes: bytes,
    expected_original_sha256: str,
    candidate_bytes: bytes,
    expected_candidate_sha256: str,
) -> None:
    """Independent byte attestation for controlled, manually reviewed rollout."""
    if sha256(actual_original_bytes) != expected_original_sha256:
        raise NativeHistoryPatchError("ORIGINAL_ROUTER_SOURCE_SHA_MISMATCH")
    if sha256(candidate_bytes) != expected_candidate_sha256:
        raise NativeHistoryPatchError("CANDIDATE_ROUTER_SOURCE_SHA_MISMATCH")
