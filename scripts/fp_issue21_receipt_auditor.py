#!/usr/bin/python3
"""FP 0512 Issue #21 immutable-receipt rotating auditor.

Read-only with respect to the MT5 terminal. Does NOT emit receipts/evidence,
change trading flags, publish broker ACKs, migrate session nonces or send orders.
The broker's independent read-only reconciler remains authoritative for ACK proof.
"""
import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

ACCOUNT_ID = os.environ.get("ABUTRON_ISSUE21_CONNECTION_ID", "")
EXPECTED_LOGIN = os.environ.get("ABUTRON_ISSUE21_ACCOUNT_LOGIN", "")
EXPECTED_SERVER = os.environ.get("ABUTRON_ISSUE21_BROKER_SERVER", "FPMarketsSC-Demo")
EXPECTED_MODE = "DEMO"
BASE = Path("/var/lib/private") / ("abutron-client-mt5-" + ACCOUNT_ID)
NATIVE = BASE / "terminal/MQL5/Files/ABUTRON_FP"
HEALTH = BASE / "worker-health.json"
RECON = Path("/var/lib/abutron-fp-execution/reconciliation/latest.json")
HISTORY = Path("/var/lib/apex/operator-live/fp-native-history.json")
STATE = Path("/var/lib/abutron-fp0512-issue21-scanner/state.json")
HASH64 = re.compile(r"[a-f0-9]{64}\Z")
MAX_FILES = 10000
BATCH_SIZE = 250


def regular(path, max_size):
    if path.is_symlink() or not path.is_file():
        raise ValueError("NONREGULAR_INPUT")
    st = path.stat()
    if st.st_size > max_size or st.st_size < 1:
        raise ValueError("SIZE_UNEXPECTED")
    return path


def load_json(path, limit=600000):
    return json.loads(regular(path, limit).read_text("utf-8"))


def atomic_json(path, obj):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("SYMLINKED_STATE_PARENT")
    fd, tmp = tempfile.mkstemp(prefix=".issue21-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def list_receipt_names(folder):
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError("RECEIPT_DIRECTORY_NOT_REGULAR")
    results = []
    for p in folder.iterdir():
        if not p.name.endswith(".txt") or not HASH64.fullmatch(p.stem):
            raise ValueError("INVALID_RECEIPT_FILENAME")
        regular(p, 2048)
        results.append(p.name)
        if len(results) > MAX_FILES:
            raise ValueError("TOO_MANY_RECEIPTS")
    return sorted(results)


def parse_receipt(path, expected_nonce):
    lines = regular(path, 2048).read_text("utf-8-sig").splitlines()
    if (len(lines) != 10 or lines[0] != "APEX_FP_NATIVE_V1"
        or lines[1] != path.stem or
        lines[2] not in ("ACK", "BLOCKED", "UNKNOWN") or
        lines[7:] != [expected_nonce, EXPECTED_LOGIN, EXPECTED_SERVER]):
        raise ValueError("RECEIPT_SCHEMA_OR_IDENTITY")
    order, deal, stamp = (int(lines[i]) for i in (4, 5, 6))
    if min(order, deal, stamp) < 0:
        raise ValueError("RECEIPT_NEGATIVE_IDENTIFIERS")
    if lines[2] == "ACK" and (order == 0 or deal == 0):
        raise ValueError("ACK_MISSING_ORDER_OR_DEAL")
    return {"kind": lines[2], "order": order, "deal": deal,
            "timestamp": stamp, "reason": lines[3]}


def parse_evidence(path, receipt, nonce):
    lines = regular(path, 4096).read_text("utf-8-sig").splitlines()
    if (len(lines) != 19 or lines[:5] != [
            "APEX_FP_BROKER_POSITION_V1", path.stem, nonce,
            EXPECTED_LOGIN, EXPECTED_SERVER]):
        raise ValueError("EVIDENCE_SCHEMA_OR_IDENTITY")
    if (lines[5] == "" or lines[6] not in ("BUY", "SELL") or
        int(lines[7]) != receipt["order"] or
        int(lines[8]) != receipt["deal"]):
        raise ValueError("EVIDENCE_ORDER_OR_DEAL_MISMATCH")
    state = lines[16]
    if state not in ("CLOSED_BROKER_VERIFIED", "OPEN_PROTECTED",
                     "OPEN_UNPROTECTED", "OPEN_UNKNOWN",
                     "OPEN_BROKER_VERIFIED", "OPEN_PROTECTED_V2"):
        # Other states are recorded as evidence-only, never certified.
        state = "UNRECOGNIZED_NATIVE_EVIDENCE_STATE"
    return state


def broker_reconciliation_current(recon, history, now):
    return (recon.get("status") == "RECONCILED_READONLY"
            and recon.get("broker_verified_ack_count") ==
            (recon.get("receipt_counts") or {}).get("ACK")
            and not recon.get("blockers")
            and history.get("history_verified_from_mt5") is True
            and history.get("account_masked") == ("••••" + EXPECTED_LOGIN[-4:])
            and history.get("environment") == "DEMO"
            and history.get("pending_orders_count") == 0
            and abs(now - RECON.stat().st_mtime) < 25
            and abs(now - HISTORY.stat().st_mtime) < 25)


def scan_native(folder, nonce, offset, seen, recon_ok, broker_acks, prior_missing=None):
    names = list_receipt_names(folder / "receipts")
    if not names:
        raise ValueError("NO_NATIVE_RECEIPTS")
    # Repeated runs cover the entire set, including names beyond the original
    # 250-first-record cutoff. Account identity is checked per file.
    offset = offset % len(names)
    batch = min(len(names), BATCH_SIZE)
    counts = {"ACK": 0, "BLOCKED": 0, "UNKNOWN": 0,
              "EA_EVIDENCE_PRESENT": 0, "ACK_WITHOUT_EA_EVIDENCE": 0}
    violations = []
    missing = []
    cumulative_missing = {v["receipt_hash"] : v for v in (prior_missing or [])
                          if isinstance(v, dict) and
                          isinstance(v.get("receipt_hash"), str) and
                          HASH64.fullmatch(v["receipt_hash"])}
    visited = set(seen)
    for i in range(batch):
        name = names[(offset + i) % len(names)]
        visited.add(name)
        try:
            rec = parse_receipt(folder / "receipts" / name, nonce)
            counts[rec["kind"]] += 1
            if rec["kind"] == "ACK":
                ep = folder / "evidence" / name
                if ep.is_file() or ep.is_symlink():
                    parse_evidence(ep, rec, nonce)
                    counts["EA_EVIDENCE_PRESENT"] += 1
                    cumulative_missing.pop(name, None)
                else:
                    counts["ACK_WITHOUT_EA_EVIDENCE"] += 1
                    gap = {"receipt_hash": name,
                           "order": rec["order"],
                           "deal": rec["deal"]}
                    missing.append(gap)
                    cumulative_missing[name] = gap
        except (OSError, ValueError, UnicodeError, IndexError) as e:
            violations.append({"receipt_hash": name[:64],
                               "problem": str(e)[:96]})
    next_offset = (offset + batch) % len(names)
    return {
        "total_receipts": len(names),
        "batch_size": batch,
        "initial_offset": offset,
        "next_offset": next_offset,
        "cycle_seen": sorted(visited),
        "cycle_coverage": len(visited),
        "cycle_complete": len(visited) == len(names),
        "batch_counts": counts,
        "missing_native_evidence": missing,
        "cycle_missing_native_evidence": list(cumulative_missing.values()),
        "cycle_missing_native_evidence_count": len(cumulative_missing),
        "native_validation_errors": violations,
        "broker_reconciler_verified": bool(recon_ok),
        "broker_reconciler_ack_count": broker_acks
    }


def execute(base=BASE, state=STATE, recon=RECON, history=HISTORY, now=None):
    now = time.time() if now is None else now
    worker = load_json(base / "worker-health.json", 30000)
    nonce = worker.get("nonce")
    if (worker.get("status") != "CONNECTED" or
        worker.get("heartbeat_fresh") is not True or
        worker.get("mode") != EXPECTED_MODE or
        worker.get("id") != ACCOUNT_ID or
        not isinstance(nonce, str) or
        not re.fullmatch("[a-f0-9]{32}", nonce)):
        raise ValueError("ACTIVE_FP0512_SESSION_UNVERIFIED")
    native = base / "terminal/MQL5/Files/ABUTRON_FP"
    reconciliation = load_json(recon, 50000)
    broker = load_json(history, 2500000)
    old = {}
    if state.is_file() and not state.is_symlink():
        try:
            old = load_json(state, 1000000)
        except (ValueError, OSError, KeyError, UnicodeError):
            old = {}
    marker = hashlib.sha256(nonce.encode()).hexdigest()
    names = list_receipt_names(native / "receipts")
    same = (old.get("nonce_sha256") == marker
            and old.get("receipt_names_digest") ==
            hashlib.sha256("\n".join(names).encode()).hexdigest())
    prior_seen = old.get("cycle_seen", []) if same else []
    prior_offset = old.get("next_offset", 0) if same else 0
    if not isinstance(prior_offset, int) or not isinstance(prior_seen, list):
        prior_offset, prior_seen = 0, []
    # Caller passes sources instead of relying on global paths in tests.
    recon_ok = (reconciliation.get("status") == "RECONCILED_READONLY"
                and reconciliation.get("broker_verified_ack_count") ==
                    (reconciliation.get("receipt_counts") or {}).get("ACK")
                and not reconciliation.get("blockers")
                and broker.get("history_verified_from_mt5") is True
                and broker.get("account_masked") == ("••••" + EXPECTED_LOGIN[-4:])
                and broker.get("environment") == "DEMO"
                and broker.get("pending_orders_count") == 0
                and abs(now - recon.stat().st_mtime) <= 25
                and abs(now - history.stat().st_mtime) <= 25)
    result = scan_native(native, nonce, prior_offset, prior_seen,
                         recon_ok, reconciliation.get("broker_verified_ack_count"),
                         old.get("cycle_missing_native_evidence", []) if same else [])
    # No impersonation: broker-authorized old ACK does not become new
    # 'native evidence' and a missing evidence file stays counted missing.
    result.update({
        "schema": "abutron.fpmarkets.issue21.readonly_rotating_scan.v1",
        "account_masked": ("••••" + EXPECTED_LOGIN[-4:]),
        "mode": "DEMO",
        "nonce_sha256": marker,
        "receipt_names_digest": hashlib.sha256("\n".join(names).encode()).hexdigest(),
        "scanned_utc_unix": now,
        "status": ("INVALID_NATIVE_CORPUS" if result["native_validation_errors"]
                   else "BROKER_RECONCILIATION_NOT_CURRENT" if not recon_ok
                   else "ROTATING_FULL_CYCLE_NATIVE_EVIDENCE_GAP_BROKER_RECONCILED"
                        if result["cycle_complete"] and
                           result["cycle_missing_native_evidence_count"] > 0
                   else "ROTATING_FULL_CYCLE_BROKER_RECONCILED"
                        if result["cycle_complete"]
                   else "ROTATING_BROKER_RECONCILED"),
        "writes_into_mt5_terminal": False,
        "can_send_orders": False
    })
    atomic_json(state, result)
    return result


def _validate_account_binding():
    """Validate account-bound environment at runtime, not import time.

    Moving this out of module-level code allows the module to be imported
    for testing without requiring environment variables. The checks still
    enforce fail-closed behavior when the script is actually executed.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", ACCOUNT_ID):
        raise RuntimeError("ACCOUNT_BOUND_CONNECTION_ID_MISSING")
    if not re.fullmatch(r"[0-9]{5,12}", EXPECTED_LOGIN):
        raise RuntimeError("ACCOUNT_BOUND_LOGIN_MISSING")
    if EXPECTED_SERVER != "FPMarketsSC-Demo":
        raise RuntimeError("BROKER_SERVER_NOT_FP_DEMO")


def main():
    _validate_account_binding()
    parser = argparse.ArgumentParser()
    parser.add_argument("--state",type=Path,default=STATE)
    a = parser.parse_args()
    result = execute(state=a.state)
    print(json.dumps({k: result[k] for k in (
        "status","total_receipts","batch_size","initial_offset",
        "next_offset","cycle_coverage","cycle_complete",
        "batch_counts","native_validation_errors",
        "cycle_missing_native_evidence_count",
        "broker_reconciler_verified","broker_reconciler_ack_count")},
        sort_keys=True))
    if result["native_validation_errors"]:
        return 2
    if not result["broker_reconciler_verified"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
