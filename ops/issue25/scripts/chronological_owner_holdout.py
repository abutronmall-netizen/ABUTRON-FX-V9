#!/usr/bin/python3
"""V9 chronological per-owner 70/30 purged holdout replay (candidate research only).
Train a NEW replay copy on pre-holdout trades and evaluate later trades;
never claim full-history already-trained candidate has OOS accuracy.
"""
from __future__ import annotations
import bisect,csv,datetime as dt,gc,hashlib,importlib.util,io,json,math,os,runpy,stat,sys,tempfile,time
from pathlib import Path
from collections import defaultdict
import numpy as np

ROOT=Path('/var/lib/abutron-v9-eight-account-learning')
RELEASE=Path('/opt/abutron-v9-brain/releases/a88f373001ee1b8fcc934a1e3d068ed5b90f947b')
PINNED=RELEASE/'data/brains/unified_m5'
SRC='/usr/local/sbin/abutron-v9-all-owner-causal-candidate-trainer'
BRAINTRAIN='/usr/local/lib/abutron-v9-realtrade-feedback/train_from_trades_causal.py'

USD=dt.timezone.utc

def _load_helper():
    """Lazy-load the production trainer script at runtime.

    Moved from import-time to runtime so the module can be imported
    for testing without the production file being present.
    """
    import importlib.util as _ilu
    sys.path.insert(0,str(RELEASE/'src'))
    mod = _ilu.module_from_spec(_ilu.spec_from_loader(
        'causal_trainer', _ilu.SourceFileLoader('causal_trainer', SRC)))
    mod.loader.exec_module(mod)
    return mod

def _load_brain_modules():
    """Lazy-load abutron brain modules at runtime.

    Moved from import-time to runtime so the module can be imported
    for testing without the production abutron package being present.
    """
    sys.path.insert(0,str(RELEASE/'src'))
    from abutron.brain.unified import load_unified
    from abutron.brain.signals import features_for_brain
    return load_unified, features_for_brain

def _load_parent():
    """Lazy-load the production trainer script via runpy at runtime."""
    return runpy.run_path(SRC,run_name='abutron_candidate_qa_non_main')

def insist(ok,code):
 if not ok:raise ValueError(code)
def event_time(row,key):
 return dt.datetime.fromisoformat(row[key]).timestamp()
def dump(path,payload):
 fd,tmp=tempfile.mkstemp(prefix='.owner-oos-',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as f:
   os.fchmod(f.fileno(),0o600)
   json.dump(payload,f,sort_keys=True,indent=2,allow_nan=False)
   f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.lexists(tmp):os.unlink(tmp)
def probability_up(brain,features):
 x=np.asarray([features],dtype=float)
 insist(x.shape==(1,49),'NOT_PINNED_49_FEATURE_STRUCTURE')
 numerator=0.0;total=0.0
 for member in brain.ensemble.members:
  probs=member.predict(x)
  insist(probs.shape==(1,2) and np.isfinite(probs).all()
    and abs(float(probs.sum())-1.0)<1e-5,'MODEL_PREDICTION_INVALID')
  numerator+=member.weight*float(probs[0,1]);total+=member.weight
 insist(total>0,'MODEL_WEIGHTING_INVALID')
 return max(0.0,min(1.0,numerator/total))
def metrics(rows):
 if not rows:return dict(n=0,accuracy=None,brier=None,logloss=None)
 return dict(n=len(rows),
   accuracy=round(sum((p>=.5)==bool(y) for p,y in rows)/len(rows),6),
   brier=round(sum((p-y)**2 for p,y in rows)/len(rows),6),
   logloss=round(-sum(y*math.log(max(1e-6,min(1-1e-6,p)))+
     (1-y)*math.log(max(1e-6,min(1-1e-6,1-p))) for p,y in rows)/len(rows),6))
def run():
 audit,fp=parent['sources']()
 owners,account=parent['events'](audit)
 index=parent['market_index']()
 aligned,counts,missing=parent['eligible'](owners,index)
 root=ROOT/'validation'
 root.mkdir(mode=0o700,parents=True,exist_ok=True)
 label=dt.datetime.now(USD).strftime('%Y%m%dT%H%M%SZ')
 output={}
 for owner,trades in sorted(aligned.items()):
  work=root/'replays'/owner/label
  work.mkdir(parents=True,mode=0o700,exist_ok=True)
  rows=sorted(trades,key=lambda r:(event_time(r,'entry_time'),r['event_id']))
  cutoff=event_time(rows[int(len(rows)*0.7)],'entry_time')
  baseline_cutoff=dt.datetime(2026,11,16,tzinfo=USD).timestamp()
  strict_cutoff=max(cutoff,baseline_cutoff)
  post=[r for r in rows if event_time(r,'entry_time')>=strict_cutoff]
  pre=[r for r in rows if event_time(r,'exit_time')+900<=strict_cutoff]
  if len(post)>=12 and len(pre)>=20:cutoff=strict_cutoff
  holdout=[r for r in rows if event_time(r,'entry_time')>=cutoff]
  training=[r for r in rows if event_time(r,'exit_time')+900<=cutoff]
  earliest=dt.datetime.fromtimestamp(cutoff,USD).isoformat()
  if len(training)<20 or len(holdout)<10:
   output[owner[:12]]=dict(status='INSUFFICIENT_PURGED_HISTORY',
       train=len(training),holdout=len(holdout),first_holdout_entry_utc=earliest)
   continue
  # Include ONLY genuine M5 already verified for the relevant pre-entry
  # feature windows, not future labels. Source candles are read-only.
  parent['runtime_market'](training+holdout,index,work/'market')
  traincsv=work/'train.csv'
  data=parent['csvbytes'](training,parent['TRADEFIELDS'])
  traincsv.write_bytes(data);os.chmod(traincsv,0o600)
  tr=helper.load_trade_file(str(traincsv))
  insist(len(tr)==len(training),'TRAINING_TRADE_LOADER_DROPPED_EXAMPLES')
  candidate=load_unified(str(PINNED),mc_samples=1)
  baseline=load_unified(str(PINNED),mc_samples=1)
  insist(candidate is not None and baseline is not None,'PINNED_BRAIN_COULD_NOT_RELOAD')
  candidate.nn_online_lr=0.0005
  align=helper.MarketAlign(str(work/'market'),timeframe='M5',
    ticks_path='',depth_path='',use_gemma=False)
  num,skip,guard=helper.train_online(candidate,tr,align)
  insist(skip==0 and num==len(training),'CHRONOLOGICAL_REPLAY_NOT_CAUSALLY_TRAINED')
  b_rows=[];c_rows=[];no_prices=0
  for row in holdout:
   entry=event_time(row,'entry_time')
   win=align.signal_window(row['symbol'],entry)
   insist(win is not None,'OOS_FEATURE_WINDOW_NOT_CLOSED_M5')
   raw=win['market_features']
   f_base=features_for_brain(baseline,raw,symbol=row['symbol'])
   f_cand=features_for_brain(candidate,raw,symbol=row['symbol'])
   # target is price-move direction, not trade-side profitability.
   move=float(row['exit_price'])-float(row['entry_price'])
   if move==0:
    no_prices+=1;continue
   y=1 if move>0 else 0
   b_rows.append((probability_up(baseline,f_base),y))
   c_rows.append((probability_up(candidate,f_cand),y))
  b_metric=metrics(b_rows);c_metric=metrics(c_rows)
  output[owner[:12]]=dict(status='CHRONOLOGICAL_REPLAY_VALIDATED_RESEARCH_ONLY',
     archived_owner_hash=owner,training_positions=len(training),
     holdout_positions=len(holdout),holdout_nonflat_positions=len(b_rows),
     no_directional_label_from_flat_prices=no_prices,
     earliest_holdout_entry_utc=earliest,
     train_exit_plus_15min_before_holdout_start=all(
       event_time(r,'exit_time')+900<=cutoff for r in training),
     two_brains_loaded_from_same_production_pin=True,
     baseline_49feature_metrics=b_metric,
     early_only_account_learning_replay_metrics=c_metric,
     raw_brier_delta_candidate_minus_baseline=(round(c_metric['brier']-b_metric['brier'],6)
       if c_metric['brier'] is not None else None),
     strict_baseline_post_training_holdout=(
       cutoff>=dt.datetime(2026,11,16,tzinfo=USD).timestamp()),
     no_claim_latest_fully_trained_candidate_is_oos=True,
     trained_new_live_model=False,orders_submitted=0)
  # report is evidence-only, no selected model is saved or deployed.
  del candidate,baseline,align;gc.collect()
 report=dict(schema='abutron.v9.four_owner_purged_chronological_broker_trade_holdout.v1',
  checked_at_utc=dt.datetime.now(USD).isoformat(),
  archive_trade_count=audit['total_causally_closed_positions_in_private_archive'],
  owner_count=4,split='70pct chronological source entry, test on later entries',
  embargo_seconds=900,
  pinned_baseline_sha256=parent['BASESHA'],
  in_sample_latest_candidate_compared=False,
  statistically_independent_full_production_model_pretraining=False,
  production_model_promoted=False,crm_changed=False,live_enabled=False,
  orders_submitted=0,owners=output)
 dump(root/'latest-chronological-holdout.json',report)
 print(json.dumps({k:dict(status=v['status'],train=v.get('training_positions'),
    test=v.get('holdout_nonflat_positions'),baseline_brier=v.get('baseline_49feature_metrics',{}).get('brier'),
    replay_brier=v.get('early_only_account_learning_replay_metrics',{}).get('brier'),
    brier_delta=v.get('raw_brier_delta_candidate_minus_baseline'),
    post_baseline=v.get('strict_baseline_post_training_holdout'))
   for k,v in output.items()},sort_keys=True))
if __name__=='__main__':run()
