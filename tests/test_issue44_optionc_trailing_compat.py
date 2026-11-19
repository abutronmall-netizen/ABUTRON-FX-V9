"""Issue #44: monotonic stops remain attributable to original Option C proof.

No source contract can replace actual signed MetaEditor compilation and
independent broker-authenticated SL/TP amendment read-back.
"""
from pathlib import Path
import math

ROOT=Path(__file__).resolve().parents[1]
EA=(ROOT/"mql5/AbutronFPNativeOrderBridgeV101.mq5").read_text()
RISK=(ROOT/"mql5/AdaptiveRiskProofV2.mqh").read_text()
TRAIL=(ROOT/"mql5/NativeChandelierTrailingV1.mqh").read_text()

def optionc_geometry(side,original_stop,new_stop,entry,target):
    if not all(math.isfinite(q) and q>0 for q in
               (original_stop,new_stop,entry,target)):
        return False
    return ((side=="BUY" and original_stop<entry<target and
             original_stop<=new_stop<target) or
            (side=="SELL" and target<entry<original_stop and
             target<new_stop<=original_stop))

def test_main_enables_strict_provenance_only_for_fp_bridge():
    assert EA.count("#define ABUTRON_FP_NATIVE_TRAILING_COMPAT")==1
    assert EA.index("#define ABUTRON_FP_NATIVE_TRAILING_COMPAT") < EA.index(
        '#include "AdaptiveRiskProofV2.mqh"')
    assert "#ifdef ABUTRON_FP_NATIVE_TRAILING_COMPAT" in RISK
    assert "if(MathAbs(stop-originalStop)>0.00000001)return false;" in RISK

def test_optionc_original_risk_verified_before_current_stop():
    assert 'OrderCalcProfit(typ,sym,vol,entry,originalStop,originalLoss)' in RISK
    assert 'OrderCalcProfit(typ,sym,vol,entry,stop,observedStopProfit)' in RISK
    assert "initialRisk=-originalLoss;" in RISK
    assert "initialRisk>budget+0.005" in RISK
    assert "reward<1.5*initialRisk" in RISK
    assert "observedStopProfit<originalLoss-0.001" in RISK
    assert "budget>originalEquity*0.001+0.00001" in RISK
    assert "cashRisk=MathMax(0.0,-observedStopProfit)" in RISK

def test_improved_stop_must_join_real_original_native_sltp_amendment():
    for phrase in ("PositionSelectByTicket(ticket)",
                   "POSITION_MAGIC)!=MAGIC", "POSITION_IDENTIFIER",
                   "trailing_proofs", "trailing_intents", "trailing_results",
                   "BROKER_SLTP_READBACK_CONFIRMED",
                   "OptionCNativeTrailProofForCurrentPosition",
                   "intent[8]!=a[5]", "result[6]!=\"1\"",
                   "originalStop<=oldStop", "originalStop>=oldStop"):
        assert phrase in RISK,phrase
    assert RISK.index("if(MathAbs(stop-originalStop)>0.00000001 &&") < RISK.index(
        "cashRisk=MathMax(0.0,-observedStopProfit)")

def test_breakeven_or_better_stops_not_rejected_by_old_geometry():
    assert "sl<tp&&entry<tp" in EA
    assert "tp<sl&&tp<entry" in EA
    assert "sl<entry&&entry<tp" not in EA
    assert "tp<entry&&entry<sl" not in EA
    assert "OptionCAnyVerifiedPosition(" in EA
    assert "OptionCVerifyOpenPosition(" in EA

def test_buy_no_wider_stop_even_above_breakeven():
    assert optionc_geometry("BUY",1.098,1.098,1.100,1.110)
    assert optionc_geometry("BUY",1.098,1.100,1.100,1.110)
    assert optionc_geometry("BUY",1.098,1.105,1.100,1.110)
    for updated in (1.097,1.110,1.120):
        assert not optionc_geometry("BUY",1.098,updated,1.1,1.11)

def test_sell_no_wider_stop_even_below_breakeven():
    assert optionc_geometry("SELL",25757.55,25757.55,25706.65,25604.85)
    assert optionc_geometry("SELL",25757.55,25706.65,25706.65,25604.85)
    assert optionc_geometry("SELL",25757.55,25680,25706.65,25604.85)
    for updated in (25780,25757.6,25500):
        assert not optionc_geometry("SELL",25757.55,updated,25706.65,25604.85)

def test_invalid_options_are_closed():
    for side in ("HOLD","","SELL","BUY"):
        assert not optionc_geometry(side,1.1,float("nan"),1.2,1.4)
        assert not optionc_geometry(side,1.1,0,1.2,1.4)
        assert not optionc_geometry(side,0,1.2,1.2,1.4)

def test_trailing_is_broker_modification_not_order_creation():
    assert "req.action=TRADE_ACTION_SLTP" in TRAIL
    assert "TRADE_ACTION_DEAL" not in TRAIL
    assert "req.tp=oldTake" in TRAIL
    assert "NativeTrailPendingOrUnverified()" in TRAIL
    assert 'input bool EnableNativeChandelierTrailing = false;' in EA

def test_native_evidence_remains_not_authoritative_certification():
    assert "OPEN_PROTECTED" in EA and "OPEN_UNPROTECTED" in EA
    assert "BROKER_SLTP_READBACK_CONFIRMED" in RISK
    assert "originalStop<=oldStop" in RISK and "originalStop>=oldStop" in RISK

def test_native_qa_readiness_exposes_real_chandelier_input_separately():
    assert "ABUTRON_FP_TRAILING_READY_V1_" in EA
    assert "APEX_FP_NATIVE_TRAILING_READY_V1" in EA
    assert "IntegerToString((long)EnableNativeChandelierTrailing)" in EA
    assert '(VerifiedDemoAccount()?"DEMO_VERIFIED":"UNVERIFIED")' in EA
    assert "FP_NATIVE_BRIDGE_READY_V2" in EA