#!/usr/bin/python3
"""ABUTRON V9: source-authenticated all-eight-account broker learning ledger.
Root-only; append-only native-deal identity, owner-isolated; closed trades only.
Ingestion never issues orders, changes CRM, or promotes a candidate brain.
"""
from __future__ import annotations
import csv,datetime as dt,hashlib,io,json,math,os,sqlite3,stat,sys,tempfile,time
from collections import defaultdict
from decimal import Decimal,InvalidOperation
from pathlib import Path

ROOT=Path("/var/lib/abutron-v9-eight-account-learning")
PRIVATE=Path("/var/lib/private")
FP=Path("/var/lib/apex-signup/fp-native-history")
CRM=Path("/var/lib/apex-signup/accounts.sqlite3")
DERIV_PROOF=Path("/var/lib/abutron-deriv-multi-native-reconciliation/latest.json")
DERIV_ALLTIME=Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/deriv-6207-native-fullrange-history.json")
LIVE_PROOF=Path("/var/lib/abutron-fp9392-live-fullrange-proof/latest.json")
EXACT={"6207":("Deriv","DEMO"),"7593":("Deriv","DEMO"),"9244":("Deriv","DEMO"),
       "0512":("FP Markets","DEMO"),"1636":("FP Markets","DEMO"),
       "1622":("FP Markets","DEMO"),"2582":("FP Markets","DEMO"),
       "9392":("FP Markets","LIVE")}
RECON={"0512":"/var/lib/abutron-fp-execution/reconciliation/latest.json",
       "1636":"/var/lib/abutron-fp-execution/1636/reconciliation/latest.json",
       "1622":"/var/lib/abutron-fp-execution/1622/reconciliation/latest.json",
       "2582":"/var/lib/abutron-fp-execution/2582/reconciliation/latest.json"}
FIELDS=["broker","account","position_id","event_id","trade_key","symbol","direction","entry_time",
        "exit_time","entry_price","exit_price","lots","pnl","entry_type","entry_quality"]
UTC=dt.timezone.utc
D=Decimal
def check(ok,reason):
    if not ok:raise ValueError(reason)
def money(x):
    try:
        d=D(str(x));check(d.is_finite(),"NONFINITE_BROKER_AMOUNT");return d
    except (InvalidOperation,TypeError):raise ValueError("INVALID_BROKER_AMOUNT") from None
def sec_bytes(path,age=180,limit=8000000):
    check(not path.is_symlink(),"SYMLINK_NATIVE_EVIDENCE")
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        s=os.fstat(fd)
        check(stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_size<limit
            and s.st_size>2 and not s.st_mode&0o022 and not s.st_mode&0o007
            and -8<=time.time()-s.st_mtime<=age,"STALE_OR_UNTRUSTED_NATIVE_EVIDENCE")
        raw=os.read(fd,limit+1);e=os.fstat(fd)
        check(len(raw)==s.st_size and (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)==
             (e.st_dev,e.st_ino,e.st_size,e.st_mtime_ns),"NATIVE_SOURCE_CHANGED_DURING_READ")
        return raw
    finally:os.close(fd)
def secure_json(path,age=180,limit=8000000):
    doc=json.loads(sec_bytes(Path(path),age,limit))
    check(type(doc)==dict,"SOURCE_NOT_JSON_OBJECT")
    return doc
def canon(row):
    return json.dumps(row,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def digest(b):
    if isinstance(b,str):b=b.encode()
    return hashlib.sha256(b).hexdigest()
def epoch(z):
    if isinstance(z,(int,float)):return int(z)
    stamp=dt.datetime.fromisoformat(str(z).replace("Z","+00:00"))
    check(stamp.tzinfo is not None,"NAIVE_BROKER_TIME")
    return int(stamp.timestamp())
def no_private_leaks_error(exc):
    msg=str(exc)
    return msg if len(msg)<=100 and all(c.isupper() or c.isdigit() or c=="_" for c in msg) else type(exc).__name__
def atomic(path,bytes_):
    fd,tmp=tempfile.mkstemp(prefix="."+path.name+".",dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"wb") as f:
            f.write(bytes_);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.lexists(tmp):os.unlink(tmp)
def to_csv(rows):
    f=io.StringIO(newline="")
    w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore",lineterminator="\n")
    w.writeheader()
    for row in sorted(rows,key=lambda z:(z["exit_time"],z["event_id"])):w.writerow(row)
    return f.getvalue().encode("utf-8")
def db_roster():
    with sqlite3.connect("file:"+str(CRM)+"?mode=ro",uri=True,timeout=10) as db:
        db.execute("PRAGMA query_only=ON")
        rows=db.execute("""SELECT x.id,x.account_id,x.account_reference,x.broker,
                        x.environment,x.status,coalesce(t.desired_enabled,0),
                        coalesce(t.effective_enabled,0) FROM apex_mt5_connections x
                        LEFT JOIN apex_trade_controls t ON t.connection_id=x.id""").fetchall()
    check(len(rows)==8,"UNEXPECTED_LINKED_ACCOUNTS")
    found={}
    for cid,owner,login,broker,mode,status,desired,effective in rows:
        suffix=str(login)[-4:]
        check(suffix in EXACT and (broker,mode)==EXACT[suffix] and suffix not in found
              and isinstance(cid,str) and len(cid)==32
              and isinstance(owner,str) and len(owner)==32,
              "CRM_ACCOUNT_IDENTITY_OR_OWNER_SCOPE_INVALID")
        if suffix=="9392":check((desired,effective)==(0,0),"LIVE_ACCOUNT_9392_NOT_OFF")
        found[suffix]=dict(cid=cid,ownerhash=digest(owner),login=str(login),broker=broker,
                           mode=mode,status=status)
    check(set(found)==set(EXACT),"ALL_EIGHT_CRM_ROSTER_CHANGED")
    return found
def normalized(ticket,order,position,symbol,side,entry,volume,price,pnl,when,kind):
    check(str(ticket).isdigit() and int(ticket)>0 and str(order).isdigit()
          and str(position).isdigit() and 0<int(when)<4102444800
          and kind in ("TRADE","CASHFLOW"),"INVALID_NATIVE_DEAL_ID_OR_TIME")
    if kind=="TRADE":
        check(int(position)>0 and side in ("BUY","SELL") and entry in ("IN","OUT")
              and symbol and len(symbol)<100 and money(volume)>0 and money(price)>0,
              "INVALID_NATIVE_TRADE_GEOMETRY")
    else:
        check(entry=="CASHFLOW","INVALID_FUNDING_DEAL_CLASS")
    return dict(ticket=str(ticket),order=str(order),position=str(position),
                symbol=str(symbol),side=side,entry=entry,
                volume=str(money(volume)),price=str(money(price)),
                net_cashflow=str(money(pnl)),time_utc=int(when),kind=kind)

def deriv(row):
    suffix=row["login"][-4:];cid=row["cid"]
    root=PRIVATE/("abutron-native-readonly-mt5-"+suffix)
    h=secure_json(root/"worker-health.json",age=90,limit=30000)
    nonce=h.get("nonce","")
    check(h.get("account_suffix")==suffix and h.get("status")=="CONNECTED"
       and h.get("broker")=="Deriv" and h.get("environment")=="DEMO"
       and h.get("native_history_source_authenticated") is True
       and h.get("execution_authority") is False
       and h.get("orders_submitted")==0
       and len(nonce)==32 and all(c in "0123456789abcdef" for c in nonce),
       "DERIV_NO_ORDER_BROKER_IDENTITY_UNVERIFIED")
    independent=secure_json(DERIV_PROOF,age=120)
    match=[r for r in independent.get("accounts",[]) if r.get("account_suffix")==suffix]
    check(independent.get("all_verified") is True and len(match)==1 and
          match[0].get("state")=="VERIFIED_READONLY","DERIV_INDEPENDENT_BROKER_AUDIT_UNVERIFIED")
    full=suffix=="6207"
    p=root/"terminal/MQL5/Files"/(
        ("ABUTRON_NATIVE_ALLTIME_V1_" if full else "ABUTRON_APEX_DEAL_HISTORY_V2_")+nonce+".tsv")
    raw=sec_bytes(p,age=170,limit=8000000)
    lines=raw.decode("utf-8-sig").splitlines()
    head=["ticket","position_id","order_id","time_utc","type","entry","reason",
          "magic","symbol","volume","price","profit","commission","fee","swap"]
    offset=10 if full else 7
    if full:
        check(lines[:5]==["ABUTRON_DERIV_NATIVE_ALLTIME_V1",nonce,row["login"],
                          "Deriv-Demo","DEMO"] and lines[7]=="0"
              and lines[9].split("\t")==head and lines[-1].startswith("END\t"),
              "DERIV_ALLTIME_NATIVE_SCHEMA_UNVERIFIED")
        meta=secure_json(DERIV_ALLTIME,age=160)
        check(meta.get("account_suffix")=="6207" and
              meta.get("full_range_cashflow_to_broker_balance_reconciled") is True
              and int(lines[8])==meta.get("native_full_range_deals"),
              "DERIV_NATIVE_FULLRANGE_VALIDATOR_UNVERIFIED")
        end=lines[-1].split("\t")
        check(len(end)==3 and end[1]==end[2] and len(lines[offset:-1])==int(end[1]),
              "DERIV_NATIVE_FULLRANGE_FOOTER_UNVERIFIED")
        dealrows=lines[offset:-1]
    else:
        check(lines[:4]==["ABUTRON_APEX_DEAL_HISTORY_V2",nonce,row["login"],"Deriv-Demo"]
              and lines[5]=="DEMO" and lines[6].split("\t")==head,
              "DERIV_NATIVE_WINDOW_SCHEMA_UNVERIFIED")
        dealrows=lines[offset:]
        check(len(dealrows)==h.get("native_deal_export_count")
              and len(dealrows)==match[0].get("native_deals_in_export_window"),
              "DERIV_NATIVE_WINDOW_RECONCILIATION_CHANGED")
    records=[];seen=set()
    for line in dealrows:
        cols=line.split("\t")
        check(len(cols)==15 and cols[0].isdigit(),"DERIV_BROKER_DEAL_FIELDS_INVALID")
        x=dict(zip(head,cols));typ=int(x["type"])
        check(typ in range(9),"UNSUPPORTED_DERIV_NATIVE_DEAL_TYPE")
        check(x["ticket"] not in seen,"DUPLICATE_BROKER_NATIVE_TICKET");seen.add(x["ticket"])
        entry={"0":"IN","1":"OUT","3":"OUT"}.get(x["entry"])
        check(typ>=2 or entry is not None,"UNSUPPORTED_DERIV_NATIVE_REVERSAL")
        cash=sum((money(x[v]) for v in ("profit","commission","fee","swap")),D(0))
        records.append(normalized(x["ticket"],x["order_id"],x["position_id"],
                       x["symbol"],"BUY" if typ==0 else "SELL" if typ==1 else "",
                       entry if typ<2 else "CASHFLOW",x["volume"],x["price"],
                       cash,int(x["time_utc"]),"TRADE" if typ<2 else "CASHFLOW"))
    return records,dict(source_scope="BROKER_EPOCH_ZERO_NATIVE_RETURN" if full else "SELECTED_NATIVE_WINDOW",
                        source_sha256=digest(raw),current_native_deals=len(records),
                        raw_broker_cashflow_rows=sum(x["kind"]=="CASHFLOW" for x in records))

def fp(row):
    suffix=row["login"][-4:];cid=row["cid"]
    p=FP/(cid+".json");raw=sec_bytes(p,age=155,limit=6000000);snap=json.loads(raw)
    recon=secure_json(RECON[suffix],age=160,limit=250000)
    h=secure_json(PRIVATE/("abutron-client-mt5-"+cid)/"worker-health.json",age=40,limit=30000)
    check(h.get("id")==cid and h.get("status")=="CONNECTED"
          and h.get("heartbeat_fresh") is True
          and h.get("broker")=="FP Markets" and h.get("mode")=="DEMO",
          "FP_NATIVE_ACCOUNT_WORKER_UNVERIFIED")
    check(snap.get("schema")=="abutron.fpmarkets.native.account_history.readonly.v1"
       and snap.get("account_masked")=="••••"+suffix
       and snap.get("account_currency")=="USD"
       and snap.get("broker")=="FP Markets" and snap.get("environment")=="DEMO"
       and snap.get("history_verified_from_mt5") is True
       and snap.get("history_complete_within_selected_window") is True
       and snap.get("orders_submitted_by_exporter")==0
       and snap.get("execution_enabled") is False,
       "FP_NATIVE_SELECTED_BROKER_HISTORY_UNVERIFIED")
    check(recon.get("schema")=="abutron.fpmarkets.execution.native_reconciliation.readonly.v1"
       and recon.get("account_masked")=="••••"+suffix
       and recon.get("status")=="RECONCILED_READONLY"
       and recon.get("broker_history_verified") is True
       and recon.get("bridge_proof_matches_broker") is True
       and recon.get("history_sha256")==snap.get("native_file_sha256")
       and recon.get("broker_trade_deals_90d")==snap.get("trade_deals_count")
       and recon.get("broker_open_positions")==snap.get("open_positions_count"),
       "FP_NATIVE_RECONCILER_SHA_OR_ACCOUNT_RACED")
    rawrows=snap.get("trade_deals")
    check(isinstance(rawrows,list) and len(rawrows)==snap.get("trade_deals_count")
          and len(rawrows)<9000,"FP_NATIVE_DEAL_TOTAL_INVALID")
    records=[];seen=set()
    for x in rawrows:
        check(isinstance(x,dict) and x.get("entry") in ("IN","OUT")
              and x.get("side") in ("BUY","SELL"),"FP_NATIVE_DEAL_ENTRY_UNSUPPORTED")
        ticket=str(x["ticket"]);check(ticket not in seen,"FP_DUPLICATE_NATIVE_TICKET")
        seen.add(ticket)
        net=sum((money(x[k]) for k in
            ("profit_usd","commission_usd","fee_usd","swap_usd")),D(0))
        check(abs(net-money(x["net_cashflow_usd"]))<=D(".025"),
              "FP_BROKER_CASHFLOW_COMPONENT_MISMATCH")
        records.append(normalized(ticket,x["order_id"],x["position_id"],x["symbol"],
                       x["side"],x["entry"],x["volume"],x["price"],net,epoch(x["time_utc"]),"TRADE"))
    check(snap.get("broker_deals_count",0)>=len(records),"FP_BROKER_TOTAL_COUNT_MISMATCH")
    return records,dict(source_scope="SELECTED_BROKER_90_DAY_WINDOW",
                        source_sha256=digest(raw),current_native_deals=snap["broker_deals_count"],
                        raw_broker_cashflow_rows=snap["broker_deals_count"]-len(records),
                        cashflow_details_stored_in_original_evidence_not_training=True)

def live(row):
    check(row["status"]=="CONNECTED","LIVE_READONLY_WORKER_DISCONNECTED")
    data=secure_json(LIVE_PROOF,age=150,limit=100000)
    check(data.get("schema")=="abutron.fp9392.live_native_epoch_zero_observer.ro.v1"
          and data.get("status")=="BROKER_NATIVE_EPOCH_ZERO_QUERY_VERIFIED"
          and data.get("broker_query_started_at_epoch_zero") is True
          and data.get("account_masked")=="••••9392"
          and data.get("environment")=="LIVE"
          and data.get("broker_returned_fullrange_deals")==0
          and data.get("current_positions_count")==0
          and data.get("current_pending_orders_count")==0
          and data.get("live_execution_authorized") is False
          and data.get("orders_submitted")==0,
          "LIVE_9392_SOURCE_OR_ZERO_DEAL_PROOF_MISSING")
    return [],dict(source_scope="NATIVE_EPOCH_ZERO_ZERO_RETURN_NOT_INCEPTION_CERT",
                   source_sha256=digest(canon(data)),current_native_deals=0,
                   raw_broker_cashflow_rows=0,live_trading_unchanged=True)

def stable_native_trade_key(broker,account,position_id,symbol,side,entry_time,entry_price,lots):
    """Stable between CRM reconnects; native position_id resolves identical entry collisions."""
    account=str(account)
    position=str(position_id)
    check(broker in ("Deriv","FP Markets") and len(account)==4 and account.isdigit()
          and position.isdigit() and int(position)>0
          and side in ("BUY","SELL") and isinstance(symbol,str) and len(symbol)>0,
          "BROKER_NATIVE_STABLE_TRADE_IDENTITY_INVALID")
    when=dt.datetime.fromisoformat(str(entry_time).replace("Z","+00:00"))
    check(when.tzinfo is not None,"BROKER_NATIVE_ENTRY_TIME_NO_TZ")
    parts=["MT5_NATIVE_POSITION_STABLE_V2",broker,account,position,symbol,side,
           when.astimezone(UTC).isoformat(timespec="microseconds"),
           format(money(entry_price).normalize(),"f"),
           format(money(lots).normalize(),"f")]
    return digest(canon(parts))

def make_closed(events,row,ownerhash):
    groups=defaultdict(list)
    for x in events:
        if x["kind"]=="TRADE":groups[x["position"]].append(x)
    closed=[];open_or_unmatched=0;not_supported=0
    for pid,seq in groups.items():
        seq.sort(key=lambda x:(x["time_utc"],int(x["ticket"])))
        ins=[x for x in seq if x["entry"]=="IN"]
        outs=[x for x in seq if x["entry"]=="OUT"]
        if not ins or not outs:
            open_or_unmatched+=1;continue
        side=ins[0]["side"];symbol=ins[0]["symbol"]
        if (any(x["symbol"]!=symbol or x["side"]!=side for x in ins)
            or any(x["symbol"]!=symbol or x["side"]==side for x in outs)):
            not_supported+=1;continue
        a=sum((money(x["volume"]) for x in ins),D(0))
        b=sum((money(x["volume"]) for x in outs),D(0))
        if a<=0 or b<=0 or a!=b:
            open_or_unmatched+=1;continue
        opened=min(x["time_utc"] for x in ins);ended=max(x["time_utc"] for x in outs)
        if opened>ended:
            not_supported+=1;continue
        # A same-position entry AFTER a partial closing deal cannot be
        # causally represented by one fixed pre-entry label. Reject it.
        first_exit_seen=False
        entry_after_exit=False
        for deal in seq:
            if deal["entry"]=="OUT":first_exit_seen=True
            elif deal["entry"]=="IN" and first_exit_seen:entry_after_exit=True
        if entry_after_exit:
            not_supported+=1
            continue
        op=sum((money(x["volume"])*money(x["price"]) for x in ins),D(0))/a
        cl=sum((money(x["volume"])*money(x["price"]) for x in outs),D(0))/b
        pnl=sum((money(x["net_cashflow"]) for x in seq),D(0))
        last_ticket=max(int(x["ticket"]) for x in outs)
        event=digest("|".join([ownerhash,row["cid"],pid,str(last_ticket),row["broker"],row["mode"]]))[:40]
        key=stable_native_trade_key(row["broker"],row["login"][-4:],pid,symbol,side,
                                    dt.datetime.fromtimestamp(opened,UTC).isoformat(),op,a)
        closed.append(dict(broker=row["broker"],account=row["login"][-4:],position_id=pid,
                           event_id=event,trade_key=key,symbol=symbol,direction=side,
                           entry_time=dt.datetime.fromtimestamp(opened,UTC).isoformat(),
                           exit_time=dt.datetime.fromtimestamp(ended,UTC).isoformat(),
                           entry_price=format(op,"f"),exit_price=format(cl,"f"),
                           lots=format(a,"f"),pnl=format(pnl,"f"),
                           entry_type="BROKER_NATIVE_PAIR_CONFIRMED",entry_quality="0.5"))
    return closed,open_or_unmatched,not_supported

def persist(row,records,proof):
    suffix=row["login"][-4:];ownerhash=row["ownerhash"]
    directory=ROOT/"owners"/ownerhash/suffix
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(directory,0o700)
    p=directory/"native-trade-history.sqlite3"
    db=sqlite3.connect(p,timeout=15)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("CREATE TABLE IF NOT EXISTS deals (ticket TEXT PRIMARY KEY,body TEXT NOT NULL,sha256 TEXT NOT NULL,first_seen_utc TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS evidence_sources (source_sha256 TEXT PRIMARY KEY,observed_utc TEXT NOT NULL,source_scope TEXT NOT NULL,native_rows INTEGER NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        previous={key:(sha,body) for key,sha,body in db.execute("SELECT ticket,sha256,body FROM deals")}
        new=0;now=dt.datetime.now(UTC).isoformat()
        for x in records:
            body=canon(x);sha=digest(body);ticket=x["ticket"]
            if ticket in previous:
                check(previous[ticket]==(sha,body),"IMMUTABLE_NATIVE_DEAL_TICKET_CHANGED")
                continue
            db.execute("INSERT INTO deals VALUES (?,?,?,?)",(ticket,body,sha,now))
            new+=1
        db.execute("INSERT OR IGNORE INTO evidence_sources VALUES (?,?,?,?)",
                   (proof["source_sha256"],now,proof["source_scope"],len(records)))
        allrows=[json.loads(r[0]) for r in db.execute("SELECT body FROM deals ORDER BY ticket")]
        closed,unmatched,unsupported=make_closed(allrows,row,ownerhash)
        db.commit()
    except:
        db.rollback();raise
    finally:db.close()
    os.chmod(p,0o600)
    body=to_csv(closed);atomic(directory/"closed_trades_verified.csv",body)
    summary=dict(broker=row["broker"],environment=row["mode"],account_masked="••••"+suffix,
        owner_hash=ownerhash,source_scope=proof["source_scope"],
        source_sha256=proof["source_sha256"],
        broker_native_rows_in_current_export=proof["current_native_deals"],
        accepted_native_trade_and_funding_rows_in_current_export=len(records),
        new_native_deal_rows_saved_this_cycle=new,
        distinct_native_deal_rows_archived=len(allrows),
        closed_positions_causally_paired=len(closed),
        unmatched_open_or_incomplete_positions=unmatched,
        unsupported_native_position_lifecycles=unsupported,
        closed_trades_csv_sha256=digest(body),
        no_independent_inception_or_equity_peak_claim=True,
        model_promotion_allowed=False,live_execution_changed=False,broker_orders_submitted=0)
    atomic(directory/"latest.json",(json.dumps(summary,sort_keys=True,indent=2)+"\n").encode())
    return summary

def selftest():
    account=dict(cid="a"*32,broker="Deriv",mode="DEMO",login="00006207")
    a=normalized("1","11","55","Volatility 10 Index.0","BUY","IN",1,100,-1,1000,"TRADE")
    b=normalized("2","22","55","Volatility 10 Index.0","SELL","OUT",1,110,7,1300,"TRADE")
    x,u,s=make_closed([b,a],account,"z"*64)
    assert len(x)==1 and u==0 and s==0 and D(x[0]["pnl"])==6
    assert x[0]["direction"]=="BUY" and x[0]["lots"]=="1"
    q,u,s=make_closed([a],account,"z"*64)
    assert q==[] and u==1
    try: money("NaN")
    except ValueError:pass
    else:raise AssertionError("NAN_MONEY_ACCEPTED")
    try:normalized("2","22","55","V","BUY","IN",-1,10,0,2000,"TRADE")
    except ValueError:pass
    else:raise AssertionError("NEGATIVE_VOLUME_ACCEPTED")
    print("ALL_EIGHT_ACCOUNT_LEARNING_ARCHIVE_SELFTEST_5_PASS")

def main():
    check(ROOT.is_dir() and ROOT.stat().st_uid==0 and stat.S_IMODE(ROOT.stat().st_mode)==0o700,
          "UNTRUSTED_LEARNING_ARCHIVE_ROOT")
    roster=db_roster();out={}
    for suffix,row in sorted(roster.items()):
        try:
            if suffix=="9392":records,proof=live(row)
            elif row["broker"]=="Deriv":records,proof=deriv(row)
            else:records,proof=fp(row)
            summary=persist(row,records,proof)
            out[suffix]=dict(status="NATIVE_DEALS_ARCHIVED",**summary)
        except (OSError,ValueError,KeyError,TypeError,UnicodeError,sqlite3.Error) as exc:
            out[suffix]=dict(account_masked="••••"+suffix,broker=row["broker"],
                             environment=row["mode"],status="NATIVE_SOURCE_UNVERIFIED",
                             reason_code=no_private_leaks_error(exc),broker_orders_submitted=0)
    status={
      "schema":"abutron.v9.eight_account.owner_isolated_native_learning_archive.ro.v1",
      "generated_utc":dt.datetime.now(UTC).isoformat(),
      "active_accounts_in_crm":8,
      "source_accounts_archived":sum(x["status"]=="NATIVE_DEALS_ARCHIVED" for x in out.values()),
      "accounts_with_training_events":sum(x.get("closed_positions_causally_paired",0)>0 for x in out.values()),
      "total_causally_closed_positions_in_private_archive":sum(x.get("closed_positions_causally_paired",0) for x in out.values()),
      "accounts":out,
      "customer_data_isolated_by_owner_hash":True,
      "broker_inception_history_certified":False,
      "broker_daystart_equity_or_peaks_certified":False,
      "live_9392_trading_switched_on":False,
      "production_brain_modified":False,
      "candidate_model_promoted":False,
      "crm_modified":False,"broker_orders_submitted":0}
    atomic(ROOT/"latest.json",(json.dumps(status,sort_keys=True,indent=2)+"\n").encode())
    print(json.dumps(dict(schema=status["schema"],source_accounts_archived=status["source_accounts_archived"],
          accounts_with_training_events=status["accounts_with_training_events"],
          closed_trades=status["total_causally_closed_positions_in_private_archive"],
          account_status={k:v["status"] for k,v in out.items()},
          no_broker_orders=status["broker_orders_submitted"]==0,
          production_brain_modified=False),sort_keys=True))
    return 0 if status["source_accounts_archived"]==8 else 2

if __name__=="__main__":
    if sys.argv[1:]==["--selftest"]:selftest()
    elif sys.argv[1:]:raise SystemExit("USAGE: --selftest or no args")
    else:sys.exit(main())
