#!/usr/bin/python3
"""ABUTRON V9: prospective, owner-isolated, 1-bar M5 shadow forecast+score.
Predictions are persisted BEFORE the target M5 candle closes; the frozen
candidate and baseline probabilities are never recomputed after outcomes.
Broker-native validated DEMO feeds only. LIVE x9392 intentionally no proxy.
No orders, CRM changes, model promotion or training.
"""
from __future__ import annotations
import datetime as dt,fcntl,hashlib,importlib.util,json,math,os,sqlite3,stat,sys,time,tempfile
from importlib.machinery import SourceFileLoader
from collections import defaultdict
from pathlib import Path
import numpy as np
ROOT=Path("/var/lib/abutron-v9-eight-account-learning")
OUT=ROOT/"validation"/"prospective-m5"
RELEASE=Path("/opt/abutron-v9-brain/releases/a88f373001ee1b8fcc934a1e3d068ed5b90f947b")
BASE=RELEASE/"data/brains/unified_m5"
PIN="2667997b1498aae316c4103ec1f3d0bd2efb805f034836455ed10019284302df"
FPFILE=Path("/usr/local/sbin/abutron-v9-fp-demo-native-inference-ro")
FPFEED=Path("/var/lib/abutron-fpmarkets-feed/partial")
DERIV=Path("/var/lib/apex/operator-live/native-market.json")
UTC=dt.timezone.utc
def _load_production_modules():
    """Lazy-load production modules at runtime.

    Moved from import-time to runtime so the module can be imported
    for testing without the production abutron/apex packages being present.
    """
    sys.path.insert(0,str(RELEASE/"src"))
    sys.path.insert(0,"/opt/apex/runtime")
    from abutron.brain.unified import load_unified
    from abutron.brain.signals import build_signal_bundle,features_for_brain
    from apex_fast_scanner import validate_snapshot,utc_now
    return load_unified, build_signal_bundle, features_for_brain, validate_snapshot, utc_now
def need(ok,code):
 if not ok:raise ValueError(code)
def h(b):return hashlib.sha256(b).hexdigest()
def iso(t):return dt.datetime.fromtimestamp(t,UTC).isoformat()
def rootfile(p,age,cap=1000000):
 need(p.is_file() and not p.is_symlink(),"UNTRUSTED_NATIVE_SOURCE_PATH")
 st=p.stat()
 need(st.st_uid==0 and st.st_mode&0o022==0 and -10<=time.time()-st.st_mtime<=age
      and 10<st.st_size<=cap,"STALE_OR_UNTRUSTED_BROKER_NATIVE_SOURCE")
 data=p.read_bytes()
 need(len(data)==st.st_size and p.stat().st_mtime_ns==st.st_mtime_ns,
      "NATIVE_SOURCE_CHANGED_DURING_READ")
 return data
def json_private(p):
 need(p.is_file() and not p.is_symlink(),"PRIVATE_OWNER_MANIFEST_ABSENT")
 st=p.stat()
 need(st.st_uid==0 and not st.st_mode&0o077,"PRIVATE_OWNER_MANIFEST_UNTRUSTED")
 return json.loads(p.read_text())
def atomic(path,obj):
 fd,tmp=tempfile.mkstemp(prefix=".shadow-",dir=path.parent)
 try:
  with os.fdopen(fd,"w") as f:
   os.fchmod(f.fileno(),0o600)
   json.dump(obj,f,sort_keys=True,indent=2,allow_nan=False);f.write("\n")
   f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.lexists(tmp):os.unlink(tmp)
def get_fp_loader():
 spec=importlib.util.spec_from_loader("abutron_fp_native_shadow_source",SourceFileLoader("abutron_fp_native_shadow_source",str(FPFILE)))
 loader=importlib.util.module_from_spec(spec)
 sys.modules[spec.name]=loader
 spec.loader.exec_module(loader)
 return loader
def roster():
 # Same account-bound owner mapping used by existing V9 broker archive.
 spec=importlib.util.spec_from_loader("abutron_all_eight_account_roster",
              SourceFileLoader("abutron_all_eight_account_roster","/usr/local/sbin/abutron-v9-eight-account-learning-archive-ro"))
 mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod
 spec.loader.exec_module(mod)
 r=mod.db_roster()
 private=json_private(ROOT/"latest.json")
 need(private.get("source_accounts_archived")==8 and set(r)==set(private.get("accounts",{})),
      "ACTIVE_OWNER_BROKER_ROSTER_CHANGED")
 for suffix,info in r.items():
  need(info["ownerhash"]==private["accounts"][suffix]["owner_hash"],
       "ACCOUNT_OWNER_SCOPING_CHANGED")
 return r
def model(owner,now):
 meta=json_private(ROOT/"candidates"/owner/"latest.json")
 need(meta["owner_hash"]==owner and meta["pinned_baseline_sha256"]==PIN
      and meta["model_promotion_allowed"] is False
      and meta["production_brain_modified"] is False
      and meta["broker_orders_sent"]==0,
      "OWNER_CANDIDATE_CHECKPOINT_NOT_RESEARCH_ONLY")
 path=Path(meta["candidate_model_path"])
 expected=ROOT/"candidates"/owner
 need(path.name=="nexus_brain.json" and
      path.parent.parent.parent.parent==expected and path.is_file()
      and path.stat().st_uid==0 and path.stat().st_mode&0o077==0,
      "OWNER_CANDIDATE_PATH_OR_PRIVACY_INVALID")
 need(h(path.read_bytes())==meta["candidate_model_sha256"]
      and dt.datetime.fromisoformat(meta["created_utc"]).timestamp()<=now,
      "OWNER_CANDIDATE_MODEL_HASH_OR_TIME_INVALID")
 loaded=load_unified(str(path.parent),mc_samples=1)
 need(loaded is not None and len(loaded.ensemble.members)==3
      and len(loaded.symbol_ids)==26,"CANDIDATE_MODEL_STRUCTURE_INVALID")
 return loaded,meta["candidate_model_sha256"]
def probability(brain,features,symbol):
 feats=features_for_brain(brain,features,symbol=symbol)
 x=np.asarray([feats],dtype=float)
 need(x.shape==(1,49) and np.isfinite(x).all(),"CANDIDATE_FEATURES_NOT_49_OR_NONFINITE")
 total=0.;num=0.
 for member in brain.ensemble.members:
  p=member.predict(x)
  need(p.shape==(1,2) and np.isfinite(p).all() and
       abs(float(p.sum())-1)<1e-5,"CANDIDATE_PROBABILITY_NOT_NORMALIZED")
  total+=member.weight;num+=member.weight*float(p[0,1])
 need(total>0,"ENSEMBLE_WEIGHTS_INVALID")
 value=num/total
 need(math.isfinite(value) and 0<=value<=1,"M5_DIRECTION_PROBABILITY_INVALID")
 return value
def init_db(db):
 db.execute("PRAGMA synchronous=FULL")
 db.execute("PRAGMA journal_mode=DELETE")
 db.execute("CREATE TABLE IF NOT EXISTS predictions ("
  "account TEXT NOT NULL, symbol TEXT NOT NULL, base_bar_open INTEGER NOT NULL,"
  "observed_utc REAL NOT NULL, source_sha256 TEXT NOT NULL,"
  "baseline_sha256 TEXT NOT NULL, candidate_sha256 TEXT NOT NULL,"
  "base_close REAL NOT NULL, base_prob_up REAL NOT NULL, candidate_prob_up REAL NOT NULL,"
  "PRIMARY KEY(account,symbol,base_bar_open)) WITHOUT ROWID")
 db.execute("CREATE TABLE IF NOT EXISTS outcomes ("
  "account TEXT NOT NULL,symbol TEXT NOT NULL,base_bar_open INTEGER NOT NULL,"
  "target_bar_open INTEGER NOT NULL,label_up INTEGER NOT NULL,"
  "next_close REAL NOT NULL,recorded_utc REAL NOT NULL,source_sha256 TEXT NOT NULL,"
  "PRIMARY KEY(account,symbol,base_bar_open)) WITHOUT ROWID")
 db.execute("CREATE TABLE IF NOT EXISTS exclusions ("
  "account TEXT NOT NULL,symbol TEXT NOT NULL,base_bar_open INTEGER NOT NULL,"
  "reason TEXT NOT NULL,recorded_utc REAL NOT NULL,"
  "PRIMARY KEY(account,symbol,base_bar_open)) WITHOUT ROWID")
 # A frozen forecast is scored against the next completed candle (5m),
 # and independently against 30m/60m outcomes after their actual close.
 db.execute("CREATE TABLE IF NOT EXISTS horizon_outcomes ("
  "account TEXT NOT NULL,symbol TEXT NOT NULL,base_bar_open INTEGER NOT NULL,"
  "horizon_minutes INTEGER NOT NULL,target_bar_open INTEGER NOT NULL,"
  "label_up INTEGER NOT NULL,next_close REAL NOT NULL,recorded_utc REAL NOT NULL,"
  "source_sha256 TEXT NOT NULL,"
  "PRIMARY KEY(account,symbol,base_bar_open,horizon_minutes)) WITHOUT ROWID")
 db.execute("CREATE TABLE IF NOT EXISTS horizon_exclusions ("
  "account TEXT NOT NULL,symbol TEXT NOT NULL,base_bar_open INTEGER NOT NULL,"
  "horizon_minutes INTEGER NOT NULL,reason TEXT NOT NULL,recorded_utc REAL NOT NULL,"
  "PRIMARY KEY(account,symbol,base_bar_open,horizon_minutes)) WITHOUT ROWID")
def record(owner,acct,symbol,rawsource,rows,cand,candsha,base,now):
 dbdir=OUT/"owners"/owner
 dbdir.mkdir(parents=True,mode=0o700,exist_ok=True)
 os.chmod(dbdir,0o700)
 dbfile=dbdir/"shadow.sqlite3"
 last=int(dt.datetime.fromisoformat(rows[-1]["open_time"]).timestamp())
 need(last%300==0,"NATIVE_LAST_M5_NOT_ALIGNED")
 # All bars are source-verified completed candles at emission.
 need(last+300<=now+2,"UNFINISHED_M5_BAR_ENTERED_SHADOW")
 last_closed=last+300
 close=float(rows[-1]["close"])
 need(math.isfinite(close) and close>0,"INVALID_BROKER_CLOSED_PRICE")
 seen=len(rows)
 need(seen>=65,"BROKER_FEED_LESS_THAN_65_NATIVE_M5")
 # Snapshot must occur within 150s after last completed M5 bar; never
 # claim this is a pre-entry trade forecast or a complete 5-minute bar.
 is_fresh=0<=now-last_closed<=150
 target_closes_at=last_closed+300
 # Generate features solely from the already CLOSED M5 rows, no future.
 baseline_score=cand_score=None
 if is_fresh:
  normalized=build_signal_bundle(rows,symbol=symbol,use_gemma=False)
  market=normalized.at(-1)["market_features"]
  baseline_score=probability(base,market,symbol)
  cand_score=probability(cand,market,symbol)
 with sqlite3.connect(dbfile,timeout=20) as db:
  init_db(db)
  # Score only previously persisted forecasts, using this newly emitted,
  # authenticated source. No target candle open/close data was available
  # at forecast time. Revisions/illiquid session gaps are excluded.
  known={int(dt.datetime.fromisoformat(x["open_time"]).timestamp()):float(x["close"]) for x in rows}
  if len(known)!=len(rows):raise ValueError("DUPLICATE_NATIVE_M5_HISTORY")
  # Enforce source-revision integrity on every still-visible original
  # broker candle: never silently rewrite a prior price or label.
  for bar,previous_close in db.execute(
       "SELECT base_bar_open,base_close FROM predictions WHERE account=? AND symbol=?",
       (acct,symbol)):
   if bar in known and abs(known[bar]-previous_close)>1e-8*max(1,abs(previous_close)):
    raise ValueError("ORIGINAL_NATIVE_M5_CLOSE_REVISED_REVIEW_REQUIRED")
  for bar,outcome_close in db.execute(
       "SELECT target_bar_open,next_close FROM outcomes WHERE account=? AND symbol=?",
       (acct,symbol)):
   if bar in known and abs(known[bar]-outcome_close)>1e-8*max(1,abs(outcome_close)):
    raise ValueError("SETTLED_SHADOW_M5_OUTCOME_PRICE_REVISED_REVIEW_REQUIRED")
  for bar,close in db.execute(
       "SELECT target_bar_open,next_close FROM horizon_outcomes WHERE account=? AND symbol=?",
       (acct,symbol)):
   if bar in known and abs(known[bar]-close)>1e-8*max(1,abs(close)):
    raise ValueError("SETTLED_MULTIHORIZON_M5_OUTCOME_PRICE_REVISED_REVIEW_REQUIRED")
  scored=0;excluded=0;new_horizons={30:0,60:0}
  old=db.execute("SELECT base_bar_open,observed_utc,base_close FROM predictions WHERE account=? AND symbol=? "
   "AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.account=predictions.account AND o.symbol=predictions.symbol "
   "AND o.base_bar_open=predictions.base_bar_open) AND NOT EXISTS "
   "(SELECT 1 FROM exclusions e WHERE e.account=predictions.account AND e.symbol=predictions.symbol "
   "AND e.base_bar_open=predictions.base_bar_open)",(acct,symbol)).fetchall()
  with db:
   for t,observed,was in old:
    nexttime=t+300
    if now <nexttime+300:continue
    if nexttime not in known:
     # Keep pending until the source bar ages out; do not assume missing.
     if nexttime <min(known):
      db.execute("INSERT OR IGNORE INTO exclusions VALUES (?,?,?,?,?)",
                 (acct,symbol,t,"NO_AUTHENTICATED_NEXT_M5_CLOSED",now));excluded+=1
     continue
    if observed>=nexttime+300:
     db.execute("INSERT OR IGNORE INTO exclusions VALUES (?,?,?,?,?)",
                (acct,symbol,t,"SHADOW_PREDICTION_RECORDED_AFTER_OUTCOME",now))
     excluded+=1;continue
    nextprice=known[nexttime]
    if nextprice==was:
     db.execute("INSERT OR IGNORE INTO exclusions VALUES (?,?,?,?,?)",
                (acct,symbol,t,"FLAT_TARGET_NO_BINARY_DIRECTION",now))
     excluded+=1;continue
    db.execute("INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?,?,?,?)",
               (acct,symbol,t,nexttime,1 if nextprice>was else 0,nextprice,now,rawsource))
    scored+=1
   # Multi-horizon evidence remains separate from existing M5 scoring.
   # Both the target bar and every intermediate 5-minute bar must have
   # completed in authenticated broker history; closed-market gaps are
   # exclusions, NEVER synthesized candle data.
   for horizon_minutes in (30,60):
    previous=db.execute("SELECT p.base_bar_open,p.observed_utc,p.base_close "
      "FROM predictions p WHERE p.account=? AND p.symbol=? "
      "AND NOT EXISTS (SELECT 1 FROM horizon_outcomes o WHERE o.account=p.account "
      "AND o.symbol=p.symbol AND o.base_bar_open=p.base_bar_open AND o.horizon_minutes=?) "
      "AND NOT EXISTS (SELECT 1 FROM horizon_exclusions e WHERE e.account=p.account "
      "AND e.symbol=p.symbol AND e.base_bar_open=p.base_bar_open AND e.horizon_minutes=?)",
      (acct,symbol,horizon_minutes,horizon_minutes)).fetchall()
    count_steps=horizon_minutes//5
    for t,observed,was in previous:
     target=t+count_steps*300
     if now<target+300:continue
     if target not in known:
      if target<min(known):
       db.execute("INSERT OR IGNORE INTO horizon_exclusions VALUES (?,?,?,?,?,?)",
        (acct,symbol,t,horizon_minutes,"MISSING_COMPLETED_TARGET_BROKER_M5",now))
      continue
     if observed>=target+300:
      db.execute("INSERT OR IGNORE INTO horizon_exclusions VALUES (?,?,?,?,?,?)",
       (acct,symbol,t,horizon_minutes,"FORECAST_WAS_NOT_RECORDED_BEFORE_HORIZON_TARGET",now))
      continue
     if any(t+i*300 not in known for i in range(1,count_steps+1)):
      db.execute("INSERT OR IGNORE INTO horizon_exclusions VALUES (?,?,?,?,?,?)",
       (acct,symbol,t,horizon_minutes,"MISSING_INTERMEDIATE_COMPLETED_BROKER_M5",now))
      continue
     target_close=known[target]
     if target_close==was:
      db.execute("INSERT OR IGNORE INTO horizon_exclusions VALUES (?,?,?,?,?,?)",
       (acct,symbol,t,horizon_minutes,"FLAT_HORIZON_NO_BINARY_DIRECTION",now))
      continue
     db.execute("INSERT OR IGNORE INTO horizon_outcomes VALUES (?,?,?,?,?,?,?,?,?)",
      (acct,symbol,t,horizon_minutes,target,1 if target_close>was else 0,
       target_close,now,rawsource))
     new_horizons[horizon_minutes]+=1
   inserted=0
   if is_fresh and now<target_closes_at:
    cur=db.execute("INSERT OR IGNORE INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?)",
      (acct,symbol,last,now,rawsource,PIN,candsha,close,baseline_score,cand_score))
    inserted=cur.rowcount
  need(db.execute("PRAGMA quick_check").fetchone()==("ok",),
      "SHADOW_PREDICTION_SQLITE_INTEGRITY_FAILED")
  counts=db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
  correct=db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
 os.chmod(dbfile,0o600)
 return dict(new_predictions=inserted,newly_scored=scored,excluded=excluded,
             new_30min_scored=new_horizons[30],new_60min_scored=new_horizons[60],
             total_prediction_rows=counts,total_scored=correct,
             last_closed_m5_utc=iso(last_closed),forecast_snapshot_fresh=is_fresh)
def sources(now):
 # 7 DEMO accounts only; LIVE x9392 lacks authenticated native LIVE feed.
 fpmod=get_fp_loader()
 feeds={}; source_errors={}
 for suffix in ("0512","1636","1622","2582"):
  # Source is authentic broker-native MT5 M5 from the EXACT account.
  cid=ACCOUNTS[suffix]["cid"]
  try:
   feed,digest=fpmod.load_native_feed(suffix,cid,now)
  except (OSError,ValueError,KeyError,TypeError) as exc:
   feeds[suffix]={}
   source_errors[suffix]=type(exc).__name__
   continue
  per={}
  for canonical in sorted(set(feed["bars"])&set(feed["quotes"])):
   try:
    native,closed,rows=fpmod.native_bars(
      feed,canonical,now,set(SYMBOL_UNIVERSE))
    per[native]=(rows,digest)
   except ValueError:continue
  feeds[suffix]=per
 try:
  derivraw=rootfile(DERIV,age=145,cap=1500000)
  native_doc=json.loads(derivraw)
  validated=validate_snapshot(native_doc,utc_now())
  fp_digest=h(derivraw)
 except (OSError,ValueError,KeyError,TypeError) as exc:
  validated={};fp_digest=''
  for suffix in ("6207","7593","9244"):
   source_errors[suffix]=type(exc).__name__
 deriv={}
 for symbol,frames in validated.items():
  try:
   vals=frames["M5"];rows=[]
   for bar in vals:
    closed=int(bar["time"])+300
    if closed>now+2:raise ValueError("NATIVE_BAR_NOT_CLOSED")
    rows.append(dict(symbol=symbol,open_time=iso(int(bar["time"])),
        open=bar["open"],high=bar["high"],low=bar["low"],close=bar["close"],
        volume=0.0))
   deriv[symbol]=(rows,fp_digest)
  except (TypeError,ValueError):continue
 for suffix in ("6207","7593","9244"):feeds[suffix]=deriv
 return feeds,source_errors
def summary(owner):
 dbfile=OUT/"owners"/owner/"shadow.sqlite3"
 if not dbfile.is_file():return dict(predictions=0,scored=0)
 with sqlite3.connect("file:"+str(dbfile)+"?mode=ro",uri=True,timeout=10) as db:
  db.execute("PRAGMA query_only=ON")
  rows=db.execute("SELECT p.base_prob_up,p.candidate_prob_up,o.label_up "
    "FROM predictions p JOIN outcomes o USING (account,symbol,base_bar_open)").fetchall()
  total=db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
  pending=total-len(rows)-db.execute("SELECT COUNT(*) FROM exclusions").fetchone()[0]
  horizon_rows={}
  for hm in (30,60):
   horizon_rows[hm]=db.execute("SELECT p.base_prob_up,p.candidate_prob_up,o.label_up "
    "FROM predictions p JOIN horizon_outcomes o USING (account,symbol,base_bar_open) "
    "WHERE o.horizon_minutes=?",(hm,)).fetchall()
 def score(k):
  if not rows:return None
  return round(sum((r[k]-r[2])**2 for r in rows)/len(rows),6)
 def accuracy(k):
  if not rows:return None
  return round(sum((r[k]>=.5)==bool(r[2]) for r in rows)/len(rows),6)
 longer={}
 for hm,rr in horizon_rows.items():
  n=len(rr)
  longer[str(hm)+"min"]={"n":n,
   "baseline_brier":round(sum((x[0]-x[2])**2 for x in rr)/n,6) if n else None,
   "candidate_brier":round(sum((x[1]-x[2])**2 for x in rr)/n,6) if n else None,
   "baseline_accuracy":round(sum((x[0]>=.5)==bool(x[2]) for x in rr)/n,6) if n else None,
   "candidate_accuracy":round(sum((x[1]>=.5)==bool(x[2]) for x in rr)/n,6) if n else None,
   "fixed_half_probability_reference_brier":0.25 if n else None}
 return dict(predictions=total,scored=len(rows),pending=pending,
  additional_observed_market_horizons=longer,
  baseline_brier=score(0),candidate_brier=score(1),
  baseline_direction_accuracy=accuracy(0),candidate_direction_accuracy=accuracy(1),
  candidate_brier_minus_baseline_brier=round(score(1)-score(0),6) if rows else None)
def main():
 now=time.time()
 os.umask(0o077)
 need(ROOT.is_dir() and ROOT.stat().st_uid==0 and stat.S_IMODE(ROOT.stat().st_mode)==0o700,
   "UNTRUSTED_PARENT_LEARNING_ARCHIVE")
 OUT.mkdir(parents=True,mode=0o700,exist_ok=True)
 lease=OUT/"single-writer.lock"
 with open(lease,"a+") as f:
  fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  global ACCOUNTS,SYMBOL_UNIVERSE
  ACCOUNTS=roster()
  need(h((BASE/"nexus_brain.json").read_bytes())==PIN,
     "PRODUCTION_BASELINE_MODEL_SHA_CHANGED")
  base=load_unified(str(BASE),mc_samples=1)
  need(base is not None and len(base.symbol_ids)==26,"PINNED_BASELINE_NOT_49_FEATURE")
  SYMBOL_UNIVERSE=set(base.symbol_ids)
  demos,source_errors=sources(now)
  models={}
  for owner in set(a["ownerhash"] for a in ACCOUNTS.values()):
   models[owner]=model(owner,now)
  accounts={};bad={}
  for suffix in sorted(ACCOUNTS):
   owner=ACCOUNTS[suffix]["ownerhash"]
   if suffix=="9392":
    accounts[suffix]=dict(state="LIVE_READONLY_NOT_EVALUATED_NO_AUTHENTICATED_LIVE_M5",
                         source_symbols=0,new_predictions=0,newly_scored=0)
    continue
   offered=demos.get(suffix,{})
   if suffix in source_errors:
    bad.setdefault(suffix,{})["account_native_source"]=source_errors[suffix]
   ok=0;new=0;scored=0;ex=0;h30=0;h60=0
   for symbol,(rows,sha) in sorted(offered.items()):
    try:
     res=record(owner,suffix,symbol,sha,rows,models[owner][0],models[owner][1],base,now)
     ok+=1;new+=res["new_predictions"];scored+=res["newly_scored"];ex+=res["excluded"]
     h30+=res["new_30min_scored"];h60+=res["new_60min_scored"]
    except (ValueError,sqlite3.Error) as err:
     # One native instrument may be unavailable: keep others and mark
     # fail-closed per symbol rather than inventing any missing bar.
     bad.setdefault(suffix,{})[symbol]=str(err)[:100]
   accounts[suffix]=dict(state=("NATIVE_SOURCE_UNAVAILABLE_NO_FORECAST" if suffix in source_errors
     else "AUTHENTICATED_DEMO_M5_SHADOW_ONLY"),
     source_symbols=len(offered),verified_symbols=ok,
     new_predictions=new,newly_scored=scored,new_exclusions=ex,
     new_30min_scored=h30,new_60min_scored=h60)
  owners={o[:12]:summary(o) for o in models}
  report=dict(schema="abutron.v9.owner_isolated.prospective_m5_broker_native_shadow.v1",
   observed_utc=iso(now),source_accounts=8,eligible_demo_accounts=7,
   live_9392_native_live_market_available=False,
   source_data_version="signals_v2_causal_proxy_v1",
   purpose="M5_CLOSED_BAR_NEXT_CLOSE_DIRECTION_FORECAST_PROXY_NOT_TRADE_PNL",
   predictions_post_source_observation_only=True,
   forecast_before_target_broker_M5_close_required=True,
   market_model_data_source="AUTHENTICATED_NATIVE_NO_FUTURE_M5_CANDLE",
   signal_horizon_seconds=300,
   additional_prospective_outcome_horizons_minutes=[30,60],
   note="The same frozen pre-target forecast is scored for different real elapsed broker M5 horizons; no model update or synthetic session bars",
   all_trading_modes_unchanged=True,model_promotion_allowed=False,
   production_brain_modified=False,broker_orders_submitted=0,
   account_status=accounts,account_missing_source=bad,owner_metrics=owners,
   model_probability_source="BASELINE_AND_PRIVATE_FROZEN_OWNER_CANDIDATE",
   shadow_is_not_broker_order_execution_certification=True)
  atomic(OUT/"latest.json",report)
  print(json.dumps(dict(utc=report["observed_utc"],
   instrument_feeds=sum(x.get("verified_symbols",0) for x in accounts.values()),
   new_predictions=sum(x.get("new_predictions",0) for x in accounts.values()),
   newly_scored=sum(x.get("newly_scored",0) for x in accounts.values()),
   scored_total=sum(x.get("scored",0) for x in owners.values()),
   missing_source_accounts={k:len(v) for k,v in bad.items()},
   live_switched_on=False,broker_orders=0,model_promoted=False)))
if __name__=="__main__":main()
