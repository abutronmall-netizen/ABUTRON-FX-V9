#!/usr/bin/env python3
"""ABUTRON V9: broker-verified entry-hour loss evidence, SHADOW ONLY.

Re-checks FP DEMO native paired receipts through the existing root-owned
feedback verifier. It NEVER touches production models, EA, orders or CRM.
A losing hour is a research candidate only after chronological validation,
not a guarantee that future trades will lose.
"""
from __future__ import annotations
import collections
import datetime as dt
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

SOURCE = Path("/usr/local/sbin/abutron-v9-broker-closed-feedback-ro.py")
OUTPUT = Path("/var/lib/abutron-v9-session-loss/latest.json")
SAST = ZoneInfo("Africa/Johannesburg")


def _utc(value):
    stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("UNVERIFIED_ENTRY_TIMEZONE")
    return stamp.astimezone(dt.timezone.utc)


def _money(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("INVALID_NET_CASHFLOW")
    return number


def normalized(rows):
    """Strictly unique and signed, closed deals only; upstream authenticates sources."""
    found = set()
    clean = []
    for row in rows:
        if row.get("broker") != "FP Markets" or row.get("account") not in ("0512", "1636"):
            raise ValueError("UNVERIFIED_BROKER_OR_ACCOUNT")
        event = str(row.get("event_id", ""))
        if len(event) != 40 or event in found:
            raise ValueError("DUPLICATE_OR_UNVERIFIED_CLOSE_ID")
        found.add(event)
        start = _utc(row["entry_time"])
        end = _utc(row["exit_time"])
        if end < start or end > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2):
            raise ValueError("INVALID_CLOSED_POSITION_TIMES")
        if row.get("direction") not in ("BUY", "SELL") or not row.get("symbol"):
            raise ValueError("INVALID_TRADE_DIRECTION_OR_SYMBOL")
        net = _money(row["pnl"])
        local = start.astimezone(SAST)
        clean.append({
            "account": row["account"], "symbol": row["symbol"],
            "direction": row["direction"], "event_id": event,
            "entry_utc": start.isoformat(), "date_sast": local.date().isoformat(),
            "hour_sast": local.hour, "pnl_usd": net,
        })
    return clean


def _stats(items):
    n = len(items)
    pnl = sum(row["pnl_usd"] for row in items)
    return {"trades": n, "days": len({r["date_sast"] for r in items}),
            "win_rate": round(sum(r["pnl_usd"] > 0 for r in items) / n, 4) if n else None,
            "total_net_usd": round(pnl, 2),
            "mean_net_usd": round(pnl / n, 2) if n else None}


def hour_profile(items):
    """Train on earlier *complete dates*, validate on later dates."""
    days = sorted({r["date_sast"] for r in items})
    train_days = set(days[:max(1, int(len(days) * 0.7))])
    earlier = [r for r in items if r["date_sast"] in train_days]
    later = [r for r in items if r["date_sast"] not in train_days]
    t, v = _stats(earlier), _stats(later)
    sufficient = t["trades"] >= 12 and v["trades"] >= 6 and t["days"] >= 2 and v["days"] >= 2
    negative = (t["mean_net_usd"] is not None and v["mean_net_usd"] is not None
                and t["mean_net_usd"] < 0 and v["mean_net_usd"] < 0
                and t["win_rate"] < .5 and v["win_rate"] < .5)
    status = ("INSUFFICIENT_OOS_HISTORY" if not sufficient else
              "LOSING_HOUR_SHADOW_CANDIDATE" if negative else "NO_VALIDATED_HOURLY_LOSS")
    return {"status": status, "total": _stats(items), "earlier_dates": t,
            "later_dates": v, "chronological_validation": True,
            "shadow_avoid_hour": bool(sufficient and negative),
            "production_veto_enabled": False}


def analyze(rows):
    clean = normalized(rows)
    account_hour = collections.defaultdict(list)
    setup_hour = collections.defaultdict(list)
    for trade in clean:
        account_hour[(trade["account"], trade["hour_sast"])].append(trade)
        setup_hour[(trade["account"], trade["symbol"], trade["direction"], trade["hour_sast"])].append(trade)
    hourly = []
    for (account, hour), entries in sorted(account_hour.items()):
        hourly.append({"account": account, "hour_sast": hour,
                       **hour_profile(entries)})
    setups = []
    for (account, symbol, direction, hour), entries in sorted(setup_hour.items()):
        p = hour_profile(entries)
        if p["total"]["trades"] >= 3 or p["shadow_avoid_hour"]:
            setups.append({"account": account, "symbol": symbol,
                           "direction": direction, "hour_sast": hour, **p})
    return {"schema": "abutron.v9.broker_hour_loss_shadow.v1",
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "timezone": "Africa/Johannesburg",
            "hour_definition": "BROKER_VERIFIED_TRADE_ENTRY_HOUR",
            "pnl_definition": "BROKER_NATIVE_NET_CASHFLOW_USD_NOT_R_MULTIPLE",
            "source_accounts": ["0512", "1636"], "closed_positions": len(clean),
            "shadow_hours_flagged": sum(h["shadow_avoid_hour"] for h in hourly),
            "account_hours": hourly, "setup_hours": setups,
            "candidate_retraining_independent": True,
            "model_promotion_allowed": False, "production_veto_enabled": False,
            "broker_orders": 0, "live_accounts_modified": False,
            "limitations": ["No inference about unseen hours",
                            "Cash PnL not normalized by stop-risk",
                            "Temporal holdout does not eliminate multiple-testing bias"]}


def verified_feed():
    if SOURCE.is_symlink() or SOURCE.stat().st_uid != 0:
        raise ValueError("FEEDBACK_VERIFIER_NOT_ROOT_OWNED")
    spec = importlib.util.spec_from_file_location("abutron_feedback_native", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    complete = []
    for account, (cid, recon) in module.ACCOUNTS.items():
        if account not in ("0512", "1636"):
            raise ValueError("UNEXPECTED_NATIVE_ACCOUNT")
        trades, metadata = module.from_account_with_bounded_reconciliation_retry(account, cid, recon)
        if metadata["closed_positions_paired"] != len(trades):
            raise ValueError("BROKER_CLOSED_TRADE_COUNT_MISMATCH")
        complete.extend(trades)
    return complete


def atomic_write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_uid != 0 or path.parent.stat().st_mode & 0o077:
        raise ValueError("UNTRUSTED_OUTPUT_DIRECTORY")
    fd, tmp = tempfile.mkstemp(prefix=".session-loss-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fp:
            json.dump(doc, fp, indent=2, sort_keys=True)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    output = analyze(verified_feed())
    atomic_write(OUTPUT, output)
    print("ABUTRON_V9_HOURLY_LOSS_SHADOW",
          "closed", output["closed_positions"],
          "account_hours", len(output["account_hours"]),
          "flagged", output["shadow_hours_flagged"],
          "orders", 0, "model_promotion", False, flush=True)


if __name__ == "__main__":
    main()