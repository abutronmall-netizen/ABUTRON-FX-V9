#!/usr/bin/python3
"""ABUTRON V9 forward-only frozen-checkpoint broker-trade shadow validation.
Select only an immutable model checkpoint CREATED BEFORE a native trade entry.
Never trains, orders, promotes, changes CRM or activates LIVE.
"""
from __future__ import annotations
import csv,datetime as dt,gc,hashlib,importlib.util,io,json,os,runpy,stat,sys,tempfile
from collections import defaultdict
from pathlib import Path
import numpy as np

ROOT=Path('/var/lib/abutron-v9-eight-account-learning')
RELEASE=Path('/opt/abutron-v9-brain/releases/a88f373001ee1b8fcc934a1e3d068ed5b90f947b')
PINNED=RELEASE/'data/brains/unified_m5'
VALID=ROOT/'validation'/'forward-oos'
UTC=dt.timezone.utc
def _load_production_modules():
    """Lazy-load production modules at runtime.

    Moved from import-time to runtime so the module can be imported
    for testing without the production abutron package or trainer
    script being present.
    """
    sys.path.insert(0,str(RELEASE/'src'))
    from abutron.brain.unified import load_unified
    from abutron.brain.signals import features_for_brain
    return load_unified, features_for_brain

def _load_trainhelper():
    """Lazy-load the production trainer helper script at runtime."""
    trainer_path='/usr/local/lib/abutron-v9-realtrade-feedback/train_from_trades_causal.py'
    sp=importlib.util.spec_from_file_location('abutron_v9_causal_market_preentry_features',trainer_path)
    trainhelper=importlib.util.module_from_spec(sp)
    sys.modules[sp.name]=trainhelper
    sp.loader.exec_module(trainhelper)
    return trainhelper
def chk(cond,reason):
 if not cond:raise ValueError(reason)
def sha(b):return hashlib.sha256(b).hexdigest()
def ts(s):return dt.datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def trusted(p):
 chk(p.is_file() and not p.is_symlink() and p.stat().st_uid==0 and not p.stat().st_mode&0o077,
  'UNTRUSTED_PRIVATE_MODEL_MANIFEST')
 return json.loads(p.read_text())
def atomic(path,x):
 fd,tmp=tempfile.mkstemp(prefix='.prospective-shadow-',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as f:
   os.fchmod(f.fileno(),0o600)
   json.dump(x,f,sort_keys=True,indent=2,allow_nan=False)
   f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.lexists(tmp):os.unlink(tmp)
def probability(brain,market,symbol):
 features=features_for_brain(brain,market,symbol=symbol)
 x=np.asarray([features],dtype=float)
 chk(x.shape==(1,49),'FORWARD_49_FEATURE_VECTOR_INVALID')
 weights=[];scores=[]
 for m in brain.ensemble.members:
  scores.append(m.predict(x))
  weights.append(m.weight)
 chk(len(scores)==3 and all(p.shape==(1,2) and np.isfinite(p).all() for p in scores),
  'FROZEN_MODEL_PROBABILITY_INVALID')
 v=float(sum(w*p[0,1] for w,p in zip(weights,scores))/sum(weights))
 chk(0<=v<=1,'INVALID_FROZEN_CLASS_PROBABILITY')
 return v
def summary(arr):
 n=len(arr)
 if n==0:return dict(n=0,directional_accuracy=None,brier=None,logloss=None)
 import math
 return dict(n=n,
  directional_accuracy=round(sum((p>=.5)==bool(y) for p,y in arr)/n,6),
  brier=round(sum((p-y)**2 for p,y in arr)/n,6),
  logloss=round(-sum(y*math.log(max(1e-6,min(1-1e-6,p)))+
   (1-y)*math.log(max(1e-6,min(1-1e-6,1-p))) for p,y in arr)/n,6))
def events():
 archived,fp=module['sources']()
 owners,rows=module['events'](archived)
 index=module['market_index']()
 aligned,counts,missing=module['eligible'](owners,index)
 return aligned,index
def checkpoints(owner):
 root=ROOT/'candidates'/owner
 verified=[]
 for p in sorted(root.glob('runs/*/candidate-manifest.json')):
  try:
   d=trusted(p)
   model=Path(d['candidate_model_path'])
   chk(d.get('schema')=='abutron.v9.owner_isolated_broker_causal_candidate.v1'
      and d.get('owner_hash')==owner
      and d.get('pinned_baseline_sha256')==module['BASESHA']
      and d.get('model_promotion_allowed') is False
      and d.get('broker_orders_sent')==0
      and model.name=='nexus_brain.json'
      and model.parent.parent.parent.parent==root
      and model.is_file() and sha(model.read_bytes())==d['candidate_model_sha256'],
      'OWNER_FROZEN_MODEL_PROVENANCE_FAILED')
   verified.append((ts(d['created_utc']),d))
  except (OSError,ValueError,KeyError,TypeError):
   # A single invalid checkpoint cannot be used for a prospective prediction.
   continue
 return sorted(verified,key=lambda x:x[0])
def run():
 chk(ROOT.is_dir() and stat.S_IMODE(ROOT.stat().st_mode)==0o700,
    'PRIVATE_LEARNING_ARCHIVE_OWNER_INVALID')
 VALID.mkdir(parents=True,mode=0o700,exist_ok=True)
 aligned,idx=events()
 baseline=load_unified(str(PINNED),mc_samples=1)
 chk(baseline is not None and sha((PINNED/'nexus_brain.json').read_bytes())==module['BASESHA'],
   'PINNED_BASELINE_CHANGED')
 report={}
 totals=dict(eligible_after_prior_model_checkpoint=0,
             verified_post_checkpoint_closed_trades_scored=0)
 for owner,rows in sorted(aligned.items()):
  frozen=checkpoints(owner)
  to_score=[]
  for r in rows:
   entry=ts(r['entry_time'])
   older=[(t,v) for t,v in frozen if entry>=t+300
        and r['event_id'] not in v.get('trained_event_ids',[])]
   if not older:continue
   most_recent=older[-1][1]
   if float(r['exit_price'])==float(r['entry_price']):continue
   to_score.append((r,most_recent))
  totals['eligible_after_prior_model_checkpoint']+=len(to_score)
  if not to_score:
   report[owner[:12]]=dict(status='AWAITING_NEW_CLOSED_POST_CHECKPOINT_TRADES',
        checkpoints_verified=len(frozen),post_checkpoint_scored=0)
   continue
  work=VALID/'runs'/dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')/owner
  work.mkdir(parents=True,mode=0o700,exist_ok=True)
  module['runtime_market']([r for r,_ in to_score],idx,work/'market')
  align=trainhelper.MarketAlign(str(work/'market'),timeframe='M5',
                               ticks_path='',depth_path='',use_gemma=False)
  per=defaultdict(lambda:dict(b=[],c=[],count=0))
  cache={}
  proof=[]
  for r,v in to_score:
   mid=v['candidate_model_sha256']
   if mid not in cache:
    model=Path(v['candidate_model_path'])
    chk(sha(model.read_bytes())==mid,'FROZEN_MODEL_CHECKPOINT_SHA_RACE')
    cache[mid]=load_unified(str(model.parent),mc_samples=1)
    chk(cache[mid] is not None,'FROZEN_MODEL_UNREADABLE')
   win=align.signal_window(r['symbol'],ts(r['entry_time']))
   chk(win is not None,'POST_ENTRY_MARKET_FEATURE_SOURCE_MISSING')
   feat=win['market_features']
   y=int(float(r['exit_price'])>float(r['entry_price']))
   b=probability(baseline,feat,r['symbol'])
   c=probability(cache[mid],feat,r['symbol'])
   per[mid]['b'].append((b,y));per[mid]['c'].append((c,y));per[mid]['count']+=1
   proof.append(dict(event_id_sha256=sha(r['event_id'].encode()),
      owner_hash=owner,source_account_masked='••••'+r['account'],
      frozen_checkpoint_sha256=mid,
      checkpoint_created_before_entry=True,
      broker_trade_entry_utc=r['entry_time'],broker_trade_exit_utc=r['exit_time'],
      market_up_label=y,baseline_p_up=round(b,8),candidate_p_up=round(c,8)))
  atomic(work/'predictions-private.json',dict(schema='abutron.v9.forward_oos.native_trade_details.ro.v1',
   observations=proof,orders_sent=0,production_promotion=False))
  allb=[e for v in per.values() for e in v['b']]
  allc=[e for v in per.values() for e in v['c']]
  report[owner[:12]]=dict(
   status='FROZEN_PRE_ENTRY_CANDIDATE_VS_BASELINE_SCORED',
   checkpoints_verified=len(frozen),post_checkpoint_scored=len(allb),
   baseline=summary(allb),candidate=summary(allc),
   candidate_brier_minus_baseline_brier=round(summary(allc)['brier']-summary(allb)['brier'],6),
   future_entry_after_model_creation_proven=True,
   prediction_details_private_path=str(work/'predictions-private.json'),
   model_promotion_allowed=False)
  totals['verified_post_checkpoint_closed_trades_scored']+=len(allb)
  del align,cache;gc.collect()
 result=dict(schema='abutron.v9.forward_oos.frozen_owner_candidate_vs_baseline.ro.v1',
  checked_at_utc=dt.datetime.now(UTC).isoformat(),
  source='BROKER_NATIVE_CLOSED_TRADES_AFTER_IMMUTABLE_PRE_ENTRY_MODEL_CHECKPOINT',
  source_owner_count=4,
  all_valid_trades_have_model_snapshot_before_entry=True,
  caveat='Retrospective scoring with a model frozen before each entry, not an actual live order or prospective logged forecast',
  live_trading_modified=False,model_promotion_allowed=False,model_promoted=False,
  risk_policy_modified=False,broker_orders_sent=0,
  totals=totals,owners=report)
 atomic(VALID/'latest.json',result)
 print(json.dumps(dict(totals=totals,per_owner={k:dict(status=v['status'],
   n=v.get('post_checkpoint_scored'),brier=v.get('candidate',{}).get('brier'),
   baseline_brier=v.get('baseline',{}).get('brier'))
   for k,v in report.items()}),sort_keys=True))
if __name__=='__main__':run()
