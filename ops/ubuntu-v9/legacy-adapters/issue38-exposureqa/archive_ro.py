#!/usr/bin/python3
"""Preserve original signed DEMO 6207 exposure snapshots with independent
current HMAC/native peer verification. Read-only broker; no trade authority.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import stat
import time
from pathlib import Path
import ingest_ro as proof

ROOT = Path("/var/lib/abutron-issue38-native-archive")
SCHEMA = "abutron.issue38.original_signed_native_snapshot.archive.v1"


def trusted_output_directory(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    s=path.lstat()
    if (not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o077):
        raise RuntimeError("PRIVATE_ARCHIVE_DIRECTORY_UNTRUSTED")


def create_once(path:Path, payload:bytes):
    if path.exists():
        fd=os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            s=os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_uid!=0:
                raise RuntimeError("ARCHIVE_FILE_UNTRUSTED")
            existing=os.read(fd,len(payload)+1)
            if hashlib.sha256(existing).digest()!=hashlib.sha256(payload).digest():
                raise RuntimeError("ARCHIVE_EXISTING_BYTES_MISMATCH")
        finally:
            os.close(fd)
        return False
    temporary=path.with_name("."+path.name+"."+str(os.getpid())+".tmp")
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(payload);f.flush();os.fsync(f.fileno())
        try:
            os.link(temporary,path,follow_symlinks=False)
        except FileExistsError:
            # A concurrent writer must never replace existing evidence.
            return create_once(path,payload)
        dirfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(dirfd)
        finally:os.close(dirfd)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def archive():
    trusted_output_directory(ROOT)
    report=proof.build()
    if report.get("status")!="SIGNED_NATIVE_EXPOSURE_RECONCILED_READONLY":
        print("SIGNED_NATIVE_ARCHIVE_SKIP",report.get("reason_code","SOURCE_NOT_CURRENT"),"NO_ORDERS")
        return False
    sha=report["signed_snapshot_sha256"]
    candidates=sorted(proof.NATIVE.glob("ABUTRON_NATIVE_EXPOSURE_V2_ABUTRON_NATIVE_*.txt"),
                      key=lambda p:p.stat().st_mtime,reverse=True)
    if not candidates:
        raise RuntimeError("SIGNED_NATIVE_SOURCE_MISSING")
    raw,_=proof.strict_file(candidates[0],1000000,private=True)
    if hashlib.sha256(raw).hexdigest()!=sha:
        raise RuntimeError("NATIVE_SOURCE_CHANGED_AFTER_VERIFICATION")
    doc=json.loads(raw)
    if str(doc.get("login","")).endswith("6207") is False:
        raise RuntimeError("SIGNED_NATIVE_ACCOUNT_CHANGED")
    nonce=doc.get("nonce","")
    timestamp=int(str(nonce).rsplit("_",1)[-1])
    day=dt.datetime.fromtimestamp(timestamp,dt.timezone.utc).strftime("%Y%m%d")
    target=ROOT/day
    trusted_output_directory(target)
    attestation={
        "schema":SCHEMA,
        "environment":"DEMO",
        "account_masked":"••••6207",
        "broker":"Deriv",
        "signed_native_file_sha256":sha,
        "original_snapshot_utc":report["snapshot_utc"],
        "independent_peer_sha256_at_verification":report["independent_reconciliation_sha256"],
        "native_positions":report["native_position_count"],
        "native_equity_usd":report["native_equity_usd"],
        "native_balance_usd":report["native_balance_usd"],
        "hmac_verified":True,
        "native_peer_reconciled":True,
        "not_proof_of_exact_intraminute_peak":True,
        "not_proof_of_full_account_lifetime":True,
        "orders_submitted":0,
        "trading_authorized":False,
    }
    original=target/(sha+".original-native.json")
    meta=target/(sha+"."+report["independent_reconciliation_sha256"]+".verified-metadata.json")
    original_created=create_once(original,raw)
    metadata_created=create_once(meta,(json.dumps(attestation,sort_keys=True,separators=(",",":"))+"\n").encode())
    print("SIGNED_NATIVE_ARCHIVE",("STORED" if original_created or metadata_created else "EXISTS"),
          "ACCOUNT","6207","POSITIONS",report["native_position_count"],"ORDERS",0)
    return True


if __name__=="__main__":
    archive()
