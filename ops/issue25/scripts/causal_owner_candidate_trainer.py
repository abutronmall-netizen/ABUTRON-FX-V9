#!/usr/bin/python3
"""ABUTRON V9: four owner-isolated causal broker-history candidate brains.
Reads authenticated eight-account append-only source ledger and SHA-pinned M5.
Candidate learning only: no production model promotion, CRM or broker orders.
"""
from __future__ import annotations
import bisect,csv,datetime as dt,fcntl,gzip,hashlib,io,json,os,stat,subprocess,sys,tempfile
from collections import Counter,defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT=Path("/var/lib/abutron-v9-eight-account-learning")
RELEASE=Path("/opt/abutron-v9-brain/releases/a88f373001ee1b8fcc934a1e3d068ed5b90f947b")
BASE=RELEASE/"data/brains/unified_m5/nexus_brain.json"
BASESHA="2667997b1498aae316c4103ec1f3d0bd2efb805f034836455ed10019284302df"
PYTHON="/opt/abutron-v9-brain/.venv-a88f373/bin/python"
TRAINER="/usr/local/lib/abutron-v9-realtrade-feedback/train_from_trades_causal.py"
CAND=ROOT/"candidates"
MARKET=ROOT/"market"
HEADER={"Deriv":["symbol","timeframe","start_time_utc","open","high","low","close"],
        "FP-Markets":["symbol","broker_symbol","timeframe","start_time_utc","open","high","low",
                      "close","broker_time_raw"]}
TRADEFIELDS=["broker","account","symbol","direction","entry_time","exit_time",
             "entry_price","exit_price","lots","pnl","entry_type","entry_quality"]
UTC=dt.timezone.utc
def chk(ok,code):
 if not ok:raise ValueError(code)
def sha(body):return hashlib.sha256(body).hexdigest()
def safe_json(p):
 chk(p.is_file() and not p.is_symlink() and p.stat().st_uid==0
     and not p.stat().st_mode&0o077,"PRIVATE_MANIFEST_UNTRUSTED")
 return json.loads(p.read_text())
def atomic(p,raw):
 fd,t=tempfile.mkstemp(prefix="."+p.name+"-",dir=p.parent)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,"wb") as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(t,p)
 finally:
  if os.path.lexists(t):os.unlink(t)
def csvbytes(data,fields):
 f=io.StringIO(newline="")
 w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore",lineterminator="\n")
 w.writeheader()
 for r in data:w.writerow(r)
 return f.getvalue().encode()

def sources():
 archive=safe_json(ROOT/"latest.json")
 chk(archive.get("schema")=="abutron.v9.eight_account.owner_isolated_native_learning_archive.ro.v1"
      and archive.get("active_accounts_in_crm")==8
      and archive.get("source_accounts_archived")==8
      and archive.get("customer_data_isolated_by_owner_hash") is True
      and archive.get("candidate_model_promoted") is False
      and archive.get("broker_orders_submitted")==0,"EIGHT_ACCOUNT_SOURCE_NOT_VERIFIED")
 chk(sha(BASE.read_bytes())==BASESHA,"PINNED_PRODUCTION_BASELINE_CHANGED")
 deriv=safe_json(MARKET/"latest.json")
 fp=safe_json(MARKET/"fp-native-four-account-m5-evidence.json")
 chk(deriv.get("schema")=="abutron.v9.manifest_verified_market_m5_candidate_input.v1"
     and deriv.get("source_broker_count")==2
     and fp.get("schema")=="abutron.v9.fp_fouraccount_authenticated_m5_candidate_source.v1"
     and sum(fp.get("overlap_mismatches",{}).values())==0
     and all(fp.get("overlapping_m5_checked",{}).get(a,0)>=60 for a in ('1622','2582','1636')),
     "NATIVE_M5_MARKET_PROVENANCE_NOT_VERIFIED")
 chk(sha((MARKET/"Deriv"/"candles.csv.gz").read_bytes())==
      deriv["brokers"]["Deriv"]["market_input_sha256"],"DERIV_M5_INPUT_SHA_CHANGED")
 chk(sha((MARKET/"FP-Markets"/"candles.csv.gz").read_bytes())==
      fp["combined_candidate_market_sha256"],"FP_M5_INPUT_SHA_CHANGED")
 return archive,fp

def source_numeric(value):
 try:
  v=Decimal(str(value))
 except (InvalidOperation,ValueError,TypeError):
  raise ValueError("BROKER_TRADE_NUMBER_NOT_DECIMAL") from None
 chk(v.is_finite(),"BROKER_TRADE_NUMBER_NONFINITE")
 return format(v.normalize(),"f")

def normalized_timestamp(value):
 t=dt.datetime.fromisoformat(str(value).replace("Z","+00:00"))
 chk(t.tzinfo is not None,"BROKER_TRADE_DATETIME_TZ_UNVERIFIED")
 return t.astimezone(UTC).isoformat(timespec="microseconds")

def stable_trade_key(row):
 """CRM reconnect cannot re-key a native MT5 position. Includes native ticket."""
 pos=str(row.get("position_id",""))
 broker=str(row.get("broker",""))
 account=str(row.get("account",""))
 side=str(row.get("direction",""))
 symbol=str(row.get("symbol",""))
 chk(broker in ("Deriv","FP Markets") and len(account)==4 and account.isdigit()
     and pos.isdigit() and int(pos)>0 and side in ("BUY","SELL")
     and bool(symbol),"INVALID_STABLE_NATIVE_BROKER_POSITION_ID")
 return sha(json.dumps([
   "MT5_NATIVE_POSITION_STABLE_V2",broker,account,pos,symbol,side,
   normalized_timestamp(row["entry_time"]),
   source_numeric(row["entry_price"]),source_numeric(row["lots"])],
   separators=(",",":"),ensure_ascii=False).encode())

def economic_fingerprint(row):
 """Join old pre-patch runs with original broker source without guessing ticket."""
 return sha(json.dumps([
   str(row.get("broker","")),str(row.get("account","")),
   str(row.get("symbol","")),str(row.get("direction","")),
   normalized_timestamp(row["entry_time"]),source_numeric(row["entry_price"]),
   source_numeric(row["lots"]),normalized_timestamp(row["exit_time"]),
   source_numeric(row["exit_price"]),source_numeric(row["pnl"]),
   str(row.get("entry_type","")),str(row.get("entry_quality",""))],
   separators=(",",":"),ensure_ascii=False).encode())

def owner_native_lookup(alltrades):
 out={}
 for row in alltrades:
  chk(stable_trade_key(row)==row.get("trade_key"),
      "NATIVE_BROKER_STABLE_KEY_MISMATCH")
  full=economic_fingerprint(row)
  chk(full not in out,"HISTORICAL_TRAINING_ROW_MATCH_AMBIGUOUS")
  out[full]=row
 return out

def trained_history_from_runs(ownerbase,alltrades):
 """Only original broker-matched run CSVs can authorize pre-patch dedup."""
 native=owner_native_lookup(alltrades)
 learned={}
 checkpoints=[]
 runs=sorted((ownerbase/"runs").glob("*/candidate-manifest.json"))
 for p in runs:
  m=safe_json(p)
  chk(m.get("schema")=="abutron.v9.owner_isolated_broker_causal_candidate.v1"
      and m.get("owner_hash")==ownerbase.name
      and m.get("pinned_baseline_sha256")==BASESHA
      and m.get("model_promotion_allowed") is False
      and m.get("broker_orders_sent")==0,
      "OWNER_HISTORIC_CANDIDATE_RUN_UNTRUSTED")
  checkpoint=Path(m["candidate_model_path"])
  chk(checkpoint.is_file() and not checkpoint.is_symlink()
      and checkpoint.parent.parent.parent.parent==ownerbase
      and checkpoint.parent.parent==p.parent
      and sha(checkpoint.read_bytes())==m["candidate_model_sha256"],
      "HISTORIC_BRAIN_CHECKPOINT_SHA_OR_OWNER_UNVERIFIED")
  csvfile=p.parent/"trades.csv"
  chk(csvfile.is_file() and not csvfile.is_symlink()
      and sha(csvfile.read_bytes())==m["source_private_training_csv_sha256"],
      "HISTORIC_TRAINER_RUN_SOURCE_SHA_CHANGED")
  with csvfile.open(newline="") as f:
   oldrows=list(csv.DictReader(f))
  chk(len(oldrows)==m["newly_trained_events"],
      "HISTORIC_RUN_TRADE_ROW_COUNT_CHANGED")
  for row in oldrows:
   full=economic_fingerprint(row)
   chk(full in native,
       "HISTORIC_TRAINED_TRADE_MISSING_FROM_IMMUTABLE_BROKER_ARCHIVE")
   matched=native[full]
   key=stable_trade_key(matched)
   chk(key not in learned or learned[key]==full,
       "DUPLICATE_STABLE_KEY_WITH_CONFLICTING_NATIVE_EXIT_OR_PNL")
   learned[key]=full
  chk(len(m.get("trained_event_ids",[]))==sum(
       len(list(csv.DictReader(z.open()))) for z in
       sorted((ownerbase/"runs").glob("*/trades.csv")) if z.parent.name<=p.parent.name),
       "HISTORICAL_RUN_CUMULATIVE_IDS_NOT_MONOTONIC")
  checkpoints.append(m)
 chk(len(runs)==len(list((ownerbase/"runs").glob("*/trades.csv"))),
      "MISSING_RUN_MANIFEST_OR_TRAIN_CSV")
 return learned,checkpoints

def recover_owner_state(ownerbase,checkpoints):
 """Latest missing: recover greatest verified cumulative checkpoint, never reset."""
 if not checkpoints:return None
 ordered=sorted(checkpoints,key=lambda m:(len(m["trained_event_ids"]),m["created_utc"]))
 candidate=ordered[-1]
 chk(len(candidate.get("trained_event_ids",[]))>=len(ordered[0].get("trained_event_ids",[])),
     "NO_VERIFIED_MAXIMUM_TRAINED_CHECKPOINT")
 return dict(candidate)

def events(archive):
 owners=defaultdict(list)
 by_account={}
 for suffix,account in archive["accounts"].items():
  chk(account["status"]=="NATIVE_DEALS_ARCHIVED"
       and suffix in ('0512','1622','1636','2582','6207','7593','9244','9392')
       and account["broker"] in ('Deriv','FP Markets')
       and account["environment"]==("LIVE" if suffix=="9392" else "DEMO")
       and account["no_independent_inception_or_equity_peak_claim"] is True
       and account["model_promotion_allowed"] is False,"ACCOUNT_ARCHIVE_NOT_VERIFIED")
  owner=account["owner_hash"]
  chk(isinstance(owner,str) and len(owner)==64 and all(c in "0123456789abcdef" for c in owner),
      "INVALID_ACCOUNT_OWNER_PARTITION")
  path=ROOT/"owners"/owner/suffix/"closed_trades_verified.csv"
  body=path.read_bytes()
  chk(sha(body)==account["closed_trades_csv_sha256"],"CLOSED_TRADE_SOURCE_DIGEST_MISMATCH")
  rows=list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
  chk(len(rows)==account["closed_positions_causally_paired"],"CLOSED_TRADE_COUNT_CHANGED")
  for r in rows:
   chk(r.get("account")==suffix and r.get("broker")==account["broker"] and
       len(r.get("event_id",""))==40 and
       r.get("direction") in ("BUY","SELL"),"CLOSED_TRADE_OWNER_ACCOUNT_UNVERIFIED")
   chk(r.get("trade_key")==stable_trade_key(r),
       "SOURCE_TRADE_KEY_CHANGED_OR_CRM_REKEYED")
   r["_owner"]=owner
   owners[owner].append(r)
  by_account[suffix]=dict(owner=owner,eligible=0,deferred=0,archived=len(rows))
 chk(len(owners)==4 and len(archive["accounts"])==8,"OWNER_LEARNING_PARTITIONS_CHANGED")
 chk(sum(len(x) for x in owners.values())==
      archive["total_causally_closed_positions_in_private_archive"],"CLOSED_TRADE_CUMULATIVE_COUNT_CONFLICT")
 return owners,by_account

def market_index():
 idx={}
 for broker in ("Deriv","FP-Markets"):
  p=MARKET/broker/"candles.csv.gz"
  groups=defaultdict(list)
  with gzip.open(p,"rt",encoding="utf-8",newline="") as f:
   cr=csv.DictReader(f);chk(cr.fieldnames==HEADER[broker],"MARKET_SOURCE_SCHEMA_CHANGED")
   for r in cr:
    chk(r["timeframe"]=="M5","NON_M5_INPUT_IN_CANDIDATE_MARKET")
    name=r["broker_symbol"] if broker=="FP-Markets" else r["symbol"]
    groups[name].append((int(r["start_time_utc"]),r))
  label="FP Markets" if broker=="FP-Markets" else broker
  for sym,vals in groups.items():
   vals.sort(key=lambda z:z[0])
   stamps=[x[0] for x in vals]
   chk(len(stamps)==len(set(stamps)),"DUPLICATE_BROKER_M5_TIMESTAMP")
   idx[label,sym]=(stamps,vals)
 return idx

def eligible(owners,idx):
 out={};account_counts=Counter();missing=Counter()
 for owner,rows in owners.items():
  accepted=[]
  for trade in rows:
   suffix=trade["account"]
   if suffix=="9392":
    missing[(suffix,"LIVE_9392_HAS_NO_CLOSED_NATIVE_TRADES")]+=1;continue
   key=(trade["broker"],trade["symbol"])
   market=idx.get(key)
   if market is None:
    missing[(suffix,"BROKER_NATIVE_M5_SYMBOL_ABSENT")]+=1;continue
   stamps,_=market
   stamp=int(dt.datetime.fromisoformat(trade["entry_time"]).timestamp())
   i=bisect.bisect_right(stamps,stamp-300)-1
   if i<219:
    missing[(suffix,"INSUFFICIENT_220_CLOSED_M5_WARMUP")]+=1;continue
   if stamp-(stamps[i]+300)>900:
    # A bar that opened by entry time but had not CLOSED at entry is
    # future information; distinguish from recoverable source gaps.
    nextbar=stamps[i+1] if i+1<len(stamps) else None
    if nextbar is not None and nextbar<=stamp<nextbar+300:
     missing[(suffix,"ENTRY_INSIDE_UNCLOSED_M5_BAR")]+=1
    else:
     missing[(suffix,"BROKER_CLOSED_M5_PREENTRY_NOT_FRESH")]+=1
    continue
   # Original V9 causal feature contract requires >=30 prior COMPLETED M5
   # observations, not 30 uninterrupted 5-minute slots. Broker sessions
   # can close; missing minutes are NOT invented or zero-filled. Preserve
   # the source gaps for later research and require a fresh last real bar.
   previous_gaps=[stamps[z]-stamps[z-1] for z in range(i-29,i+1)
                  if stamps[z]-stamps[z-1]!=300]
   trade['_preentry_native_m5_time_gap_count']=len(previous_gaps)
   trade['_max_native_preentry_bar_gap_seconds']=max(previous_gaps,default=0)
   accepted.append(trade);account_counts[suffix]+=1
  out[owner]=accepted
 return out,account_counts,missing

def runtime_market(eligible_rows,idx,dest):
 grouped=defaultdict(set)
 for t in eligible_rows:
  key=(t["broker"],t["symbol"])
  stamps,_=idx[key]
  stamp=int(dt.datetime.fromisoformat(t["entry_time"]).timestamp())
  i=bisect.bisect_right(stamps,stamp-300)-1
  for k in range(i-219,i+1):grouped[key].add(k)
 for brok,label in (("Deriv","Deriv"),("FP-Markets","FP Markets")):
  out=dest/brok
  out.mkdir(mode=0o700,parents=True)
  path=out/"candles.csv.gz"
  with gzip.open(path,"wt",encoding="utf-8",newline="") as f:
   w=csv.DictWriter(f,fieldnames=HEADER[brok],lineterminator="\n")
   w.writeheader()
   for (provider,symbol),indices in sorted(grouped.items()):
    if provider!=label:continue
    _,items=idx[provider,symbol]
    for j in sorted(indices):w.writerow(items[j][1])
  os.chmod(path,0o600)

def run_owner(owner,alltrades,aligned,fail_missing):
 ownerbase=CAND/owner
 ownerbase.mkdir(mode=0o700,parents=True,exist_ok=True)
 state=ownerbase/"latest.json"
 learned,checkpoints=trained_history_from_runs(ownerbase,alltrades)
 latest_missing=not state.exists()
 old=safe_json(state) if not latest_missing else recover_owner_state(ownerbase,checkpoints)
 prev=set()
 base=BASE.parent
 if old is not None:
  chk(old.get("schema")=="abutron.v9.owner_isolated_broker_causal_candidate.v1"
      and old.get("owner_hash")==owner
      and old.get("pinned_baseline_sha256")==BASESHA
      and old.get("model_promotion_allowed") is False
      and old.get("broker_orders_sent")==0,
      "PREVIOUS_CUSTOMER_BRAIN_CHAIN_UNTRUSTED")
  prev=set(old["trained_event_ids"])
  model=Path(old["candidate_model_path"])
  chk(model.is_file() and not model.is_symlink()
      and model.parent.parent.parent.parent==ownerbase
      and sha(model.read_bytes())==old["candidate_model_sha256"],
      "PREVIOUS_OWNER_CANDIDATE_CHECKPOINT_CHANGED")
  chk(len(prev)==sum(m["newly_trained_events"] for m in checkpoints),
      "HISTORIC_MODEL_EVENT_COUNT_DIFFERS_FROM_ORIGINAL_TRAINING_RUNS")
  if "trained_trade_keys" in old:
   chk(set(old["trained_trade_keys"])==set(learned),
       "DURABLE_STABLE_TRADE_KEYS_DISAGREE_WITH_PROVEN_BROKER_HISTORY")
  if "trained_trade_fingerprints" in old:
   chk(old["trained_trade_fingerprints"]==learned,
       "DURABLE_ECONOMIC_TRADE_PROVENANCE_CONFLICT")
  base=model.parent
 else:
  chk(not checkpoints and not learned,
      "MISSING_OWNER_STATE_WITH_EXISTING_TRAINING_HISTORY")
 # Reconnect changes CRM cid and event_id; never re-learn a broker
 # position that is already present in authenticated run history.
 fresh=[]
 fresh_keys={}
 for row in aligned:
  key=stable_trade_key(row)
  evidence=economic_fingerprint(row)
  chk(key not in fresh_keys or fresh_keys[key]==evidence,
      "NATIVE_STABLE_KEY_COLLISION_IN_CURRENT_DATA")
  fresh_keys[key]=evidence
  if key in learned:
   chk(learned[key]==evidence,"ALREADY_LEARNED_BROKER_POSITION_ECONOMICS_CHANGED")
  else:
   chk(row["event_id"] not in prev,
       "HISTORIC_EVENT_ID_WITHOUT_STABLE_TRAINED_TRADE_PROOF")
   fresh.append(row)
 if not fresh:
  if old is not None:
   old["trained_trade_keys"]=sorted(learned)
   old["trained_trade_fingerprints"]=dict(sorted(learned.items()))
   old["stable_trade_key_schema"]="MT5_NATIVE_POSITION_STABLE_V2"
   old["recovered_after_missing_latest"]=latest_missing
   atomic(state,(json.dumps(old,sort_keys=True,indent=2)+"\n").encode())
  return dict(owner_hash=owner,status="NO_NEW_CAUSALLY_ALIGNED_NATIVE_TRADE_EVENTS",
   previously_trained=len(prev),stable_native_trade_keys=len(learned),
   recovered_after_missing_latest=latest_missing,
   archived_closed_events=len(alltrades),
   aligned_closed_events=len(aligned),newly_trained=0,
   model_promotion_allowed=False,live_trading_modified=False)
 runid=dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
 runpath=ownerbase/"runs"/runid
 chk(not runpath.exists(),"CANDIDATE_RUN_ID_ALREADY_EXISTS")
 runpath.mkdir(mode=0o700,parents=True)
 path=runpath/"trades.csv"
 # This source is ALREADY paired one-row per settled position; intentionally
 # omit position_id so the trainer never misinterprets it as raw MT5 DEALS.
 body=csvbytes(fresh,TRADEFIELDS)
 atomic(path,body)
 runtime_market(fresh,idx=MARKET_INDEX,dest=runpath/"market")
 target=runpath/"model";target.mkdir(mode=0o700)
 env=dict(PATH="/usr/bin:/bin",LANG="C.UTF-8",PYTHONPATH=str(RELEASE/"src"),
   PYTHONDONTWRITEBYTECODE="1",ABUTRON_ENV="demo",
   ABUTRON_GEMMA_ENABLED="false",ABUTRON_LIVE_TRADING_ENABLED="false",
   ABUTRON_DATA_ROOT=str(runpath/"runtime"))
 cmd=[PYTHON,"-B",TRAINER,"--trades",str(path),"--data",str(runpath/"market"),
      "--mode","online","--lr","0.0005","--ticks","","--depth","",
      "--in",str(base),"--out",str(target)]
 proc=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=360)
 # logs are private but do not let untrusted subprocess output leak public.
 atomic(runpath/"trainer.log",(proc.stdout[-12000:]+"\n"+proc.stderr[-5000:]).encode())
 chk(proc.returncode==0,"CAUSAL_OWNER_CANDIDATE_TRAINER_FAILURE")
 report=json.loads((target/"trade_report_online.json").read_text())
 chk(report.get("mode")=="online" and
     report.get("trained")==len(fresh) and
     report.get("skipped")==0 and
     report.get("trades_loaded")==len(fresh),
     "CANDIDATE_MODEL_DID_NOT_LEARN_ALL_PASSED_EVENTS")
 model=target/"nexus_brain.json"
 weights=json.loads(model.read_text())
 chk(weights.get("n_features")==49 and len(weights.get("members",[]))==3,
     "CANDIDATE_BRAIN_SCHEMA_NOT_49FEATURES")
 current=set(r["event_id"] for r in fresh)
 chk(len(current)==len(fresh) and not (current&prev),"DUPLICATED_LEARNING_EVENT_IDS")
 for row in fresh:
  key=stable_trade_key(row)
  chk(key not in learned,"STABLE_KEY_NEW_RUN_ALREADY_LEARNED")
  learned[key]=economic_fingerprint(row)
 manifest=dict(
   schema="abutron.v9.owner_isolated_broker_causal_candidate.v1",
   owner_hash=owner,created_utc=dt.datetime.now(UTC).isoformat(),
   pinned_baseline_sha256=BASESHA,
   candidate_model_path=str(model),candidate_model_sha256=sha(model.read_bytes()),
   trained_event_ids=sorted(prev|current),previous_trained=len(prev),
   trained_trade_keys=sorted(learned),
   trained_trade_fingerprints=dict(sorted(learned.items())),
   stable_trade_key_schema="MT5_NATIVE_POSITION_STABLE_V2",
   recovered_after_missing_latest=latest_missing,
   newly_trained_events=len(fresh),archived_closed_events=len(alltrades),
   market_causality_30_actual_completed_preentry_M5_with_220_real_warmup=True,
   original_broker_session_gaps_preserved_without_synthetic_prices=True,
   recent_noncontiguous_broker_bar_windows_trained=sum(
       int(r.get('_preentry_native_m5_time_gap_count',0)>0) for r in fresh),
   source_private_training_csv_sha256=sha(body),
   loss_guard_patterns=len(weights.get("loss_guard",{}).get("patterns",[])),
   online_updates=weights.get("online",{}).get("updates"),
   model_promotion_allowed=False,
   production_brain_modified=False,crm_modified=False,live_trading_modified=False,
   broker_orders_sent=0,status="OWNER_ISOLATED_CANDIDATE_TRAINED_NOT_PROMOTED")
 atomic(runpath/"candidate-manifest.json",(json.dumps(manifest,sort_keys=True,indent=2)+"\n").encode())
 atomic(state,(json.dumps(manifest,sort_keys=True,indent=2)+"\n").encode())
 return dict(owner_hash=owner,status=manifest["status"],newly_trained=len(fresh),
     cumulative_trained=len(prev|current),archived_closed_events=len(alltrades),
     aligned_closed_events=len(aligned),model_promotion_allowed=False,broker_orders_sent=0)

def main(validate=False):
 os.umask(0o077)
 archive,fp=sources()
 owners,by_account=events(archive)
 global MARKET_INDEX
 MARKET_INDEX=market_index()
 aligned,count,missing=eligible(owners,MARKET_INDEX)
 if validate:
  status={}
  for owner,alltrades in owners.items():
   directory=CAND/owner
   learned,checkpoints=trained_history_from_runs(directory,alltrades)
   candidate=safe_json(directory/"latest.json") if (directory/"latest.json").exists() else recover_owner_state(directory,checkpoints)
   if candidate is not None:
    chk(set(candidate["trained_event_ids"])==set(checkpoints[-1]["trained_event_ids"]),
        "OWNER_LATEST_EVENT_IDS_NOT_PROVEN_BY_ORIGINAL_TRAINING_RUN")
    if "trained_trade_keys" in candidate:
     chk(set(candidate["trained_trade_keys"])==set(learned),
         "OWNER_DURABLE_TRADE_KEY_STATE_NOT_PROVEN")
   missing_new=sum(stable_trade_key(z) not in learned for z in aligned[owner])
   status[owner[:12]]=dict(trained_event_ids=len(candidate["trained_event_ids"]) if candidate else 0,
       stable_trade_keys_backfilled=len(learned),would_newly_train=missing_new,
       latest_manifest_missing=not (directory/"latest.json").exists())
  permanently={s:sum(n for (ss,c),n in missing.items()
       if ss==s and c=="ENTRY_INSIDE_UNCLOSED_M5_BAR") for s in sorted(by_account)}
  recoverable={s:sum(n for (ss,c),n in missing.items()
       if ss==s and c!="ENTRY_INSIDE_UNCLOSED_M5_BAR") for s in sorted(by_account)}
  print(json.dumps(dict(owners=len(owners),account_causally_aligned=dict(count),
    account_still_missing_native_M5={s:{reason:num for (s2,reason),num in missing.items() if s2==s}
        for s in sorted(by_account)},permanently_unlearnable=permanently,
    source_gap_deferrals=recoverable,per_owner_durable_state=status,
    verified_market_symbols=len(MARKET_INDEX),
    total_current_causally_aligned=sum(map(len,aligned.values())),
    source_trades=sum(map(len,owners.values())),model_promotion_allowed=False),
    sort_keys=True))
  return 0
 CAND.mkdir(mode=0o700,parents=True,exist_ok=True)
 output={}
 for owner,allrows in sorted(owners.items()):
  try:output[owner[:12]]=run_owner(owner,allrows,aligned[owner],missing)
  except (OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired) as exc:
   reason=str(exc)
   output[owner[:12]]=dict(status="OWNER_CANDIDATE_TRAINING_FAILED",
    reason_code=(reason if len(reason)<100 and all(c.isupper() or c.isdigit() or c=="_" for c in reason) else type(exc).__name__),
    production_brain_modified=False,orders_sent=0)
 status=dict(schema="abutron.v9.all_owner_isolated_causal_candidate_training.v1",
   checked_at_utc=dt.datetime.now(UTC).isoformat(),owners=len(owners),
   source_accounts_archived=archive["source_accounts_archived"],
   source_closed_trades=archive["total_causally_closed_positions_in_private_archive"],
   causal_m5_aligned_counts_by_account=dict(count),
   deferred_missing_native_m5_counts_by_account={s:sum(z for (ss,r),z in missing.items() if ss==s)
     for s in sorted(by_account)},
   per_owner=output,
   production_model_promoted=False,live_execution_changed=False,
   broker_orders_sent=0)
 atomic(CAND/"latest.json",(json.dumps(status,sort_keys=True,indent=2)+"\n").encode())
 print(json.dumps(dict(owners=len(owners),per_owner={k:{kk:vv for kk,vv in val.items()
   if kk in ("status","newly_trained","cumulative_trained","archived_closed_events","reason_code")}
   for k,val in output.items()},causal_m5_aligned_counts_by_account=dict(count),
   production_model_promoted=False,orders_sent=0),sort_keys=True))
 return 2 if any(x["status"]=="OWNER_CANDIDATE_TRAINING_FAILED" for x in output.values()) else 0
if __name__=="__main__":
 if sys.argv[1:]==["--validate"]:sys.exit(main(True))
 elif sys.argv[1:]:sys.exit("USAGE: --validate or no arguments")
 else:
  chk(ROOT.is_dir() and stat.S_IMODE(ROOT.stat().st_mode)==0o700,
      "UNTRUSTED_PRIVATE_OWNER_ARCHIVE")
  lease=ROOT/"candidate-owner-trainer.lock"
  with open(lease,"a+") as lock:
   fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
   sys.exit(main(False))
