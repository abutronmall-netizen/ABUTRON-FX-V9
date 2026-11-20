"""Authenticated native position stop/TP proof before writing CRM."""
import hashlib
import hmac
import json
import sqlite3
import time

import pytest
from abutron.reconciliation.native_crm_6207 import (
    NativeCRMReconcileError, signed_native_exposure,
    match_protective_order, record_reconciled_crm)


def native_source(tmp_path):
    secret="local-test-shared-secret"
    cid="f"*32
    login=32456207
    nonce="ABUTRON_NATIVE_"+str(login)+"_"+str(int(time.time()))
    pos=dict(ticket=123456,symbol="V50",direction="BUY",volume=0.5,
             entry=100.0,sl=99.0,tp=102.0,swap=0.0,stop_risk_usd=1.0,
             ordercalcprofit_verified=True)
    data=dict(nonce=nonce,connection_id=cid,login=str(login),
              server="Deriv-Demo",currency="USD",equity=10000.0,
              balance=10000.0,position_count=1,challenge="test-challenge",
              positions=[pos])
    canonical=(f"{cid}|{login}|Deriv-Demo|USD|10000.00|10000.00|"
               f"{nonce}|test-challenge|1|"
               f"123456:V50:BUY:0.50:100.00000:99.00000:102.00000:1.00:true")
    data["sig"]=hmac.new(secret.encode(),canonical.encode(),hashlib.sha256).hexdigest()
    folder=tmp_path/"MQL5"/"Files"
    folder.mkdir(parents=True)
    target=folder/("ABUTRON_NATIVE_EXPOSURE_V2_"+nonce+".txt")
    target.write_text(json.dumps(data),encoding="utf-8")
    return folder,data,secret,cid,login,target


def test_genuine_native_hmac_open_position_and_sl_tp_to_crm(tmp_path):
    folder,data,secret,cid,login,target=native_source(tmp_path)
    verified,sha=signed_native_exposure(str(folder),cid=cid,
                                        login=login,server="Deriv-Demo",
                                        secret=secret)
    result=match_protective_order(
        {"ticket":123456,"state":"ACK"},
        {"symbol":"V50","direction":"BUY","volume":0.5,
         "stop_loss":99.0,"take_profit":102.0},
        verified,tick_size=0.01)
    assert result["status"]=="BROKER_PROTECTED_SLTP"
    crm=str(tmp_path/"crm.sqlite3")
    record_reconciled_crm(crm,request_id="req-1",connection_id=cid,
                          symbol="V50",position=result,source_sha=sha)
    with sqlite3.connect(crm) as conn:
        item=conn.execute("SELECT request_id, ticket,state FROM abutron_v9_native_execution_receipts").fetchone()
    assert item==("req-1",123456,"BROKER_PROTECTED_SLTP")


def test_corrupt_native_HMAC_not_accepted(tmp_path):
    folder,data,secret,cid,login,target=native_source(tmp_path)
    data["positions"][0]["sl"]=95.0
    target.write_text(json.dumps(data))
    with pytest.raises(NativeCRMReconcileError, match="HMAC"):
        signed_native_exposure(str(folder),cid=cid,login=login,
                               server="Deriv-Demo",secret=secret)


@pytest.mark.parametrize("ticket,sl,tp,volume,expected", [
    (5,99.0,102.0,0.5,"BROKER_POSITION_PROOF_PENDING"),
    (123456,98.5,102.0,0.5,"BROKER_STOP_WIDENED"),
    (123456,99.0,0.0,0.5,"BROKER_PROTECTION_NOT_CONFIRMED"),
    (123456,99.0,102.0,0.6,"BROKER_VOLUME_MISMATCH"),
])
def test_reconcile_does_not_mark_unprotected_trade(tmp_path,
                                                    ticket,sl,tp,volume,expected):
    folder,data,secret,cid,login,target=native_source(tmp_path)
    data["positions"][0].update(sl=sl,tp=tp,volume=volume)
    result=match_protective_order(
        {"ticket":ticket,"state":"ACK"},
        {"symbol":"V50","direction":"BUY","volume":0.5,
         "stop_loss":99.0,"take_profit":102.0},
        data,tick_size=0.01)
    assert result["status"]==expected
    with pytest.raises(NativeCRMReconcileError,match="UNPROTECTED"):
        record_reconciled_crm(str(tmp_path/"crm.sqlite3"),
            request_id="bad",connection_id=cid,
            symbol="V50",position=result,source_sha="x"*64)


def test_late_native_ack_resolved_without_resending(tmp_path):
    from abutron.reconciliation.native_crm_6207 import reconcile_engine_receipts
    folder,data,secret,cid,login,target=native_source(tmp_path)
    folder.chmod(0o700)
    key=folder/"ABUTRON_ACTIVATION_SECRET.txt"
    key.write_text(secret)
    key.chmod(0o600)
    rid="ABUTRON_REQ_late_ack_0001"
    path=folder/"ABUTRON_NATIVE_RECEIPTS_V2_test.txt"
    path.write_text(json.dumps(dict(
        request_id=rid,login=login,state="ACK",ticket=123456,deal="14567")))
    class Engine:
        def __init__(self):self.updates=[];self.candidate_updates=[]
        def list_receipts(self,connection_id,limit):
            return [dict(receipt_id="r-1",candidate_id="c-1",
                  execution_state="UNKNOWN",reconciled_state="UNKNOWN",
                  reason_code="REQUEST_ID="+rid+";NATIVE_RECEIPT_TIMEOUT_NO_RETRY")]
        def get_candidate(self,candidate_id):
            return dict(candidate_id="c-1",symbol="V50",
                        direction="BUY",volume=0.5,stop_loss=99.,take_profit=102.)
        def update_receipt(self,receipt_id,**kwargs):
            self.updates.append((receipt_id,kwargs))
        def set_candidate_state(self,candidate_id,state,reason):
            self.candidate_updates.append((candidate_id,state,reason))
    e=Engine()
    crm=str(tmp_path/"real_crm.sqlite3")
    result=reconcile_engine_receipts(
        engine_db=e,crm_db=crm,cid=cid,login=login,
        server="Deriv-Demo",files_dir=str(folder),
        tick_sizes={"V50":0.01})
    assert result["protected"]==1 and result["unknown"]==0
    assert e.updates[0][1]["native_ticket"]==123456
    assert e.candidate_updates[0][1]=="ACK"
    with sqlite3.connect(crm) as db:
        r=db.execute("SELECT ticket,state FROM abutron_v9_native_execution_receipts").fetchone()
    assert r==(123456,"BROKER_PROTECTED_SLTP")
