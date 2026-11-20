"""Regression checks for the restored production-known EA plus lot-step correction."""
from __future__ import annotations
import importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"mql5/ApexNativeWriter.mq5"

def text(): return SRC.read_text()

def test_utf8_pending(): assert "FileOpen(pending, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8)" in text()
def test_utf8_order_flow(): assert "FileOpen(lastFile, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8)" in text()
def test_valid_filehandle_check(): assert "if(h == INVALID_HANDLE) { return; }" in text()
def test_no_ansi_bare_reads():
    for line in text().splitlines():
        if "FileOpen(" in line and "FILE_READ" in line and "FILE_WRITE" not in line:
            assert "FILE_ANSI" in line
def test_working_trade_magic(): assert "InpTradeMagic     = 20261120" in text()
def test_order_magic_bound(): assert "req.magic     = InpTradeMagic;" in text()
def test_position_scoped_trailing(): assert "POSITION_MAGIC) != InpTradeMagic" in text()
def test_trailing_called_on_timer():
    assert "ManageAgenticTrailingStops();" in text()[text().index("void OnTimer()"):text().index("void ExportNativeData()")]
def test_trailing_survives_restart_by_scanning_positions(): assert "PositionsTotal()" in text()[text().index("void ManageAgenticTrailingStops()"):]
def test_trailing_broker_sl_tp(): 
    s=text()
    assert "modify.action = TRADE_ACTION_SLTP;" in s
    assert "modify.position = ticket;" in s
    assert "modify.tp = tp;" in s
    assert "modify.magic = InpTradeMagic;" in s
def test_trailing_readback():
    s=text()
    assert "PositionGetDouble(POSITION_SL)" in s
    assert "BROKER_READBACK_VERIFIED" in s
    assert "WriteAgenticTrailingReceipt" in s
def test_trailing_tightening_only():
    s=text()
    assert "target <= oldSL + tickSize" in s and "target >= oldSL - tickSize" in s
def test_trailing_preset_enabled(): assert "InpTrailingEnabled = true" in text()
def test_epsilon_lot_floor(): assert "MathFloor(v / step + 1.0e-9) * step" in text()
def test_real_native_order_check(): assert "OrderCheck(req, nativeCheck)" in text()
def test_no_order_probe_generator():
    p=ROOT/"scripts/generate_issue38_exposure_probe.py"
    spec=importlib.util.spec_from_file_location("noorder_probe",p)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    probe=mod.make_probe(text())
    assert "OrderSend(" not in probe
    assert "ProcessOrderRequests(" not in probe
    assert "ManageAgenticTrailingStops(" not in probe
    assert "TRADE_ACTION_SLTP" not in probe
    assert "void ExportNativeData()" in probe
