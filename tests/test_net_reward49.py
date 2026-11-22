"""Research tests: broker-net learning MUST NOT invent opposite-side outcomes."""
from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest

from abutron.brain.net_reward49 import (
    ClosedTrade, NetReward49, chronological_split, group_results,
)

UTC = timezone.utc
BASE = datetime(2026, 11, 20, tzinfo=UTC)


def native_row(i=1, *, side="BUY", x=0.8, gross=10.0, commission=-1.0,
               native=True, entry=None, exit=None, account="DEMO_A"):
    t = entry or (BASE + timedelta(minutes=5 * i))
    vec = [0.0] * 49
    vec[0] = x
    vec[47] = 1.0  # symbol identity slot remains part of canonical width
    row = {
        "account": account, "trade_key": "ticket-" + str(i) + "-" + side,
        "symbol": "Volatility 50 Index.0", "direction": side,
        "entry_time": t.isoformat(),
        "exit_time": (exit or t + timedelta(minutes=2)).isoformat(),
        "lots": 10.0,
        "gross_usd": gross,
        "commission_usd": commission, "fee_usd": 0.0, "swap_usd": 0.0,
        "net_usd": gross + commission,
        "stop_loss_exit": int(gross < 0), "take_profit_exit": int(gross > 0),
        "features_json": json.dumps(vec),
        "native_deal_verified": int(native), "cost_breakdown_verified": 1,
    }
    return row


def test_native_costs_reconcile_and_keep_feature_width():
    x = ClosedTrade.from_native_row(native_row())
    assert len(x.features) == 49
    assert x.gross_per_lot == 1.0
    assert x.cost_per_lot == 0.1
    assert x.net_per_lot == 0.9


def test_native_deal_is_required():
    with pytest.raises(ValueError, match="unverified"):
        ClosedTrade.from_native_row(native_row(native=False))


def test_wrong_net_and_future_or_missing_exit_rejected():
    record = native_row()
    record["net_usd"] += 3.00
    with pytest.raises(ValueError, match="reconcile"):
        ClosedTrade.from_native_row(record)
    record = native_row()
    record["exit_time"] = record["entry_time"]
    with pytest.raises(ValueError, match="later"):
        ClosedTrade.from_native_row(record)


def test_closing_after_a_split_boundary_is_purged():
    val = BASE + timedelta(days=1)
    test = BASE + timedelta(days=2)
    rows = [
        ClosedTrade.from_native_row(native_row(1)),
        ClosedTrade.from_native_row(native_row(
            2, exit=val + timedelta(minutes=10))),
        ClosedTrade.from_native_row(native_row(
            3, entry=val + timedelta(hours=1))),
        ClosedTrade.from_native_row(native_row(
            4, entry=test + timedelta(hours=1))),
    ]
    tr, va, te, excluded = chronological_split(rows, val, test)
    assert (len(tr), len(va), len(te), excluded) == (1, 1, 1, 1)


def test_duplicate_native_trade_id_rejected():
    x = ClosedTrade.from_native_row(native_row())
    with pytest.raises(ValueError, match="duplicate"):
        chronological_split([x, x], BASE + timedelta(days=1),
                            BASE + timedelta(days=2))


def sample(n, start, *, side="BUY"):
    out = []
    for i in range(n):
        x = (i % 4) / 3
        gross = 12.0 if x > 0.5 else -12.0
        out.append(ClosedTrade.from_native_row(native_row(
            i + 2000 + start.day * 100, side=side, x=x, gross=gross,
            entry=start + timedelta(minutes=5 * i),
        )))
    return out


def test_losing_buy_is_not_labeled_profitable_sell():
    tr = sample(60, BASE)
    va = sample(20, BASE + timedelta(days=1))
    candidate = NetReward49(identity_slot=47)
    s = candidate.fit(tr, va, penalties=(0.01, 0.1, 1.0))
    assert s["BUY"]["train"] == 60
    assert s["SELL"]["status"] == "INSUFFICIENT_ACTUAL_SIDE_TRADES"
    p = candidate.predict(tr[-1].features, "SELL")
    assert p["status"] == "UNTRAINED_EXECUTED_SIDE"
    assert candidate.choose(tr[-1].features)["direction"] in ("BUY", "HOLD")


def test_canonical_identity_is_neutralized_without_changing_49_features():
    tr = sample(60, BASE)
    va = sample(20, BASE + timedelta(days=1))
    model = NetReward49(identity_slot=47)
    model.fit(tr, va)
    p = model.predict(tr[0].features, "BUY")
    mutated = list(tr[0].features)
    mutated[47] = 9999.0
    p2 = model.predict(mutated, "BUY")
    assert p["broker_net_usd_per_lot"] == pytest.approx(p2["broker_net_usd_per_lot"])
    assert len(mutated) == 49


def test_current_broker_cost_cannot_improve_predicted_net():
    model = NetReward49(identity_slot=47)
    tr = sample(60, BASE)
    model.fit(tr, sample(20, BASE + timedelta(days=1)))
    old = model.predict(tr[0].features, "BUY")
    expensive = model.predict(tr[0].features, "BUY", broker_current_cost_per_lot=500)
    assert expensive["broker_net_usd_per_lot"] <= old["broker_net_usd_per_lot"]
    assert expensive["cost_usd_per_lot"] >= 500


def test_online_update_requires_actual_closed_trade_and_is_idempotent():
    model = NetReward49(identity_slot=47)
    tr = sample(60, BASE)
    model.fit(tr, sample(20, BASE + timedelta(days=1)))
    fresh = ClosedTrade.from_native_row(native_row(
        9999, x=0.9, entry=BASE + timedelta(days=4)))
    with pytest.raises(ValueError, match="precede"):
        model.online_update(fresh, learned_at=fresh.entered)
    assert model.online_update(fresh, learned_at=fresh.closed + timedelta(minutes=2))
    assert not model.online_update(fresh, learned_at=fresh.closed + timedelta(minutes=3))
    assert model.online_updates == 1


def test_only_realized_broker_net_used_in_report():
    trades = [
        ClosedTrade.from_native_row(native_row(1, gross=-10.0)),
        ClosedTrade.from_native_row(native_row(2, gross=20.0)),
    ]
    result = group_results(trades, [2.0, -1.0])
    assert result["predicted_positive"]["trades"] == 1
    assert result["predicted_positive"]["broker_net_usd"] == -11.0
    assert result["predicted_nonpositive"]["broker_net_usd"] == 19.0
