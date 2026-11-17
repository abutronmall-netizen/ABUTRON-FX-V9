"""Regression: trade-history labels must represent MARKET direction, not PnL direction.

The neural classifier predicts UP (1) or DOWN (0); trade-memory learns
broker net PnL independently. A profitable short in a falling market MUST
not train the model to buy.
"""
from types import SimpleNamespace

import pytest

from scripts.train_from_trades import market_return_for_trade


@pytest.mark.parametrize("side,entry,exit_price,net,expected", [
    ("BUY", 100.0, 110.0, 9.0, 10.0),
    ("BUY", 100.0, 90.0, -11.0, -10.0),
    ("SELL", 100.0, 90.0, 9.0, -10.0),
    ("SELL", 100.0, 110.0, -11.0, 10.0),
    ("SELL", 0.0, 0.0, 1.0, -1.0),
    ("SELL", 0.0, 0.0, -1.0, 1.0),
])
def test_market_direction_not_trade_relative_direction(
        side, entry, exit_price, net, expected):
    trade = SimpleNamespace(direction=side, entry_price=entry,
                            exit_price=exit_price, pnl=net)
    assert market_return_for_trade(trade) == expected
