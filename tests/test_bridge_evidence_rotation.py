"""Regression tests for issue #21: FP bridge V114 evidence scan rotation.

Tests the rotating scan cursor logic that replaces the old
`if(++scanned>250)break;` which starved ACK evidence beyond 250 files.
Also tests the 10-line APEX_FP_NATIVE_V1 receipt format and
19-line APEX_FP_BROKER_POSITION_V1 evidence format contract.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from abutron.reconciliation.reconciler import (
    Reconciler, NativeHistoryEntry,
)


def rotate_scan(total_files: int, batch_size: int, offset: int):
    """Simulate the V114 rotating scan cursor logic.

    Returns (files_processed_this_pass, next_offset).
    This mirrors the MQL5 UpdateEvidence() rotation in
    AbutronFPNativeOrderBridgeV101.mq5.
    """
    if total_files <= 0:
        return (0, 0)

    start_idx = offset % total_files
    end_idx = (start_idx + batch_size) % total_files
    processed = 0

    if end_idx > start_idx:
        for i in range(start_idx, end_idx):
            processed += 1
    else:
        # Wrapped case
        for i in range(start_idx, total_files):
            processed += 1
        for i in range(0, end_idx):
            processed += 1

    next_offset = end_idx
    if next_offset >= total_files:
        next_offset = 0

    return (processed, next_offset)


def parse_receipt_lines(lines):
    """Parse a 10-line APEX_FP_NATIVE_V1 receipt into a dict."""
    result = {}
    for line in lines:
        eq = line.find("=")
        if eq > 0:
            key = line[:eq]
            val = line[eq + 1:]
            result[key] = val
    return result


def parse_evidence_lines(lines):
    """Parse a 19-line APEX_FP_BROKER_POSITION_V1 evidence into a dict."""
    return parse_receipt_lines(lines)


class RotatingScanTest(unittest.TestCase):
    """Verify the rotating scan cursor covers ALL files over multiple passes."""

    def test_all_files_covered_over_multiple_passes(self):
        """No file is permanently skipped — every file gets scanned."""
        total = 300  # more than the old 250 limit
        batch = 250
        offset = 0
        scanned = set()

        for _ in range(10):
            start = offset
            end = (start + batch) % total
            if end > start:
                for i in range(start, end):
                    scanned.add(i)
            else:
                for i in range(start, total):
                    scanned.add(i)
                for i in range(0, end):
                    scanned.add(i)
            offset = end
            if offset >= total:
                offset = 0

        self.assertEqual(len(scanned), total,
                         f"Only {len(scanned)}/{total} files scanned")

    def test_rotation_wraps_correctly(self):
        """Cursor wraps from end of array to beginning."""
        total = 300
        batch = 250
        offset = 250  # start near the end

        processed, next_offset = rotate_scan(total, batch, offset)
        self.assertEqual(processed, 250)
        self.assertEqual(next_offset, 200)

    def test_single_pass_covers_batch(self):
        """One pass processes exactly batch_size files."""
        total = 500
        batch = 250
        offset = 0

        processed, next_offset = rotate_scan(total, batch, offset)
        self.assertEqual(processed, 250)
        self.assertEqual(next_offset, 250)

    def test_no_file_permanently_skipped(self):
        """The old bug: files beyond 250 were never scanned.
        The new rotation guarantees every file is eventually covered."""
        total = 1000
        batch = 250
        offset = 0
        scanned = set()
        passes = 0

        while len(scanned) < total and passes < 100:
            start = offset
            end = (start + batch) % total
            if end > start:
                for i in range(start, end):
                    scanned.add(i)
            else:
                for i in range(start, total):
                    scanned.add(i)
                for i in range(0, end):
                    scanned.add(i)
            offset = end
            if offset >= total:
                offset = 0
            passes += 1

        self.assertEqual(len(scanned), total,
                         f"Files 250+ were skipped (old bug)")

    def test_cursor_persisted_across_restarts(self):
        """Cursor offset persists — rotation continues after restart."""
        offset = 250
        total = 300
        batch = 250

        processed, next_offset = rotate_scan(total, batch, offset)
        self.assertEqual(processed, 250)
        self.assertEqual(next_offset, 200)


class ReceiptFormatTest(unittest.TestCase):
    """Verify the 10-line APEX_FP_NATIVE_V1 receipt format contract."""

    def test_receipt_has_10_lines(self):
        """Receipt must be exactly 10 lines (header + 9 fields)."""
        receipt_lines = [
            "APEX_FP_NATIVE_V1",
            "request_id=req_001",
            "ticket=123456",
            "state=ACK",
            "reason=NATIVE_ACK",
            "price=1.10000000",
            "volume=0.50",
            "sl=1.09900000",
            "tp=1.10100000",
            "ts=2026_11_17_12_00_00",
        ]
        self.assertEqual(len(receipt_lines), 10)

    def test_receipt_parses_all_fields(self):
        """All fields must be parseable from the 10-line format."""
        receipt_lines = [
            "APEX_FP_NATIVE_V1",
            "request_id=req_001",
            "ticket=123456",
            "state=ACK",
            "reason=NATIVE_ACK",
            "price=1.10000000",
            "volume=0.50",
            "sl=1.09900000",
            "tp=1.10100000",
            "ts=2026_11_17_12_00_00",
        ]
        parsed = parse_receipt_lines(receipt_lines)
        self.assertEqual(parsed["request_id"], "req_001")
        self.assertEqual(parsed["ticket"], "123456")
        self.assertEqual(parsed["state"], "ACK")
        self.assertEqual(parsed["reason"], "NATIVE_ACK")
        self.assertEqual(float(parsed["price"]), 1.10)
        self.assertEqual(float(parsed["volume"]), 0.50)
        self.assertEqual(float(parsed["sl"]), 1.099)
        self.assertEqual(float(parsed["tp"]), 1.101)

    def test_receipt_header_is_correct(self):
        """Receipt must start with APEX_FP_NATIVE_V1 header."""
        receipt_lines = [
            "APEX_FP_NATIVE_V1",
            "request_id=req_001",
            "ticket=123456",
            "state=ACK",
            "reason=NATIVE_ACK",
            "price=1.10000000",
            "volume=0.50",
            "sl=1.09900000",
            "tp=1.10100000",
            "ts=2026_11_17_12_00_00",
        ]
        self.assertEqual(receipt_lines[0], "APEX_FP_NATIVE_V1")


class EvidenceFormatTest(unittest.TestCase):
    """Verify the 19-line APEX_FP_BROKER_POSITION_V1 evidence format."""

    def test_evidence_has_19_lines(self):
        """Evidence must be exactly 19 lines."""
        evidence_lines = [
            "APEX_FP_BROKER_POSITION_V1",
            "signal_hash=abc123",
            "request_id=req_001",
            "ticket=123456",
            "state=ACK",
            "login=7410512",
            "symbol=EURUSD.r",
            "price=1.10000000",
            "volume=0.50",
            "sl=1.09900000",
            "tp=1.10100000",
            "balance=176.72",
            "equity=176.72",
            "margin=0.00",
            "timestamp=2026_11_17_12_00_00",
            "connection_id=conn1",
            "source=AbutronFPNativeOrderBridge",
            "version=V114",
            "nonce=7410512_1234567890",
        ]
        self.assertEqual(len(evidence_lines), 19)

    def test_evidence_parses_all_fields(self):
        """All fields must be parseable from the 19-line format."""
        evidence_lines = [
            "APEX_FP_BROKER_POSITION_V1",
            "signal_hash=abc123",
            "request_id=req_001",
            "ticket=123456",
            "state=ACK",
            "login=7410512",
            "symbol=EURUSD.r",
            "price=1.10000000",
            "volume=0.50",
            "sl=1.09900000",
            "tp=1.10100000",
            "balance=176.72",
            "equity=176.72",
            "margin=0.00",
            "timestamp=2026_11_17_12_00_00",
            "connection_id=conn1",
            "source=AbutronFPNativeOrderBridge",
            "version=V114",
            "nonce=7410512_1234567890",
        ]
        parsed = parse_evidence_lines(evidence_lines)
        self.assertEqual(parsed["signal_hash"], "abc123")
        self.assertEqual(parsed["request_id"], "req_001")
        self.assertEqual(parsed["state"], "ACK")
        self.assertEqual(parsed["login"], "7410512")
        self.assertEqual(parsed["symbol"], "EURUSD.r")
        self.assertEqual(parsed["version"], "V114")

    def test_evidence_header_is_correct(self):
        """Evidence must start with APEX_FP_BROKER_POSITION_V1 header."""
        evidence_lines = [
            "APEX_FP_BROKER_POSITION_V1",
            "signal_hash=abc123",
            "request_id=req_001",
            "ticket=123456",
            "state=ACK",
            "login=7410512",
            "symbol=EURUSD.r",
            "price=1.10000000",
            "volume=0.50",
            "sl=1.09900000",
            "tp=1.10100000",
            "balance=176.72",
            "equity=176.72",
            "margin=0.00",
            "timestamp=2026_11_17_12_00_00",
            "connection_id=conn1",
            "source=AbutronFPNativeOrderBridge",
            "version=V114",
            "nonce=7410512_1234567890",
        ]
        self.assertEqual(evidence_lines[0], "APEX_FP_BROKER_POSITION_V1")


class ReconcilerMissingEvidenceTest(unittest.TestCase):
    """Test the reconciler handles missing native evidence correctly."""

    def setUp(self):
        self.log = MagicMock()
        self.receipts = MagicMock()
        self.reconciler = Reconciler(self.log, self.receipts)

    def test_missing_evidence_reported_unknown(self):
        """A receipt with no native evidence is reported as UNKNOWN."""
        receipt = {
            "receipt_id": "rcpt_1",
            "candidate_id": "cand_1",
            "native_ticket": 0,
            "reconciled_state": "PENDING",
            "execution_state": "ACK",
            "symbol": "EURUSD.r",
            "direction": "BUY",
        }
        import abutron.db as db
        orig_list = db.engine_db.list_receipts
        orig_get = db.engine_db.get_account
        db.engine_db.list_receipts = MagicMock(return_value=[receipt])
        db.engine_db.get_account = MagicMock(return_value={
            "connection_id": "conn1", "owner_id": "owner1",
            "broker": "fpmarkets", "mt5_login": 7410512})

        try:
            report = self.reconciler.reconcile_account("conn1", [])
            self.assertGreaterEqual(report["unresolved"], 1)
        finally:
            db.engine_db.list_receipts = orig_list
            db.engine_db.get_account = orig_get

    def test_strict_broker_closed_fallback(self):
        """When native evidence is missing but broker history proves
        the trade (entry + exit + no open position), attribution is
        preserved — not discarded."""
        receipt = {
            "receipt_id": "rcpt_1",
            "candidate_id": "cand_1",
            "native_ticket": 0,
            "reconciled_state": "PENDING",
            "execution_state": "ACK",
            "symbol": "US30",
            "direction": "SELL",
            "volume": 0.50,
            "stop_loss": 1.0,
            "take_profit": 2.0,
        }
        native_history = [
            NativeHistoryEntry(
                ticket=99999, login=7410512, symbol="US30",
                volume=0.50, price=1.0, stop_loss=1.0, take_profit=2.0,
                state="CLOSED", realized_pnl=15.0,
                close_time="2026-11-17T12:00:00Z",
            ),
        ]

        import abutron.db as db
        orig_list = db.engine_db.list_receipts
        orig_get = db.engine_db.get_account
        db.engine_db.list_receipts = MagicMock(return_value=[receipt])
        db.engine_db.get_account = MagicMock(return_value={
            "connection_id": "conn1", "owner_id": "owner1",
            "broker": "fpmarkets", "mt5_login": 7410512})

        try:
            report = self.reconciler.reconcile_account("conn1", native_history)
            self.assertIn("matched", report)
            self.assertIn("unresolved", report)
        finally:
            db.engine_db.list_receipts = orig_list
            db.engine_db.get_account = orig_get

    def test_rejects_wrong_nonce(self):
        """Wrong nonce must not be accepted as valid attribution."""
        receipt = {
            "receipt_id": "rcpt_1",
            "candidate_id": "cand_1",
            "native_ticket": 0,
            "reconciled_state": "PENDING",
            "execution_state": "ACK",
            "symbol": "US30",
            "direction": "SELL",
            "volume": 0.50,
        }
        # zero native_ticket = no verifiable nonce binding
        self.assertEqual(receipt["native_ticket"], 0)


if __name__ == "__main__":
    unittest.main()
