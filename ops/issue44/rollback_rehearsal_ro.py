"""Issue44 offline rollback rehearsal. NEVER sends orders or restarts MT5.

This source tests only byte-perfect recovery of staged files in disposable
temporary directories. Passing does not prove that broker positions survive
a real restart, and does not grant trading authority.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

class RehearsalError(ValueError):
    pass

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def read_exact(path,digest):
    if not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):
        raise RehearsalError("BAD_SHA_PIN")
    st=path.lstat()
    if not stat.S_ISREG(st.st_mode) or path.is_symlink() or st.st_nlink!=1 or not 0<st.st_size<5_000_000:
        raise RehearsalError("UNTRUSTED_SOURCE")
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        old=os.fstat(fd)
        raw=os.read(fd,5_000_001)
        new=os.fstat(fd)
        if (old.st_dev,old.st_ino,old.st_size,old.st_mtime_ns)!=(
            new.st_dev,new.st_ino,new.st_size,new.st_mtime_ns
        ) or len(raw)!=old.st_size or sha(raw)!=digest:
            raise RehearsalError("SOURCE_CHANGED_OR_SHA_INVALID")
        return raw
    finally:
        os.close(fd)

def write_once(p,raw):
    fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())

def replace_temp(p,raw):
    tmp=p.parent/(".replace-"+p.name+"-"+str(os.getpid()))
    write_once(tmp,raw)
    os.replace(tmp,p)
    if sha(p.read_bytes())!=sha(raw):
        raise RehearsalError("SIMULATED_WRITE_SHA_INVALID")

def simulate(artifacts):
    if (not isinstance(artifacts,dict) or not 1<=len(artifacts)<=16 or
        any(not isinstance(k,str) or not k.isidentifier() or
            not isinstance(v,tuple) or len(v)!=2 or
            any(not isinstance(x,bytes) or not 0<len(x)<=5_000_000 for x in v)
            for k,v in artifacts.items())):
        raise RehearsalError("INVALID_SIMULATION_ARTIFACTS")
    keys=list(artifacts)
    for fault in list(range(1,len(keys)+1))+[None]:
        with tempfile.TemporaryDirectory(prefix="issue44-fail-restore-") as folder:
            root=Path(folder)
            before=root/"before"
            after=root/"after"
            before.mkdir()
            after.mkdir()
            for key,(original,candidate) in artifacts.items():
                write_once(before/key,original)
                write_once(after/key,original)
            original_hashes={k:sha(v[0]) for k,v in artifacts.items()}
            try:
                for index,key in enumerate(keys,1):
                    replace_temp(after/key,artifacts[key][1])
                    if fault==index:
                        raise RuntimeError("SIMULATED_CRASH")
                if any(sha((after/k).read_bytes())!=sha(artifacts[k][1]) for k in keys):
                    raise RehearsalError("CANDIDATE_SHA_FAILED")
            except RuntimeError as exc:
                if str(exc)!="SIMULATED_CRASH":raise
            for key in keys:
                original=(before/key).read_bytes()
                if sha(original)!=original_hashes[key]:
                    raise RehearsalError("IMMUTABLE_BACKUP_DRIFT")
                replace_temp(after/key,original)
            if any(sha((after/k).read_bytes())!=original_hashes[k] for k in keys):
                raise RehearsalError("ROLLBACK_MISMATCH")
    return {
        "schema":"abutron.issue44.offline_rollback_rehearsal.v1",
        "status":"ISOLATED_BYTE_RECOVERY_PASSED",
        "files":len(keys),
        "fault_boundaries_tested":len(keys),
        "successful_promotion_then_rollback_tested":True,
        "original_sha256":{k:sha(v[0]) for k,v in artifacts.items()},
        "candidate_sha256":{k:sha(v[1]) for k,v in artifacts.items()},
        "production_worker_restarted":False,
        "broker_position_preservation_certified":False,
        "broker_sltp_amendment_certified":False,
        "live_order_authority_changed":False,
    }

def run_manifest(manifest):
    if not isinstance(manifest,dict) or manifest.get("schema")!="abutron.issue44.rollback_sources.v1":
        raise RehearsalError("INVALID_PRIVATE_MANIFEST")
    records=manifest.get("artifacts")
    if not isinstance(records,list) or not 1<=len(records)<=16:
        raise RehearsalError("NO_ACCOUNT_RELEASE_ARTIFACTS")
    # Validate ALL names/schema before opening any external source path.
    # A duplicate entry must not cause an unintended read of its first item.
    seen=set()
    for rec in records:
        if not isinstance(rec,dict) or set(rec)!={"name","old_path","old_sha","new_path","new_sha"}:
            raise RehearsalError("BAD_ARTIFACT_SCHEMA")
        name=rec["name"]
        if not isinstance(name,str) or not name.isidentifier() or name in seen:
            raise RehearsalError("DUPLICATE_OR_BAD_ARTIFACT")
        seen.add(name)
    items={}
    for rec in records:
        name=rec["name"]
        items[name]=(read_exact(Path(rec["old_path"]),rec["old_sha"]),
                     read_exact(Path(rec["new_path"]),rec["new_sha"]))
    result=simulate(items)
    result["verified_at_utc"]=dt.datetime.now(dt.timezone.utc).isoformat()
    return result

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--manifest",required=True)
    p.add_argument("--out",required=True)
    a=p.parse_args()
    source=Path(a.manifest)
    target=Path(a.out)
    if (os.geteuid()!=0 or source.is_symlink() or not source.is_file() or
        source.stat().st_uid!=0 or target.parent.is_symlink() or
        target.parent.stat().st_uid!=0 or target.parent.stat().st_mode&0o077):
        raise RehearsalError("ROOT_PRIVATE_FILES_REQUIRED")
    payload=json.loads(read_exact(source,sha(source.read_bytes())))
    result=run_manifest(payload)
    fd,tmp=tempfile.mkstemp(prefix=".issue44-rehearsal-",dir=target.parent)
    try:
        with os.fdopen(fd,"w") as f:
            os.fchmod(f.fileno(),0o600)
            json.dump(result,f,indent=2,sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,target)
    finally:
        if os.path.lexists(tmp):os.unlink(tmp)
    print(result["status"],result["files"],result["fault_boundaries_tested"],
          "NO_PRODUCTION_BROKER_RESTART")
if __name__=="__main__":
    main()
