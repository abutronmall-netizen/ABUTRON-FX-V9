#!/usr/bin/python3
"""Issue #38: strict read-only verifier for isolated Deriv DEMO 6207 signed exposure.

Never promotes owner certification or touches MT5/CRM/trading.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

ACCOUNT_ID = __import__("os").environ["ABUTRON_CRM_ACCOUNT_ID_6207"]
EA_SHA = "7540e51c752df990edc669fa6fd004e70d8eef6ad88e3ab22b00df6a3a469e21"
BASE = Path("/var/lib/private/abutron-issue38-deriv6207-exposureqa")
NATIVE = BASE / "terminal/MQL5/Files"
HEALTH = BASE / "issue38-health.json"
PEER = Path("/var/lib/abutron-deriv-multi-native-reconciliation/latest.json")
SIDECAR = Path("/var/lib/private/abutron-native-readonly-mt5-6207/worker-health.json")
CRM = Path("/var/lib/apex-signup/accounts.sqlite3")
OUT = Path("/var/lib/abutron-issue38-exposure-readonly/latest.json")
MAX_SNAPSHOT_AGE = 420
MAX_PEER_AGE = 140


def strict_file(path, limit=1000000, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size < 2 or st.st_size > limit:
            raise ValueError("UNTRUSTED_SOURCE_FILE")
        if st.st_mode & 0o022 or (private and st.st_mode & 0o077):
            raise ValueError("SOURCE_FILE_PERMISSIONS_INVALID")
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(raw) != st.st_size or (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns):
            raise ValueError("SOURCE_READ_RACE")
        return raw, st
    finally:
        os.close(fd)


def numeric(value, label, positive=False):
    if type(value) not in (float, int) or not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError(label)
    return float(value)


def canonical_and_positions(d, login):
    if not isinstance(d, dict) or set(d) != {"balance", "challenge", "connection_id", "currency", "equity", "login", "nonce", "position_count", "positions", "server", "sig"}:
        raise ValueError("NATIVE_SCHEMA_MISMATCH")
    if str(d["login"]) != login or d["connection_id"] != "issue38-deriv-demo-6207" or d["server"] != "Deriv-Demo" or d["currency"] != "USD":
        raise ValueError("ACCOUNT_BROKER_IDENTITY_MISMATCH")
    positions = d["positions"]
    if type(d["position_count"]) is not int or not isinstance(positions, list) or len(positions) != d["position_count"] or len(positions) > 1000:
        raise ValueError("NATIVE_POSITION_COUNT_INVALID")
    equity, balance = numeric(d["equity"], "EQUITY_INVALID", True), numeric(d["balance"], "BALANCE_INVALID", True)
    if not isinstance(d["challenge"], str) or not (1 <= len(d["challenge"]) <= 256):
        raise ValueError("NATIVE_CHALLENGE_INVALID")
    parts, risks, tickets = [], [], set()
    for p in sorted(positions, key=lambda p: int(p["ticket"])):
        ticket = int(p["ticket"])
        if ticket <= 0 or ticket in tickets or type(p["ticket"]) not in (str, int):
            raise ValueError("POSITION_TICKET_INVALID")
        tickets.add(ticket)
        if not isinstance(p["symbol"], str) or not (1 <= len(p["symbol"]) <= 70) or p["direction"] not in ("BUY", "SELL"):
            raise ValueError("POSITION_IDENTITY_INVALID")
        volume = numeric(p["volume"], "POSITION_VOLUME_INVALID", True)
        entry = numeric(p["entry"], "POSITION_ENTRY_INVALID", True)
        sl = numeric(p["sl"], "POSITION_STOP_INVALID", True)
        tp = numeric(p.get("tp", 0), "POSITION_TARGET_INVALID")
        risk = numeric(p["stop_risk_usd"], "POSITION_RISK_INVALID")
        if p.get("ordercalcprofit_verified") is not True:
            raise ValueError("POSITION_NATIVE_ORDER_CALC_PROFIT_UNVERIFIED")
        parts.append(f"{ticket}:{p['symbol']}:{p['direction']}:{volume:.2f}:{entry:.5f}:{sl:.5f}:{tp:.5f}:{risk:.2f}:true")
        risks.append(risk)
    canonical = (f"issue38-deriv-demo-6207|{login}|Deriv-Demo|USD|{equity:.2f}|{balance:.2f}|"
                 f"{d['nonce']}|{d['challenge']}|{len(positions)}|{';'.join(parts)}")
    return canonical, len(positions), sum(risks), equity, balance


def verify_signature(d, key, login, now):
    nonce = str(d.get("nonce", ""))
    match = re.fullmatch(r"ABUTRON_NATIVE_(\d+)_(\d+)", nonce)
    if not match or match.group(1) != login:
        raise ValueError("NATIVE_NONCE_IDENTITY_MISMATCH")
    age = now - int(match.group(2))
    if not 0 <= age <= MAX_SNAPSHOT_AGE:
        raise ValueError("NATIVE_NONCE_STALE_OR_FUTURE")
    canonical, count, risk, equity, balance = canonical_and_positions(d, login)
    if not isinstance(d["sig"], str) or not re.fullmatch("[0-9a-fA-F]{64}", d["sig"]):
        raise ValueError("HMAC_FORMAT_INVALID")
    expected = hmac.new(key.encode("ascii"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, d["sig"].lower()):
        raise ValueError("NATIVE_HMAC_INVALID")
    return count, risk, equity, balance, age


def build(now=None):
    now = time.time() if now is None else float(now)
    report = {"schema": "abutron.issue38.deriv6207.signed_native_exposure_reconcile.readonly.v1",
              "checked_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
              "status": "NOT_VERIFIED", "account_masked": "••••6207",
              "mode": "DEMO", "broker": "Deriv", "signature_verified": False,
              "dual_native_crosscheck_verified": False,
              "trading_authorized": False, "customer_wide_risk_certified": False,
              "new_order_authority_changed": False, "live_accounts_changed": False}
    try:
        with sqlite3.connect(f"file:{CRM}?mode=ro", uri=True, timeout=4) as db:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute("SELECT account_reference,broker,environment,status FROM apex_mt5_connections WHERE id=?", (ACCOUNT_ID,)).fetchall()
        if len(rows) != 1 or rows[0][1:] != ("Deriv", "DEMO", "CONNECTED"):
            raise ValueError("CRM_DEMO_ACCOUNT_IDENTITY_NOT_CONNECTED")
        login = str(rows[0][0])
        if not login.isdigit() or not login.endswith("6207"):
            raise ValueError("CRM_ACCOUNT_LOGIN_INVALID")
        raw_health, health_stat = strict_file(HEALTH, 10000, private=True)
        health = json.loads(raw_health)
        if health.get("status") != "SIGNED_NATIVE_EXPOSURE_VERIFIED" or health.get("signature_verified") is not True or health.get("native_probe_sha256") != EA_SHA:
            raise ValueError("QA_WORKER_ATTESTATION_INVALID")
        if health.get("environment") != "DEMO" or health.get("account_suffix") != "6207":
            raise ValueError("QA_WORKER_IDENTITY_INVALID")
        if not 0 <= now - health_stat.st_mtime <= MAX_SNAPSHOT_AGE:
            raise ValueError("QA_WORKER_ATTESTATION_STALE")
        raw_exe, _ = strict_file(BASE / "terminal/MQL5/Experts/ABUTRON/ApexNativeExposureProbe.ex5", 100000)
        if hashlib.sha256(raw_exe).hexdigest() != EA_SHA:
            raise ValueError("PROBE_BINARY_HASH_MISMATCH")
        candidates = list(NATIVE.glob("ABUTRON_NATIVE_EXPOSURE_V2_ABUTRON_NATIVE_*.txt"))
        if not candidates:
            raise ValueError("NATIVE_SOURCE_FILE_MISSING")
        snapshot_path = max(candidates, key=lambda p: p.stat().st_mtime)
        raw, st = strict_file(snapshot_path, 1000000, private=True)
        snapshot_sha = hashlib.sha256(raw).hexdigest()
        if snapshot_sha != health.get("snapshot_sha256") or not 0 <= now - st.st_mtime <= MAX_SNAPSHOT_AGE:
            raise ValueError("SIGNED_NATIVE_SNAPSHOT_HASH_OR_TIME_INVALID")
        doc = json.loads(raw)
        key_raw, _ = strict_file(NATIVE / "ABUTRON_ACTIVATION_SECRET.txt", 256, private=True)
        key = key_raw.decode("ascii").strip()
        if not re.fullmatch("[0-9a-f]{64}", key):
            raise ValueError("HMAC_KEY_FORMAT_INVALID")
        count, risk, equity, balance, age = verify_signature(doc, key, login, now)
        report.update({"signature_verified": True, "signed_snapshot_sha256": snapshot_sha,
                       "native_position_count": count, "broker_native_existing_position_risk_usd": round(risk,2),
                       "sample_age_seconds": round(age,1), "snapshot_utc": dt.datetime.fromtimestamp(int(doc["nonce"].rsplit("_",1)[-1]), dt.timezone.utc).isoformat()})
        raw_sidecar, sst = strict_file(SIDECAR, 20000, private=True)
        sidecar = json.loads(raw_sidecar)
        if sidecar.get("status") != "CONNECTED" or sidecar.get("broker_status") != "AUTHENTICATED_READ_ONLY_DEMO" or sidecar.get("environment") != "DEMO" or sidecar.get("account_suffix") != "6207" or sidecar.get("broker_session_identity_verified") is not True or sidecar.get("execution_authority") is not False or not 0 <= now - sst.st_mtime <= MAX_PEER_AGE:
            raise ValueError("INDEPENDENT_SIDECAR_NOT_CURRENT")
        raw_peer, pst = strict_file(PEER, 600000)
        if not 0 <= now - pst.st_mtime <= MAX_PEER_AGE:
            raise ValueError("DERIV_DUAL_NATIVE_RECONCILIATION_STALE")
        peer = json.loads(raw_peer)
        if peer.get("schema") != "abutron.deriv.three_demo.native_dualproof.readonly.v1" or peer.get("execution_authorized") is not False or peer.get("orders_submitted") != 0:
            raise ValueError("INDEPENDENT_RECONCILIATION_CONTRACT_INVALID")
        accounts = peer.get("accounts")
        matches = [a for a in accounts if a.get("account_suffix") == "6207"] if isinstance(accounts, list) else []
        if len(matches) != 1:
            raise ValueError("INDEPENDENT_6207_ACCOUNT_NOT_FOUND")
        a = matches[0]
        if a.get("state") != "VERIFIED_READONLY" or a.get("broker_execution_authorized") is not False or a.get("pending_orders") != 0 or a.get("positions") != count:
            raise ValueError("SIGNED_NATIVE_VS_INDEPENDENT_POSITION_MISMATCH")
        if abs(numeric(a.get("balance_usd"), "PEER_BALANCE_INVALID") - balance) > 0.05 or abs(numeric(a.get("equity_usd"), "PEER_EQUITY_INVALID") - equity) > 0.05:
            raise ValueError("SIGNED_NATIVE_VS_INDEPENDENT_EQUITY_MISMATCH")
        if count == 0 and a.get("current_exposure") != "ZERO_DUAL_NATIVE_PROOF":
            raise ValueError("FLAT_NATIVE_STATE_MISMATCH")
        if count > 0:
            if a.get("current_exposure") != "PROTECTED_NATIVE_POSITIONS_DUAL_PROOF" or a.get("all_current_positions_sl_tp_usd_risk_verified") is not True:
                raise ValueError("OPEN_POSITIONS_RISK_INCOMPLETE")
            if abs(numeric(a.get("open_position_usd_stop_risk"), "PEER_RISK_INVALID") - risk) > 0.05:
                raise ValueError("SIGNED_NATIVE_VS_PEER_STOP_RISK_MISMATCH")
        report.update({"status":"SIGNED_NATIVE_EXPOSURE_RECONCILED_READONLY",
                       "dual_native_crosscheck_verified": True,
                       "independent_reconciliation_sha256": hashlib.sha256(raw_peer).hexdigest(),
                       "native_equity_usd": round(equity,2), "native_balance_usd": round(balance,2)})
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, OverflowError, sqlite3.Error) as exc:
        report["status"] = "NOT_VERIFIED"
        report["reason_code"] = str(exc)[:96] if isinstance(exc, ValueError) else type(exc).__name__.upper()
    return report


def main():
    report = build()
    OUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if OUT.parent.is_symlink() or OUT.parent.stat().st_uid != 0 or OUT.parent.stat().st_mode & 0o077:
        raise SystemExit("PRIVATE_OUTPUT_DIR_INVALID")
    fd, tmp = tempfile.mkstemp(prefix=".issue38-", dir=OUT.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, sort_keys=True, separators=(",",":"), allow_nan=False)
            f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, OUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("ISSUE38_SIGNED_EXPOSURE", report["status"], report.get("reason_code","OK"), "EXECUTION_OFF", flush=True)


if __name__ == "__main__":
    main()
