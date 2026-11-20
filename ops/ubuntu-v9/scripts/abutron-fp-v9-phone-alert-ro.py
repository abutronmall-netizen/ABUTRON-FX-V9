#!/usr/bin/env python3
"""ABUTRON FP DEMO V9 phone alerts through the existing private Telegram bot.

Reads only sanitized dashboard alert transitions, never trades or modifies CRM/risk.
Receipts are local root-only state; no bot token is printed or saved to logs.
"""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import os
import re
import stat
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SOURCE = Path("/var/lib/apex/operator-live/fp-v9-demo-readiness.json")
CREDENTIALS = Path("/etc/abutron/v7-telegram-signals.env")
DATA_DIR = Path("/var/lib/abutron-fp-v9-phone-alert")
STATE = DATA_DIR / "delivery-state.json"
LOCK = DATA_DIR / "lock"
SCHEMA = "abutron.apex.operator.fp_v9_demo_readiness.v1"
EVENT_TYPES = {"FIRST_13_OF_13", "LOST_13_OF_13", "RECOVERED_13_OF_13"}
VALID_IDS = re.compile(r"^FPV9-[0-9]{6,10}$")
MAX_SENT = 240
HTTP_TIMEOUT_SEC = 9

class AlertError(RuntimeError):
    pass

def utc_date(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AlertError("EVENT_TIMEZONE_REQUIRED")
    return parsed

def require(condition, code):
    if not condition:
        raise AlertError(code)

def read_attested(path: Path, limit: int, root_only=False):
    require(not path.is_symlink(), "INPUT_SYMLINK")
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == 0
                and not (st.st_mode & 0o022)
                and 1 <= st.st_size < limit, "INPUT_UNTRUSTED")
        if root_only:
            require(not (st.st_mode & 0o077), "SECRET_PERMISSIONS_TOO_WIDE")
        data = os.read(fd, limit)
        after = os.fstat(fd)
        require(len(data) == st.st_size
                and (st.st_ino, st.st_mtime_ns, st.st_size)
                    == (after.st_ino, after.st_mtime_ns, after.st_size),
                "INPUT_CHANGED")
        return data
    finally:
        os.close(fd)

def read_config():
    raw = read_attested(CREDENTIALS, 8192, root_only=True).decode("utf-8")
    config = {}
    for line in raw.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if match:
            config[match.group(1)] = match.group(2).strip().strip("\"'")
    require(config.get("ABUTRON_TELEGRAM_ENABLED", "").lower() in
            ("true", "1", "yes", "on"), "PHONE_CHANNEL_DISABLED")
    token = config.get("ABUTRON_TELEGRAM_BOT_TOKEN", "")
    chat = config.get("ABUTRON_TELEGRAM_CHAT_ID", "")
    require(re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{25,100}", token) is not None
            and re.fullmatch(r"-?[0-9]{4,20}", chat) is not None,
            "PHONE_CHANNEL_NOT_CONFIGURED")
    return token, chat

def read_status():
    raw = read_attested(SOURCE, 32000)
    data = json.loads(raw)
    require(isinstance(data, dict) and data.get("schema") == SCHEMA,
            "STATUS_SCHEMA_INVALID")
    require(data.get("broker") == "FP Markets"
            and data.get("environment") == "DEMO"
            and data.get("ready_for_order_execution") is False
            and data.get("orders_submitted_by_monitor") == 0,
            "STATUS_NOT_READ_ONLY")
    alerts = data.get("coverage_alerts")
    require(isinstance(alerts, dict)
            and alerts.get("schema") == "abutron.apex.fp_v9_demo.coverage_alerts.v1",
            "ALERTS_SCHEMA_INVALID")
    history = alerts.get("history")
    require(isinstance(history, list) and len(history) <= 24,
            "ALERTS_HISTORY_INVALID")
    events = []
    found = set()
    for row in reversed(history):
        require(isinstance(row, dict) and row.get("type") in EVENT_TYPES
                and row.get("account") in ("0512", "1636")
                and isinstance(row.get("id"), str)
                and VALID_IDS.fullmatch(row["id"]) is not None,
                "ALERT_EVENT_INVALID")
        require(row["id"] not in found, "ALERT_EVENT_DUPLICATE")
        found.add(row["id"])
        when = utc_date(row.get("at_utc"))
        require(-15 <= (dt.datetime.now(dt.timezone.utc) - when).total_seconds()
                        <= 30 * 86400, "ALERT_EVENT_TIME_OUTSIDE_WINDOW")
        events.append(row)
    return events

def load_receipts():
    if not STATE.exists():
        return None
    data = json.loads(read_attested(STATE, 150000, root_only=True))
    require(isinstance(data, dict) and data.get("schema") == "abutron.fp_v9.telegram_delivery.v1",
            "PHONE_RECEIPT_SCHEMA_INVALID")
    ids = data.get("sent_ids")
    require(isinstance(ids, list) and len(ids) <= MAX_SENT
            and all(isinstance(x, str) and VALID_IDS.fullmatch(x) for x in ids),
            "PHONE_RECEIPTS_INVALID")
    return data

def save_receipts(state):
    fd, tmp = tempfile.mkstemp(prefix=".receipts-", dir=str(DATA_DIR))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, sort_keys=True, separators=(",", ":"))
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, STATE)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)

def telegram(token, chat, message, request=urllib.request.urlopen):
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    body = json.dumps({
        "chat_id": chat,
        "text": message,
        "disable_web_page_preview": True,
    }, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=body,
             headers={"Content-Type": "application/json",
                      "User-Agent": "ABUTRON-FP-DEMOV9-PhoneAlerts/1"},
             method="POST")
    try:
        with request(req, timeout=HTTP_TIMEOUT_SEC,
                     context=ssl.create_default_context()) as response:
            reply = json.loads(response.read(32768))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AlertError("TELEGRAM_NETWORK_DELIVERY_FAILED") from None
    require(isinstance(reply, dict) and reply.get("ok") is True
            and isinstance(reply.get("result"), dict)
            and type(reply["result"].get("message_id")) is int,
            "TELEGRAM_API_NOT_ACCEPTED")
    return reply["result"]["message_id"]

def event_message(row):
    account = row["account"]
    ident = row["id"]
    timestamp = row["at_utc"]
    if row["type"] == "FIRST_13_OF_13":
        return (f"🎉 ABUTRON V9 | FP Markets DEMO {account}\n"
                f"FIRST 13/13 ACHIEVED: all 13 symbols have authenticated broker-native M5 inference.\n"
                f"Time: {timestamp} UTC reference\n"
                f"Event: {ident}\n"
                "Dashboard: https://abutronfx.online/apex/\n"
                "Coverage milestone only; does NOT authorize broker orders.")
    if row["type"] == "RECOVERED_13_OF_13":
        return (f"✅ ABUTRON V9 | FP Markets DEMO {account}\n"
                f"INFERENCE RECOVERED: 13/13 symbols broker-verified.\n"
                f"Time: {timestamp} UTC reference\n"
                f"Event: {ident}\n"
                "Dashboard: https://abutronfx.online/apex/\n"
                "Research coverage only; not order execution authorization.")
    code = str(row.get("reason_code", "BROKER_FEED_UNVERIFIED"))[:70]
    missing = row.get("missing_symbols") or []
    missing = [m for m in missing if isinstance(m, str)
               and re.fullmatch(r"[A-Z0-9]{3,12}", m)][:13]
    missing_line = ", ".join(missing) if missing else "broker/inference evidence"
    return (f"🚨 ABUTRON V9 | FP Markets DEMO {account}\n"
            f"INFERENCE COVERAGE LOST after verified 13/13.\n"
            f"Missing: {missing_line}\n"
            f"Reason: {code}\n"
            f"Time: {timestamp} UTC reference\n"
            f"Event: {ident}\n"
            "Dashboard: https://abutronfx.online/apex/\n"
            "Research coverage alert, not a broker-order event.")

def run():
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(stat.S_IMODE(DATA_DIR.stat().st_mode) == 0o700
            and DATA_DIR.stat().st_uid == 0, "PHONE_STATE_DIR_UNTRUSTED")
    lockfd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX)
        events = read_status()
        state = load_receipts()
        if state is None:
            # Enable for FUTURE events; don't deliver old dashboard history on first run.
            state = {
                "schema": "abutron.fp_v9.telegram_delivery.v1",
                "sent_ids": [e["id"] for e in events][-MAX_SENT:],
                "initialized_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "last_telegram_message_id": None,
            }
            save_receipts(state)
            print("FP_V9_PHONE_ALERTS_ARMED", "historical_events_suppressed",len(events))
            return
        pending = [e for e in events if e["id"] not in set(state["sent_ids"])]
        if not pending:
            return
        token, chat = read_config()
        for event in pending:
            message_id = telegram(token, chat, event_message(event))
            state["sent_ids"] = (state["sent_ids"] + [event["id"]])[-MAX_SENT:]
            state["last_telegram_message_id"] = message_id
            state["last_delivered_event_id"] = event["id"]
            state["last_delivered_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_receipts(state)
            print("FP_V9_PHONE_ALERT_DELIVERED",event["id"],event["type"],
                  "DEMO_"+event["account"],"telegram_accepted")
    finally:
        os.close(lockfd)

if __name__ == "__main__":
    try:
        run()
    except (OSError, ValueError, TypeError, KeyError, AlertError) as error:
        # Deliberately never log secrets or URLs containing bot tokens.
        print("FP_V9_PHONE_ALERT_ERROR",
              str(error) if isinstance(error, AlertError) else type(error).__name__,
              file=sys.stderr)
        raise SystemExit(1)
