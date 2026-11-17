"""Issue #21: native V101 receipt scanning and positional broker protocol.

These checks make source/drift regressions visible in Python CI.
Real MetaEditor and live broker checks remain separate evidence.
"""
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "mql5"
    / "AbutronFPNativeOrderBridgeV101.mq5"
)
INCLUDE = SOURCE.with_name("AdaptiveRiskProofV2.mqh")


def _source():
    return SOURCE.read_text(encoding="utf-8")


def test_bridge_native_dependency_is_in_repository():
    text = _source()
    assert '#include "AdaptiveRiskProofV2.mqh"' in text
    assert INCLUDE.is_file()
    include_text = INCLUDE.read_text(encoding="utf-8")
    assert "OptionCWriteIntent" in include_text
    assert "if(++inspected>250)break;" not in include_text


def test_bridge_scans_beyond_250_receipts_across_timer_ticks():
    text = _source()
    assert "gEvidenceScanOffset=(offset+batch)%count;" in text
    assert "int batch=MathMin(250,count);" in text
    assert "SaveEvidenceScanOffset();" in text
    assert "LoadEvidenceScanOffset();" in text
    assert "if(++scanned>250)break;" not in text

    # The same persisted modulo cursor covers a 279-file account corpus.
    total, batch, offset = 279, 250, 0
    seen = set()
    for _ in range(3):
        for i in range(min(batch, total)):
            seen.add((offset+i) % total)
        offset = (offset + min(batch, total)) % total
    assert len(seen) == total


def test_bridge_uses_valid_native_file_enumeration():
    text = _source()
    assert 'FileFindFirst(DIR_RECEIPTS+"*.txt",filename)' in text
    assert "while(FileFindNext(h,filename))" in text
    assert "FileFindClose(h);" in text
    assert "FileFindNext(searchHandle)" not in text
    assert "DirectoryGetFiles(" not in text
    assert "DirectoryCreate(" not in text


def test_bridge_keeps_broker_positional_receipt_formats():
    text = _source()
    assert '"APEX_FP_NATIVE_V1\\r\\n"+key' in text
    assert '"APEX_FP_BROKER_POSITION_V1\\r\\n"+key' in text
    assert 'header!="APEX_FP_NATIVE_V1"' in text
    assert 'kind!="ACK"' in text
    assert 'nonce!=gNonce||login!=gLogin||server!=gServer' in text
    # No new key=value JSON-style receipt contract is introduced.
    assert '"request_id=" + reqId' not in text
    assert 'string _SignalHash(' not in text


def test_bridge_keeps_native_order_risk_and_protection():
    text = _source()
    for token in ("OrderCheck(", "OrderCalcProfit(", "OrderSend(",
                  "OptionCWriteIntent(", "UpdateEvidenceFor(",
                  "ACCOUNT_TRADE_MODE_DEMO", "TRADE_RETCODE_DONE"):
        assert token in text, token
    assert "input bool EnableNativeDemoOrders = false;" in text
    assert "EventSetTimer(2);" in text