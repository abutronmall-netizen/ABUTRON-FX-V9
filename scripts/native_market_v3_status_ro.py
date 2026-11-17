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
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from abutron.feeds.native_market_v3 import read_v3

SHA="a4a5bdbec180f8b4f7d0945b1611a945ca36d1ed49e4e23137133b9e4d592f67"
CID={
    "0512":("35b81a287b6a030f2f46ff426b135e6c","7410512"),
    "1636":("80cf848587fcec6311dd93560c1c46c6","7411636"),
}
REPORT=Path("/var/lib/abutron-v9-native-market-v3/latest.json")

def scan():
    evidence=[]
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
        for symbol in sorted(set(symbols))[:25]:
            detail=read_v3(worker_root=str(root),connection_id=cid,
                login=login,server="FPMarketsSC-Demo",symbol=symbol,
                approved_binary_sha256=SHA)
            entry["symbols"].append(detail.summary())
        if not entry["indicator_pinned_on_disk"]:
            entry["state"]="BINARY_NOT_PINNED"
        elif not symbols:
            entry["state"]="STAGED_NOT_ATTACHED_NO_NATIVE_V3_FILES"
        elif any(x.get("verified_broker_orderflow") for x in entry["symbols"]):
            entry["state"]="NATIVE_DATA_PARTIALLY_CERTIFIED"
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
