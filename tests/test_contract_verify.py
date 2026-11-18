"""Tests for broker contract verification (V9 Phase 1)."""
from __future__ import annotations

import unittest

from abutron.risk.contract_verify import (
    verify_contract,
    verify_all_contracts,
    verify_owner_exposure,
)
from abutron.apex.eligibility import SYMBOL_REGISTRY, SymbolContract


class ContractVerifyTest(unittest.TestCase):
    """Verify risk math against MT5 contract specs."""

    def test_verify_known_symbol(self):
        r = verify_contract("EURUSD.r", equity=10000, stop_distance_points=26.0)
        self.assertEqual(r.symbol, "EURUSD.r")
        self.assertGreater(r.point, 0)
        self.assertGreater(r.point_value, 0)
        self.assertGreater(r.lot_min, 0)
        # Min-lot stop risk = lot_min * stop_pts * point_value
        expected = r.lot_min * 26.0 * r.point_value
        self.assertAlmostEqual(r.min_lot_stop_risk_usd, expected, places=4)

    def test_verify_all_returns_all_symbols(self):
        results = verify_all_contracts(equity=10000, stop_distance_points=26.0)
        self.assertEqual(len(results), len(SYMBOL_REGISTRY))

    def test_unknown_symbol_flagged(self):
        r = verify_contract("NONEXISTENT.r", equity=10000,
                            stop_distance_points=26.0)
        self.assertTrue(any("SYMBOL_NOT_IN_REGISTRY" in i
                            for i in r.issues))
        self.assertFalse(r.to_dict()["verified"])

    def test_owner_exposure_calc(self):
        # EURUSD.r: point=0.0001, pv=1.0
        # Entry 1.1000, SL 1.0990 → 100 pts stop
        # vol 0.10 lots → 0.10 * 100 * 1.0 = 10.0 USD risk
        res = verify_owner_exposure(
            "owner1",
            equity_by_conn={"conn1": 10000.0},
            open_positions={"conn1": [
                {"symbol": "EURUSD.r", "fill_price": 1.1000,
                 "stop_loss": 1.0990, "volume": 0.10},
            ]},
        )
        self.assertAlmostEqual(res["aggregate_stop_risk_usd"], 10.0, places=2)
        self.assertFalse(res["cap_exceeded"])

    def test_owner_exposure_cap_exceeded(self):
        # 0.50 lots at 100 pts stop = 50 USD risk on 100 equity → cap 20 USD
        res = verify_owner_exposure(
            "owner2",
            equity_by_conn={"conn1": 100.0},
            open_positions={"conn1": [
                {"symbol": "EURUSD.r", "fill_price": 1.1000,
                 "stop_loss": 1.0990, "volume": 0.50},
            ]},
        )
        self.assertTrue(res["cap_exceeded"])
        self.assertTrue(any("PORTFOLIO_CAP_EXCEEDED" in i for i in res["issues"]))

    def test_owner_exposure_unknown_symbol_flagged(self):
        res = verify_owner_exposure(
            "owner3",
            equity_by_conn={"conn1": 10000.0},
            open_positions={"conn1": [
                {"symbol": "UNKNOWN.r", "fill_price": 1.0,
                 "stop_loss": 0.99, "volume": 0.10},
            ]},
        )
        self.assertIn("UNKNOWN.r", res["unknown_symbols"])
        self.assertTrue(any("UNKNOWN_SYMBOLS" in i for i in res["issues"]))

    def test_to_dict_verified_field(self):
        r = verify_contract("EURUSD.r", equity=10000, stop_distance_points=26.0)
        d = r.to_dict()
        self.assertIn("verified", d)
        self.assertIn("min_lot_stop_risk_usd", d)


if __name__ == "__main__":
    unittest.main()
