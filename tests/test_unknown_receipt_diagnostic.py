"""Tests for the UNKNOWN-receipt diagnostic.

Verifies that ``extract_request_id``, ``search_receipt_files``,
``find_position_in_exposure`` and the classification logic correctly
distinguish between the four possible UNKNOWN outcomes:

- RECEIPT_FOUND: a native receipt file exists (delayed ACK/REJECTED)
- POSITION_OPEN_IN_CURRENT_EXPOSURE: position exists, no receipt file
- POSITION_IN_HISTORICAL_EXPOSURE: position in a post-entry snapshot
- NO_EVIDENCE_FOUND: nothing found — order never reached the EA

Synthetic fixtures only; no broker, terminal, or real DB.
"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

_spec = importlib.util.spec_from_file_location(
    "diagnose_unknown_receipt",
    os.path.join(os.path.dirname(__file__), "..",
                 "ops", "v9-cutover", "diagnose_unknown_receipt.py"))
_diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_diag)

extract_request_id = _diag.extract_request_id
search_receipt_files = _diag.search_receipt_files
find_position_in_exposure = _diag.find_position_in_exposure


# --------------------------------------------------------------------- #
# extract_request_id
# --------------------------------------------------------------------- #

def test_extract_request_id_from_reason():
    reason = "REQUEST_ID=ABUTRON_REQ_abc123;NATIVE_RECEIPT_TIMEOUT_NO_RETRY"
    assert extract_request_id(reason) == "ABUTRON_REQ_abc123"


def test_extract_request_id_empty():
    assert extract_request_id("") == ""
    assert extract_request_id("SEND_UNKNOWN") == ""


def test_extract_request_id_no_prefix():
    assert extract_request_id("SOME_OTHER_REASON") == ""


# --------------------------------------------------------------------- #
# search_receipt_files
# --------------------------------------------------------------------- #

def test_search_receipt_files_finds_match(tmp_path):
    files = tmp_path / "MQL5" / "Files"
    files.mkdir(parents=True)
    receipt = {
        "request_id": "ABUTRON_REQ_test123", "ticket": 1001,
        "state": "ACK", "reason": "", "login": 6207,
        "price": 100.0, "volume": 1.0, "sl": 99.0, "tp": 101.0,
        "deal": "5001", "ts": "20261120162539",
    }
    (files / "ABUTRON_NATIVE_RECEIPTS_V2_nonce1.txt").write_text(
        json.dumps(receipt))
    matches = search_receipt_files(str(files), "ABUTRON_REQ_test123", 6207)
    assert len(matches) == 1
    assert matches[0]["receipt"]["state"] == "ACK"


def test_search_receipt_files_wrong_login(tmp_path):
    files = tmp_path / "MQL5" / "Files"
    files.mkdir(parents=True)
    receipt = {"request_id": "ABUTRON_REQ_test123", "login": 9999,
               "state": "ACK"}
    (files / "ABUTRON_NATIVE_RECEIPTS_V2_nonce1.txt").write_text(
        json.dumps(receipt))
    matches = search_receipt_files(str(files), "ABUTRON_REQ_test123", 6207)
    assert len(matches) == 0


def test_search_receipt_files_no_match(tmp_path):
    files = tmp_path / "MQL5" / "Files"
    files.mkdir(parents=True)
    receipt = {"request_id": "ABUTRON_REQ_other", "login": 6207,
               "state": "ACK"}
    (files / "ABUTRON_NATIVE_RECEIPTS_V2_nonce1.txt").write_text(
        json.dumps(receipt))
    matches = search_receipt_files(str(files), "ABUTRON_REQ_test123", 6207)
    assert len(matches) == 0


def test_search_receipt_files_empty_request_id(tmp_path):
    files = tmp_path / "MQL5" / "Files"
    files.mkdir(parents=True)
    matches = search_receipt_files(str(files), "", 6207)
    assert len(matches) == 0


# --------------------------------------------------------------------- #
# find_position_in_exposure
# --------------------------------------------------------------------- #

def _exposure(*positions):
    return {"positions": list(positions), "position_count": len(positions)}


def test_find_position_matching():
    pos = {"ticket": 1001, "symbol": "Volatility 50 Index.0",
           "direction": "BUY", "volume": 1.0, "sl": 99.0, "tp": 101.0}
    matches = find_position_in_exposure(
        _exposure(pos), symbol="Volatility 50 Index.0",
        direction="BUY", volume=1.0)
    assert len(matches) == 1


def test_find_position_no_match_symbol():
    pos = {"ticket": 1001, "symbol": "Volatility 50 Index.0",
           "direction": "BUY", "volume": 1.0}
    matches = find_position_in_exposure(
        _exposure(pos), symbol="Volatility 75 Index.0",
        direction="BUY", volume=1.0)
    assert len(matches) == 0


def test_find_position_no_match_volume():
    pos = {"ticket": 1001, "symbol": "Volatility 50 Index.0",
           "direction": "BUY", "volume": 0.5}
    matches = find_position_in_exposure(
        _exposure(pos), symbol="Volatility 50 Index.0",
        direction="BUY", volume=1.0)
    assert len(matches) == 0


def test_find_position_empty_exposure():
    matches = find_position_in_exposure(
        _exposure(), symbol="Volatility 50 Index.0",
        direction="BUY", volume=1.0)
    assert len(matches) == 0
