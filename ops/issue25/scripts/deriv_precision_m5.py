#!/usr/bin/python3
"""V9 read-only Deriv broker-native M5 precision-history augmentation.
Compare append-only imported Deriv M5 with two-terminal cross-verified HCC
and current authenticated source. Modify candidate-only M5 + provenance.
No broker orders, CRM, production model changes or synthetic bars.
"""
from __future__ import annotations
import csv,datetime as dt,gzip,hashlib,io,json,math,os,sqlite3,stat,sys,tempfile
from pathlib import Path

ROOT=Path('/var/lib/abutron-v9-eight-account-learning')
MARKET=ROOT/'market'/'Deriv'/'candles.csv.gz'
REPORT=ROOT/'market'/'latest.json'
OUT=ROOT/'market'/'deriv-native-precision-m5-proof.json'
DB=Path('/var/lib/apex/precision-history/closed-candles.sqlite3')
LIVE=Path('/var/lib/apex/operator-live/native-market.json')
COLS=['symbol','timeframe','start_time_utc','open','high','low','close']
SYMS=[f'Volatility {v} Index.0' for v in (10,25,50,75,100)]
UTC=dt.timezone.utc
def _load_scanner():
    """Lazy-load apex_fast_scanner at runtime.

    Moved from import-time to runtime so the module can be imported
    for testing without the production apex package being present.
    """
    sys.path.insert(0,'/opt/apex/runtime')
    from apex_fast_scanner import validate_snapshot,utc_now
    return validate_snapshot,utc_now

def check(ok,reason):
 if not ok:raise ValueError(reason)
def sha(blob):return hashlib.sha256(blob).hexdigest()
def safe_file(path):
 p=Path(path);st=p.lstat()
 check(stat.S_ISREG(st.st_mode) and st.st_mode&0o022==0 and not p.is_symlink(),
   'UNTRUSTED_NATIVE_PRECISION_FILE')
 return p
def atomic(path,body):
 fd,t=tempfile.mkstemp(prefix='.deriv-precision-candidate-',dir=path.parent)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,'wb') as f:f.write(body);f.flush();os.fsync(f.fileno())
  os.replace(t,path)
 finally:
  if os.path.lexists(t):os.unlink(t)
def original_market():
 old={}
 with gzip.open(MARKET,'rt',encoding='utf-8',newline='') as f:
  r=csv.DictReader(f);check(r.fieldnames==COLS,'CANDIDATE_DERIV_BAR_SCHEMA_CHANGED')
  for x in r:
   check(x['timeframe']=='M5','NON_M5_CANDIDATE_BAR')
   key=x['symbol'],int(x['start_time_utc'])
   check(key not in old,'DUPLICATE_CANDIDATE_NATIVE_M5_KEY')
   old[key]=x
 return old
def main():
 safe_file(DB);safe_file(LIVE);safe_file(REPORT)
 dbstat=DB.stat();check(dbstat.st_uid==DB.parent.stat().st_uid,'PRECISION_WRITER_UID_UNVERIFIED')
 source=json.loads(LIVE.read_text())
 data=validate_snapshot(source,utc_now())
 check(source.get('schema')=='apex.mt5.market.v1'
   and source.get('source')=='Deriv DEMO MT5'
   and source.get('execution_enabled') is False,'CURRENT_DERIV_NATIVE_MARKET_NOT_NOORDER')
 check(set(SYMS).issubset(data),'CURRENT_DERIV_VOLATILITY_SOURCE_INCOMPLETE')
 old=original_market();ledger={}
 with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=10) as db:
  db.execute('PRAGMA query_only=ON')
  check(db.execute('PRAGMA quick_check').fetchone()[0]=='ok','BROKER_PRECISION_SQLITE_CORRUPT')
  newest=db.execute('SELECT imported_at_utc,broker_generated_at_utc,candles_added FROM imports ORDER BY imported_at_utc DESC LIMIT 1').fetchone()
  check(newest is not None and
    (utc_now()-dt.datetime.fromisoformat(newest[0])).total_seconds()<240,
    'BROKER_PRECISION_LATEST_IMPORT_NOT_CURRENT')
  for sym,t,o,h,l,c in db.execute('SELECT symbol,start_time_utc,open,high,low,close FROM candles WHERE timeframe=? AND symbol IN (?,?,?,?,?)',
                                  ('M5',*SYMS)):
   check(sym in SYMS and type(t) is int and t%300==0
     and t+300<=utc_now().timestamp()+1
     and all(math.isfinite(v) and v>0 for v in (o,h,l,c))
     and l<=min(o,c)<=max(o,c)<=h,'NATIVE_PRECISION_M5_GEOMETRY_FAILED')
   ledger[sym,t]=dict(symbol=sym,timeframe='M5',start_time_utc=str(t),
                      open=str(o),high=str(h),low=str(l),close=str(c))
  check(len(ledger)>20000,'BROKER_PRECISION_NATIVE_M5_TOO_FEW_BARS')
  import_latest=dt.datetime.fromisoformat(newest[0]).isoformat()
 current_counts={}
 for sym in SYMS:
  same=0;different=0
  for bar in data[sym]['M5']:
   key=sym,bar['time'];oldrow=ledger.get(key)
   if oldrow is None:continue
   if all(abs(float(oldrow[k])-float(bar[k]))<=1e-8*max(1,abs(float(bar[k])))
         for k in ('open','high','low','close')):
    same+=1
   else:different+=1
  check(same>=30 and different==0,'PRECISION_M5_CURRENT_NATIVE_BROKER_FEED_MISMATCH')
  current_counts[sym]=same
 overlap={sym:0 for sym in SYMS};new={sym:0 for sym in SYMS}
 for (sym,t),r in ledger.items():
  if (sym,t) in old:
   chk=old[sym,t]
   check(all(abs(float(chk[v])-float(r[v]))<=1e-8*max(1,abs(float(r[v])))
        for v in ('open','high','low','close')),
     'PRECISION_M5_HCC_HISTORICAL_OVERLAP_CONFLICT')
   overlap[sym]+=1
  else:new[sym]+=1
 check(all(overlap[sym]>=100 for sym in SYMS),'NATIVE_PRECISION_HCC_OVERLAP_LESS_THAN_100')
 combined=dict(old);combined.update(ledger)
 buff=io.BytesIO()
 with gzip.GzipFile(fileobj=buff,filename='',mode='wb',compresslevel=6,mtime=0) as z:
  with io.TextIOWrapper(z,encoding='utf-8',newline='',write_through=True) as f:
   w=csv.DictWriter(f,fieldnames=COLS,lineterminator='\n');w.writeheader()
   for _,r in sorted(combined.items(),key=lambda i:(i[0][0],i[0][1])):w.writerow(r)
 raw=buff.getvalue()
 check(len(raw)>1000,'EMPTY_CANDIDATE_PRECISION_MARKET')
 # data and source event metadata are root-private; no source files altered.
 atomic(MARKET,raw)
 result=dict(schema='abutron.v9.native_deriv_precision_broker_m5.verified_candidate.v1',
   checked_at_utc=dt.datetime.now(UTC).isoformat(),
   source_precision_db_sha256=sha(DB.read_bytes()),
   source_broker_native_current_market_sha256=sha(LIVE.read_bytes()),
   last_native_import_at_utc=import_latest,
   verified_current_broker_overlaps=current_counts,
   verified_historical_HCC_or_prior_market_overlaps=overlap,
   newly_recovered_precision_M5=new,
   new_total_M5=sum(new.values()),
   candidate_market_sha256=sha(raw),
   broker_orders_submitted=0,production_models_modified=False,
   live_trading_modified=False,model_promotion_allowed=False)
 old_report=json.loads(REPORT.read_text())
 check(old_report.get('schema')=='abutron.v9.manifest_verified_market_m5_candidate_input.v1',
    'DERIV_NATIVE_MARKET_MANIFEST_SCHEMA_CHANGED')
 old_report['brokers']['Deriv']['market_input_sha256']=sha(raw)
 old_report['brokers']['Deriv']['native_precision_extension_sha256']=sha(raw)
 atomic(REPORT,(json.dumps(old_report,sort_keys=True,indent=2)+'\n').encode())
 atomic(OUT,(json.dumps(result,sort_keys=True,indent=2)+'\n').encode())
 print(json.dumps({'added_per_volatility_symbol':new,'new_total_M5':sum(new.values()),
   'current_native_M5_overlap_verified':current_counts,'candidate_only':True,
   'orders_sent':0},sort_keys=True))
if __name__=='__main__':main()
