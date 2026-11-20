#!/usr/bin/env python3
# Migration export: CRM connection IDs are supplied by per-account environment variables.
"""ABUTRON V9 authentic FP DEMO broker-closed-trade feedback, no orders.

Only accepts account-bound V101 native deal exports paired with independently
reconciled broker receipts. Reconstructs fully settled positions and writes
a root-only, deterministic cumulative training input. Does NOT call MT5.
"""
from __future__ import annotations
import csv, datetime as dt, decimal, hashlib, io, json, math, os
from pathlib import Path
import stat, tempfile, time

ROOT=Path("/var/lib/abutron-v9-realtrade-feedback")
PUBLISHED=Path("/var/lib/apex-signup/fp-native-history")
ACCOUNTS={
 "0512":(__import__("os").environ["ABUTRON_CRM_CONNECTION_ID_0512"],Path("/var/lib/abutron-fp-execution/reconciliation/latest.json")),
 "1636":(__import__("os").environ["ABUTRON_CRM_CONNECTION_ID_1636"],Path("/var/lib/abutron-fp-execution/1636/reconciliation/latest.json")),
}
TRAINING_CUTOFF="2026-11-16T00:00:00+00:00" # pinned trade-history source dated 2026-11-15; include Nov 16 broker closes
FIELDS=["broker","account","position_id","event_id","symbol","direction","entry_time",
        "exit_time","entry_price","exit_price","lots","pnl","entry_type","entry_quality"]
D=decimal.Decimal
def ensure(b,code):
 if not b:raise ValueError(code)
def root_json(path,uid=None,maxsize=5000000,maxage=195):
 ensure(not path.is_symlink(),"SYMLINK_INPUT")
 fd=os.open(str(path),os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  s=os.fstat(fd)
  ensure(stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_size>15
         and s.st_size<maxsize and not(s.st_mode&0o007)
         and not(s.st_mode&0o022) and (uid is None or s.st_uid==uid)
         and -10<=time.time()-s.st_mtime<=maxage,"SOURCE_UNTRUSTED_OR_STALE")
  raw=os.read(fd,maxsize+1)
  e=os.fstat(fd)
  ensure(len(raw)==s.st_size and (s.st_ino,s.st_size,s.st_mtime_ns)==
         (e.st_ino,e.st_size,e.st_mtime_ns),"SOURCE_RACE")
  return json.loads(raw)
 finally:os.close(fd)
def safe_dec(value):
 try:
  v=D(str(value))
  ensure(v.is_finite(),"NONFINITE_BROKER_AMOUNT")
  return v
 except (decimal.InvalidOperation,TypeError,ValueError):
  raise ValueError("BROKER_NUMBER_INVALID") from None
def utc(value):
 stamp=dt.datetime.fromisoformat(str(value).replace("Z","+00:00"))
 ensure(stamp.tzinfo is not None,"BROKER_TIME_INVALID")
 return stamp.astimezone(dt.timezone.utc).isoformat()
def atomic_bytes(path,body):
 fd,tmp=tempfile.mkstemp(prefix="."+path.name+"-",dir=str(path.parent))
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,"wb") as f:
   f.write(body);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.lexists(tmp):os.unlink(tmp)
def canonical_csv(rows):
 out=io.StringIO(newline="")
 w=csv.DictWriter(out,fieldnames=FIELDS,lineterminator="\n",extrasaction="ignore")
 w.writeheader()
 for row in rows:w.writerow(row)
 return out.getvalue().encode("utf-8")
def from_account(suffix,cid,recon_path):
 snap=root_json(PUBLISHED/(cid+".json"),uid=985,maxsize=4000000)
 recon=root_json(recon_path,uid=0,maxsize=100000,maxage=155)
 # A genuine BLOCKED broker reconciliation is not a cross-file SHA race.
 # Do not waste the bounded SHA retry window on unverified native receipts.
 ensure(recon.get("status")=="RECONCILED_READONLY",
        "BROKER_RECONCILIATION_NOT_CERTIFIED")
 ensure(snap.get("schema")=="abutron.fpmarkets.native.account_history.readonly.v1"
        and snap.get("broker")=="FP Markets" and snap.get("environment")=="DEMO"
        and snap.get("account_masked")=="••••"+suffix
        and snap.get("account_currency")=="USD"
        and snap.get("history_verified_from_mt5") is True
        and snap.get("history_complete_within_selected_window") is True
        and snap.get("execution_enabled") is False
        and snap.get("orders_submitted_by_exporter")==0,
        "NATIVE_FP_SOURCE_IDENTITY_NOT_VERIFIED")
 ensure(recon.get("broker")=="FP Markets" and recon.get("environment")=="DEMO"
        and recon.get("account_masked")=="••••"+suffix
        and recon.get("status")=="RECONCILED_READONLY"
        and recon.get("broker_history_verified") is True
        and recon.get("history_sha256")==snap.get("native_file_sha256")
        and recon.get("broker_trade_deals_90d")==len(snap.get("trade_deals",[]))
        and recon.get("broker_open_positions")==snap.get("open_positions_count"),
        "BROKER_RECONCILIATION_OR_SOURCE_SHA_UNVERIFIED")
 tstamp=utc(snap["generated_at_utc"])
 ensure(-10<time.time()-dt.datetime.fromisoformat(tstamp).timestamp()<165,
        "NATIVE_BROKER_EXPORT_EXPIRED")
 rows=snap.get("trade_deals")
 ensure(isinstance(rows,list) and len(rows)>0 and len(rows)<8000
        and len(rows)==snap.get("trade_deals_count"),"BROKER_DEAL_COUNT_UNVERIFIED")
 groups={};tickets=set(); in_count=0; out_count=0
 for row in rows:
  ensure(isinstance(row,dict),"BROKER_DEAL_NOT_OBJECT")
  ticket=str(row["ticket"]);pos=str(row["position_id"])
  ensure(ticket.isdigit() and int(ticket)>0 and ticket not in tickets
         and pos.isdigit() and int(pos)>0,"BROKER_DEAL_TICKET_INVALID_OR_DUPLICATE")
  tickets.add(ticket)
  kind=row["entry"]
  ensure(kind in ("IN","OUT"),"PARTIAL_OR_REVERSAL_ENTRY_UNSUPPORTED")
  v=safe_dec(row["volume"]);price=safe_dec(row["price"])
  ensure(v>0 and price>0 and row.get("side") in ("BUY","SELL"),
         "BROKER_DEAL_GEOMETRY_INVALID")
  cash=safe_dec(row["net_cashflow_usd"])
  when=utc(row["time_utc"])
  groups.setdefault(pos,[]).append((kind,v,price,cash,when,row["side"],row["symbol"],ticket))
  if kind=="IN":in_count+=1
  else:out_count+=1
 ensure(in_count+out_count==len(rows) and
        out_count==snap.get("closing_deals_count"),"BROKER_DEAL_IN_OUT_MISMATCH")
 completed=[];partial=0
 for pos, events in groups.items():
  events.sort(key=lambda e:(e[4],int(e[7])))
  ins=[r for r in events if r[0]=="IN"]
  outs=[r for r in events if r[0]=="OUT"]
  if not ins or not outs:
   partial+=1
   continue
  sym=ins[0][6];direction=ins[0][5]
  ensure(all(x[6]==sym and x[5]==direction for x in ins)
         and all(x[6]==sym and x[5]!=direction for x in outs),
         "POSITION_DEAL_SYMBOL_OR_SIDE_MISMATCH")
  in_vol=sum((r[1] for r in ins),D(0))
  out_vol=sum((r[1] for r in outs),D(0))
  ensure(out_vol<=in_vol+D("0.000001"),"BROKER_EXIT_EXCEEDS_ENTRY_VOLUME")
  if abs(in_vol-out_vol)>D("0.000001"):
   partial+=1
   continue
  opened=min(r[4] for r in ins);closed=max(r[4] for r in outs)
  ensure(opened<=closed,"CLOSE_BEFORE_ENTRY")
  weighted_open=sum((r[1]*r[2] for r in ins),D(0))/in_vol
  weighted_close=sum((r[1]*r[2] for r in outs),D(0))/out_vol
  pnl=sum((r[3] for r in events),D(0))
  close_ticket=max(int(x[7]) for x in outs)
  event_id=hashlib.sha256(("FP Markets|DEMO|"+cid+"|"+pos+"|"+str(close_ticket)).encode()).hexdigest()[:40]
  completed.append(dict(
    broker="FP Markets",account=suffix,position_id=pos,event_id=event_id,
    symbol=sym,direction=direction,entry_time=opened,exit_time=closed,
    entry_price=format(weighted_open,"f"),exit_price=format(weighted_close,"f"),
    lots=format(in_vol,"f"),pnl=format(pnl,"f"),
    entry_type="BROKER_MARKET_CONFIRMED",entry_quality="0.5",
  ))
 ensure(len(completed)<=out_count,"CLOSE_COUNT_IMPOSSIBLE")
 return completed,dict(
     native_snapshot_timestamp=tstamp,native_source_sha256=snap["native_file_sha256"],
     authenticated_trade_deals=len(rows),closed_positions_paired=len(completed),
     unpaired_or_still_open_positions=partial,
     broker_reconciliation_source=recon_path.name,
     broker_receipt_acknowledged_count=recon.get("broker_verified_ack_count"),
 )
def from_account_with_bounded_reconciliation_retry(suffix,cid,recon_path):
 # Native exports and the independent reconcilers publish atomically, but not
 # as one transaction. Retry only the known cross-file snapshot race.
 # Every successful attempt still passes all original SHA, broker, owner,
 # history, timestamp and deal checks in from_account().
 # FP V101 history publishes every ~15s; independent reconcilers refresh
 # every ~20-25s. A 3s retry cannot bridge a normal two-file rollover.
 # Wait up to ~31s per account ONLY for this exact transient SHA mismatch.
 # Missing account proof and every other validation error still fail closed.
 for attempt in range(125):
  try:
   return from_account(suffix,cid,recon_path)
  except ValueError as exc:
   if str(exc)!="BROKER_RECONCILIATION_OR_SOURCE_SHA_UNVERIFIED" or attempt==124:
    raise
   time.sleep(0.25)

def build():
 rows=[];reports={}
 for suffix,(cid,path) in ACCOUNTS.items():
  one,meta=from_account_with_bounded_reconciliation_retry(suffix,cid,path)
  rows.extend(one);reports[suffix]=meta
 ensure(len({r["event_id"] for r in rows})==len(rows),"CLOSED_TRADE_EVENT_ID_DUPLICATED")
 rows.sort(key=lambda r:(r["exit_time"],r["event_id"]))
 # Trades included in the PINNED Nov 16 training are not used twice.
 new=[r for r in rows if r["exit_time"]>=TRAINING_CUTOFF]
 body=canonical_csv(new)
 manifest={
  "schema":"abutron.v9.broker_verified_closed_trade_feedback.v1",
  "generated_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
  "source":"AUTHENTICATED_FP_V101_NATIVE_DEALS_PLUS_INDEPENDENT_RECON",
  "model_training_cutoff_exclusive":TRAINING_CUTOFF,
  "all_closed_in_selected_native_windows":len(rows),
  "new_since_training_cutoff":len(new),
  "native_candidate_input_sha256":hashlib.sha256(body).hexdigest(),
  "native_candidate_input_path":str(ROOT/"new_since_v9_pinned.csv"),
  "accounts":reports,
  "order_submission_authority":False,"model_promotion_authority":False,
  "orders_submitted":0,"live_accounts_modified":False,
  "genuinely_closed_not_pending":True,
 }
 return body,manifest
if __name__=="__main__":
 ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
 ensure(ROOT.stat().st_uid==0 and
        stat.S_IMODE(ROOT.stat().st_mode)==0o700,"UNTRUSTED_FEEDBACK_ROOT")
 body,manifest=build()
 atomic_bytes(ROOT/"new_since_v9_pinned.csv",body)
 atomic_bytes(ROOT/"latest.json",(json.dumps(manifest,sort_keys=True,indent=2)+"\n").encode())
 print("ABUTRON_BROKER_CONFIRMED_REAL_TRADE_FEEDBACK",manifest["new_since_training_cutoff"],
       "new closed trades, total",manifest["all_closed_in_selected_native_windows"],
       "orders",0,"promotion_disabled",flush=True)
