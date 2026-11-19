"""Issue #39: native EA must preserve meaningful evidence generations.

Read-only source-contract regressions, not a substitute for MetaEditor compile
or authenticated Deriv/FP Markets DEMO chart round-trip.
"""
from pathlib import Path
import hashlib
import re

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "mql5" / "AbutronFPNativeOrderBridgeV101.mq5"


def source():
    return BRIDGE.read_text(encoding="utf-8")


def sample_evidence():
    return [
        "APEX_FP_BROKER_POSITION_V1", "a" * 64, "b" * 32,
        "7410512", "FPMarketsSC-Demo", "EURUSD.r", "BUY",
        "123", "456", "789", "0.10", "1.10000", "1.09000",
        "1.12000", "50.0", "100.0", "OPEN_PROTECTED",
        "1795080000", "0",
    ]


def semantic(lines):
    assert len(lines) == 19
    normalized = list(lines)
    normalized[17] = "BROKER_OBSERVATION_TIMESTAMP_IGNORED"
    return hashlib.sha256(("\n".join(normalized) + "\n").encode()).hexdigest()


def test_original_native_receipt_and_19_line_evidence_contracts_remain():
    text = source()
    assert 'Receipt(key,"ACK","BROKER_ORDER_ACK_POSITION_STILL_UNVERIFIED"' in text
    assert '"APEX_FP_BROKER_POSITION_V1\\r\\n"+key' in text
    assert 'IntegerToString(exit_deal)+"\\r\\n"' in text
    assert "void UpdateEvidenceFor(string key,long order,long deal)" in text


def test_native_evidence_events_precede_legacy_last_view_overwrite():
    text = source()
    body = text.split("void UpdateEvidenceFor(", 1)[1].split(
        "void SaveEvidenceScanOffset()", 1)[0]
    assert "ReadExistingEvidence(filename,readable)" in body
    assert "SameEvidenceExceptObservedTime(previous,text)" in body
    assert "SealEvidenceGeneration(key,previous)" in body
    assert "SealEvidenceGeneration(key,text)" in body
    assert "FileOpen(filename,FILE_WRITE" in body
    assert body.index("SealEvidenceGeneration(key,previous)") < body.index(
        "SealEvidenceGeneration(key,text)") < body.index(
        "FileOpen(filename,FILE_WRITE")


def test_timestamp_alone_is_not_a_distinct_position_evidence_generation():
    text = source()
    assert "if(i==17)continue;" in text
    original = sample_evidence()
    updated = list(original)
    updated[17] = "1795080005"
    assert semantic(updated) == semantic(original)


def test_stop_loss_risk_exit_and_state_changed_are_distinct_generations():
    original = sample_evidence()
    for index, value in [
        (9, "799"), (10, "0.20"), (12, "1.09500"),
        (13, "1.12500"), (14, "55.0"), (15, "111.0"),
        (16, "CLOSED_BROKER_VERIFIED"), (18, "987654"),
    ]:
        modified = list(original)
        modified[index] = value
        assert semantic(modified) != semantic(original), index


def test_event_identifier_contains_key_utc_uptime_and_local_monotonic_counter():
    text = source()
    assert 'DIR_EVIDENCE_EVENTS+key+"."' in text
    assert "IntegerToString((long)TimeGMT())" in text
    assert "IntegerToString((long)GetTickCount64())" in text
    assert "IntegerToString(gEvidenceEventSequence)" in text
    assert "if(FileIsExist(name))continue;" in text
    assert "gEvidenceEventSequence++" in text


def test_seal_failure_does_not_overwrite_compatibility_latest_view():
    text = source()
    body = text.split("void UpdateEvidenceFor(", 1)[1].split(
        "void SaveEvidenceScanOffset()", 1)[0]
    assert 'if(previous!=""&&!SealEvidenceGeneration(key,previous))return;' in body
    assert "if(!SealEvidenceGeneration(key,text))return;" in body
    assert "if(!readable)return;" in body


def test_native_broker_demo_trade_defaults_remain_disabled():
    text = source()
    assert "input bool EnableNativeDemoOrders = false;" in text
    assert "input bool AdaptiveV2ReconcilerApproved = false;" in text
    assert "if(!VerifiedDemoAccount())" in text
    assert text.count("bool sent=OrderSend(req,res);") == 1
    assert text.count("bool checked=OrderCheck(req,check);") == 1


def test_append_only_generation_folder_and_bounded_original_reader():
    text = source()
    assert '#define DIR_EVIDENCE_EVENTS "ABUTRON_FP\\\\evidence_events\\\\"' in text
    assert 'FolderCreate("ABUTRON_FP\\\\evidence_events")' in text
    assert "count<24" in text
    assert "if(!FileIsEnding(h))ok=false;" in text
    assert "written==(uint)StringLen(text)" in text
