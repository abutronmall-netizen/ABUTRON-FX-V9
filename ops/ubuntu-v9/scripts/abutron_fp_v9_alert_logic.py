"""Persistent, transition-based dashboard alerts for FP Markets V9 DEMO 13/13 coverage.

Emits one FIRST_13_OF_13 milestone per account, one LOSS after verified
coverage disappears, and RECOVERED events on genuine restoration.
Both accounts are independent. Milestone markers survive service restarts.
Never authorizes orders, imports broker clients, or modifies risk.
"""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path

NAMES = ("0512", "1636")
MAX_HISTORY = 24

def iso(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()

def safe_previous(path):
    try:
        if path.is_symlink() or path.stat().st_size > 250000:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") != "abutron.apex.operator.fp_v9_demo_readiness.v1":
            return {}
        return raw
    except (OSError, ValueError, TypeError):
        return {}

def make_reason(current, suffix):
    detail = (current.get("accounts") or {}).get(suffix, {})
    if not current.get("available") or not isinstance(detail, dict):
        return ("EVIDENCE_UNVERIFIED", [], "Current broker/inference proof is unavailable")
    reasons = detail.get("reasons", [])
    if not isinstance(reasons, list):
        reasons = []
    for row in reasons:
        if isinstance(row, dict) and row.get("reason_code"):
            code = str(row.get("reason_code"))[:75]
            text = str(row.get("reason", "Native feed or model evidence incomplete"))[:140]
            names = sorted(set(str(x)[:20] for x in detail.get("missing", [])
                               if isinstance(x, str)))[:13]
            return (code, names, text)
    return ("INFERENCE_INCOMPLETE", [], "Verified 13/13 model inference is incomplete")

def enrich(current, previous, now):
    previous = previous if isinstance(previous, dict) else {}
    prev_alert = previous.get("coverage_alerts")
    prev_alert = prev_alert if isinstance(prev_alert, dict) else {}
    prev_accounts = previous.get("accounts")
    prev_accounts = prev_accounts if isinstance(prev_accounts, dict) else {}
    was_armed = prev_alert.get("armed", {})
    was_armed = was_armed if isinstance(was_armed, dict) else {}
    first_announced = prev_alert.get("first_13of13_announced", {})
    first_announced = first_announced if isinstance(first_announced, dict) else {}
    last_verified = prev_alert.get("last_verified_utc", {})
    last_verified = last_verified if isinstance(last_verified, dict) else {}
    active = prev_alert.get("active", {})
    active = {key: dict(v) for key, v in active.items()
              if key in NAMES and isinstance(v, dict)} if isinstance(active, dict) else {}
    history = prev_alert.get("history", [])
    history = [dict(v) for v in history if isinstance(v, dict)][:MAX_HISTORY] if isinstance(history, list) else []
    sequence = prev_alert.get("sequence", 0)
    sequence = sequence if type(sequence) is int and 0 <= sequence <= 100000000 else 0
    state = {
        "schema": "abutron.apex.fp_v9_demo.coverage_alerts.v1",
        "active": active, "history": history, "sequence": sequence,
        "armed": {}, "last_verified_utc": {}, "first_13of13_announced": {}, "status": "CLEAR",
    }
    created = []
    timestamp = iso(now)
    account_map = current.get("accounts") if isinstance(current.get("accounts"), dict) else {}
    for suffix in NAMES:
        detail = account_map.get(suffix)
        is_full = (current.get("available") is True
                   and isinstance(detail, dict)
                   and detail.get("inference_certified") is True
                   and detail.get("status") == "COMPLETE_13_OF_13")
        prior_account = prev_accounts.get(suffix, {})
        prior_full = (isinstance(prior_account, dict)
                      and prior_account.get("inference_certified") is True)
        previously_certified = was_armed.get(suffix) is True or prior_full
        # Older versions only recorded "armed"; honor that history so upgrades
        # cannot generate a duplicate "first ever" milestone.
        already_announced = (first_announced.get(suffix) is True
                             or previously_certified)
        armed = previously_certified or is_full
        state["armed"][suffix] = armed
        state["first_13of13_announced"][suffix] = already_announced
        last = last_verified.get(suffix)
        if isinstance(last, str):
            state["last_verified_utc"][suffix] = last
        elif prior_full and isinstance(previous.get("inference_generated_utc"), str):
            state["last_verified_utc"][suffix] = previous["inference_generated_utc"]
        else:
            state["last_verified_utc"][suffix] = None
        if is_full:
            state["last_verified_utc"][suffix] = str(current.get("inference_generated_utc") or timestamp)
            if not already_announced and suffix not in active:
                state["sequence"] += 1
                event = {
                    "id": "FPV9-%06d" % state["sequence"], "type": "FIRST_13_OF_13",
                    "account": suffix, "at_utc": timestamp,
                    "message": "First authenticated 13/13 V9 DEMO inference coverage achieved",
                }
                history.insert(0, event)
                created.append(event)
                state["first_13of13_announced"][suffix] = True
            if suffix in active:
                alert = active.pop(suffix)
                state["sequence"] += 1
                event = {
                    "id": "FPV9-%06d" % state["sequence"], "type": "RECOVERED_13_OF_13",
                    "account": suffix, "at_utc": timestamp,
                    "related_loss_id": alert.get("id"),
                    "message": "Account restored to authenticated 13/13 inference coverage",
                }
                history.insert(0, event)
                created.append(event)
        elif armed:
            code, missing, reason = make_reason(current, suffix)
            if suffix not in active:
                state["sequence"] += 1
                event_id = "FPV9-%06d" % state["sequence"]
                alert = {
                    "id": event_id,
                    "account": suffix,
                    "type": "LOST_13_OF_13",
                    "raised_at_utc": timestamp,
                    "last_verified_at_utc": state["last_verified_utc"][suffix],
                    "reason_code": code,
                    "missing_symbols": missing,
                    "reason": reason,
                }
                active[suffix] = alert
                event = {
                    "id": event_id, "type": "LOST_13_OF_13",
                    "account": suffix, "at_utc": timestamp,
                    "reason_code": code, "missing_symbols": missing,
                    "message": "Previously verified 13/13 inference coverage lost",
                }
                history.insert(0, event)
                created.append(event)
            else:
                # Update cause/details while retaining initial alert ID and timestamp.
                active[suffix]["reason_code"] = code
                active[suffix]["missing_symbols"] = missing
                active[suffix]["reason"] = reason
    state["history"] = history[:MAX_HISTORY]
    state["status"] = "ACTIVE" if active else "CLEAR"
    current["coverage_alerts"] = state
    return current, created
