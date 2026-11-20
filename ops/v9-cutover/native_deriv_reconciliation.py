#!/usr/bin/python3
"""Independent read-only Deriv DEMO 6207/7593/9244 native exposure + 90d deal proof.

Three authenticated MT5 native accounts and three separate no-order MT5 history
attestors. This evidence is NOT trading authorization, model qualification or
permission to claim unobserved broker orders.
"""
import collections
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

DB=Path('/var/lib/apex-signup/accounts.sqlite3')
P=Path('/var/lib/private')
DEST=Path('/var/lib/abutron-deriv-multi-native-reconciliation')
ACCOUNTS={'6207','7593','9244'}
COLUMNS=['ticket','position_id','order_id','time_utc','type','entry','reason',
         'magic','symbol','volume','price','profit','commission','fee','swap']
HEX32=re.compile(r'^[a-f0-9]{32}$')
def fresh_lines(path,now,limit):
    if path.is_symlink() or not path.is_file():
        raise ValueError('NATIVE_FILE_MISSING_OR_SYMLINK')
    stat=path.stat()
    if not 0 <= now-stat.st_mtime < limit or stat.st_size > 6000000:
        raise ValueError('NATIVE_FILE_EXPIRED_OR_OVERSIZED')
    return path.read_text(encoding='utf-8-sig').splitlines()

def v9_signed_protection(cid, login, server, *, ticket, symbol, side,
                         lots, entry, sl, tp, positions):
    """Use signed V9 broker proof only when old APEX proof is missing.

    Independent native DEAL history and the primary broker worker are
    already checked by one(); this is another identity-bound native
    source, never a guessed stop-risk or open-position count.
    """
    from abutron.reconciliation.native_crm_6207 import signed_native_exposure
    from abutron.execution.writer_gateway import SingleWriterGateway
    files = "/var/lib/private/abutron-pr37-direct-writer-demo6207-qa/terminal/MQL5/Files"
    proof, sha = signed_native_exposure(
        files, cid=cid, login=int(login), server=server,
        secret=SingleWriterGateway.read_existing_secret(files), ttl=70)
    if proof.get("position_count") != positions:
        raise ValueError("V9_BROKER_POSITION_COUNT_DISAGREEMENT")
    verified = [p for p in proof["positions"]
                if str(p["ticket"]) == ticket
                and p["symbol"] == symbol
                and p["direction"] == ("BUY" if side == "0" else "SELL")
                and abs(float(p["volume"]) - lots) < 0.00001
                and abs(float(p["entry"]) - entry) < 0.00001
                and abs(float(p["sl"]) - sl) < 0.00001
                and abs(float(p["tp"]) - tp) < 0.00001
                and p["ordercalcprofit_verified"] is True]
    if len(verified) != 1:
        raise ValueError("V9_NATIVE_PROTECTED_SL_TP_POSITION_MISMATCH")
    loss = verified[0]["stop_risk_usd"]
    if type(loss) not in (int, float) or not math.isfinite(loss) or loss < 0:
        raise ValueError("V9_NATIVE_STOP_RISK_INVALID")
    return float(loss)

def one(cid,login,server,now):
    suffix=str(login)[-4:]
    if suffix not in ACCOUNTS or not HEX32.fullmatch(cid) or server!='Deriv-Demo':
        raise ValueError('BROKER_ACCOUNT_NOT_IN_SCOPE')
    root=P/('abutron-client-mt5-'+cid)
    worker=json.loads((root/'worker-health.json').read_text())
    nonce=worker.get('nonce')
    if (not isinstance(nonce,str) or not HEX32.fullmatch(nonce)
        or worker.get('id')!=cid or worker.get('broker')!='Deriv'
        or worker.get('mode')!='DEMO' or worker.get('server')!=server
        or worker.get('status')!='CONNECTED' or
        worker.get('heartbeat_fresh') is not True
        or not 0<=now-float(worker.get('verified_at',0))<18):
        raise ValueError('PRIMARY_BROKER_WORKER_NOT_CURRENT')
    files=root/'terminal/MQL5/Files'
    ready=fresh_lines(files/('ABUTRON_APEX_BRIDGE_READY_V3_'+nonce+'.txt'),now,18)
    if (len(ready)!=11 or
        ready[:5]!=['APEX_NATIVE_BRIDGE_READY_V3',nonce,str(login),server,'DERIV_DEMO']
        or not 0<=now-int(ready[5])<18):
        raise ValueError('BROKER_BRIDGE_IDENTITY_INVALID')
    pending,positions=int(ready[6]),int(ready[7])
    if pending!=0 or not 0<=positions<=13:
        raise ValueError('NATIVE_PENDING_OR_POSITION_COUNT_INVALID')
    acct=fresh_lines(files/('ABUTRON_NATIVE_ACCOUNT_V2_'+nonce+'.txt'),now,18)
    if (len(acct)!=7+positions or acct[:3]!=['ABUTRON_NATIVE_ACCOUNT_V2',nonce,str(login)]
        or acct[5]!='USD' or acct[6]!=str(positions)):
        raise ValueError('BROKER_EQUITY_RECORD_INVALID')
    balance,equity=float(acct[3]),float(acct[4])
    if not all(math.isfinite(x) and x>0 for x in [equity,balance]):
        raise ValueError('NONPOSITIVE_BROKER_EQUITY')
    side=P/('abutron-native-readonly-mt5-'+suffix)
    s=json.loads((side/'worker-health.json').read_text())
    snonce=s.get('nonce')
    if (s.get('status')!='CONNECTED' or s.get('broker')!='Deriv'
        or s.get('environment')!='DEMO' or s.get('account_suffix')!=suffix
        or s.get('broker_session_identity_verified') is not True
        or s.get('native_history_source_authenticated') is not True
        or s.get('orders_submitted')!=0 or s.get('execution_authority') is not False
        or not 0<=now-float(s.get('broker_checked_utc',0))<45
        or not isinstance(snonce,str) or not HEX32.fullmatch(snonce)):
        raise ValueError('INDEPENDENT_NATIVE_BROKER_HISTORY_NOT_CURRENT')
    history=fresh_lines(side/'terminal/MQL5/Files'/
        ('ABUTRON_APEX_DEAL_HISTORY_V2_'+snonce+'.tsv'),now,150)
    if (len(history)<7 or
        history[:4]!=['ABUTRON_APEX_DEAL_HISTORY_V2',snonce,str(login),server]
        or history[5]!='DEMO' or history[6].split('\t')!=COLUMNS):
        raise ValueError('NATIVE_DEAL_HISTORY_SCHEMA_CHANGED')
    rows=[x.split('\t') for x in history[7:]]
    if len(rows)!=s.get('native_deal_export_count') or any(len(x)!=15 for x in rows):
        raise ValueError('NATIVE_HISTORY_INCOMPLETE')
    if len(set(x[0] for x in rows))!=len(rows):
        raise ValueError('DUPLICATED_BROKER_TICKET')
    opened,closed=collections.defaultdict(float),collections.defaultdict(float)
    for r in rows:
        typ,entry=int(r[4]),int(r[5])
        volume=float(r[9])
        if not math.isfinite(volume) or volume<0:
            raise ValueError('BROKER_VOLUME_INVALID')
        if typ>=2 or r[1]=='0':continue
        if volume<=0 or not r[1].isdecimal():
            raise ValueError('BROKER_POSITION_ID_OR_LOTS_INVALID')
        if entry==0:opened[r[1]]+=volume
        elif entry in (1,3):closed[r[1]]+=volume
        else:raise ValueError('UNSUPPORTED_BROKER_DEAL_TYPE')
    ids=set(opened)|set(closed)
    residual={k:round(opened[k]-closed[k],6) for k in ids
              if abs(opened[k]-closed[k])>=1e-5}
    if any(v<=0 for v in residual.values()) or len(residual)!=positions:
        raise ValueError('UNMATCHED_NATIVE_OPEN_EXIT_POSITION_ID')
    open_stop_risk_usd=0.0
    for line in acct[7:]:
        fields=line.split('\t')
        if len(fields)!=8 or not fields[0].isdigit() or fields[2] not in ('0','1'):
            raise ValueError('OPEN_NATIVE_POSITION_INVALID')
        ticket,symbol,side=fields[:3]
        lots,entry,sl,tp=map(float,fields[3:7])
        if (ticket not in residual or abs(residual[ticket]-lots)>0.00001
            or not all(math.isfinite(v) and v>0 for v in (lots,entry,sl,tp))
            or not ((sl<tp and entry<tp) if side=='0' else (tp<sl and tp<entry))):
            raise ValueError('OPEN_NATIVE_SL_TP_OR_VOLUME_INVALID')
        match=[r for r in rows if r[1]==ticket and int(r[5])==0]
        if (len(match)!=1 or match[0][8]!=symbol or int(match[0][4])!=int(side)
            or abs(float(match[0][9])-lots)>0.00001):
            raise ValueError('INDEPENDENT_BROKER_ENTRY_NOT_MATCHED')
        # Native protected evidence is produced after a real broker fill.
        # An existing broker-protected position can outlive its MT5 worker.
        # Preserve the previous session's authentic receipt, checking its old
        # nonce against THAT session's native bridge identity; never rewrite
        # it to the new nonce. Independently compare today's broker position,
        # native deal history, volume, entry, SL and TP above and below.
        proof_dirs=[(files/'ABUTRON/evidence',nonce)]
        if suffix=='6207':
            for prior in sorted(root.glob('terminal-retired-*'),
                                key=lambda q:q.stat().st_mtime,reverse=True)[:32]:
                if prior.is_symlink() or not prior.is_dir():continue
                priorfiles=prior/'MQL5/Files'
                readyfiles=list(priorfiles.glob('ABUTRON_APEX_BRIDGE_READY_V3_*.txt'))
                if len(readyfiles)!=1 or readyfiles[0].is_symlink():continue
                oldready=readyfiles[0].read_text(encoding='utf-8-sig').splitlines()
                if (len(oldready)!=11 or oldready[0]!='APEX_NATIVE_BRIDGE_READY_V3'
                    or not HEX32.fullmatch(oldready[1])
                    or oldready[2:5]!=[str(login),server,'DERIV_DEMO']):continue
                proof_dirs.append((priorfiles/'ABUTRON/evidence',oldready[1]))
        proofs=[]
        for proofdir,proof_nonce in proof_dirs:
            matches=[]
            for proofpath in proofdir.glob('*.txt'):
                if proofpath.is_symlink() or not proofpath.is_file():continue
                proof=proofpath.read_text(encoding='utf-8-sig').splitlines()
                if (len(proof)>=18 and proof[0]=='APEX_BROKER_POSITION_V2'
                    and proof[2:5]==[proof_nonce,str(login),server]
                    and proof[5:7]==[symbol,'BUY' if side=='0' else 'SELL']
                    and proof[9]==ticket and proof[16] in ('OPEN_PROTECTED','OPEN_UNPROTECTED')
                    and abs(float(proof[10])-lots)<0.00001
                    and abs(float(proof[11])-entry)<0.00001
                    and abs(float(proof[12])-sl)<0.00001
                    and abs(float(proof[13])-tp)<0.00001):matches.append(proof)
            if len(matches)>1:raise ValueError('NATIVE_PROTECTED_SL_TP_PROOF_NOT_UNIQUE')
            if matches:
                proofs=matches
                break
        if len(proofs)==1:
            loss=float(proofs[0][14])
        elif not proofs and suffix=='6207':
            loss=v9_signed_protection(
                cid,login,server,ticket=ticket,symbol=symbol,side=side,
                lots=lots,entry=entry,sl=sl,tp=tp,positions=positions)
        else:
            raise ValueError('NATIVE_PROTECTED_SL_TP_PROOF_NOT_UNIQUE')
        if (not math.isfinite(loss) or loss<0 or (loss==0 and not (sl>=entry if side=='0' else sl<=entry)) or loss>equity*(.002 if suffix=='6207' else .001)+0.02):
            raise ValueError('NATIVE_POSITION_RISK_OUTSIDE_ACCOUNT_POLICY')
        open_stop_risk_usd+=loss
    return {'account_suffix':suffix,'positions':positions,'pending_orders':0,
        'equity_usd':equity,'balance_usd':balance,
        'native_deals_in_export_window':len(rows),'matched_position_ids':len(ids),
        'open_position_usd_stop_risk':round(open_stop_risk_usd,5),
        'all_current_positions_sl_tp_usd_risk_verified':True,
        'current_exposure':('ZERO_DUAL_NATIVE_PROOF' if positions==0 else
                            'PROTECTED_NATIVE_POSITIONS_DUAL_PROOF'),
        'window_complete':False,'broker_execution_authorized':False}

def inspect(now=None):
    now=time.time() if now is None else float(now)
    with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=6) as db:
        db.execute('PRAGMA query_only=ON')
        data=db.execute("""SELECT id,account_reference,broker_server
          FROM apex_mt5_connections WHERE broker='Deriv'
          AND environment='DEMO' AND status='CONNECTED'""").fetchall()
    out=[]
    for suffix in sorted(ACCOUNTS):
        matches=[r for r in data if str(r[1]).endswith(suffix)]
        if len(matches)!=1:
            out.append({'account_suffix':suffix,'state':'BLOCKED_ACCOUNT_BINDING'})
            continue
        # MT5 writes account, bridge, and deal files independently. A single
        # non-atomic sampling instant may straddle their publication cycle.
        # Retry only fresh native reads; never accept a previous cached proof.
        for attempt in range(3):
            try:
                p=one(*matches[0],time.time())
                p['state']='VERIFIED_READONLY'
                out.append(p)
                break
            except (ValueError,TypeError,KeyError,OSError,UnicodeError,OverflowError) as exc:
                if attempt==2:
                    # Preserve failed state while exposing the actual native
                    # source failure rather than an uninformative generic flag.
                    out.append({'account_suffix':suffix,'state':'BLOCKED_NATIVE_PROOF',
                                'failure_code':type(exc).__name__+':'+str(exc)[:100]})
                else:
                    time.sleep(1)
    return {'schema':'abutron.deriv.three_demo.native_dualproof.readonly.v1',
        'generated_utc':dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),
        'accounts':out,'all_verified':all(x['state']=='VERIFIED_READONLY' for x in out),
        'customer_wide_risk_certified':False,'execution_authorized':False,
        'orders_submitted':0,'live_enabled_by_job':False,
        'complete_historical_reconciliation':False}

def main():
    j=inspect()
    if not DEST.is_dir() or DEST.is_symlink() or DEST.stat().st_uid!=0:
        raise ValueError('ROOT_PRIVATE_OUTPUT_DIR_REQUIRED')
    temp=DEST/('.new-'+str(os.getpid())+'.tmp')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w') as fp:
            json.dump(j,fp,sort_keys=True,allow_nan=False)
            fp.write('\n');fp.flush();os.fsync(fp.fileno())
        os.replace(temp,DEST/'latest.json')
    finally:temp.unlink(missing_ok=True)
    print(json.dumps({'all_verified':j['all_verified'],
        'accounts':[(x['account_suffix'],x['state']) for x in j['accounts']],
        'orders':0}))
    return 0 if j['all_verified'] else 1

if __name__=='__main__':sys.exit(main())
