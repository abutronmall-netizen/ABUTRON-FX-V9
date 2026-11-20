#!/usr/bin/env python3
"""ABUTRON V9: daily historical paper feedback -> isolated candidate only."""
import collections, csv, datetime as dt, fcntl, gzip, hashlib, io, json, os
from pathlib import Path
import subprocess, sys
from zoneinfo import ZoneInfo

ROOT=Path("/var/lib/abutron-v9-papertrain")
RELEASE=Path("/opt/abutron-v9-brain/releases/a88f373001ee1b8fcc934a1e3d068ed5b90f947b")
BASE=RELEASE/"data/brains/unified_m5/nexus_brain.json"
DATA=ROOT/"market-data"
RUNNER=Path("/usr/local/lib/abutron-v9-papertrain/candidate_train_from_paper.py")
PYTHON="/opt/abutron-v9-brain/.venv-a88f373/bin/python"
PIN="2667997b1498aae316c4103ec1f3d0bd2efb805f034836455ed10019284302df"

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def atomic_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name("."+path.name+".tmp")
    with temporary.open("w") as f:
        json.dump(data,f,sort_keys=True,indent=2,allow_nan=False)
        f.write("\n");f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)

def extract(broker,symbol,target):
    parts=sorted((DATA/broker).glob("candles.csv.gz.part-*"))
    if not parts or sum(f.stat().st_size for f in parts)>300_000_000:
        raise ValueError("MISSING_OR_OVERSIZED_MARKET_DATA:"+broker)
    raw=b"".join(f.read_bytes() for f in parts)
    reader=csv.DictReader(io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(raw)),encoding="utf-8"))
    m5,h1=collections.deque(maxlen=650),collections.deque(maxlen=160)
    for row in reader:
        name=row.get("broker_symbol") if broker=="FP-Markets" else row.get("symbol")
        if name!=symbol:continue
        if row.get("timeframe")=="M5":m5.append(row)
        if row.get("timeframe")=="H1":h1.append(row)
    if len(m5)<200 or len(h1)<20:
        raise ValueError("INSUFFICIENT_PAPER_HISTORY:"+symbol)
    target.mkdir(parents=True,exist_ok=True)
    with gzip.open(target/"candles.csv.gz","wt",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerows(sorted([*m5,*h1],key=lambda r:(r["symbol"],r["timeframe"],int(r["start_time_utc"]))))
    return {"M5":len(m5),"H1":len(h1)}

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/"run.lock").open("w") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if sha(BASE)!=PIN:raise ValueError("PINNED_PRODUCTION_MODEL_CHANGED")
        base_doc=json.loads(BASE.read_text())
        ids=base_doc["symbol_ids"]
        if len(ids)!=26 or len(set(ids))!=26 or base_doc["n_features"]!=49:
            raise ValueError("UNEXPECTED_BASELINE_FEATURE_SCHEMA")
        now=dt.datetime.now(ZoneInfo("Africa/Johannesburg"))
        pair_i=now.date().toordinal()%13
        # Explicit one-time supervised invocation. The daily timer remains
        # unchanged and the training output remains isolated.
        if len(sys.argv)==3 and sys.argv[1]=="--pair-index":
            pair_i=int(sys.argv[2])
            if not 0<=pair_i<13:raise ValueError("MANUAL_PAIR_INDEX_OUT_OF_RANGE")
        elif len(sys.argv)!=1:
            raise ValueError("UNEXPECTED_TRAINING_ARGS")
        symbols=[ids[pair_i],ids[13+pair_i]]
        subprocess.run(["git","-C",str(DATA),"fetch","--depth=1","origin","main"],check=True,timeout=150,stdout=subprocess.DEVNULL)
        subprocess.run(["git","-C",str(DATA),"reset","--hard","FETCH_HEAD"],check=True,timeout=50,stdout=subprocess.DEVNULL)
        commit=subprocess.check_output(["git","-C",str(DATA),"rev-parse","HEAD"],text=True).strip()
        donefile=ROOT/"completed-pairs.json"
        done=json.loads(donefile.read_text()) if donefile.exists() else {}
        key="|".join(symbols)+"|"+commit
        if key in done:
            print("NO_NEW_PAPER_DATA",symbols,"commit",commit[:12],flush=True)
            return 0
        stamp=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        work=ROOT/"runs"/stamp/"data"
        coverage={symbols[0]:extract("FP-Markets",symbols[0],work/"FP-Markets"),
                  symbols[1]:extract("Deriv",symbols[1],work/"Deriv")}
        latest=ROOT/"latest-candidate.json"
        incoming=BASE
        if latest.is_file():
            prev=json.loads(latest.read_text())
            candidate=Path(prev.get("candidate_path",""))
            if (candidate.is_file() and not candidate.is_symlink()
                    and candidate.name=="nexus_brain.json"
                    and candidate.parent.parent==ROOT/"candidates"
                    and sha(candidate)==prev.get("candidate_sha256")):
                incoming=candidate
        before=json.loads(incoming.read_text())
        output=ROOT/"candidates"/stamp
        env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8",
             "PYTHONPATH":str(RELEASE/"src"),"PYTHONDONTWRITEBYTECODE":"1",
             "ABUTRON_DATA_ROOT":str(ROOT/"runtime"),"ABUTRON_ENV":"demo",
             "ABUTRON_LIVE_TRADING_ENABLED":"false",
             "ABUTRON_PER_TRADE_PCT":"0.001","ABUTRON_RISK_CAP_PCT":"0.20",
             "ABUTRON_GEMMA_ENABLED":"false"}
        args=[PYTHON,"-B","-u",str(RUNNER),"--data",str(work),
              "--symbols",*symbols,"--mode","both","--epochs","2",
              "--risk-pct","0.001","--in",str(incoming.parent),"--out",str(output)]
        print("PAPER_CANDIDATE_TRAIN_START",symbols,"data",commit[:12],flush=True)
        completed=subprocess.run(args,env=env,timeout=1050,check=False)
        if completed.returncode:
            raise RuntimeError("PAPER_TRAIN_EXIT_"+str(completed.returncode))
        model=output/"nexus_brain.json"
        if not model.is_file():
            print("NO_PAPER_OUTCOME_CANDIDATE",symbols,flush=True)
            return 0
        doc=json.loads(model.read_text())
        json.dumps(doc,allow_nan=False)
        if (doc.get("n_features")!=49 or doc.get("symbol_ids")!=ids
                or len(doc.get("members",[]))!=3
                or doc.get("online",{}).get("updates",0)<=before["online"]["updates"]):
            raise ValueError("CANDIDATE_MODEL_VALIDATION_FAILED")
        result={"status":"CANDIDATE_AWAITING_OUT_OF_SAMPLE_VALIDATION",
                "generated_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
                "market_data_commit":commit,
                "training_symbols":symbols,"input_rows":coverage,
                "source_model":str(incoming),"candidate_path":str(model),
                "candidate_sha256":sha(model),
                "feature_count":49,"symbols":26,
                "online_updates":doc["online"]["updates"],
                "promotion_allowed":False,"broker_orders":0,
                "portfolio_cap_fraction":0.20,"demo_per_trade_risk_fraction":0.001}
        atomic_json(output/"candidate-manifest.json",result)
        atomic_json(latest,result)
        done[key]={"model_sha256":sha(model),"completed_utc":result["generated_utc"]}
        atomic_json(donefile,done)
        print("PAPER_CANDIDATE_SAVED",sha(model)[:16],"PROMOTION_DISABLED",flush=True)
        return 0

if __name__=="__main__":
    try:sys.exit(main())
    except Exception as e:
        print("PAPER_CANDIDATE_FAILED",type(e).__name__,str(e)[:180],flush=True)
        sys.exit(2)
