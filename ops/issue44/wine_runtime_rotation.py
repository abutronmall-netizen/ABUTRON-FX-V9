"""Issue44 FP DEMO — account-scoped, reversible MT5/Wine runtime rotation.

The existing MT5 worker normally reuses state/wine on every restart.
Native DEMO QA showed that a new Wine prefix with the *same* portable
terminal profile recovers original broker positions and history, while
a reused Wine prefix failed on a second launch.

This primitive is intended ONLY for an already-stopped, separately verified
account worker, after real original backups and broker-native preflight.
Never call it directly on the running production worker. It changes no
broker order, CRM, client switch or LIVE account and does not perform a
systemd action. The caller independently verifies the account owner and
stopped worker before invoking it; a new session is NOT a broker ACK.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

OLD_NAME="wine"
PREVIOUS_NAME="wine-before-issue44-sha-verified"
FAILED_NAME="wine-failed-issue44-sha-verified"
MARKER=".issue44-wine-rotation-journal.json"
SCHEMA="abutron.issue44.fp_demo_rotation.v1"

class WineRotationError(ValueError):
    pass

def _fsync_parent(path:Path)->None:
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)

def _ensure_private_state(state:Path,expected_basename:str)->None:
    if (not isinstance(state,Path) or not isinstance(expected_basename,str)
        or not re.fullmatch(r"abutron-client-mt5-[0-9a-f]{32}",expected_basename)
        or state.name!=expected_basename or
        state.is_symlink() or not state.is_dir()):
        raise WineRotationError("INVALID_ACCOUNT_STATE_SCOPE")
    st=state.stat()
    if st.st_mode&0o022:
        raise WineRotationError("UNTRUSTED_STATE_WRITABLE_BY_OTHERS")

def _safe_dir(path:Path)->bool:
    if path.is_symlink():raise WineRotationError("WINE_PREFIX_SYMLINK_REFUSED")
    return path.is_dir()

def _journal(state:Path)->dict:
    path=state/MARKER
    if not path.is_file() or path.is_symlink():
        raise WineRotationError("MISSING_OR_UNTRUSTED_ROTATION_JOURNAL")
    d=json.loads(path.read_text())
    if (d.get("schema")!=SCHEMA or d.get("account_state_basename")!=state.name
        or d.get("previous_prefix")!=PREVIOUS_NAME or d.get("active_prefix")!=OLD_NAME
        or d.get("failed_prefix")!=FAILED_NAME):
        raise WineRotationError("ROTATION_JOURNAL_ACCOUNT_MISMATCH")
    return d

def rotate_after_stop(state:Path,expected_basename:str)->dict:
    """Rotate ONLY after independent proof this account's worker stopped.

    A journal is written and fsynced BEFORE any rename. An interrupted
    rotation leaves the former prefix intact or recoverable by a separate
    stopped-worker rollback. Never silently wipe an old prefix.
    """
    _ensure_private_state(state,expected_basename)
    old=state/OLD_NAME
    previous=state/PREVIOUS_NAME
    failed=state/FAILED_NAME
    marker=state/MARKER
    if (marker.exists() or marker.is_symlink() or
        previous.exists() or previous.is_symlink() or
        failed.exists() or failed.is_symlink() or not _safe_dir(old)):
        raise WineRotationError("ROTATION_NOT_IN_UNMODIFIED_BASELINE")
    if old.stat().st_uid!=state.stat().st_uid:
        raise WineRotationError("WINE_PREFIX_OWNER_MISMATCH")
    journal={"schema":SCHEMA,"account_state_basename":state.name,
             "previous_prefix":PREVIOUS_NAME,"active_prefix":OLD_NAME,
             "failed_prefix":FAILED_NAME,
             "description":"isolated-account prefix rotation before verified broker reconnect"}
    encoded=(json.dumps(journal,sort_keys=True,separators=(",",":"))+"\n").encode()
    fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as f:
        f.write(encoded);f.flush();os.fsync(f.fileno())
    _fsync_parent(state)
    old.rename(previous)
    _fsync_parent(state)
    old.mkdir(mode=0o700)
    _fsync_parent(state)
    if not _safe_dir(old) or not _safe_dir(previous):
        raise WineRotationError("NEW_PREFIX_CREATE_FAILED")
    return {"status":"ROTATED_FOR_ISOLATED_ACCOUNT_START",
            "previous_preserved":True,"fresh_active_prefix":True,
            "journal_sha256":sha256(encoded).hexdigest()}

def rollback_after_stop(state:Path,expected_basename:str)->dict:
    """Revert only the exact original archived prefix after worker stopped."""
    _ensure_private_state(state,expected_basename)
    _journal(state)
    old=state/OLD_NAME
    previous=state/PREVIOUS_NAME
    failed=state/FAILED_NAME
    if not _safe_dir(previous):
        raise WineRotationError("PREVIOUS_WINE_RUNTIME_NOT_PRESENT")
    if failed.exists() or failed.is_symlink():
        raise WineRotationError("REFUSE_REPEATED_OR_AMBIGUOUS_ROLLBACK")
    if old.exists() and not _safe_dir(old):
        raise WineRotationError("UNTRUSTED_NEW_RUNTIME")
    if old.exists():
        old.rename(failed)
        _fsync_parent(state)
    previous.rename(old)
    _fsync_parent(state)
    if not _safe_dir(old):
        raise WineRotationError("PREVIOUS_WINE_RUNTIME_NOT_RESTORED")
    return {"status":"PREVIOUS_WINE_RUNTIME_RESTORED",
            "preserved_failed_new_runtime":failed.exists(),
            "read_only_broker_proof_still_required":True}

def audit(state:Path,expected_basename:str)->dict:
    """Reports the local rotation state. No broker certification implied."""
    _ensure_private_state(state,expected_basename)
    old=state/OLD_NAME
    prev=state/PREVIOUS_NAME
    failed=state/FAILED_NAME
    return {
        "journal_exists":(state/MARKER).exists(),
        "current_wine_exists":_safe_dir(old),
        "previous_wine_preserved":_safe_dir(prev),
        "failed_wine_archived":_safe_dir(failed),
        "native_broker_reconnection_certified":False,
        "production_release_certified":False,
    }
