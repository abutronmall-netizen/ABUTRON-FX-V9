#!/usr/bin/python3
"""ABUTRON V9 forward shadow: correlation-aware confidence audit, READ ONLY.
Distinguish account rows from unique broker/symbol/bar outcomes and clustered
time/day evidence. Information-only: never promotes models or edits risk/CRM.
"""
from __future__ import annotations
import datetime as dt,hashlib,json,math,os,random,sqlite3,stat,sys,tempfile,time
from collections import Counter,defaultdict
from pathlib import Path

ROOT=Path("/var/lib/abutron-v9-eight-account-learning")
SHADOW=ROOT/"validation"/"prospective-m5"
OUT=ROOT/"validation"/"statistical-confidence"
PIN="2667997b1498aae316c4103ec1f3d068ed5b90f947b"
PIN_SHA="2667997b1498aae316c4103ec1f3d0bd2efb805f034836455ed10019284302df"
SCHEMA="abutron.v9.owner_native_m5_shadow.correlation_aware_confidence.ro.v1"
ACTIVE={"0512","1622","1636","2582","6207","7593","9244","9392"}
UTC=dt.timezone.utc
def check(ok,message):
    if not ok:raise ValueError(message)
def sha(v):
    return hashlib.sha256(v).hexdigest()
def trusted_json(p,cap=900000):
    check(p.is_file() and not p.is_symlink(),"PRIVATE_EVIDENCE_SOURCE_MISSING")
    s=p.stat()
    check(s.st_uid==0 and stat.S_ISREG(s.st_mode) and not s.st_mode&0o077
          and 0<s.st_size<cap,"UNTRUSTED_PRIVATE_EVIDENCE_PERMISSIONS")
    raw=p.read_bytes()
    check(len(raw)==s.st_size and s.st_mtime_ns==p.stat().st_mtime_ns,
          "PRIVATE_EVIDENCE_CHANGED_DURING_READ")
    return json.loads(raw)
def atomic(path,data):
    fd,temp=tempfile.mkstemp(prefix=".v9-confidence-",dir=path.parent)
    try:
        with os.fdopen(fd,"w") as f:
            os.fchmod(f.fileno(),0o600)
            json.dump(data,f,sort_keys=True,indent=2,allow_nan=False)
            f.write("\n");f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.lexists(temp):os.unlink(temp)
def iso(ts):
    return dt.datetime.fromtimestamp(ts,UTC).isoformat()
def source_roster():
    archive=trusted_json(ROOT/"latest.json")
    check(archive.get("schema")=="abutron.v9.eight_account.owner_isolated_native_learning_archive.ro.v1"
        and archive.get("source_accounts_archived")==8
        and archive.get("customer_data_isolated_by_owner_hash") is True
        and archive.get("broker_orders_submitted")==0
        and set(archive["accounts"])==ACTIVE,
        "ACTIVE_PRIVATE_OWNER_ARCHIVE_UNVERIFIED")
    owners={}
    for suffix,a in archive["accounts"].items():
        owner=a.get("owner_hash")
        check(isinstance(owner,str) and len(owner)==64
            and all(k in "0123456789abcdef" for k in owner),
            "INVALID_OWNER_HASH_IDENTITY")
        owners[suffix]=(owner,a.get("broker"),a.get("environment"))
        check((a.get("environment")=="LIVE")== (suffix=="9392"),
              "LIVE_OWNER_ACCOUNT_ROLE_CHANGED")
    return owners,archive
def snapshot_models(owner):
    folder=ROOT/"candidates"/owner/"runs"
    seen={}
    for p in sorted(folder.glob("*/candidate-manifest.json")):
        try:
            d=trusted_json(p,cap=220000)
            model=Path(d["candidate_model_path"])
            if (d.get("schema")!="abutron.v9.owner_isolated_broker_causal_candidate.v1"
                or d.get("owner_hash")!=owner
                or d.get("pinned_baseline_sha256")!=PIN_SHA
                or d.get("model_promotion_allowed") is not False
                or d.get("broker_orders_sent")!=0
                or model.name!="nexus_brain.json"
                or model.parent.parent.parent.parent!=ROOT/"candidates"/owner
                or model.is_symlink() or not model.is_file()
                or model.stat().st_uid!=0 or model.stat().st_mode&0o077):
                continue
            digest=d["candidate_model_sha256"]
            if len(digest)!=64 or sha(model.read_bytes())!=digest:continue
            stamp=dt.datetime.fromisoformat(d["created_utc"])
            if stamp.tzinfo is None:continue
            seen[digest]=stamp.timestamp()
        except (OSError,ValueError,KeyError,TypeError,UnicodeError):
            continue
    return seen
def metric(records):
    n=len(records)
    if not n:return {"n":0,"baseline_brier":None,"candidate_brier":None,
        "paired_brier_delta":None,"baseline_logloss":None,
        "candidate_logloss":None,"baseline_direction_accuracy":None,
        "candidate_direction_accuracy":None,"mean_abs_probability_shift":None,
        "direction_changes":0,"candidate_better_rows":0,
        "baseline_better_rows":0}
    def brier(k):
        return sum((r[k]-r["label"])**2 for r in records)/n
    def logloss(k):
        return -sum(r["label"]*math.log(max(1e-9,min(1-1e-9,r[k])))+
                    (1-r["label"])*math.log(max(1e-9,min(1-1e-9,1-r[k])))
                    for r in records)/n
    b0=brier("baseline");b1=brier("candidate")
    d=[(r["candidate"]-r["label"])**2-
       (r["baseline"]-r["label"])**2 for r in records]
    return {"n":n,"baseline_brier":round(b0,8),"candidate_brier":round(b1,8),
       "paired_brier_delta":round(b1-b0,8),
       # The fixed 50/50 reference is chosen a priori; not the empirically
       # optimal hindsight prevalence from the observed sample.
       "uninformative_half_up_probability_brier":0.25,
       "candidate_brier_minus_half_probability_reference":round(b1-0.25,8),
       "baseline_brier_minus_half_probability_reference":round(b0-0.25,8),
       "candidate_outperforms_half_probability_reference":b1<0.25,
       "actual_up_fraction":round(sum(r["label"] for r in records)/n,6),
       "baseline_predicted_up_fraction":round(sum(r["baseline"]>=.5 for r in records)/n,6),
       "candidate_predicted_up_fraction":round(sum(r["candidate"]>=.5 for r in records)/n,6),
       "mean_baseline_up_probability":round(sum(r["baseline"] for r in records)/n,6),
       "mean_candidate_up_probability":round(sum(r["candidate"] for r in records)/n,6),
       "baseline_logloss":round(logloss("baseline"),8),
       "candidate_logloss":round(logloss("candidate"),8),
       "baseline_direction_accuracy":round(sum((r["baseline"]>=.5)==bool(r["label"])
                 for r in records)/n,6),
       "candidate_direction_accuracy":round(sum((r["candidate"]>=.5)==bool(r["label"])
                 for r in records)/n,6),
       "mean_abs_probability_shift":round(sum(abs(r["candidate"]-r["baseline"])
                 for r in records)/n,8),
       "direction_changes":sum((r["candidate"]>=.5)!=(r["baseline"]>=.5)
                 for r in records),
       "candidate_better_rows":sum(x<0 for x in d),
       "baseline_better_rows":sum(x>0 for x in d)}
def validate_row(rec,owner,roster,checkpoints):
    a=rec["account"];check(a in roster and roster[a][0]==owner
                        and a!="9392","CUSTOMER_FORECAST_OWNER_MISMATCH")
    check(roster[a][1] in ("Deriv","FP Markets") and
        roster[a][2]=="DEMO","ACCOUNT_NOT_APPROVED_DEMO_SOURCE")
    t=rec["bar"];observed=rec["observed"];outcome_time=rec["recorded"]
    check(type(t) is int and t%300==0 and 1600000000<t<2000000000
          and math.isfinite(observed) and math.isfinite(outcome_time)
          and t+300<=observed<t+600
          and outcome_time>=t+600
          and rec["target_bar"]==t+300,
          "SHADOW_OBSERVATION_NOT_BEFORE_TARGET_CLOSE")
    check(rec["baseline_sha"]==PIN_SHA
          and rec["candidate_sha"] in checkpoints
          and checkpoints[rec["candidate_sha"]]<=observed,
          "FROZEN_OWNER_MODEL_NOT_PROVEN_BEFORE_FORECAST")
    check(all(isinstance(rec[k],str) and len(rec[k])==64
        for k in ("source_sha","outcome_source_sha")),"NATIVE_BROKER_SOURCE_DIGEST_MISSING")
    check(rec["label"] in (0,1) and
          all(math.isfinite(rec[k]) for k in ("baseline","candidate","base_close","next_close"))
          and 0<=rec["baseline"]<=1 and 0<=rec["candidate"]<=1
          and rec["base_close"]>0 and rec["next_close"]>0
          and rec["base_close"]!=rec["next_close"]
          and int(rec["next_close"]>rec["base_close"])==rec["label"],
          "SHADOW_BROKER_PRICE_LABEL_INTEGRITY_FAILED")
    rec["broker"]=roster[a][1]
    rec["date"]=dt.datetime.fromtimestamp(t,UTC).strftime("%Y-%m-%d")
    return rec
def audited_rows(owner,roster,checkpoints):
    p=SHADOW/"owners"/owner/"shadow.sqlite3"
    check(p.is_file() and not p.is_symlink()
          and p.stat().st_uid==0 and p.stat().st_mode&0o077==0,
          "OWNER_NATIVE_SHADOW_SQLITE_PRIVATE_SOURCE_UNVERIFIED")
    with sqlite3.connect("file:"+str(p)+"?mode=ro",uri=True,timeout=12) as db:
        db.execute("PRAGMA query_only=ON")
        check(db.execute("PRAGMA quick_check").fetchone()==("ok",),
              "SHADOW_BROKER_SOURCE_SQLITE_INTEGRITY_FAILED")
        n=db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        excluded=db.execute("SELECT COUNT(*) FROM exclusions").fetchone()[0]
        # Pull predictions+labels together, preserving authentic source hashes.
        rows=db.execute("""
          SELECT p.account,p.symbol,p.base_bar_open,p.observed_utc,
                 p.source_sha256,p.baseline_sha256,p.candidate_sha256,
                 p.base_close,p.base_prob_up,p.candidate_prob_up,
                 o.target_bar_open,o.label_up,o.next_close,o.recorded_utc,o.source_sha256
          FROM predictions p JOIN outcomes o USING(account,symbol,base_bar_open)
          ORDER BY p.account,p.symbol,p.base_bar_open
        """).fetchall()
    events=[]
    for a,s,t,observed,source,base_sha,cand_sha,baseclose,pb,pc,tt,label,nc,ot,osrc in rows:
        rec=dict(account=a,symbol=s,bar=t,observed=observed,recorded=ot,
            target_bar=tt,baseline_sha=base_sha,candidate_sha=cand_sha,
            source_sha=source,outcome_source_sha=osrc,
            base_close=baseclose,next_close=nc,baseline=pb,candidate=pc,label=label)
        events.append(validate_row(rec,owner,roster,checkpoints))
    check(len(events)+excluded<=n,"SHADOW_OUTCOMES_EXCEED_CAPTURED_PREDICTIONS")
    return events,dict(forecasts=n,scored=len(events),excluded=excluded,
                       pending=n-len(events)-excluded)
def count_unique(events):
    # Price/label must be the same for duplicate accounts on a shared broker
    # instrument and candle. Keep one per distinct independent model owner,
    # but never count repeated customer accounts as independent outcomes.
    clusters=defaultdict(list)
    for rec in events:clusters[(rec["broker"],rec["symbol"],rec["bar"])].append(rec)
    conflicts=0
    for group in clusters.values():
        if len({r["label"] for r in group})!=1:conflicts+=1
        if len({round(r["next_close"],8) for r in group})!=1:conflicts+=1
        if len({round(r["base_close"],8) for r in group})!=1:conflicts+=1
    check(conflicts==0,"CROSS_ACCOUNT_BROKER_CANDLE_LABEL_DISAGREEMENT")
    return clusters
def cluster_ci_by_day(events,seed):
    # Daily block bootstrap. It is NOT meaningful with a handful of bars
    # from a single day. Require at least ten distinct UTC days before CI.
    byday=defaultdict(list)
    for rec in events:byday[rec["date"]].append(rec)
    if len(byday)<10 or len({r["bar"] for r in events})<100:
        return dict(status="INSUFFICIENT_DISTINCT_UTC_DAYS_OR_TIME_BLOCKS",
            independent_utc_days=len(byday),distinct_five_minute_times=len({r["bar"] for r in events}),
            confidence_interval_95_paired_brier_delta=None,
            upper_confidence_bound_below_zero=False)
    keys=sorted(byday)
    rng=random.Random(seed)
    samples=[]
    for i in range(2400):
        drawn=[byday[keys[rng.randrange(len(keys))]] for j in keys]
        seq=[r for group in drawn for r in group]
        numerator=sum((r["candidate"]-r["label"])**2-
                      (r["baseline"]-r["label"])**2 for r in seq)
        samples.append(numerator/len(seq))
    samples.sort()
    lo,hi=samples[round(.025*(len(samples)-1))],samples[round(.975*(len(samples)-1))]
    return dict(status="DAILY_BLOCK_BOOTSTRAP_RESEARCH_ONLY",
        independent_utc_days=len(byday),distinct_five_minute_times=len({r["bar"] for r in events}),
        confidence_interval_95_paired_brier_delta=[round(lo,8),round(hi,8)],
        upper_confidence_bound_below_zero=hi<0,
        caveat="Resampling by UTC day preserves shared market-candle correlation within day; not trade PnL.")
def frozen_trade_evidence():
    path=ROOT/"validation"/"forward-oos"/"latest.json"
    try:
        d=trusted_json(path,cap=120000)
        timestamp=dt.datetime.fromisoformat(d["checked_at_utc"])
        check(timestamp.tzinfo is not None and 0<=time.time()-timestamp.timestamp()<=10800,
              "FROZEN_TRADE_OOS_SOURCE_STALE")
        check(d.get("schema")=="abutron.v9.forward_oos.frozen_owner_candidate_vs_baseline.ro.v1"
          and d.get("all_valid_trades_have_model_snapshot_before_entry") is True
          and d.get("model_promoted") is False
          and d.get("broker_orders_sent")==0
          and d.get("source_owner_count")==4,
          "FROZEN_TRADE_OOS_SOURCE_SCHEMA_UNVERIFIED")
        count=d["totals"]["verified_post_checkpoint_closed_trades_scored"]
        check(type(count) is int and 0<=count<=100000,
              "FROZEN_TRADE_OOS_EVENT_COUNT_UNTRUSTED")
        groups={}
        for owner,a in d["owners"].items():
            check(len(owner)==12 and all(c in "0123456789abcdef" for c in owner),
                  "FROZEN_TRADE_OOS_OWNER_ID_INVALID")
            n=a.get("post_checkpoint_scored",0)
            check(type(n) is int and n>=0 and n<=count,
                  "FROZEN_TRADE_OOS_OWNER_TRADES_INVALID")
            groups[owner]={"broker_closed_event_directions_scored":n,
                 "baseline_direction_accuracy":a.get("baseline",{}).get("directional_accuracy"),
                 "candidate_direction_accuracy":a.get("candidate",{}).get("directional_accuracy"),
                 "baseline_brier":a.get("baseline",{}).get("brier"),
                 "candidate_brier":a.get("candidate",{}).get("brier")}
        check(sum(x["broker_closed_event_directions_scored"] for x in groups.values())==count,
              "FROZEN_TRADE_OOS_PER_OWNER_COUNTS_DO_NOT_SUM")
        return {"status":"VERIFIED_BUT_NOT_ECONOMIC_PNL_PROOF",
             "scored_broker_closed_trade_directions":count,
             "checked_at_utc":d["checked_at_utc"],
             "per_owner":groups,"candidate_placed_these_broker_orders":False,
             "market_direction_metric_is_not_strategy_profitability":True}
    except (OSError,ValueError,KeyError,TypeError,UnicodeError) as exc:
        return {"status":"FROZEN_TRADE_SOURCE_INCOMPLETE",
            "reason_code":type(exc).__name__,
            "candidate_placed_these_broker_orders":False,
            "market_direction_metric_is_not_strategy_profitability":True}

def main():
    check(ROOT.is_dir() and ROOT.stat().st_uid==0 and stat.S_IMODE(ROOT.stat().st_mode)==0o700,
          "ROOT_PRIVATE_ARCHIVE_PERMISSIONS_CHANGED")
    roster,archive=source_roster()
    perowner=defaultdict(list);records=[];errors={};counts={}
    for owner in sorted({v[0] for v in roster.values()}):
        try:
            checkpoints=snapshot_models(owner)
            check(checkpoints,"NO_ATTESTED_OWNER_CANDIDATE_CHECKPOINT")
            events,count=audited_rows(owner,roster,checkpoints)
            perowner[owner]=events;records+=events;counts[owner]=count
        except (OSError,ValueError,sqlite3.Error,KeyError,TypeError) as exc:
            reason=str(exc)
            errors[owner[:12]]=(reason if 5<len(reason)<120 and reason.replace("_","").isalnum() else type(exc).__name__)
    clusters=count_unique(records)
    ownerreports={}
    for owner in sorted({v[0] for v in roster.values()}):
        if owner[:12] in errors:
            ownerreports[owner[:12]]=dict(status="SOURCE_UNVERIFIED",reason_code=errors[owner[:12]])
            continue
        rows=perowner[owner]
        distinct=count_unique(rows)
        perbroker={}
        for broker in ("Deriv","FP Markets"):
            part=[r for r in rows if r["broker"]==broker]
            perbroker[broker]=metric(part)
        m=metric(rows)
        bydays=cluster_ci_by_day(rows,int(owner[:12],16))
        ownerreports[owner[:12]]=dict(status="MONITOR_ONLY_NOT_PROMOTION_CERTIFICATION",
            **counts[owner],account_records_scored=len(rows),
            distinct_broker_instrument_candle_outcomes=len(distinct),
            distinct_UTC_five_minute_times=len({r["bar"] for r in rows}),
            distinct_UTC_days=len({r["date"] for r in rows}),
            metric=m,per_broker=perbroker,daily_block_confidence=bydays,
            sufficient_evidence_to_conclude_candidate_superior=False,
            live_model_promotion_allowed=False)
    first=min((r["bar"] for r in records),default=None)
    last=max((r["bar"] for r in records),default=None)
    result=dict(schema=SCHEMA,
        generated_utc=dt.datetime.now(UTC).isoformat(),
        source="ROOT_PRIVATE_AUTHENTICATED_DEMO_M5_PRESCORED_SHADOW_DATABASE",
        source_account_inventory=8,
        demo_accounts_with_forecast_sources=7,
        live_9392_not_eligible_without_authenticated_live_market=True,
        raw_scored_account_rows=len(records),
        unique_broker_instrument_time_outcomes=len(clusters),
        repeated_account_outcome_count=len(records)-len(clusters),
        distinct_UTC_five_minute_times=len({r["bar"] for r in records}),
        distinct_UTC_days=len({r["date"] for r in records}),
        outcome_first_base_bar_utc=iso(first) if first is not None else None,
        outcome_latest_base_bar_utc=iso(last) if last is not None else None,
        source_validation_issues=errors,
        broker_closed_frozen_model_direction_comparison=frozen_trade_evidence(),
        owner_groups=ownerreports,
        confidence_research_only=True,
        forecast_horizon="NEXT_CLOSED_BROKER_M5_DIRECTION_NOT_REAL_EXECUTION_PNL",
        no_automatic_model_promotion=True,
        statistical_significance_established=False,
        production_brain_changed=False,
        crm_or_trading_switches_changed=False,
        risk_policy_changed=False,
        broker_orders_submitted=0)
    OUT.mkdir(mode=0o700,parents=True,exist_ok=True)
    atomic(OUT/"latest.json",result)
    print(json.dumps(dict(raw_scored_account_rows=result["raw_scored_account_rows"],
      unique_broker_instrument_time_outcomes=result["unique_broker_instrument_time_outcomes"],
      distinct_time_buckets=result["distinct_UTC_five_minute_times"],
      distinct_days=result["distinct_UTC_days"],
      per_owner={k:dict(status=v["status"],n=v.get("account_records_scored"),
      brier_delta=v.get("metric",{}).get("paired_brier_delta"),
      confidence=v.get("daily_block_confidence",{}).get("status")) for k,v in ownerreports.items()},
      validation_errors=errors,model_promoted=False,orders_sent=0),sort_keys=True))
    return 0 if not errors else 2
if __name__=="__main__":
    if sys.argv[1:]==["--selftest"]:
        test=[
            dict(broker="Deriv",symbol="Volatility 10 Index.0",bar=100,label=1,
                 next_close=100.,base_close=99.,baseline=.5,candidate=.8,
                 date="2026-11-18"),
            dict(broker="Deriv",symbol="Volatility 10 Index.0",bar=100,label=1,
                 next_close=100.,base_close=99.,baseline=.5,candidate=.8,
                 date="2026-11-18")]
        assert len(count_unique(test))==1
        assert metric(test)["n"]==2
        assert cluster_ci_by_day(test,42)["confidence_interval_95_paired_brier_delta"] is None
        wrong=[dict(z) for z in test];wrong[1]["label"]=0
        try:count_unique(wrong)
        except ValueError:pass
        else:raise AssertionError("DUPLICATE_BROKER_LABEL_CONFLICT_ACCEPTED")
        print("V9_STATISTICAL_CONFIDENCE_MONITOR_SELFTEST_4_PASS")
    elif sys.argv[1:]:sys.exit("USAGE: --selftest or no args")
    else:sys.exit(main())
