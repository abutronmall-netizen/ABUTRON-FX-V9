#!/usr/bin/python3
"""Read-only, account-cross-verified FP broker M5 price augmentation for V9 candidates.
Never export private broker orders/trades; only market candles. Historical originals unchanged.
"""
from __future__ import annotations
import csv,gzip,hashlib,io,json,os,sqlite3,stat,tempfile,datetime as dt
from pathlib import Path
from collections import Counter,defaultdict
ROOT=Path('/var/lib/abutron-v9-eight-account-learning')
DB=Path('/var/lib/abutron-fp-native-m5-archive/broker-closed-m5.sqlite3')
OUT=ROOT/'market'/'FP-Markets'/'candles.csv.gz'
NATIVE_ORIG=ROOT/'market'/'FP-Markets'/'candles.csv.gz'
HEADER=['symbol','broker_symbol','timeframe','start_time_utc','open','high','low','close','broker_time_raw']
def chk(cond,reason):
 if not cond:raise ValueError(reason)
def digest(blob):return hashlib.sha256(blob).hexdigest()
def run():
 chk(DB.is_file() and DB.stat().st_uid==0 and stat.S_IMODE(DB.stat().st_mode)==0o600,
     'FP_NATIVE_M5_ARCHIVE_UNTRUSTED')
 with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=10) as db:
  db.execute('PRAGMA query_only=ON')
  chk(db.execute('PRAGMA quick_check').fetchone()[0]=='ok','FP_M5_SQLITE_INTEGRITY_FAILED')
  raw=list(db.execute('SELECT account,symbol,broker_symbol,start_time_utc,open,high,low,close,broker_time_raw FROM closed_m5'))
 chk(len(raw)>10000,'FOUR_ACCOUNT_NATIVE_M5_ARCHIVE_INSUFFICIENT')
 grouped=defaultdict(dict)
 by_account=Counter()
 for acct,sym,actual,ts,o,h,l,c,broker_raw in raw:
  chk(acct in ('0512','1636','1622','2582') and type(ts)==int and ts%300==0
      and broker_raw-ts==7200 and 0<l<=min(o,c)<=max(o,c)<=h,
      'FP_M5_ARCHIVE_IDENTITY_OR_GEOMETRY_INVALID')
  key=(actual,ts)
  chk(acct not in grouped[key],'DUPLICATE_M5_NATIVE_ACCOUNT_KEY')
  grouped[key][acct]=(sym,actual,ts,o,h,l,c,broker_raw)
  by_account[acct]+=1
 chk(set(by_account)=={'0512','1636','1622','2582'},'FP_NATIVE_FOUR_ACCOUNT_SCOPE_INCOMPLETE')
 counts=Counter();bad=Counter();per=defaultdict(Counter)
 for (symbol,t),members in grouped.items():
  primary=members.get('0512')
  if primary is None:continue
  for acct in ('1636','1622','2582'):
   alt=members.get(acct)
   if alt is not None:
    counts[acct]+=1;per[acct][symbol]+=1
    if primary[3:]!=alt[3:]:bad[acct]+=1
 chk(all(counts[a]>=60 for a in ('1636','1622','2582'))
     and sum(bad.values())==0,
     'CROSS_ACCOUNT_FP_NATIVE_M5_OVERLAP_CONFLICT')
 # Read the independently SHA-verified original historical FP source, not
 # this already augmented output, so each run is deterministic and no
 # original market snapshot is overwritten.
 source=Path('/var/lib/abutron-v9-brain/market-data/FP-Markets')
 manifest=json.loads((source/'MANIFEST.json').read_text())
 ds=next(d for d in manifest['datasets'] if d.get('file')=='candles.csv.gz')
 expected=ds['sha256']
 rawparts=bytearray()
 for part in ds['parts']:
  p=source/part['name'];data=p.read_bytes()
  chk(digest(data)==part['sha256'] and len(data)==part['size_bytes'],
      'FP_HISTORICAL_MARKET_PART_CHANGED')
  rawparts+=data
 chk(digest(rawparts)==expected,'FP_HISTORICAL_MARKET_SOURCE_CHANGED')
 records={}
 with gzip.GzipFile(fileobj=io.BytesIO(rawparts),mode='rb') as src:
  with io.TextIOWrapper(src,encoding='utf-8',newline='') as f:
   reader=csv.DictReader(f);chk(reader.fieldnames==HEADER,'FP_CANDLE_HEADER_CHANGED')
   for z in reader:
    if z['timeframe']!='M5':continue
    key=(z['broker_symbol'],int(z['start_time_utc']))
    if key in records:
     prv=records[key]
     chk(all(prv[k]==z[k] for k in HEADER),'FP_DUPLICATE_SOURCE_M5_CONFLICT')
    records[key]=z
 new=0;equal=0
 # Account-bound native closed M5 candles are used to bridge post-Nov13 gaps.
 # Price bytes can be shared only after 0 mismatches across >=60 overlapping
 # independent FPMarketsSC-Demo exports from other accounts.
 for (actual,ts),accounts in grouped.items():
  src=accounts.get('0512')
  if src is None:continue
  sym,actual,ts,o,h,l,c,broker_time=src
  rr=dict(symbol=sym,broker_symbol=actual,timeframe='M5',start_time_utc=str(ts),
   open=str(o),high=str(h),low=str(l),close=str(c),broker_time_raw=str(broker_time))
  key=actual,ts
  if key in records:
   old=records[key]
   chk(all(abs(float(old[v])-float(rr[v]))<=1e-8 for v in ('open','high','low','close')),
       'FP_PUBLIC_AND_NATIVE_M5_CONFLICT')
   equal+=1
  else:
   records[key]=rr;new+=1
 dest=OUT;temp=dest.with_name('.native-augmented-FP-market.tmp.gz')
 chk(not temp.exists(),'EXISTING_FP_MARKET_TEMP_CONFLICT')
 try:
  with open(temp,'wb') as file:
   os.fchmod(file.fileno(),0o600)
   with gzip.GzipFile(filename='',mode='wb',fileobj=file,mtime=0,compresslevel=6) as gz:
    with io.TextIOWrapper(gz,encoding='utf-8',newline='',write_through=True) as txt:
     writer=csv.DictWriter(txt,fieldnames=HEADER,lineterminator='\n')
     writer.writeheader()
     for row in sorted(records.values(),key=lambda x:(x['broker_symbol'],int(x['start_time_utc']))):
      writer.writerow(row)
  os.replace(temp,dest)
 finally:
  if temp.exists():temp.unlink()
 manifest=dict(schema='abutron.v9.fp_fouraccount_authenticated_m5_candidate_source.v1',
  checked_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
  validated_original_fp_market_sha256=expected,
  native_four_account_source_db_sha256=digest(DB.read_bytes()),
  overlapping_m5_checked=dict(counts),overlap_mismatches=dict(bad),
  added_native_broker_closed_M5_rows=new,public_native_equal_M5_rows=equal,
  combined_M5_rows=len(records),combined_candidate_market_sha256=digest(dest.read_bytes()),
  customer_trade_records_shared=False,raw_account_secrets_copied=False,
  promotion_allowed=False,live_orders_enabled=False)
 path=ROOT/'market'/'fp-native-four-account-m5-evidence.json'
 path.write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n');os.chmod(path,0o600)
 print(json.dumps({k:manifest[k] for k in ('overlapping_m5_checked','overlap_mismatches',
  'added_native_broker_closed_M5_rows','combined_M5_rows','customer_trade_records_shared')}))
if __name__=='__main__':run()
