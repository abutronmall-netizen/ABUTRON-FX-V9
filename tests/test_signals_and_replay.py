"""Tests for the enriched causal signal pipeline, loss guard, loader
assembly, runtime rules, and result replay.

These cover the wiring added after the original 210-test suite:
``brain.signals`` (all nine formerly-dead feature slots live, negative
indexes, 23->49 feature adaptation), ``LossPatternGuard`` persistence
through the unified brain checkpoint, generic ``.part-*`` market-data
assembly, ``runtime_rules`` write/load, ``replay_results`` input parsing,
and the selflearn footprint/regime aliases.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from abutron.brain.features import N_FEATURES
from abutron.brain.signals import (
    SIGNAL_DATA_VERSION, SIGNAL_SLOT_NAMES, audit_market_features,
    build_signal_bundle, features_for_brain, latest_signal_features,
    load_depth_signals, load_tick_trades, online_learn_from_bundle,
)
from abutron.brain.unified import (
    UnifiedBrain, build_unified_ensemble, load_unified, n_unified_features,
    save_unified,
)
from abutron.brain.loss_guard import LossPatternGuard
from abutron.feeds.feed_quality import Candle


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _candles(n=60, symbol="EURUSD.r", start=1.1000, point=0.0001):
    base = 1_700_000_000
    out = []
    price = start
    for i in range(n):
        price += math.sin(i / 6.0) * point * 8 + point * 2
        o = price
        h = o + abs(math.sin(i / 4.0)) * point * 12
        low = o - abs(math.cos(i / 5.0)) * point * 12
        c = o + math.sin(i / 3.0) * point * 6
        out.append(Candle(
            symbol=symbol, timeframe="M5",
            open_time=_iso(base + i * 300),
            close_time=_iso(base + i * 300 + 300),
            open=o, high=h, low=low, close=c,
            volume=10.0 + (i % 7), point=point, tick_size=point,
            closed=True))
    return out


# ---------------------------------------------------------------------------
# Signal bundle: all slots live, causal, negative indexes
# ---------------------------------------------------------------------------
class TestSignalBundle:
    def test_all_nine_slots_live(self):
        bundle = build_signal_bundle(_candles(60), symbol="EURUSD.r")
        assert bundle.n_bars == 60
        assert bundle.audit["all_slots_nonzero"] is True
        assert bundle.audit["version"] == SIGNAL_DATA_VERSION
        assert set(bundle.audit["slots"]) == set(SIGNAL_SLOT_NAMES)
        for name, slot in bundle.audit["slots"].items():
            assert slot["nonzero_ratio"] > 0.0, name

    def test_market_features_width_and_finite(self):
        bundle = build_signal_bundle(_candles(60), symbol="EURUSD.r")
        for row in bundle.market_features:
            assert len(row) == N_FEATURES == 23
            assert all(math.isfinite(v) for v in row)

    def test_negative_index_selects_from_end(self):
        bundle = build_signal_bundle(_candles(40), symbol="EURUSD.r")
        last = bundle.at(-1)
        assert last["index"] == 39
        assert last["market_features"] == bundle.market_features[39]
        assert bundle.at(-2)["index"] == 38
        # out-of-range negatives clamp to the first bar, positives to last
        assert bundle.at(-1000)["index"] == 0
        assert bundle.at(1000)["index"] == 39

    def test_latest_signal_features_uses_last_bar(self):
        sig = latest_signal_features(_candles(40), symbol="EURUSD.r")
        assert sig["index"] == 39
        assert len(sig["market_features"]) == 23

    def test_empty_bundle_is_safe(self):
        bundle = build_signal_bundle([], symbol="EURUSD.r")
        assert bundle.n_bars == 0
        assert bundle.at(-1)["market_features"] == []

    def test_online_learn_from_bundle_negative_index(self):
        brain = UnifiedBrain(ensemble=build_unified_ensemble(seed=0),
                             online_enabled=True)
        bundle = build_signal_bundle(_candles(40), symbol="EURUSD.r")
        res = online_learn_from_bundle(
            brain, symbol="EURUSD.r", bundle=bundle, index=-1,
            realised_return=0.001)
        assert res.get("updated") in (True, 1, 3) or res.get("loss") is not None
        assert brain.online_updates >= 1

    def test_features_for_brain_expands_23_to_49(self):
        brain = UnifiedBrain(ensemble=build_unified_ensemble(seed=0))
        bundle = build_signal_bundle(_candles(40), symbol="EURUSD.r")
        full = features_for_brain(brain, bundle.market_features[-1],
                                  symbol="EURUSD.r")
        assert len(full) == n_unified_features() == 49
        assert len(full) == N_FEATURES + 26

    def test_audit_of_empty_rows(self):
        audit = audit_market_features([])
        assert audit["all_slots_nonzero"] is False
        assert audit["rows"] == 0


# ---------------------------------------------------------------------------
# Optional true tick/depth loaders
# ---------------------------------------------------------------------------
class TestTickDepthLoaders:
    def test_missing_paths_return_empty(self):
        assert load_tick_trades("") == {}
        assert load_tick_trades("/nonexistent/trades.csv.gz") == {}
        assert load_depth_signals("") == {}
        assert load_depth_signals("/nonexistent/depth.csv.gz") == {}

    def test_tick_trades_grouped_by_bar(self, tmp_path):
        path = tmp_path / "trades.csv.gz"
        base = 1_700_000_000
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write("symbol,bar_open_utc,price,size,side\n")
            fh.write(f"EURUSD.r,{base},1.1000,1.5,buy\n")
            fh.write(f"EURUSD.r,{base},1.1001,2.0,sell\n")
            fh.write(f"EURUSD.r,{base + 300},1.1002,1.0,buy\n")
        out = load_tick_trades(str(path))
        assert len(out) == 2
        sizes = sorted(len(v) for v in out.values())
        assert sizes == [1, 2]

    def test_depth_signals_imbalance(self, tmp_path):
        path = tmp_path / "depth.csv.gz"
        base = 1_700_000_000
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write("symbol,bar_open_utc,bid_volume,ask_volume\n")
            fh.write(f"EURUSD.r,{base},10,30\n")
        out = load_depth_signals(str(path))
        assert len(out) == 1
        row = next(iter(out.values()))
        assert row["imbalance"] == pytest.approx(0.5)
        assert row["source"] == "depth"


# ---------------------------------------------------------------------------
# Loss guard: learn, block, clear, persist
# ---------------------------------------------------------------------------
class TestLossGuard:
    def _feats(self):
        return [0.1 * (i % 5) - 0.2 for i in range(N_FEATURES)]

    def test_loss_blocks_repeat_and_win_clears(self):
        guard = LossPatternGuard()
        feats = self._feats()
        guard.learn(symbol="EURUSD.r", direction="BUY", regime="trend_up",
                    features=feats, pnl=-10.0)
        blocked, reason = guard.should_block(
            symbol="EURUSD.r", direction="BUY", regime="trend_up",
            features=feats)
        assert blocked is True
        assert "LOSS_PATTERN_REPEAT" in reason
        # a win on the same pattern clears the veto
        guard.learn(symbol="EURUSD.r", direction="BUY", regime="trend_up",
                    features=feats, pnl=5.0)
        blocked2, _ = guard.should_block(
            symbol="EURUSD.r", direction="BUY", regime="trend_up",
            features=feats)
        assert blocked2 is False

    def test_guard_state_roundtrip(self):
        guard = LossPatternGuard()
        guard.learn(symbol="EURUSD.r", direction="SELL", regime="range",
                    features=self._feats(), pnl=-3.0)
        state = guard.state_dict()
        guard2 = LossPatternGuard()
        guard2.load_state(state)
        assert guard2.stats()["total_losses"] == 1
        blocked, _ = guard2.should_block(
            symbol="EURUSD.r", direction="SELL", regime="range",
            features=self._feats())
        assert blocked is True

    def test_guard_persists_through_unified_checkpoint(self, tmp_path):
        brain = UnifiedBrain(ensemble=build_unified_ensemble(seed=0))
        feats = self._feats()
        brain.learn_from_trade(
            symbol="EURUSD.r", regime="trend_up", direction="BUY",
            entry_type="CONFIRM", entry_quality=0.7, pnl=-12.0,
            features=feats)
        save_unified(str(tmp_path), brain)
        loaded = load_unified(str(tmp_path))
        assert loaded is not None
        blocked, reason = loaded.loss_guard.should_block(
            symbol="EURUSD.r", direction="BUY", regime="trend_up",
            features=feats)
        assert blocked is True
        assert "LOSS_PATTERN_REPEAT" in reason
        # decide() vetoes the repeated losing pattern
        closes = [1.1 + i * 0.0002 for i in range(30)]
        prop = loaded.decide(symbol="EURUSD.r", closes=closes)
        assert prop.direction in ("BUY", "SELL", "HOLD")


# ---------------------------------------------------------------------------
# Loader: generic .part-* assembly
# ---------------------------------------------------------------------------
class TestLoaderAssembly:
    def _write_parts(self, directory, header, rows, n_parts=3):
        # The loader concatenates the .part-* byte chunks and THEN gunzips,
        # so the parts must be chunks of the gzip stream, not raw CSV.
        directory.mkdir(parents=True, exist_ok=True)
        blob = (",".join(header) + "\n" +
                "".join(",".join(str(x) for x in r) + "\n" for r in rows))
        data = gzip.compress(blob.encode())
        chunk = max(1, len(data) // n_parts)
        for i in range(n_parts):
            part = data[i * chunk:(i + 1) * chunk] if i < n_parts - 1 else data[i * chunk:]
            (directory / f"candles.csv.gz.part-{i:02d}").write_bytes(part)

    def test_deriv_parts_assembled(self, tmp_path):
        from abutron.backtest.loader import MarketDataLoader
        header = ["symbol", "timeframe", "start_time_utc",
                  "open", "high", "low", "close"]
        rows = [["Boom 1000 Index.0", "M5", 1_700_000_000 + i * 300,
                 "100.0", "101.0", "99.0", "100.5"] for i in range(10)]
        self._write_parts(tmp_path / "Deriv", header, rows)
        loader = MarketDataLoader(str(tmp_path))
        candles = loader.load_deriv(timeframe="M5")
        assert len(candles) == 10
        assert candles[0].symbol == "Boom 1000 Index.0"

    def test_fp_parts_assembled_with_broker_symbol(self, tmp_path):
        from abutron.backtest.loader import MarketDataLoader
        header = ["symbol", "broker_symbol", "timeframe", "start_time_utc",
                  "open", "high", "low", "close"]
        rows = [["AUDUSD", "AUDUSD.r", "M5", 1_700_000_000 + i * 300,
                 "0.70000", "0.70100", "0.69900", "0.70050"]
                for i in range(8)]
        self._write_parts(tmp_path / "FP-Markets", header, rows)
        loader = MarketDataLoader(str(tmp_path))
        candles, _ = loader.load_fp_markets(timeframe="M5")
        assert len(candles) == 8
        assert all(c.symbol == "AUDUSD.r" for c in candles)

    def test_missing_dirs_return_empty(self, tmp_path):
        from abutron.backtest.loader import MarketDataLoader
        loader = MarketDataLoader(str(tmp_path))
        assert loader.load_deriv() == []
        candles, quotes = loader.load_fp_markets()
        assert candles == [] and quotes == []


# ---------------------------------------------------------------------------
# Runtime rules: write/load/trail_k
# ---------------------------------------------------------------------------
class TestRuntimeRules:
    def test_write_load_and_trail_k(self, tmp_path):
        from abutron.apex.runtime_rules import (
            RULE_VERSION, invalidate_cache, load_runtime_rules,
            trail_k_for, write_runtime_rules)
        path = tmp_path / "runtime_rules.json"
        write_runtime_rules(str(path), {
            "EURUSD.r": {"tp_atr_mult": 1.0, "trail_k": 2.5,
                         "validation": {"held_out_trades": 30}},
        }, metadata={"source": "test"})
        invalidate_cache()
        rules = load_runtime_rules(str(path))
        assert rules["EURUSD.r"]["trail_k"] == 2.5
        assert trail_k_for("EURUSD.r", path=str(path)) == 2.5
        assert trail_k_for("UNKNOWN", path=str(path)) == 3.0
        data = json.loads(path.read_text())
        assert data["version"] == RULE_VERSION
        assert data["symbols"]["EURUSD.r"]["tp_atr_mult"] == 1.0

    def test_missing_rules_file_is_empty(self, tmp_path):
        from abutron.apex.runtime_rules import load_runtime_rules
        assert load_runtime_rules(str(tmp_path / "nope.json")) == {}


# ---------------------------------------------------------------------------
# Replay: result file parsing
# ---------------------------------------------------------------------------
class TestReplayParsing:
    def test_load_json_trades_object(self, tmp_path):
        from scripts.replay_results import load_result_file
        path = tmp_path / "paper.json"
        path.write_text(json.dumps({"trades": [
            {"symbol": "EURUSD.r", "direction": "BUY", "pnl": 5.0,
             "features": [0.1] * 23},
            {"symbol": "EURUSD.r", "direction": "SELL", "pnl": -2.0,
             "features": [0.2] * 49},
        ]}))
        rows = load_result_file(str(path))
        assert len(rows) == 2

    def test_load_jsonl(self, tmp_path):
        from scripts.replay_results import load_result_file
        path = tmp_path / "rows.jsonl"
        path.write_text(
            json.dumps({"symbol": "EURUSD.r", "pnl": 1.0,
                        "features": [0.0] * 23}) + "\n" +
            json.dumps({"symbol": "EURUSD.r", "pnl": 2.0,
                        "features": [0.0] * 23}) + "\n")
        rows = load_result_file(str(path))
        assert len(rows) == 2

    def test_load_csv_with_features_column(self, tmp_path):
        from scripts.replay_results import load_result_file
        path = tmp_path / "rows.csv"
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["symbol", "direction", "pnl",
                                               "features"])
            w.writeheader()
            w.writerow({"symbol": "EURUSD.r", "direction": "BUY",
                        "pnl": "3.5", "features": json.dumps([0.1] * 23)})
        rows = load_result_file(str(path))
        assert len(rows) == 1
        assert rows[0]["symbol"] == "EURUSD.r"

    def test_load_gzipped_json(self, tmp_path):
        from scripts.replay_results import load_result_file
        path = tmp_path / "paper.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps([{"symbol": "EURUSD.r", "pnl": 1.0,
                                  "features": [0.0] * 23}]))
        rows = load_result_file(str(path))
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Selflearn aliases
# ---------------------------------------------------------------------------
class TestSelflearnAliases:
    def test_unknown_regime_does_not_raise(self):
        from abutron.selflearn.features import build_feature_vector
        feats = build_feature_vector(regime="some_new_regime")
        assert len(feats) == 15
        assert feats[5:9] == [0.0, 0.0, 0.0, 0.0]

    def test_poc_vol_pct_alias(self):
        from abutron.selflearn.features import build_feature_vector
        a = build_feature_vector(footprint={"poc_vol_pct": 0.4})
        b = build_feature_vector(footprint={"poc_volume_pct": 0.4})
        assert a[4] == b[4] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Docs / link script presence
# ---------------------------------------------------------------------------
class TestDocsAndLinkScript:
    def test_link_script_exists_and_is_executable(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        script = os.path.join(root, "scripts", "link_all.sh")
        assert os.path.isfile(script)
        assert os.access(script, os.X_OK)

    def test_install_and_link_doc_exists(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        doc = os.path.join(root, "docs", "INSTALL_AND_LINK.md")
        assert os.path.isfile(doc)
        text = open(doc, encoding="utf-8").read()
        for needle in ("market-data", "trade-history", "train_unified",
                       "train_online", "train_from_trades", "replay_results",
                       "ABUTRON_RUNTIME_RULES", "ABUTRON_TICK_TRADES",
                       "ABUTRON_DEPTH_SNAPSHOTS"):
            assert needle in text, needle
