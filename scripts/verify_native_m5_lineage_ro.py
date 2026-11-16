#!/usr/bin/env python3
"""Verify broker-export M5 candles against existing local broker-native warehouses.

Read-only source audit. Does not start MT5, link accounts or write broker state.
"""
from __future__ import annotations
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

SYMBOLS = {"Deriv":("Volatility 10 Index.0","Volatility 75 Index.0"),
           "FP-Markets":("GBPUSD","EURUSD")}


def archive_rows(folder:Path, symbols:tuple[str,...]) -> tuple[dict,int,int]:
    p=folder/"candles.csv.gz"
    manifest=json.loads((folder/"candles.csv.gz.manifest.json").read_text())
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    if digest!=manifest["sha256"]:
        raise ValueError(f"{folder.name}: archive hash invalid")
    unique={}
    total=0
    duplicated=0
    with gzip.open(p,"rt",newline="") as f:
        for r in csv.DictReader(f):
            total+=1
            if r["timeframe"]!="M5" or r["symbol"] not in symbols:
                continue
            key=(r["symbol"],int(r["start_time_utc"]))
            value=tuple(float(r[k]) for k in ("open","high","low","close"))
            if key in unique:
                duplicated+=1
                if unique[key]!=value:
                    raise ValueError(f"{folder.name}: conflicting duplicate")
            else:
                unique[key]=value
    if total!=manifest["rows"]:
        raise ValueError(f"{folder.name}: manifest row count mismatch")
    return unique,duplicated,total


def warehouse_rows(db_path:str, broker:str, symbols:tuple[str,...]) -> dict:
    c=sqlite3.connect("file:"+db_path+"?mode=ro",uri=True,timeout=4)
    c.execute("PRAGMA query_only=ON")
    table = "candles" if broker=="Deriv" else "fp_candles"
    tscol="start_time_utc"
    res={}
    for sym in symbols:
        q=f"SELECT {tscol},open,high,low,close FROM {table} WHERE symbol=? AND timeframe='M5'"
        for row in c.execute(q,(sym,)):
            key=(sym,int(row[0]))
            values=tuple(float(x) for x in row[1:])
            res.setdefault(key,set()).add(values)
    c.close()
    return res


def verify(root:Path, deriv_db:str, fp_db:str) -> dict:
    evidence={}
    for broker in SYMBOLS:
        arch,dups,total=archive_rows(root/broker,SYMBOLS[broker])
        target=deriv_db if broker=="Deriv" else fp_db
        db=warehouse_rows(target,broker,SYMBOLS[broker])
        matched=sum(1 for key,val in arch.items() if val in db.get(key,set()))
        absent=sum(1 for key in arch if key not in db)
        conflicts=sum(1 for key,val in arch.items() if key in db and val not in db[key])
        evidence[broker]={"selected_unique":len(arch),
            "exact_native_warehouse_matches":matched,
            "native_warehouse_conflicts":conflicts,
            "warehouse_keys_absent":absent,
            "selected_archive_key_duplicates":dups,
            "all_archive_rows":total,
            "read_only_native_warehouse":target}
        if matched!=len(arch) or conflicts or absent:
            raise ValueError(f"{broker}: selected M5 sources differ from local native warehouse")
    return evidence


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source",required=True)
    p.add_argument("--deriv-db",default="/var/lib/apex/precision-history/closed-candles.sqlite3")
    p.add_argument("--fp-db",default="/var/lib/abutron-quant-first/dual-broker/fp-immutable-native-candles.sqlite3")
    p.add_argument("--out")
    args=p.parse_args()
    data=verify(Path(args.source),args.deriv_db,args.fp_db)
    payload={"verification":"EXACT_MATCH_TO_LOCAL_BROKER_NATIVE_WAREHOUSE",
             "account_ids_in_export":False,
             "not_broker_cryptographically_signed":True,
             "results":data}
    if args.out:
        Path(args.out).write_text(json.dumps(payload,indent=2)+"\n")
    print(json.dumps(payload,indent=2))


if __name__=="__main__":
    main()
