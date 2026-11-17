#!/usr/bin/env python3
"""Inspect pinned FP/Deriv DEMO V3 native market print / DOM evidence (read-only).

Do NOT certify orderflow based on raw file counts or account order history.
A chart-attachment attestation is required before assigning VERIFIED.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from abutron.feeds.native_market_v3 import read_v3

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worker-root",required=True)
    p.add_argument("--connection-id",required=True)
    p.add_argument("--login",required=True)
    p.add_argument("--server",required=True)
    p.add_argument("--symbol",required=True)
    p.add_argument("--binary-sha256",required=True)
    p.add_argument("--json",action="store_true")
    args=p.parse_args()
    result=read_v3(worker_root=args.worker_root,
        connection_id=args.connection_id,login=args.login,server=args.server,
        symbol=args.symbol,approved_binary_sha256=args.binary_sha256)
    print(json.dumps(result.summary(),indent=2,sort_keys=True))
    return 0 if result.verified_broker_orderflow else 2

if __name__=="__main__":
    sys.exit(main())
