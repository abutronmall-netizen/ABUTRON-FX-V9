"""Issue #44 source-contract and negative risk tests.

These do NOT assert that a broker modification has executed. Actual
MetaEditor compilation and isolated DEMO terminal/broker read-back are
separate requirements before promoting any EX5 to the running chart.
"""
from pathlib import Path
import math
import re

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "mql5/AbutronFPNativeOrderBridgeV101.mq5").read_text()
TRAIL = (ROOT / "mql5/NativeChandelierTrailingV1.mqh").read_text()
CI = (ROOT / ".github/workflows/issue39-fp-native-evidence-compile.yml").read_text()


def strictly_safe_stop(side, old, new, entry, take, close, distance, tick):
    """Independent pure risk geometry; not native broker price confirmation."""
    if not all(math.isfinite(x) and x > 0 for x in
               (old, new, entry, take, close, distance, tick)):
        return False
    if side == "BUY":
        return (new >= old + 2 * tick - 1e-8 and
                new >= entry + tick - 1e-8 and new < take and
                new < close - distance - tick)
    if side == "SELL":
        return (new <= old - 2 * tick + 1e-8 and
                new <= entry - tick + 1e-8 and new > take and
                new > close + distance + tick)
    return False


def test_trailing_is_separate_default_off_and_included_only_in_existing_ea():
    assert MAIN.count("input bool EnableNativeChandelierTrailing = false;") == 1
    assert MAIN.count('#include "NativeChandelierTrailingV1.mqh"') == 1
    assert MAIN.count("NativeChandelierTrailing();") == 1
    assert MAIN.index("if(!feedOk)") < MAIN.index("NativeChandelierTrailing();")
    assert MAIN.index("WriteReadiness();UpdateEvidence();") < MAIN.index("NativeChandelierTrailing();")
    assert "NativeChandelierTrailingV1.mqh" in CI


def test_no_alternate_order_bridge_or_new_position_from_trailing():
    assert TRAIL.count("req.action=TRADE_ACTION_SLTP;") == 1
    assert "TRADE_ACTION_DEAL" not in TRAIL
    assert "req.position=ticket;" in TRAIL
    assert "req.tp=oldTake;" in TRAIL
    assert TRAIL.count("OrderSend(req,res)") == 1
    assert "APEX_FP_NATIVE_ORDER_V1.req" not in TRAIL
    assert "FileDelete(REQ)" not in TRAIL


def test_account_session_magic_and_safety_guards():
    for marker in (
        "EnableNativeChandelierTrailing", "VerifiedDemoAccount()",
        "TERMINAL_TRADE_ALLOWED", "MQL_TRADE_ALLOWED",
        "ACCOUNT_TRADE_ALLOWED", "POSITION_MAGIC)!=MAGIC",
        "ApprovedSymbol(symbol)", "OrdersTotal()!=0",
        "PositionGetInteger(POSITION_IDENTIFIER)",
        "PositionGetString(POSITION_SYMBOL)!=symbol",
        "PositionGetDouble(POSITION_SL)-oldStop",
        "gNonce", "gLogin", "gServer",
    ):
        assert marker in TRAIL


def test_completed_m5_ohlc_atr_and_broker_quote_freshness():
    for marker in (
        "CopyRates(symbol,PERIOD_M5,1,30,bars)",
        "TimeCurrent()-closedBar>610",
        "MathAbs((double)(TimeCurrent()-(long)tick.time))>20",
        "i>=16", "i>=8", "highest-3.0*atr", "lowest+3.0*atr",
        "SYMBOL_TRADE_STOPS_LEVEL", "SYMBOL_TRADE_FREEZE_LEVEL",
        "SYMBOL_TRADE_TICK_SIZE",
    ):
        assert marker in TRAIL


def test_at_least_one_r_and_no_wider_stop_requirements():
    for marker in (
        "floating<=0", "floating<MathMax(MathAbs(oldStopProfit)",
        "candidate<oldStop+2*ticksize",
        "candidate>oldStop-2*ticksize",
        "improvedStopProfit<=oldStopProfit",
        "OrderCalcProfit(side,symbol,volume,entry,quoteClose,floating)",
    ):
        assert marker in TRAIL


def test_durable_intent_before_broker_sltp_send():
    assert TRAIL.index("NativeTrailSaveOnce(DIR_TRAIL_INTENTS+stem") < TRAIL.index("bool sent=OrderSend(req,res);")
    assert TRAIL.index("bool sent=OrderSend(req,res);") < TRAIL.index("NativeTrailSaveOnce(DIR_TRAIL_RESULTS+stem")
    assert TRAIL.index("NativeTrailSaveOnce(DIR_TRAIL_RESULTS+stem") < TRAIL.index("NativeTrailReadBack(stem);")
    for marker in ("FILE_WRITE|FILE_TXT|FILE_ANSI",
                   "FileFlush(h);FileClose(h)", "FileIsExist(path)"):
        assert marker in TRAIL


def test_broker_ack_without_position_readback_is_not_proof():
    for marker in (
        "result=DIR_TRAIL_RESULTS+stem", "original=DIR_TRAIL_INTENTS+stem",
        'intent[0]!="APEX_FP_NATIVE_TRAIL_INTENT_V1"',
        "intent[10]!=newStr", "intent[11]!=tpStr",
        "PositionSelectByTicket(ticket)", "POSITION_IDENTIFIER",
        "TRADE_RETCODE_DONE", "MathAbs(nowSL-newStop)>ticksize*0.51",
        "MathAbs(nowTP-tp)>ticksize*0.51",
        "BROKER_SLTP_READBACK_CONFIRMED",
    ):
        assert marker in TRAIL
    assert TRAIL.index('if(!complete||intent[0]') < TRAIL.index("NativeTrailSaveOnce(proof,body)")


def test_unknown_or_replayed_amendment_never_auto_retried():
    assert "NativeTrailPendingOrUnverified()" in TRAIL
    assert "if(NativeTrailPendingOrUnverified())return;" in TRAIL
    assert 'FileIsExist(DIR_TRAIL_INTENTS+stem+".txt")' in TRAIL
    assert "if(!NativeTrailReadBack(stem))pending=true;" in TRAIL
    assert "if(checked>500){pending=true;break;}" in TRAIL
    assert "NativeTrailSaveOnce(proof,body)" in TRAIL


def test_production_original_entry_code_retained():
    for marker in ("req.action=TRADE_ACTION_DEAL",
                   "bool checked=OrderCheck(req,check);",
                   "bool sent=OrderSend(req,res);",
                   "Receipt(key,\"UNKNOWN\"",
                   "EnableNativeDemoOrders = false",
                   "AdaptiveV2ReconcilerApproved = false",
                   "SaveEvidenceScanOffset()",
                   "UpdateEvidence();"):
        assert marker in MAIN


def test_mql5_source_does_not_flip_live_execution():
    assert "ACCOUNT_TRADE_MODE_DEMO" in MAIN
    assert "FPMarketsSC-Demo" in MAIN
    assert "TRADE_ACTION_DEAL" not in TRAIL
    assert "EnableNativeChandelierTrailing = true" not in MAIN


def test_sell_stop_only_tightens_after_profit_at_broker_distance():
    assert strictly_safe_stop("SELL", 25757.55, 25700.00,
                              25706.65, 25604.85, 25680.0, 1.0, 0.1)
    for proposed in (25757.65, 25757.55, 25706.70, 25680.50, 25590.0):
        assert not strictly_safe_stop("SELL", 25757.55, proposed,
                                      25706.65, 25604.85, 25680.0, 1.0, 0.1)


def test_buy_stop_only_tightens_and_preserves_tp():
    assert strictly_safe_stop("BUY", 1.0980, 1.1001, 1.1000, 1.1100,
                              1.1020, 0.0003, 0.0001)
    for proposed in (1.0970, 1.0980, 1.0998, 1.1018, 1.1100):
        assert not strictly_safe_stop("BUY", 1.0980, proposed, 1.1000,
                                      1.1100, 1.1020, 0.0003, 0.0001)


def test_invalid_side_zero_tick_nonfinite_and_close_side_fail():
    for side in ("HOLD", "SELL", "BUY"):
        assert not strictly_safe_stop(side, 1.101, float("nan"), 1.10,
                                      1.08, 1.09, 0.001, 0.0001)
        assert not strictly_safe_stop(side, 1.101, 1.100, 1.10,
                                      1.08, 1.09, 0.001, 0.0)


def test_chandelier_source_is_not_broker_confirmation():
    assert "No fresh entries" in TRAIL
    assert "A local OrderSend ACK is NOT a proof" in TRAIL
    assert "BROKER_SLTP_READBACK_CONFIRMED" in TRAIL
    assert "EnableNativeChandelierTrailing = false" in MAIN