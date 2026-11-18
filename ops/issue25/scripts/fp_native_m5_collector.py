#!/usr/bin/env python3
"""Append-only closed M5 archive from authenticated FP DEMO V305 exports.
No MT5 connection, order execution, CRM mutation, or model promotion.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

FEED = Path("/var/lib/abutron-fpmarkets-feed/partial")
ROOT = Path("/var/lib/abutron-fp-native-m5-archive")
DB = ROOT / "broker-closed-m5.sqlite3"
CRM = Path("/var/lib/apex-signup/accounts.sqlite3")
EXPECTED_FP_DEMO = ("0512", "1636", "1622", "2582")

def account_connections(db_path=CRM):
    """Discover current FP DEMO profile IDs from read-only CRM, never from public source."""
    require(db_path.is_file() and not db_path.is_symlink(), "CRM_DB_UNAVAILABLE")
    with sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True, timeout=10) as db:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute(
            "SELECT id,account_reference,broker,environment FROM apex_mt5_connections"
        ).fetchall()
    found = {}
    for cid,login,broker,mode in rows:
        suffix = str(login)[-4:]
        if suffix not in EXPECTED_FP_DEMO:
            continue
        require(suffix not in found and broker == "FP Markets" and mode == "DEMO"
                and isinstance(cid,str) and len(cid) == 32
                and all(c in "0123456789abcdef" for c in cid),
                "CRM_FP_DEMO_NATIVE_PROFILE_ID_UNVERIFIED")
        found[suffix] = cid
    require(set(found)==set(EXPECTED_FP_DEMO), "CRM_FP_DEMO_ACCOUNT_SCOPE_INCOMPLETE")
    return found

def require(ok, code):
    if not ok:
        raise ValueError(code)

def authenticated_source(account, cid, now):
    path = FEED / (cid + ".json")
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == 0
                and st.st_nlink == 1 and stat.S_IMODE(st.st_mode) == 0o600
                and 100 <= st.st_size <= 3_000_000
                and -10 <= now - st.st_mtime < 170, "NATIVE_SOURCE_FILE_UNTRUSTED")
        body = os.read(fd, st.st_size + 1)
        end = os.fstat(fd)
        require(len(body) == st.st_size
                and (st.st_ino, st.st_mtime_ns, st.st_size)
                    == (end.st_ino, end.st_mtime_ns, end.st_size),
                "NATIVE_SOURCE_CHANGED_DURING_READ")
    finally:
        os.close(fd)
    doc = json.loads(body)
    checked = dt.datetime.fromisoformat(doc["checked_at_utc"].replace("Z","+00:00"))
    require(checked.tzinfo is not None
            and -10 <= now - checked.timestamp() < 170,
            "NATIVE_SNAPSHOT_NOT_CURRENT")
    require(doc.get("schema") == "abutron.fpmarkets.native.partial.readonly.v1"
            and doc.get("connection_id") == cid
            and doc.get("account_mode") == "DEMO"
            and doc.get("broker_server") == "FPMarketsSC-Demo"
            and doc.get("execution_enabled") is False
            and doc.get("orders_submitted") == 0
            and doc.get("parallel_forex_status") == "SIX_FOREX_R_VERIFIED_V305",
            "BROKER_ACCOUNT_OR_MODE_NOT_ATTESTED")
    mapping, bars = doc["mapping"], doc["bars"]
    require(isinstance(mapping, dict) and isinstance(bars, dict)
            and len(mapping) == len(bars) == 13
            and set(mapping) == set(bars),
            "BROKER_CATALOGUE_INVALID")
    out = []
    for canonical, group in bars.items():
        meta = mapping[canonical]
        actual = meta["actual"]
        require(meta.get("status") == "MAPPED" and meta.get("trade_mode") == 4
                and re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", actual) is not None,
                "BROKER_SYMBOL_MAPPING_UNVERIFIED")
        rows = group.get("M5")
        require(isinstance(rows, list) and len(rows) >= 60,
                "BROKER_M5_WINDOW_INCOMPLETE")
        previous = 0
        for bar in rows:
            t = bar["time"]
            require(type(t) is int and t > previous and t % 300 == 0,
                    "BROKER_M5_BAR_TIME_INVALID")
            previous = t
            if t + 300 > now:  # never store current/unclosed bar
                continue
            vals = tuple(bar[x] for x in ("open", "high", "low", "close"))
            require(all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in vals)
                    and vals[2] <= min(vals[0], vals[3])
                    <= max(vals[0], vals[3]) <= vals[1],
                    "BROKER_M5_PRICE_INVALID")
            raw = bar["broker_time_raw"]
            require(type(raw) is int and raw - t == 7200,
                    "BROKER_TIME_OFFSET_MISMATCH")
            out.append((account, canonical, actual, t, *vals, raw))
    return hashlib.sha256(body).hexdigest(), out

def run():
    now = time.time()
    accounts = account_connections()
    sources = [(account, *authenticated_source(account,cid,now))
               for account,cid in accounts.items()]
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    require(ROOT.stat().st_uid == 0 and stat.S_IMODE(ROOT.stat().st_mode)==0o700,
            "ARCHIVE_DIRECTORY_NOT_ROOT_PRIVATE")
    with sqlite3.connect(DB,timeout=8) as con:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("CREATE TABLE IF NOT EXISTS closed_m5 ("
                    "account TEXT NOT NULL, symbol TEXT NOT NULL, broker_symbol TEXT NOT NULL,"
                    "start_time_utc INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,"
                    "low REAL NOT NULL, close REAL NOT NULL, broker_time_raw INTEGER NOT NULL,"
                    "source_sha256 TEXT NOT NULL, first_seen_utc TEXT NOT NULL,"
                    "PRIMARY KEY(account,symbol,start_time_utc)) WITHOUT ROWID")
        con.execute("CREATE TABLE IF NOT EXISTS source_checks ("
                    "source_sha256 TEXT PRIMARY KEY, account TEXT NOT NULL,"
                    "observed_utc TEXT NOT NULL, input_rows INTEGER NOT NULL,"
                    "new_rows INTEGER NOT NULL)")
        con.execute("BEGIN IMMEDIATE")
        by_account = {}
        for account,digest,rows in sources:
            added = 0
            for row in rows:
                old=con.execute("SELECT broker_symbol,open,high,low,close,broker_time_raw "
                                "FROM closed_m5 WHERE account=? AND symbol=? AND start_time_utc=?",
                                (row[0],row[1],row[3])).fetchone()
                expected = (row[2], *row[4:8], row[8])
                require(old is None or old == expected,
                        "IMMUTABLE_BROKER_M5_BAR_CHANGED")
                if old is None:
                    con.execute("INSERT INTO closed_m5 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (*row,digest,dt.datetime.now(dt.timezone.utc).isoformat()))
                    added+=1
            con.execute("INSERT OR IGNORE INTO source_checks VALUES (?,?,?,?,?)",
                        (digest,account,dt.datetime.now(dt.timezone.utc).isoformat(),
                         len(rows),added))
            by_account[account]={"input_bars":len(rows),"inserted":added}
        require(set(by_account) == set(accounts),"ACCOUNT_SCOPE_INCOMPLETE")
        con.commit()
        os.chmod(DB,0o600)
        total=con.execute("SELECT COUNT(*) FROM closed_m5").fetchone()[0]
        print("FP_AUTHENTICATED_M5_ARCHIVE",json.dumps(
            {"accounts":by_account,"stored_total":total,
             "readonly_sources":True,"orders":0,"live_modified":False},
            sort_keys=True),flush=True)

if __name__ == "__main__":
    run()
