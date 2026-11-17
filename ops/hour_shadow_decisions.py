#!/usr/bin/env python3
"""Account-bound FP V9 inference ↔ verified losing-hour SHADOW join. No orders."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from zoneinfo import ZoneInfo
REPORT=Path("/var/lib/abutron-v9-session-loss/latest.json")
INFERENCE=Path("/var/lib/abutron-v9-brain/decisions/fp-demo-authoritative-latest.json")
OUTPUT=Path("/var/lib/abutron-v9-hour-shadow/latest.json")
UTC=dt.timezone.utc
SAST=ZoneInfo("Africa/Johannesburg")
def stamp(value):
 t=dt.datetime.fromisoformat(str(value).replace("Z","+00:00"))
 if t.tzinfo is None:raise ValueError("UNTRUSTED_TIMEZONE")
 return t.astimezone(UTC)
def read(path,now,age):
 st=path.lstat()
 if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_nlink!=1 or st.st_mode&0o077 or st.st_size>500000:
  raise ValueError("UNTRUSTED_SOURCE_OWNER_OR_PERMISSIONS")
 raw=path.read_bytes()
 if len(raw)!=st.st_size:raise ValueError("SOURCE_RACE")
 j=json.loads(raw)
 if not -10<=(now-stamp(j["generated_utc"])).total_seconds()<=age:
  raise ValueError("SOURCE_STALE")
 return j,hashlib.sha256(raw).hexdigest()
def join(report,inference,now):
 if (report.get("schema")!="abutron.v9.broker_hour_loss_shadow.v1" or
     report.get("timezone")!="Africa/Johannesburg" or
     report.get("production_veto_enabled") is not False or
     report.get("model_promotion_allowed") is not False or
     report.get("broker_orders")!=0):
  raise ValueError("HOUR_RESEARCH_IDENTITY_NOT_VERIFIED")
 if (inference.get("schema")!="abutron.v9.fpmarkets.demo.native.account_trained_m5.v1" or
     inference.get("trained") is not True or inference.get("model_features")!=49 or
     inference.get("orders_submitted")!=0):
  raise ValueError("NATIVE_INFERENCE_NOT_VERIFIED")
 accounts=inference.get("accounts",{})
 if set(accounts)!={"0512","1636"}:raise ValueError("ACCOUNT_SCOPE_UNVERIFIED")
 hour=now.astimezone(SAST).hour
 flags={}
 for h in report.get("account_hours",[]):
  key=h.get("account"),h.get("hour_sast")
  if key[0] not in accounts or type(key[1]) is not int or not 0<=key[1]<=23 or key in flags:
   raise ValueError("DUPLICATE_OR_UNKNOWN_HOUR_OWNER")
  if h.get("production_veto_enabled") is not False or type(h.get("shadow_avoid_hour")) is not bool:
   raise ValueError("UNVERIFIED_HOUR_VETO")
  flags[key]=h["shadow_avoid_hour"]
 signals=[]
 for account,payload in sorted(accounts.items()):
  if (payload.get("broker")!="FP Markets" or payload.get("environment")!="DEMO" or
      payload.get("scope_certified") is not True or payload.get("symbols_current")!=13 or
      len(payload.get("symbols",[]))!=13):
   raise ValueError("UNVERIFIED_NATIVE_ACCOUNT_FEED")
  seen=set()
  for s in payload["symbols"]:
   sym=s.get("symbol"); direction=s.get("model_direction")
   if (not sym or sym in seen or direction not in ("BUY","SELL","HOLD")
       or s.get("environment")!="DEMO" or s.get("broker")!="FP Markets"):
    raise ValueError("INVALID_NATIVE_SYMBOL_OR_DIRECTION")
   seen.add(sym)
   if not -10<=now.timestamp()-float(s["bar_closed_ts"])<=620:
    raise ValueError("STALE_CLOSED_M5_SIGNAL")
   would_hold=bool(flags.get((account,hour),False) and direction!="HOLD")
   signals.append({"account":account,"symbol":sym,"brain_direction":direction,
    "hour_sast":hour,"research_would_hold":would_hold,
    "research_direction":"HOLD" if would_hold else direction,
    "production_direction_unchanged":True})
 return {"schema":"abutron.v9.fp_demo.hour_shadow_decisions.v1",
  "generated_utc":now.isoformat(),"hour_sast":hour,"signals":signals,
  "symbols_evaluated":len(signals),
  "research_would_hold_count":sum(s["research_would_hold"] for s in signals),
  "production_veto_enabled":False,"model_promotion_allowed":False,
  "production_model_modified":False,"broker_orders":0,
  "crm_modified":False,"live_accounts_modified":False}
def atomic(path,doc):
 path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 s=path.parent.stat()
 if s.st_uid!=0 or s.st_mode&0o077:raise ValueError("UNTRUSTED_OUTPUT_DIRECTORY")
 fd,tmp=tempfile.mkstemp(prefix=".hour-shadow-",dir=path.parent)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,"w") as f:
   json.dump(doc,f,sort_keys=True,indent=2)
   f.write("\n");f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def main():
 now=dt.datetime.now(UTC)
 r,rh=read(REPORT,now,900)
 i,ih=read(INFERENCE,now,200)
 j=join(r,i,now)
 j["hour_report_sha256"]=rh;j["native_inference_sha256"]=ih
 atomic(OUTPUT,j)
 print("V9_HOURLY_SHADOW","symbols",j["symbols_evaluated"],
       "would_hold",j["research_would_hold_count"],"orders",0,flush=True)
if __name__=="__main__":main()