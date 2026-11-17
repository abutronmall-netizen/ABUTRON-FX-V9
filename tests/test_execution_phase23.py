"""Tests for V9 Phase 2 (execution integration) and Phase 3 (feedback learning)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from abutron.execution.state import (
    RESERVED, SEND, UNKNOWN, ACK, REJECTED, OPEN, PROTECTED,
    CLOSED, SETTLED, DUPLICATE, BLOCKED_INCOMPLETE_EXPOSURE,
    VALID_TRANSITIONS, state_machine,
)
from abutron.execution.receipts import ReceiptManager
from abutron.learning.feedback import (
    realised_return_from_pnl,
    FeedbackResult,
)


class StateMachineBLOCKEDTest(unittest.TestCase):
    """Verify BLOCKED_INCOMPLETE_EXPOSURE is a valid execution state."""

    def test_blocked_state_exists(self):
        self.assertEqual(BLOCKED_INCOMPLETE_EXPOSURE,
                         "BLOCKED_INCOMPLETE_EXPOSURE")

    def test_reserved_can_transition_to_blocked(self):
        self.assertIn(BLOCKED_INCOMPLETE_EXPOSURE,
                      VALID_TRANSITIONS[RESERVED])

    def test_blocked_can_retransition_to_reserved(self):
        self.assertIn(RESERVED, VALID_TRANSITIONS[BLOCKED_INCOMPLETE_EXPOSURE])
        self.assertIn(REJECTED, VALID_TRANSITIONS[BLOCKED_INCOMPLETE_EXPOSURE])

    def test_blocked_cannot_go_to_send(self):
        self.assertNotIn(SEND, VALID_TRANSITIONS[BLOCKED_INCOMPLETE_EXPOSURE])


class ReceiptsVerificationTest(unittest.TestCase):
    """Test broker-side SL/TP and Chandelier trail verification."""

    def setUp(self):
        self.log = MagicMock()
        self.mgr = ReceiptManager(self.log)

    def test_verify_protective_stops_match(self):
        res = self.mgr.verify_protective_stops(
            receipt_id="rcpt_test1",
            broker_sl=1.0990, broker_tp=1.1010,
            expected_sl=1.0990, expected_tp=1.1010,
            point=0.0001)
        self.assertTrue(res["sl_ok"])
        self.assertTrue(res["tp_ok"])
        self.assertTrue(res["verified"])
        self.assertEqual(len(res["issues"]), 0)

    def test_verify_protective_stops_mismatch(self):
        res = self.mgr.verify_protective_stops(
            receipt_id="rcpt_test2",
            broker_sl=1.0985, broker_tp=1.1010,
            expected_sl=1.0990, expected_tp=1.1010,
            point=0.0001)
        self.assertFalse(res["sl_ok"])
        self.assertTrue(res["tp_ok"])
        self.assertFalse(res["verified"])
        self.assertTrue(any("SL_MISMATCH" in i for i in res["issues"]))

    def test_verify_chandelier_trail_applied(self):
        res = self.mgr.verify_chandelier_trail(
            receipt_id="rcpt_test3",
            broker_sl=1.0995, broker_trail_stop=1.0995,
            point=0.0001)
        self.assertTrue(res["trail_ok"])
        self.assertTrue(res["verified"])

    def test_verify_chandelier_trail_not_applied(self):
        res = self.mgr.verify_chandelier_trail(
            receipt_id="rcpt_test4",
            broker_sl=1.0990, broker_trail_stop=1.0995,
            point=0.0001)
        self.assertFalse(res["trail_ok"])
        self.assertFalse(res["verified"])
        self.assertTrue(any("TRAIL_NOT_APPLIED" in i
                            for i in res["issues"]))


class FeedbackLearningTest(unittest.TestCase):
    """Test closed-trade feedback learning math."""

    def test_realised_return_long(self):
        # Long: entry 1.1000, exit 1.1010 → 0.001/1.10 = 0.0909%
        r = realised_return_from_pnl(direction="long",
                                     entry_price=1.1000,
                                     exit_price=1.1010)
        self.assertAlmostEqual(r, 0.001 / 1.1, places=6)

    def test_realised_return_short(self):
        # Short: entry 1.1000, exit 1.0990 → profit 0.001/1.10
        r = realised_return_from_pnl(direction="short",
                                     entry_price=1.1000,
                                     exit_price=1.0990)
        self.assertAlmostEqual(r, 0.001 / 1.1, places=6)

    def test_realised_return_short_loss(self):
        # Short: entry 1.1000, exit 1.1010 → loss
        r = realised_return_from_pnl(direction="short",
                                     entry_price=1.1000,
                                     exit_price=1.1010)
        self.assertAlmostEqual(r, -0.001 / 1.1, places=6)

    def test_realised_return_zero_entry(self):
        r = realised_return_from_pnl(direction="long",
                                     entry_price=0.0,
                                     exit_price=1.1)
        self.assertEqual(r, 0.0)

    def test_feedback_result_to_dict(self):
        fr = FeedbackResult(candidate_id="c1", symbol="EURUSD.r",
                            realised_return=0.001, updated=True,
                            reason="OK", pnl_usd=10.0)
        d = fr.to_dict()
        self.assertEqual(d["candidate_id"], "c1")
        self.assertTrue(d["updated"])
        self.assertEqual(d["pnl_usd"], 10.0)


if __name__ == "__main__":
    unittest.main()
