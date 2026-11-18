#!/usr/bin/env python3
"""Isolated read-only production monitor for staged ABUTRON V3 DEMO market data.

Inspects only configured FP DEMO accounts, MQL5 Files and SHA-pinned
read-only indicators. No chart attach, activation, writer or order operations.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from abutron.feeds.native_market_v3 import read_v3, trusted_fp_demo_clock_offset

SHA="a4a5bdbec180f8b4f7d0945b1611a945ca36d1ed49e4e23137133b9e4d592f67"
# Account profiles are resolved from explicit JSON, otherwise authorized
# read-only CRM. Only the two FP Markets DEMO monitor accounts are eligible.
SUPPORTED_FP_DEMO = frozenset(("0512", "1636"))
CRM_DB = Path("/var/lib/apex-signup/accounts.sqlite3")

def _validate_accounts(profiles):
    """Reject malformed, duplicated, or incorrectly scoped profile mappings."""
    if not isinstance(profiles, dict):
        return {}
    valid = {}
    seen_ids = set()
    for suffix, entry in profiles.items():
        if (suffix not in SUPPORTED_FP_DEMO or
                not isinstance(entry, (list, tuple)) or len(entry) != 2):
            return {}
        cid, login = entry
        if (not isinstance(cid, str) or not isinstance(login, str) or
                not re.fullmatch(r"[a-f0-9]{32}", cid) or
                not re.fullmatch(r"\d{5,12}", login) or
                not login.endswith(suffix) or cid in seen_ids):
            return {}
        valid[suffix] = (cid, login)
        seen_ids.add(cid)
    return valid

def _resolve_accounts(crm_path=CRM_DB):
    """Use valid explicit config, or query the FP DEMO CRM read-only.

    A malformed explicit environment value fails closed rather than silently
    reverting to another account source. Absent configuration may fall back
    to approved CRM account identities. No CRM state is modified.
    """
    raw = os.environ.get("ABUTRON_FP_ACCOUNT_PROFILES", "")
    if raw.strip():
        try:
            return _validate_accounts(json.loads(raw))
        except (ValueError, TypeError):
            return {}
    try:
        path = Path(crm_path)
        if path.is_symlink() or not path.is_file():
            return {}
        with sqlite3.connect("file:" + str(path) + "?mode=ro",
                             uri=True, timeout=5) as db:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute(
                "SELECT id,account_reference,broker,environment "
                "FROM apex_mt5_connections"
            ).fetchall()
        candidates = {}
        for cid, login, broker, environment in rows:
            suffix = str(login)[-4:]
            if suffix not in SUPPORTED_FP_DEMO:
                continue
            if (broker != "FP Markets" or environment != "DEMO" or
                    suffix in candidates):
                return {}
            candidates[suffix] = (cid, str(login))
        return _validate_accounts(candidates)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {}

CID = _resolve_accounts()
REPORT=Path("/var/lib/abutron-v9-native-market-v3/latest.json")

def scan():
    evidence=[]
    if not CID:
        return {"schema":"abutron.v9.native_market_v3.readonly_status.v1",
            "checked_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
            "state":"NO_CONFIGURED_ACCOUNTS",
            "accounts":[],
            "orders_sent":0,"client_switches_modified":False,
            "live_trading_modified":False,
            "existing_chart_eas_replaced":False,
            "risk_certified":False}
    for suffix,(cid,login) in CID.items():
        root=Path("/var/lib/private/abutron-client-mt5-"+cid)
        binary=root/"terminal/MQL5/Indicators/ABUTRON/ApexMarketFeedV3.ex5"
        entry={"account_suffix":suffix,"broker":"FP Markets","environment":"DEMO",
               "indicator_pinned_on_disk":False,"native_chart_attested":False,
               "market_prints_verified":False,"symbols":[]}
        if binary.is_file() and not binary.is_symlink():
            entry["indicator_pinned_on_disk"]=(
                hashlib.sha256(binary.read_bytes()).hexdigest()==SHA)
        base=root/"terminal/MQL5/Files/ABUTRON_V9_NATIVE"
        symbols=[]
        if base.is_dir() and not base.is_symlink():
            for p in base.glob("ABUTRON_NATIVE_MARKET_V3_*_????????.jsonl"):
                sym=p.name.removeprefix("ABUTRON_NATIVE_MARKET_V3_")[:-15]
                if sym and re.fullmatch(r"[A-Za-z0-9._-]{1,64}",sym):
                    symbols.append(sym)
        if symbols:
            if suffix=="0512":
                hist="/var/lib/apex-signup/fp-native-history/"+cid+".json"
                recon="/var/lib/abutron-fp-execution/reconciliation/latest.json"
            else:
                hist="/var/lib/apex/operator-live/fp-native-history-account-"+cid+".json"
                recon="/var/lib/abutron-fp-execution/1636/reconciliation/latest.json"
            try:
                broker_offset=trusted_fp_demo_clock_offset(
                    native_history_file=hist,
                    native_reconciliation_file=recon,account_suffix=suffix)
            except (OSError,ValueError,TypeError) as exc:
                broker_offset=None
                entry["clock_proof_blocker"]=str(exc)[:120]
            for symbol in sorted(set(symbols))[:25]:
                detail=read_v3(worker_root=str(root),connection_id=cid,
                    login=login,server="FPMarketsSC-Demo",symbol=symbol,
                    approved_binary_sha256=SHA,
                    broker_clock_offset_seconds=broker_offset)
                entry["symbols"].append(detail.summary())
                if detail.native_chart_attested:
                    entry["native_chart_attested"]=True
        if not entry["indicator_pinned_on_disk"]:
            entry["state"]="BINARY_NOT_PINNED"
        elif not symbols:
            entry["state"]="STAGED_NOT_ATTACHED_NO_NATIVE_V3_FILES"
        elif any(x.get("verified_broker_orderflow") for x in entry["symbols"]):
            entry["state"]="BROKER_NATIVE_MARKET_PRINTS_VERIFIED"
        elif any(x.get("verified_native_quote_ticks") for x in entry["symbols"]):
            entry["state"]="BROKER_QUOTES_AND_OPTIONAL_DOM_VERIFIED_NO_MARKET_PRINTS"
        else:
            entry["state"]="NATIVE_DATA_UNCERTIFIED"
        evidence.append(entry)
    return {"schema":"abutron.v9.native_market_v3.readonly_status.v1",
        "checked_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
        "state":"WAITING_FOR_GENUINE_MARKET_PRINT_AND_CHART_PROOF",
        "accounts":evidence,
        "orders_sent":0,"client_switches_modified":False,
        "live_trading_modified":False,
        "existing_chart_eas_replaced":False,
        "risk_certified":False}

def main():
    report=scan()
    if REPORT.parent.is_symlink() or REPORT.parent.stat().st_uid!=0:
        raise RuntimeError("NATIVE_MONITOR_REPORT_DIRECTORY_INVALID")
    fd,tmp=tempfile.mkstemp(dir=REPORT.parent,prefix=".native-v3-status-")
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"w") as f:
            json.dump(report,f,sort_keys=True,indent=2,allow_nan=False)
            f.write("\n");f.flush();os.fsync(f.fileno())
        os.replace(tmp,REPORT)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print(json.dumps({"checked_utc":report["checked_utc"],
        "state":report["state"],
        "accounts":[{"account_suffix":x["account_suffix"],"state":x["state"],
                     "native_files":len(x["symbols"])} for x in report["accounts"]],
        "orders_sent":0},sort_keys=True))
    return 0

if __name__=="__main__":
    sys.exit(main())