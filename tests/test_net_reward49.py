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
    with pytest.raises(ValueError, match="symbol identity mapping"):
        model.predict(mutated, "BUY")
    p2 = model.predict(tr[0].features, "BUY")
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


def test_isolated_checkpoint_survives_restart_and_rejects_duplicate_revisions():
    base_trades = sample(60, BASE)
    model = NetReward49(identity_slot=47)
    model.fit(base_trades, sample(20, BASE + timedelta(days=1)))
    new_trade = ClosedTrade.from_native_row(native_row(
        9999, x=0.9, entry=BASE + timedelta(days=4)
    ))
    assert model.online_update(new_trade, learned_at=new_trade.closed + timedelta(minutes=1))
    snapshot = json.loads(json.dumps(model.artifact()))
    restored = NetReward49.from_artifact(snapshot)
    assert restored.account_scope == "DEMO_A"
    assert restored.online_updates == 1
    assert restored.predict(new_trade.features, "BUY")["status"] == "RESEARCH_PREDICTION"
    assert not restored.online_update(new_trade, learned_at=new_trade.closed + timedelta(minutes=2))

    revised_row = native_row(9999, x=0.9, entry=BASE + timedelta(days=4))
    revised_row["gross_usd"] += 10
    revised_row["net_usd"] += 10
    revised = ClosedTrade.from_native_row(revised_row)
    with pytest.raises(ValueError, match="previously learned broker evidence changed"):
        restored.online_update(revised, learned_at=revised.closed + timedelta(minutes=5))


def test_reward_checkpoint_digest_tampering_rejected():
    model = NetReward49(identity_slot=47)
    model.fit(sample(60, BASE), sample(20, BASE + timedelta(days=1)))
    obj = model.artifact()
    obj["online_updates"] = 9000
    with pytest.raises(ValueError, match="digest"):
        NetReward49.from_artifact(obj)


def test_owner_isolation_during_fit_and_online_learning():
    model = NetReward49(identity_slot=47)
    train_a = sample(60, BASE)
    validation_a = sample(20, BASE + timedelta(days=1))
    wrong_owner = ClosedTrade.from_native_row(native_row(
        77, account="DEMO_B", entry=BASE + timedelta(days=4)
    ))
    with pytest.raises(ValueError, match="one isolated DEMO account"):
        model.fit(train_a + [wrong_owner], validation_a)
    model.fit(train_a, validation_a)
    with pytest.raises(ValueError, match="cross-account"):
        model.online_update(wrong_owner, learned_at=wrong_owner.closed + timedelta(minutes=1))


def test_reloaded_model_still_cannot_invent_profitable_sells():
    model = NetReward49(identity_slot=47)
    model.fit(sample(60, BASE), sample(20, BASE + timedelta(days=1)))
    restored = NetReward49.from_artifact(model.artifact())
    assert restored.predict(sample(1, BASE)[0].features, "SELL")["status"] == \
        "UNTRAINED_EXECUTED_SIDE"
    assert restored.choose(sample(1, BASE)[0].features)["direction"] in ("BUY", "HOLD")


def test_wrong_symbol_onehot_rejected_on_fit():
    row = native_row()
    features = json.loads(row["features_json"])
    features[47] = 0
    features[48] = 1.0
    row["features_json"] = json.dumps(features)
    wrong = ClosedTrade.from_native_row(row)
    model = NetReward49(identity_slot=47)
    with pytest.raises(ValueError, match="symbol identity mapping"):
        model.fit([wrong] + sample(60, BASE), sample(20, BASE + timedelta(days=1)))


def test_end_to_end_sqlite_training_is_account_isolated(tmp_path):
    import sqlite3
    from pathlib import Path
    from scripts.train_broker_net_reward49 import run

    ledger = tmp_path / "closed_native.sqlite3"
    fields = [
        "account", "trade_key", "symbol", "direction", "entry_time", "exit_time",
        "features_json", "lots", "gross_usd", "commission_usd", "fee_usd",
        "swap_usd", "net_usd", "stop_loss_exit", "take_profit_exit",
        "native_deal_verified", "cost_breakdown_verified"
    ]
    with sqlite3.connect(str(ledger)) as c:
        c.execute("CREATE TABLE observations (" + ", ".join(
            name + (" TEXT" if name in {
                "account", "trade_key", "symbol", "direction", "entry_time",
                "exit_time", "features_json"
            } else " REAL") for name in fields
        ) + ")")
        for account in ("6207", "7593"):
            for day, n in ((0, 32), (1, 18), (2, 18)):
                for i in range(n):
                    idx = day * 100 + i + 1
                    row = native_row(
                        idx, account=account,
                        entry=BASE + timedelta(days=day, minutes=5 * i),
                        x=(i % 5) / 5,
                        gross=10.0 if (i % 4) >= 2 else -9.0,
                    )
                    # Actual 49-feature registry: V50 symbol identity is slot 46.
                    vector = json.loads(row["features_json"])
                    vector[47] = 0.0
                    vector[46] = 1.0
                    row["features_json"] = json.dumps(vector)
                    c.execute(
                        "INSERT INTO observations (" + ",".join(fields) +
                        ") VALUES (" + ",".join("?" for _ in fields) + ")",
                        tuple(row[name] for name in fields)
                    )
        c.commit()

    folder = tmp_path / "research_output"
    result = run(
        ledger, folder, BASE + timedelta(days=1),
        BASE + timedelta(days=2), {"6207", "7593"}
    )
    assert result["account_isolation"]
    assert set(result["accounts"]) == {"6207", "7593"}
    for account in ("6207", "7593"):
        assert result["accounts"][account]["status"] == "ISOLATED_RESEARCH_CANDIDATE"
        path = folder / account / "net_reward49_candidate.json"
        assert path.exists()
        restored = NetReward49.from_artifact(json.loads(path.read_text()))
        assert restored.account_scope == account
        assert len(restored.observed_ids) == 32
        assert all(key[0] == account for key in restored.observed_ids)
    assert not (folder / "net_reward49_candidate.json").exists()
    assert result["model_automatically_promoted"] is False
    assert result["broker_orders_submitted"] == 0
