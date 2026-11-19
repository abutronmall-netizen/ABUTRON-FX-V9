"""Static native order-handler proof; no order submissions."""
from pathlib import Path

EA = Path(__file__).parents[1] / "mql5" / "ApexNativeWriter.mq5"

def handler():
    text = EA.read_text()
    return text.split("void ProcessOrderRequests()",1)[1].split(
        "//| Floor volume to broker lot step",1)[0]

def test_requested_symbol_price_not_chart_symbol():
    src = handler()
    assert "SymbolInfoDouble(symbol, SYMBOL_ASK)" in src
    assert "SymbolInfoDouble(symbol, SYMBOL_BID)" in src
    assert "SymbolInfoDouble(_Symbol, SYMBOL_ASK)" not in src
    assert "SymbolInfoDouble(_Symbol, SYMBOL_BID)" not in src

def test_native_stop_risk_and_ordercheck_precede_send():
    src = handler()
    a = src.index("OrderCalcProfit(req.type, symbol, normVol, price, sl, brokerStopPnL)")
    b = src.index("OrderCheck(req, nativeCheck)")
    c = src.index("OrderSend(req, res)")
    assert a < b < c
    assert "accountEquity * InpRiskFraction / 100.0" in src

def test_qa_exposure_only_is_default():
    text=EA.read_text()
    assert "InpExposureOnly   = true" in text
    assert "if(!InpExposureOnly)" in text
    assert "ACCOUNT_TRADE_MODE_DEMO" in text
