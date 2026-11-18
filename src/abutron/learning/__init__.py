"""Feedback learning — broker-confirmed outcomes into the brain."""
from .feedback import (
    learn_from_closed_trades,
    collect_closed_trades_from_receipts,
    realised_return_from_pnl,
    FeedbackResult,
)

__all__ = [
    "learn_from_closed_trades",
    "collect_closed_trades_from_receipts",
    "realised_return_from_pnl",
    "FeedbackResult",
]
