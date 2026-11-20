#!/usr/bin/env python3
# Migration export: CRM connection IDs are supplied by per-account environment variables.
"""ABUTRON V9 account-bound, causal broker-closed-trade candidate trainer.

Only advances a separately pinned candidate after genuine FP DEMO broker
entry+exit receipts are verified. Completes signals from authenticated,
CLOSED native M5 bars, no synthetic candles, no intrabar lookahead.
Never edits production nexus_brain.json, CRM, MT5 or LIVE.
"""
from __future__ import annotations
import csv,datetime as dt,fcntl,gzip,hashlib,io,json,math,os,sqlite3,stat
from pathlib import Path
import subprocess,sys,tempfile,time
from importlib.machinery import SourceFileLoader
import importlib.util
ROOT=Path("/var/lib/abutron-v9-realtrade-feedback")
RELEASE=Path("/opt/abutron-v9-brain/releases/a88f373001ee1b8fcc934a1e3d068ed5b90f947b")
MODEL=RELEASE/"data/brains/unified_m5/nexus_brain.json"
EXPECTED_SHA="2667997b1498aae316c4103ec1f3d0bd2efb805f034836455ed10019284302df"
CANDIDATE=ROOT/"latest-candidate.json"
FEED=Path("/var/lib/abutron-fpmarkets-feed/partial")
ARCHIVE=Path("/var/lib/abutron-fp-native-m5-archive/broker-closed-m5.sqlite3")
HCC=Path("/var/lib/abutron-v9-hcc-verified/native-m5.json")
HCC_EXPECTED_SHA="04efd7bf5d97c664249af83e2c90be8e13b1dd0a7396a306faf6af873ae77b4b"
HCC_EARLY=Path("/var/lib/abutron-v9-hcc-verified/native-m5-early-20261117.json")
HCC_EARLY_EXPECTED_SHA="1b455fc5664b85e96e8db9a01432364b5d9ecc9c1820918cca827c85b9e8cd72"
SOURCE=ROOT/"new_since_v9_pinned.csv"
SOURCE_MANIFEST=ROOT/"latest.json"
V9_REPORT=Path("/var/lib/abutron-v9-brain/decisions/fp-demo-authoritative-latest.json")
PYTHON="/opt/abutron-v9-brain/.venv-a88f373/bin/python"
TRAINER="/usr/local/lib/abutron-v9-realtrade-feedback/train_from_trades_causal.py"
ACCOUNT_MAP={"0512":__import__("os").environ["ABUTRON_CRM_CONNECTION_ID_0512"],
             "1636":__import__("os").environ["ABUTRON_CRM_CONNECTION_ID_1636"]}
FIELDS=["broker","account","position_id","event_id","symbol","direction",
        "entry_time","exit_time","entry_price","exit_price","lots","pnl",
        "entry_type","entry_quality"]
CANDLES=["symbol","broker_symbol","timeframe","start_time_utc","open",
         "high","low","close","broker_time_raw"]

def check(v,why):
 if not v:raise ValueError(why)
def stamp(v):
 t=dt.datetime.fromisoformat(str(v).replace("Z","+00:00"))
 check(t.tzinfo is not None,"NAIVE_NATIVE_TIME")
 return t.timestamp()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def secured(p,age,uid=0):
 p=Path(p)
 check(not p.is_symlink() and p.stat().st_uid==uid and
       not p.stat().st_mode&0o077 and
       -10<=time.time()-p.stat().st_mtime<=age,"UNTRUSTED_OR_STALE_ROOT_REPORT")
 j=json.loads(p.read_text())
 check(isinstance(j,dict),"INVALID_ROOT_REPORT")
 return j
def atomic(path,doc):
 fd,tmp=tempfile.mkstemp(prefix="."+path.name,dir=path.parent)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,"w") as f:
   json.dump(doc,f,sort_keys=True,indent=2);f.write("\n")
   f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.lexists(tmp):os.unlink(tmp)
def native_bars(cid,report_digest,source_checked_at,expected_missing):
 j=secured(FEED/(cid+".json"),150)
 check(j.get("schema")=="abutron.fpmarkets.native.partial.readonly.v1"
   and j.get("connection_id")==cid and j.get("account_mode")=="DEMO"
   and j.get("broker_server")=="FPMarketsSC-Demo"
   and j.get("execution_enabled") is False and j.get("orders_submitted")==0
   and j.get("parallel_forex_status")=="SIX_FOREX_R_VERIFIED_V305",
   "BROKER_NATIVE_M5_IDENTITY_NOT_VERIFIED")
 # The native feed refreshes independently every ~60 seconds, so a
 # newer authenticated same-account export is legitimate. Never accept
 # an older export than the V9 attested source or a foreign account ID.
 digest=sha(FEED/(cid+".json"))
 if digest!=report_digest:
  check(stamp(j.get("checked_at_utc"))>=stamp(source_checked_at)
        and 0<=time.time()-stamp(j.get("checked_at_utc"))<=150,
        "SAME_ACCOUNT_NATIVE_M5_SHA_ROLLOVER_NOT_VERIFIED")
 mapping=j["mapping"];bars=j["bars"];now=time.time()
 check(len(mapping)==13 and isinstance(expected_missing,list)
       and len(expected_missing)==len(set(expected_missing))
       and set(expected_missing)<=set(mapping)
       and set(bars)==set(mapping)-set(expected_missing)
       and len(bars)>=1
       and len({v["actual"] for v in mapping.values()})==13
       and all(v.get("status")=="MAPPED" and v.get("trade_mode")==4
               for v in mapping.values()),"NATIVE_SYMBOL_COVERAGE_MISMATCH")
 table=[];coverage={entry["actual"]:canonical for canonical,entry in mapping.items()};timebars={}
 for canonical,bundle in bars.items():
  entry=mapping[canonical];actual=entry["actual"]
  check(entry["status"]=="MAPPED" and entry["trade_mode"]==4 and
        isinstance(bundle.get("M5"),list) and len(bundle["M5"])>=65,
        "NATIVE_SYMBOL_BAR_SCOPE_INVALID")
  seen=[];last=0
  for row in bundle["M5"]:
   t=row["time"]
   check(type(t)==int and t>last and t%300==0,"M5_TIME_INVALID")
   last=t
   # Never use the open/current bar for training labels.
   if now < t+300: continue
   vals=[row.get(x) for x in ("open","high","low","close")]
   check(all(type(v) in (float,int) and math.isfinite(v) and v>0 for v in vals)
         and vals[2]<=min(vals[0],vals[3])<=max(vals[0],vals[3])<=vals[1],
         "MT5_CANDLE_GEOMETRY_INVALID")
   seen.append(t)
   table.append(dict(symbol=canonical,broker_symbol=actual,timeframe="M5",
        start_time_utc=t,open=vals[0],high=vals[1],low=vals[2],
        close=vals[3],broker_time_raw=t))
  check(len(seen)>=60 and 0<=now-seen[-1]-300<=615,
        "NO_FRESH_COMPLETED_M5_BROKER_EVIDENCE")
  coverage[actual]=canonical;timebars[actual]=seen
 return table,coverage,timebars
def extend_native_m5_from_verified_archive(account, feedrows, mapping, timebars):
    """Use older authenticated CLOSED M5 rows while keeping native current snapshot.

    Archive is append-only, account bound and source-SHA indexed by the existing
    native V305 importer; each row is checked against its source receipt.
    Never synthesize or interpolate missing candles.
    """
    folder=ARCHIVE.parent
    check(folder.is_dir() and not folder.is_symlink()
          and folder.stat().st_uid==0
          and stat.S_IMODE(folder.stat().st_mode)==0o700,
          "AUTHENTICATED_M5_ARCHIVE_DIRECTORY_UNTRUSTED")
    check(ARCHIVE.is_file() and not ARCHIVE.is_symlink()
          and ARCHIVE.stat().st_uid==0
          and stat.S_IMODE(ARCHIVE.stat().st_mode)==0o600,
          "AUTHENTICATED_M5_ARCHIVE_FILE_UNTRUSTED")
    known={(r["symbol"],int(r["start_time_utc"])):r for r in feedrows}
    actual_by_canonical={v:k for k,v in mapping.items()}
    now=time.time()
    added=0
    con=sqlite3.connect("file:"+str(ARCHIVE)+"?mode=ro",uri=True,timeout=5)
    try:
        con.execute("PRAGMA query_only=ON")
        check(con.execute("PRAGMA quick_check").fetchone()[0]=="ok",
              "AUTHENTICATED_M5_ARCHIVE_SQLITE_CORRUPT")
        sql="""SELECT b.symbol,b.broker_symbol,b.start_time_utc,
          b.open,b.high,b.low,b.close,b.broker_time_raw,
          b.source_sha256,s.account
          FROM closed_m5 AS b
          LEFT JOIN source_checks AS s ON b.source_sha256=s.source_sha256
          WHERE b.account=? ORDER BY b.symbol,b.start_time_utc"""
        for canonical,actual,t,o,h,l,c,raw,digest,source_account in con.execute(sql,(account,)):
            check(canonical in actual_by_canonical
                  and actual==actual_by_canonical[canonical]
                  and source_account==account
                  and isinstance(digest,str) and len(digest)==64
                  and all(c in "0123456789abcdef" for c in digest),
                  "AUTHENTICATED_M5_ARCHIVE_PROVENANCE_OR_MAPPING_FAILED")
            check(type(t)==int and t>0 and t%300==0
                  and type(raw)==int and raw-t==7200
                  and t+300<=now,
                  "AUTHENTICATED_M5_ARCHIVE_TIME_NOT_CAUSAL")
            vals=(o,h,l,c)
            check(all(type(v) in (float,int) and math.isfinite(v) and v>0 for v in vals)
                  and l<=min(o,c)<=max(o,c)<=h,
                  "AUTHENTICATED_M5_ARCHIVE_PRICE_INVALID")
            key=(canonical,t)
            if key in known:
                existing=known[key]
                check(existing["broker_symbol"]==actual
                      and tuple(existing[k] for k in ("open","high","low","close"))==vals,
                      "IMMUTABLE_NATIVE_BAR_ARCHIVE_CONFLICT")
                continue
            known[key]=dict(symbol=canonical,broker_symbol=actual,timeframe="M5",
                            start_time_utc=t,open=o,high=h,low=l,close=c,
                            broker_time_raw=t)
            added+=1
    finally:
        con.close()
    combined=sorted(known.values(),key=lambda row:(row["symbol"],row["start_time_utc"]))
    timelines={actual:[] for actual in mapping}
    for row in combined:
        timelines[row["broker_symbol"]].append(int(row["start_time_utc"]))
    check(all(len(v)>=60 and all(b>a for a,b in zip(v,v[1:])) for v in timelines.values()),
          "M5_ARCHIVE_OR_NATIVE_TIMELINE_INVALID")
    return combined,timelines,added

def extend_from_two_account_hcc(feedrows, mapping):
    """Add cross-account identical HCC native M1-derived CLOSED M5 history.

    A static SHA-pinned, root-private snapshot is used; no historical prices
    may be fabricated, future-dated, remapped, or changed by training.
    """
    check(HCC.parent.stat().st_uid==0
          and stat.S_IMODE(HCC.parent.stat().st_mode)==0o700
          and HCC.is_file() and not HCC.is_symlink()
          and HCC.stat().st_uid==0 and stat.S_IMODE(HCC.stat().st_mode)==0o600
          and sha(HCC)==HCC_EXPECTED_SHA,
          "HCC_TWO_ACCOUNT_SOURCE_PIN_UNVERIFIED")
    j=json.loads(HCC.read_text())
    check(j.get("schema")=="abutron.fp.native.hcc.cross_account_closed_m5.v1"
          and j.get("accounts")==["0512","1636"]
          and j.get("source_days")==["2026-11-13","2026-11-16"]
          and j.get("cross_account_minute_prices_identical") is True
          and j.get("complete_m5_only") is True
          and j.get("broker_server_offset_seconds")==7200
          and j.get("orders")==0 and j.get("model_promotion_allowed") is False
          and len(j.get("rows",[]))==j.get("row_count")
          and len(j.get("native_source_sha256_per_account_symbol",[]))==26,
          "HCC_CROSS_ACCOUNT_PROOF_SCHEMA_INVALID")
    present=set()
    for record in j["native_source_sha256_per_account_symbol"]:
        account=record.get("account");symbol=record.get("symbol")
        digest=record.get("hcc_sha256")
        check(account in ("0512","1636") and symbol in mapping.values()
              and record.get("broker_symbol") in mapping
              and mapping[record["broker_symbol"]]==symbol
              and isinstance(digest,str) and len(digest)==64
              and all(c in "0123456789abcdef" for c in digest)
              and (account,symbol) not in present,
              "HCC_CROSS_ACCOUNT_SYMBOL_DIGEST_UNVERIFIED")
        present.add((account,symbol))
    check(len(present)==26,"HCC_TWO_ACCOUNT_PRICE_SOURCES_INCOMPLETE")
    existing={(r["symbol"],int(r["start_time_utc"])):r for r in feedrows}
    added=0;now=time.time()
    seen=set()
    for row in j["rows"]:
        check(isinstance(row,list) and len(row)==7,"HCC_PRICE_ROW_INVALID")
        canonical,actual,t,o,h,l,c=row
        check(actual in mapping and mapping[actual]==canonical
              and type(t)==int and t%300==0 and t>0 and t+300<=now
              and dt.datetime.fromtimestamp(t,dt.timezone.utc).date().isoformat()
                  in ("2026-11-13","2026-11-16")
              and all(type(v) in (float,int) and math.isfinite(v) and v>0
                      for v in (o,h,l,c))
              and l<=min(o,c)<=max(o,c)<=h,
              "HCC_PRICE_NOT_CAUSAL_OR_BROKER_BOUND")
        key=(canonical,t)
        check(key not in seen,"HCC_DUPLICATE_COMPLETED_M5")
        seen.add(key)
        if key in existing:
            old=existing[key]
            check(old["broker_symbol"]==actual
                  and tuple(old[k] for k in ("open","high","low","close"))
                      ==(o,h,l,c),
                  "HCC_AND_NATIVE_BROKER_PRICE_CONFLICT")
        else:
            existing[key]=dict(symbol=canonical,broker_symbol=actual,
                timeframe="M5",start_time_utc=t,open=o,high=h,low=l,
                close=c,broker_time_raw=t)
            added+=1
    merged=sorted(existing.values(),key=lambda r:(r["symbol"],r["start_time_utc"]))
    timeline={actual:[] for actual in mapping}
    for row in merged:
        timeline[row["broker_symbol"]].append(int(row["start_time_utc"]))
    check(all(len(v)>=60 and len(set(v))==len(v) for v in timeline.values()),
          "MERGED_M5_CANDIDATE_HISTORY_INVALID")
    return merged,timeline,added

def extend_from_authenticated_early_hcc(feedrows, mapping):
    """Restore the five-sym November 17 early M5 gap via native-price overlap.

    For each symbol 30+ complete M5 bars were verified exactly against the
    independently authenticated root-owned V305 bar ledger. Immutable snapshot
    pinned with SHA256. No fabricated OHLC or unverified source accounts.
    """
    check(HCC_EARLY.parent.is_dir() and HCC_EARLY.parent.stat().st_uid==0
          and stat.S_IMODE(HCC_EARLY.parent.stat().st_mode)==0o700
          and HCC_EARLY.is_file() and not HCC_EARLY.is_symlink()
          and HCC_EARLY.stat().st_uid==0
          and stat.S_IMODE(HCC_EARLY.stat().st_mode)==0o600
          and sha(HCC_EARLY)==HCC_EARLY_EXPECTED_SHA,
          "EARLY_HCC_BROKER_OVERLAP_SNAPSHOT_UNVERIFIED")
    j=json.loads(HCC_EARLY.read_text())
    symbols={"EURUSD":"EURUSD.r","GBPUSD":"GBPUSD.r",
             "NZDUSD":"NZDUSD.r","USDCHF":"USDCHF.r","USDJPY":"USDJPY.r"}
    check(j.get("schema")=="abutron.fp.hcc.early.with_native_m5_overlap.v1"
          and j.get("account")=="0512"
          and j.get("second_account_verified_for_previous_day") is True
          and j.get("native_m5_bar_overlap_match_minimum")==30
          and j.get("timezone_offset_seconds")==7200
          and j.get("orders")==0 and j.get("model_promotion") is False
          and len(j.get("rows",[]))==j.get("row_count")
          and len(j.get("sources",[]))==5
          and all(mapping.get(actual)==canonical
                  for canonical,actual in symbols.items()),
          "EARLY_HCC_NATIVE_ACCOUNT_OR_SCHEMA_UNVERIFIED")
    verified=set()
    for attestation in j["sources"]:
        symbol=attestation.get("symbol")
        digest=attestation.get("native_hcc_sha256")
        check(symbol in symbols
              and attestation.get("broker_symbol")==symbols[symbol]
              and attestation.get("broker_native_m5_overlap_identical",0)>=30
              and isinstance(digest,str) and len(digest)==64
              and all(c in "0123456789abcdef" for c in digest)
              and symbol not in verified,
              "EARLY_HCC_NATIVE_M5_OVERLAP_PROOF_MISSING")
        verified.add(symbol)
    check(verified==set(symbols),"EARLY_HCC_SOURCE_SYMBOLS_INCOMPLETE")
    previous={(r["symbol"],int(r["start_time_utc"])):r for r in feedrows}
    seen=set();added=0
    start=int(dt.datetime(2026,11,17,0,tzinfo=dt.timezone.utc).timestamp())
    end=int(dt.datetime(2026,11,17,1,40,tzinfo=dt.timezone.utc).timestamp())
    for rec in j["rows"]:
        check(type(rec)==list and len(rec)==7,
              "EARLY_HCC_RECORD_TYPE_INVALID")
        canonical,actual,t,o,h,l,c=rec
        check(canonical in symbols and symbols[canonical]==actual
              and start<=t<end and type(t)==int and t%300==0
              and all(type(v) in (float,int) and math.isfinite(v) and v>0
                      for v in (o,h,l,c))
              and l<=min(o,c)<=max(o,c)<=h,
              "EARLY_HCC_PRICE_OR_ENTRY_TIME_NOT_CAUSAL")
        key=(canonical,t)
        check(key not in seen,"EARLY_HCC_DUPLICATE_BAR")
        seen.add(key)
        if key in previous:
            old=previous[key]
            check(old["broker_symbol"]==actual
                  and tuple(old[k] for k in ("open","high","low","close"))
                      ==(o,h,l,c),
                  "EARLY_HCC_PRICE_CONFLICT_WITH_NATIVE")
            continue
        previous[key]=dict(symbol=canonical,broker_symbol=actual,
                           timeframe="M5",start_time_utc=t,open=o,high=h,
                           low=l,close=c,broker_time_raw=t)
        added+=1
    check(len(seen)==100,"EARLY_HCC_VERIFIED_BAR_COUNT_CHANGED")
    combined=sorted(previous.values(),key=lambda x:(x["symbol"],x["start_time_utc"]))
    timelines={actual:[] for actual in mapping}
    for record in combined:
        timelines[record["broker_symbol"]].append(int(record["start_time_utc"]))
    check(all(len(v)>=60 and len(v)==len(set(v)) for v in timelines.values()),
          "EARLY_HCC_COMPLETE_M5_TIMELINE_INVALID")
    return combined,timelines,added

def csv_write(path,cols,rows):
 with open(path,"w",newline="",encoding="utf-8") as f:
  writer=csv.DictWriter(f,fieldnames=cols,extrasaction="ignore")
  writer.writeheader();writer.writerows(rows)
def trainer(args,env,out):
 proc=subprocess.run(args,env=env,text=True,stdout=subprocess.PIPE,
     stderr=subprocess.STDOUT,check=False,timeout=240)
 log=(out/"trainer.log")
 log.write_text(proc.stdout)
 os.chmod(log,0o600)
 check(proc.returncode==0,"BROKER_CANDIDATE_TRAINING_EXIT_"+str(proc.returncode))
 return proc.stdout
def main():
 check(ROOT.is_dir() and ROOT.stat().st_uid==0
       and ROOT.stat().st_mode&0o777==0o700,"ROOT_TRAINING_STATE_UNTRUSTED")
 check(sha(MODEL)==EXPECTED_SHA,"PRODUCTION_BRAIN_PIN_CHANGED")
 source=secured(SOURCE_MANIFEST,160)
 inf=secured(V9_REPORT,120)
 check(inf.get("trained") is True and inf.get("model_sha256")==EXPECTED_SHA
       and inf.get("model_features")==49 and inf.get("orders_submitted")==0
       and set(inf.get("accounts",{}))=={"0512","1636"}
       and all(isinstance(inf["accounts"][a].get("symbols_missing"),list)
               and isinstance(inf["accounts"][a].get("symbols_current"),int)
               and inf["accounts"][a]["symbols_current"]>=1
               and inf["accounts"][a]["symbols_current"]
                   +len(inf["accounts"][a]["symbols_missing"])==13
               for a in ACCOUNT_MAP),
       "PRODUCTION_49FEATURE_NATIVE_MODEL_IDENTITY_NOT_CURRENT")
 body=SOURCE.read_bytes()
 check(hashlib.sha256(body).hexdigest()==source["native_candidate_input_sha256"],
       "BROKER_CLOSED_TRADE_INPUT_RACE")
 rows=list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
 check(len(rows)==source["new_since_training_cutoff"] and
       len({row["event_id"] for row in rows})==len(rows),
       "BROKER_CLOSED_TRADE_EVENTS_INVALID")
 prev=None
 if CANDIDATE.is_file():
  prev=json.loads(CANDIDATE.read_text())
  check(prev.get("schema")=="abutron.v9.realtrade.candidate.causal.v1"
        and prev.get("baseline_sha256")==EXPECTED_SHA
        and prev.get("model_promotion_allowed") is False,
        "CANDIDATE_PREVIOUS_CHAIN_UNTRUSTED")
  model_file=Path(prev["candidate_model_path"])
  check(model_file.is_file() and model_file.name=="nexus_brain.json"
        and ROOT/"candidates" in model_file.parents
        and sha(model_file)==prev["candidate_model_sha256"],
        "PREVIOUS_CANDIDATE_MODEL_HASH_UNVERIFIED")
 previous=set(prev["trained_event_ids"]) if prev else set()
 groups={};excluded=[]
 for suffix,cid in ACCOUNT_MAP.items():
  m=inf["accounts"][suffix]
  check(m["connection_id"]==cid and m["environment"]=="DEMO"
        and m["symbols_current"]>=1
        and m["symbols_current"]+len(m["symbols_missing"])==13,
        "ACCOUNT_NATIVE_MODEL_BINDING_UNVERIFIED")
  feedrows,map_actual,bar_index=native_bars(cid,m["native_source_sha256"],
     m["native_export_checked_at_utc"],m["symbols_missing"])
  feedrows,bar_index,archived_added=extend_native_m5_from_verified_archive(
      suffix,feedrows,map_actual,bar_index)
  feedrows,bar_index,hcc_added=extend_from_two_account_hcc(feedrows,map_actual)
  feedrows,bar_index,early_added=extend_from_authenticated_early_hcc(
      feedrows,map_actual)
  eligible=[]
  for row in rows:
   if row["account"]!=suffix or row["event_id"] in previous:continue
   if row["symbol"] not in map_actual:
    excluded.append((row["event_id"],"SYMBOL_NOT_IN_BROKER_NATIVE_MODEL"))
    continue
   ts=stamp(row["entry_time"])
   times=bar_index[row["symbol"]]
   # Need 30 prior CLOSED M5 bars before the actual deal entry.
   prior=[x for x in times if x+300<=ts]
   if len(prior)<31:
    excluded.append((row["event_id"],"NO_30_PREENTRY_CLOSED_BROKER_M5"))
    continue
   if ts-(prior[-1]+300)>900:
    excluded.append((row["event_id"],"ENTRY_M5_STALE_NO_LOOKAHEAD"))
    continue
   check(stamp(row["exit_time"])>=ts and float(row["lots"])>0,
         "CLOSED_BROKER_TRADE_TIME_INVALID")
   eligible.append(row)
  groups[suffix]=(feedrows,eligible,archived_added,hcc_added,early_added)
 if not any(trades for _,trades,_,_,_ in groups.values()):
  print("NO_NEW_CAUSAL_BROKER_CLOSED_TRADES_TO_TRAIN",
        "already_consumed",len(previous),"ineligible",len(excluded))
  return
 label=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
 runpath=ROOT/"runs"/label
 runpath.mkdir(parents=True,mode=0o700,exist_ok=False)
 output=ROOT/"candidates"/label
 output.mkdir(parents=True,mode=0o700,exist_ok=False)
 base=Path(prev["candidate_model_path"]).parent if prev else MODEL.parent
 incoming_online=json.loads((base/"nexus_brain.json").read_text())["online"]["updates"]
 used=[];counts={}
 env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8",
      "PYTHONPATH":str(RELEASE/"src"),"PYTHONDONTWRITEBYTECODE":"1",
      "ABUTRON_ENV":"demo","ABUTRON_GEMMA_ENABLED":"false",
      "ABUTRON_LIVE_TRADING_ENABLED":"false",
      "ABUTRON_DATA_ROOT":str(ROOT/"runtime")}
 for suffix in ACCOUNT_MAP:
  feedrows,trades,archived_added,hcc_added,early_added=groups[suffix]
  if not trades:continue
  wk=runpath/suffix
  data=wk/"data"/"FP-Markets"
  data.mkdir(parents=True,mode=0o700)
  os.chmod(data,0o700)
  with gzip.open(data/"candles.csv.gz","wt",encoding="utf-8",newline="") as f:
   writer=csv.DictWriter(f,fieldnames=CANDLES)
   writer.writeheader();writer.writerows(feedrows)
  inp=wk/"causal_native_closed_trades.csv"
  # The legacy trainer interprets any `position_id` column as raw MT5
  # per-deal data. This file is already fully PAIRED and verified; export
  # its canonical settled-trade columns only to avoid double-pairing.
  settled_fields=[k for k in FIELDS if k not in ("position_id","event_id")]
  csv_write(inp,settled_fields,trades)
  target=output/suffix
  target.mkdir(mode=0o700)
  command=[PYTHON,"-B",TRAINER,"--trades",str(inp),
     "--data",str(wk/"data"),"--mode","online",
     "--lr","0.0005","--ticks","","--depth","",
     "--in",str(base),"--out",str(target)]
  trainer(command,env,target)
  report=json.loads((target/"trade_report_online.json").read_text())
  check(report["mode"]=="online" and
        report["trained"]==len(trades) and report["skipped"]==0 and
        report["trades_loaded"]==len(trades),
        "BROKER_CAUSAL_TRAINING_DID_NOT_CONSUME_EACH_VERIFIED_TRADE")
  actual=json.loads((target/"nexus_brain.json").read_text())
  check(actual["n_features"]==49 and len(actual["members"])==3
        and report["online_updates_total"]>=incoming_online+len(trades),
        "TRAINED_CANDIDATE_MODEL_SCHEMA_OR_LEARNING_INVALID")
  incoming_online=report["online_updates_total"]
  base=target
  used += [row["event_id"] for row in trades]
  counts[suffix]={"broker_closed_trades_trained":len(trades),
                  "native_m5_rows":len(feedrows),
                  "authenticated_archive_older_m5_rows":archived_added,
                  "cross_account_hcc_m5_rows":hcc_added,
                  "early_hcc_native_overlap_m5_rows":early_added,
                  "train_report_path":str(target/"trade_report_online.json")}
 result={
  "schema":"abutron.v9.realtrade.candidate.causal.v1",
  "generated_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
  "status":"REAL_BROKER_CLOSED_TRADES_TRAINED_CANDIDATE_AWAITING_OOS_VALIDATION",
  "baseline_sha256":EXPECTED_SHA,
  "source_feedback_sha256":source["native_candidate_input_sha256"],
  "training_cutoff_exclusive":source["model_training_cutoff_exclusive"],
  "candidate_model_path":str(base/"nexus_brain.json"),
  "candidate_model_sha256":sha(base/"nexus_brain.json"),
  "causal_native_completed_m5_only":True,
  "tracked_broker_closed_positions":source["all_closed_in_selected_native_windows"],
  "new_broker_closed_trades":source["new_since_training_cutoff"],
  "trained_new_events_this_pass":len(used),
  "trained_event_ids":sorted(previous|set(used)),
  "not_aligned_due_to_native_history_window":len(excluded),
  "native_account_training":counts,
  "online_updates":incoming_online,
  "model_promotion_allowed":False,
  "live_model_modified":False,
  "demo_model_modified":False,
  "crm_modified":False,
  "broker_orders":0}
 atomic(output/"candidate-manifest.json",result)
 atomic(CANDIDATE,result)
 print("V9_REAL_CLOSED_TRADE_CANDIDATE_TRAINED",
       "trained",len(used),"cumulative",len(previous|set(used)),
       "online_updates",incoming_online,"no promotion","no broker orders",flush=True)
if __name__=="__main__":
 try:
  ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
  with open(ROOT/"candidate-training.lock","a+") as lease:
   fcntl.flock(lease.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
   main()
 except Exception as exc:
  # No secrets or raw deal rows are printed.
  print("V9_REAL_CLOSED_TRADE_CANDIDATE_BLOCKED",
        type(exc).__name__,str(exc)[:90],file=sys.stderr)
  raise SystemExit(1)
