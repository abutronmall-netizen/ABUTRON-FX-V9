"""Offline, synthetic read-only trailing audit unit tests; no server state touched."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "ops/v9-cutover/diagnose_trailing_native.py"
spec = importlib.util.spec_from_file_location("diagnose_trailing_native", SOURCE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

R = {
    "ticket": 1234,
    "symbol": "Volatility 50 Index.0",
    "previous_sl": 81.10,
    "requested_sl": 81.20,
    "broker_sl": 81.20,
    "tp": 81.90,
    "state": "BROKER_READBACK_VERIFIED",
    "retcode": 10009,
}


def position(sl, *, ticket=1234, tp=81.90, volume=0.10, direction="BUY", symbol="Volatility 50 Index.0"):
    return {
        "ticket": ticket,
        "symbol": symbol,
        "sl": sl,
        "tp": tp,
        "volume": volume,
        "direction": direction,
    }


def check(receipt=None, pre=None, post=None, tick=0.0001):
    return audit.evaluate_proof(
        receipt if receipt is not None else R,
        [(1000, {"positions": pre if pre is not None else [position(81.10)]})],
        [(1005, {"positions": post if post is not None else [position(81.20)]})],
        tick,
    )["classification"]


class TestTrailingProof(unittest.TestCase):
    def test_buy_favorable_with_pre_post_proof(self):
        self.assertEqual(check(), "SIGNED_PRE_POST_PROOF")

    def test_sell_favorable_with_pre_post_proof(self):
        r = {**R, "previous_sl": 82.40, "requested_sl": 82.30, "broker_sl": 82.30}
        self.assertEqual(check(r, [position(82.40, direction="SELL")], [position(82.30, direction="SELL")]), "SIGNED_PRE_POST_PROOF")

    def test_no_before_position(self):
        self.assertEqual(check(pre=[]), "SIGNED_PRE_POSITION_MISSING")

    def test_no_after_position(self):
        self.assertEqual(check(post=[]), "SIGNED_POST_POSITION_MISSING")

    def test_same_symbol_wrong_ticket_is_not_proof(self):
        self.assertEqual(check(post=[position(81.20, ticket=7788)]), "SIGNED_POST_POSITION_MISSING")

    def test_same_ticket_wrong_symbol_is_not_proof(self):
        self.assertEqual(check(pre=[position(81.10, symbol="EURUSD")]), "SIGNED_PRE_POSITION_MISSING")

    def test_no_unauthenticated_receipt_is_promoted(self):
        self.assertEqual(check({**R, "state": "UNKNOWN"}), "RECEIPT_NOT_BROKER_VERIFIED")

    def test_rejected_native_retcode_not_promoted(self):
        self.assertEqual(check({**R, "retcode": 10030}), "RECEIPT_NOT_BROKER_VERIFIED")

    def test_post_tp_must_match_before(self):
        self.assertEqual(check(post=[position(81.20, tp=81.50)]), "SIGNED_POSITION_DETAILS_MISMATCH")

    def test_post_volume_must_match_before(self):
        self.assertEqual(check(post=[position(81.20, volume=0.08)]), "SIGNED_POSITION_DETAILS_MISMATCH")

    def test_widening_sl_is_not_verified(self):
        r = {**R, "requested_sl": 81.00, "broker_sl": 81.00}
        self.assertEqual(check(r, post=[position(81.00)]), "SIGNED_POSITION_DETAILS_MISMATCH")

    def test_signed_after_sl_must_equal_broker_readback(self):
        self.assertEqual(check(post=[position(81.30)]), "SIGNED_POSITION_DETAILS_MISMATCH")

    def test_broker_sl_must_equal_requested(self):
        self.assertEqual(check({**R, "broker_sl": 81.30}), "INVALID_REPORTED_SL_TP")

    def test_missing_tick_size_disallows_proof(self):
        self.assertEqual(check(tick=0), "INVALID_TICK_SIZE")

    def test_direction_cannot_change(self):
        self.assertEqual(check(post=[position(81.20, direction="SELL")]), "SIGNED_POSITION_DETAILS_MISMATCH")


if __name__ == "__main__":
    unittest.main()
