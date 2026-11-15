"""Read-only Deriv DEMO native position-vs-ledger recovery proof.

This module never modifies account state, MT5 folders, ledgers or broker orders.
A matched EA receipt is still separate from the required owner-wide cash-risk
attestation and is NEVER automatic order authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

HEX32 = re.compile(r"^[a-f0-9]{32}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
ACTIVE = ("RESERVED", "SUBMISSION_UNKNOWN", "BROKER_ACK_UNRECONCILED",
          "OPEN_PROTECTED", "UNPROTECTED")


class RecoveryUnverified(ValueError):
    pass


def _need(value: bool, reason: str) -> None:
    if not value:
        raise RecoveryUnverified(reason)


def _read_lines(path: Path, *, max_bytes: int = 5000) -> list[str]:
    st = path.lstat()
    _need(stat.S_ISREG(st.st_mode) and not path.is_symlink()
          and 0 < st.st_size <= max_bytes and not st.st_mode & 0o022,
          "NATIVE_PROOF_FILE_UNTRUSTED")
    return path.read_text(encoding="utf-8-sig").splitlines()


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
        _need(result.is_finite() and result > 0, name + "_INVALID")
        return result
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise RecoveryUnverified(name + "_INVALID") from exc


def _epoch(value: Any, now: float, maximum_age: int, name: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecoveryUnverified(name + "_INVALID") from exc
    _need(0 <= now - v <= maximum_age, name + "_STALE")
    return v


def _m5_bar_for_key(owner: str, cid: str, symbol: str, side: str,
                    signal: str, stamp: int) -> int | None:
    # The native ACK typically follows a closed M5 candle shortly thereafter.
    # Wide range is read-only historical reconstruction, never signal replay.
    first = ((stamp - 24 * 3600) // 300) * 300
    last = (stamp // 300) * 300
    for bar in range(first, last + 300, 300):
        if bar + 300 > stamp:
            continue
        key = hashlib.sha256(
            (owner + "|" + cid + "|" + symbol + "|M5|" +
             str(bar) + "|" + side).encode()).hexdigest()
        if key == signal:
            return bar
    return None


def assess_position_evidence(*, owner: str, cid: str, login: str, server: str,
                             nonce: str, native_position: dict,
                             evidence: list[str], receipt: list[str],
                             intent: list[str], ledger_keys: set[str],
                             ledger_tickets: set[int],
                             now: float) -> dict:
    """Read-only cross-binding: CRM owner + native session + ACK + protected position.

    Never infer compliant risk from broker price geometry; require OrderCalcProfit
    amounts already exported by native EA.
    """
    _need(bool(HEX32.fullmatch(owner)) and bool(HEX32.fullmatch(cid))
          and bool(HEX32.fullmatch(nonce)), "NATIVE_ACCOUNT_IDENTITY_INVALID")
    _need(server == "Deriv-Demo" and login.isdigit(), "NATIVE_NOT_DERIV_DEMO")
    _need(len(evidence) == 19 and evidence[0] == "APEX_BROKER_POSITION_V2",
          "POSITION_PROOF_SCHEMA_INVALID")
    key, proof_nonce, proof_login, proof_server = evidence[1:5]
    _need(bool(HEX64.fullmatch(key))
          and (proof_nonce, proof_login, proof_server) ==
              (nonce, login, server),
          "POSITION_PROOF_ACCOUNT_BINDING_INVALID")
    symbol, direction = evidence[5:7]
    _need(direction in ("BUY", "SELL"), "POSITION_PROOF_DIRECTION_INVALID")
    _need(evidence[16] == "OPEN_PROTECTED", "POSITION_NOT_BROKER_PROTECTED")
    _epoch(evidence[17], now, 130, "NATIVE_POSITION_PROOF")
    try:
        order, deal, ticket = map(int, evidence[7:10])
    except (TypeError, ValueError) as exc:
        raise RecoveryUnverified("POSITION_TICKETS_INVALID") from exc
    _need(min(order, deal, ticket) > 0, "POSITION_TICKETS_INVALID")
    vol, entry, sl, tp, cash_loss, cash_reward = (
        _decimal(value, "POSITION_NATIVE_NUMERIC")
        for value in evidence[10:16])
    _need(sl < entry < tp if direction == "BUY" else tp < entry < sl,
          "POSITION_NATIVE_STOP_GEOMETRY_INVALID")
    _need(cash_loss > 0 and cash_reward > 0,
          "POSITION_NATIVE_ORDER_CALC_PROFIT_INVALID")
    _need((native_position.get("symbol"), native_position.get("side"),
           str(native_position.get("ticket"))) == (symbol, direction, str(ticket))
          and native_position.get("protected") is True,
          "BROKER_FEED_POSITION_IDENTITY_MISMATCH")
    for key_name, expected in (("volume", vol), ("sl", sl), ("tp", tp)):
        _need(_decimal(native_position.get(key_name), key_name) == expected,
              "BROKER_FEED_POSITION_GEOMETRY_MISMATCH")

    _need(len(receipt) == 10 and receipt[:2] == ["APEX_NATIVE_V2", key]
          and receipt[2] == "ACK",
          "NATIVE_ORDER_RECEIPT_NOT_ACK")
    _need(receipt[7:10] == [nonce, login, server],
          "NATIVE_ORDER_RECEIPT_ACCOUNT_MISMATCH")
    _epoch(receipt[6], now, 2 * 86400, "NATIVE_ORDER_RECEIPT")
    _need(str(order) == receipt[4] and str(deal) == receipt[5],
          "BROKER_DEAL_AND_POSITION_NOT_LINKED")
    bar = _m5_bar_for_key(owner, cid, symbol, direction, key, int(receipt[6]))
    _need(bar is not None, "NATIVE_SIGNAL_KEY_NOT_LINKED_TO_CRM_OWNER")
    _need(len(intent) == 14 and intent[:7] == [
        "APEX_OPTIONC_NATIVE_RISK_INTENT_V2", key, nonce, login, server,
        symbol, direction], "NATIVE_RISK_INTENT_ACCOUNT_MISMATCH")
    _epoch(intent[13], now, 60 * 86400, "NATIVE_RISK_INTENT")
    equity, budget, lots, intended_entry, initial_sl, target = (
        _decimal(x, "NATIVE_INTENT") for x in intent[7:13])
    _need(lots == vol and target == tp, "NATIVE_INTENT_BROKER_VOLUME_TP_MISMATCH")
    _need((sl >= initial_sl if direction == "BUY" else sl <= initial_sl),
          "NATIVE_BROKER_STOP_WIDENED")
    _need((initial_sl < intended_entry < target if direction == "BUY"
           else target < intended_entry < initial_sl),
          "NATIVE_INTENT_STOP_GEOMETRY_INVALID")
    _need(key not in ledger_keys and ticket not in ledger_tickets,
          "NATIVE_POSITION_ALREADY_EXISTS_IN_LEDGER")

    contract_limit = equity * Decimal("0.001")
    risk_limit_pass = (budget <= contract_limit and
                       cash_loss <= min(budget * Decimal("1.01"), contract_limit)
                       + Decimal("0.005") and
                       cash_reward >= cash_loss * Decimal("1.5"))
    return {
        "position_state": "BROKER_NATIVE_OPEN_PROTECTED",
        "receipt_state": "ACCOUNT_BOUND_NATIVE_ACK",
        "crm_m5_signal_reconstructed": True,
        "signal_bar_utc": datetime.fromtimestamp(
            bar, tz=timezone.utc).isoformat(),
        "position_ticket": ticket,
        "native_risk_usd": float(cash_loss),
        "risk_budget_usd": float(budget),
        "equity_at_intent_usd": float(equity),
        "native_stop_risk_pct_of_entry_equity": round(
            float(cash_loss / equity * Decimal("100")), 5),
        "native_0_10_percent_contract_pass": risk_limit_pass,
        "ledger_coverage": "MISSING",
        "recovery_class": ("UNLEDGERED_NATIVE_POSITION_WITH_POLICY_CONFLICT"
                           if not risk_limit_pass else
                           "UNLEDGERED_NATIVE_POSITION_REQUIRES_CONTROLLED_RECONCILIATION"),
        "execution_authorized": False,
        "ledger_modified": False,
    }


def inspect_6207(*, cid: str, crm_db: Path, ledger_db: Path,
                 feed_root: Path, worker_root: Path,
                 now: float | None = None) -> dict:
    instant = time.time() if now is None else float(now)
    _need(bool(HEX32.fullmatch(cid)), "CONNECTION_ID_INVALID")
    with sqlite3.connect(f"file:{crm_db}?mode=ro", uri=True, timeout=3) as db:
        row = db.execute("""
          SELECT x.account_id,x.account_reference,x.broker,x.environment,
                 x.broker_server,x.status,x.worker_binding,
                 COALESCE(t.desired_enabled,0),COALESCE(t.effective_enabled,0)
          FROM apex_mt5_connections x LEFT JOIN apex_trade_controls t
          ON t.connection_id=x.id WHERE x.id=?""", (cid,)).fetchone()
    _need(row is not None, "CRM_DERIV_ACCOUNT_NOT_LINKED")
    owner, login, broker, env, server, state, binding, desired, effective = row
    _need(bool(HEX32.fullmatch(owner)) and login.endswith("6207")
          and (broker, env, server, state, binding) ==
              ("Deriv", "DEMO", "Deriv-Demo", "CONNECTED",
               "abutron-client-mt5-" + cid),
          "CRM_DERIV_DEMO_6207_IDENTITY_INVALID")
    with sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True, timeout=3) as db:
        rows = db.execute(
            "SELECT signal_key,broker_position,state FROM attempts "
            "WHERE connection_id=?", (cid,)).fetchall()
    ledger_keys = {r[0] for r in rows}
    ledger_tickets = {int(r[1]) for r in rows if r[1] is not None}
    ledger_active = sum(r[2] in ACTIVE for r in rows)

    worker_health = json.loads("\n".join(
        _read_lines(worker_root / "worker-health.json", max_bytes=12000)))
    _need(worker_health.get("id") == cid
          and worker_health.get("broker") == broker
          and worker_health.get("mode") == env
          and worker_health.get("server") == server
          and worker_health.get("status") == "CONNECTED"
          and worker_health.get("heartbeat_fresh") is True,
          "MT5_WORKER_IDENTITY_OR_HEALTH_INVALID")
    _epoch(worker_health.get("verified_at"), instant, 20, "MT5_WORKER")
    nonce = worker_health.get("nonce")
    _need(isinstance(nonce, str) and bool(HEX32.fullmatch(nonce)),
          "MT5_WORKER_NONCE_INVALID")
    files = worker_root / "terminal/MQL5/Files"
    marker = files / ("ABUTRON_APEX_BRIDGE_READY_V3_" + nonce + ".txt")
    marker_lines = _read_lines(marker)
    _need(0 <= instant - marker.stat().st_mtime <= 12
          and len(marker_lines) == 11
          and marker_lines[:5] == [
              "APEX_NATIVE_BRIDGE_READY_V3", nonce, login, server,
              "DERIV_DEMO"], "MT5_NATIVE_V3_BRIDGE_NOT_ATTESTED")
    _epoch(marker_lines[5], instant, 18, "MT5_NATIVE_V3_BRIDGE")
    _need(int(marker_lines[6]) == 0,
          "MT5_NATIVE_PENDING_ORDERS_UNVERIFIED")
    native_positions = int(marker_lines[7])
    _need(marker_lines[8] in ("0", "1") and
          marker_lines[9] in ("0", "1") and
          marker_lines[10] in ("0", "1"),
          "MT5_NATIVE_TRADE_CAPABILITY_INVALID")
    native_order_enabled = marker_lines[10] == "1"

    feed_path = feed_root / (cid + ".json")
    doc = json.loads("\n".join(
        _read_lines(feed_path, max_bytes=150000)))
    _need(doc.get("connection_id") == cid and
          doc.get("account_id") == owner and
          doc.get("status") == "BROKER_DATA_VERIFIED_SHADOW" and
          doc.get("broker_data_verified") is True,
          "BROKER_FEED_OWNER_OR_STATUS_NOT_VERIFIED")
    try:
        stamp = datetime.fromisoformat(
            doc["checked_at_utc"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError) as exc:
        raise RecoveryUnverified("BROKER_FEED_TIMESTAMP_INVALID") from exc
    _need(0 <= instant - stamp <= 90, "BROKER_FEED_NOT_FRESH")
    account = doc.get("account") or {}
    positions = account.get("positions")
    _need(isinstance(positions, list)
          and type(account.get("open_positions")) is int
          and account["open_positions"] == len(positions) == native_positions
          and account.get("all_positions_protected") is True,
          "NATIVE_BROKER_POSITION_COUNTS_NOT_COHERENT")

    result = {
        "schema": "abutron.v9.deriv_demo_6207_ledger_recovery_audit.readonly.v1",
        "checked_at_utc": datetime.fromtimestamp(
            instant, tz=timezone.utc).isoformat(),
        "account_suffix": "6207",
        "crm_desired_on": desired == 1,
        "crm_effective_on": effective == 1,
        "native_broker_positions": len(positions),
        "native_bridge_protocol": 3,
        "native_ea_orders_enabled": native_order_enabled,
        "ledger_active_positions": ledger_active,
        "ledger_modified": False,
        "orders_submitted": 0,
        "execution_authorized": False,
        "positions": [],
        "blockers": [],
    }
    for position in positions:
        matched = []
        folder = files / "ABUTRON"
        for proof_path in (folder / "evidence").glob("*.txt"):
            if not HEX64.fullmatch(proof_path.stem):
                continue
            try:
                evidence = _read_lines(proof_path)
            except (OSError, RecoveryUnverified):
                continue
            if len(evidence) != 19 or str(position.get("ticket")) != evidence[9]:
                continue
            key = proof_path.stem
            receipt = _read_lines(folder / "receipts" / (key + ".txt"))
            intent = _read_lines(folder / "optionc_v2" / (key + ".txt"))
            proof = assess_position_evidence(
                owner=owner, cid=cid, login=login, server=server,
                nonce=nonce, native_position=position, evidence=evidence,
                receipt=receipt, intent=intent, ledger_keys=ledger_keys,
                ledger_tickets=ledger_tickets, now=instant)
            matched.append(proof)
        if len(matched) != 1:
            result["blockers"].append(
                "NATIVE_POSITION_EVIDENCE_MISSING_OR_AMBIGUOUS")
            continue
        result["positions"].append(matched[0])
        if not matched[0]["native_0_10_percent_contract_pass"]:
            result["blockers"].append(
                "NATIVE_POSITION_RISK_EXCEEDS_0_10_PERCENT_CONTRACT")
        if matched[0]["ledger_coverage"] == "MISSING":
            result["blockers"].append("BROKER_POSITION_MISSING_FROM_APEX_LEDGER")
    if ledger_active != len(positions):
        result["blockers"].append("LEDGER_BROKER_POSITION_COUNT_MISMATCH")
    if not native_order_enabled:
        result["blockers"].append("NATIVE_EA_ORDER_CAPABILITY_DISABLED")
    result["state"] = ("RECONSTRUCTION_REQUIRES_POLICY_REVIEW"
                       if result["blockers"] else "RECONCILED_READONLY")
    return result
