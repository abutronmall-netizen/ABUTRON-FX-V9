"""Account-bound DEMO native HMAC handoff smoke.

Intentionally uses less than broker minimum lots so the MQL5 EA rejects
BEFORE OrderCheck/OrderSend. Never puts a real order at risk.
"""
from __future__ import annotations

import json
import math
import secrets
from pathlib import Path

from abutron.crypto import hmac_sign
from abutron.execution.native_file_handoff import NativeFileHandoff
from abutron.execution.writer_gateway import SingleWriterGateway, _canonical_order

CID = __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_6207"]
LOGIN = int(__import__("os").environ["ABUTRON_DERIV_DEMO_LOGIN"])
FILES = "/var/lib/private/abutron-pr37-direct-writer-demo6207-qa/terminal/MQL5/Files"
COST = "/var/lib/abutron-v4-broker-crm-supervisor-6207/v4-native-execution-cost-6207.json"


def main():
    data=json.loads(Path(COST).read_text())
    symbol="Volatility 50 Index.0"
    spec=data["instruments"][symbol]
    minimum=float(spec["min_lots"])
    volume=minimum/1000.0
    assert math.isfinite(minimum) and minimum>0
    assert 0<volume<minimum
    price=float(spec["ask"])
    assert price>0
    cid=CID
    secret=SingleWriterGateway.read_existing_secret(FILES)
    request_id="ABUTRON_REQ_"+secrets.token_hex(16)
    candidate=dict(candidate_id="QA_NATIVE_HANDOFF_MINLOT_REJECT",
                   symbol=symbol,direction="BUY",volume=volume,
                   price=price,stop_loss=price*0.99,
                   take_profit=price*1.02,entry_type="CONFIRM",
                   trigger_price=price,trail_k=2.0,trail_stop=0.0)
    request=dict(request_id=request_id,connection_id=cid,
                 mt5_login=LOGIN,**candidate)
    request["activation_revision"]=hmac_sign(secret,cid+"|0")
    request["signed"]=hmac_sign(
        secret,_canonical_order(request_id,candidate,cid))
    state,receipt=NativeFileHandoff(
        FILES,connection_id=cid,mt5_login=LOGIN).send(
            request,timeout=14)
    reason=receipt.get("reason","") if isinstance(receipt,dict) else ""
    print("DEMO_NATIVE_HANDOFF_RESULT",state)
    print("DEMO_EA_REASON",reason[:100])
    print("BELOW_MIN_LOT_REJECT_REQUIRED",minimum>volume)
    print("BROKER_ORDERS_SUBMITTED_BY_SMOKE",0)
    if state!="REJECTED" or reason!="FP_MIN_LOT_EXCEEDS_RISK":
        raise SystemExit(2)


if __name__=="__main__":
    main()
