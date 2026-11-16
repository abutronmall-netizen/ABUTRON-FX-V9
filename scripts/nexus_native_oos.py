#!/usr/bin/env python3
"""Offline, source-hashed ABUTRON NexusBrain vs APEX M5 experiment.

Never imports any live broker order writer; does not touch V9's active model.
Cost assumptions are labeled and are not broker-confirmed fills.
"""
from __future__ import annotations
import argparse
import bisect
import collections
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time
from typing import Any

import numpy as np
from abutron.brain import default_ensemble
import abutron.brain.neural as neural
from abutron.brain.features import build_feature_vector, returns_from_closes
from abutron.brain.trainer import TrainingSample, walk_forward_train, save_brain
from abutron_replacement.bridge import AbutronApex11SignalBridge

SOURCES = {
    "Deriv": ["Volatility 10 Index.0", "Volatility 75 Index.0"],
    "FP-Markets": ["GBPUSD", "EURUSD"],
}
TEST_START = int(datetime(2026, 11, 12, tzinfo=timezone.utc).timestamp())
FIELDS = ("open", "high", "low", "close")


def verify_source(root: Path, broker: str, basename: str) -> dict[str, Any]:
    p = root / broker / basename
    manifest = json.loads((root / broker / (basename + ".manifest.json")).read_text())
    sha = hashlib.sha256()
    with p.open("rb") as fd:
        while chunk := fd.read(1024 * 1024):
            sha.update(chunk)
    if sha.hexdigest() != manifest["sha256"]:
        raise ValueError(f"{broker}/{basename} source SHA256 mismatch")
    return manifest


def load_selected(root: Path) -> tuple[dict, dict, dict]:
    selected = {}
    facts = {}
    for broker, symbols in SOURCES.items():
        manifest = verify_source(root, broker, "candles.csv.gz")
        path = root / broker / "candles.csv.gz"
        grouped = {name: {} for name in symbols}
        n, duplicates, invalid = 0, 0, 0
        for record in csv.DictReader(gzip.open(path, "rt", newline="")):
            n += 1
            if record["timeframe"] != "M5" or record["symbol"] not in grouped:
                continue
            stamp = int(record["start_time_utc"])
            vals = tuple(float(record[f]) for f in FIELDS)
            o, h, l, c = vals
            if stamp <= 0 or not all(math.isfinite(x) and x > 0 for x in vals) or l > min(o, c) or h < max(o, c):
                invalid += 1
                continue
            existing = grouped[record["symbol"]].get(stamp)
            if existing is not None:
                duplicates += 1
                if existing != vals:
                    raise ValueError(f"{broker} conflicting duplicate {record['symbol']}/{stamp}")
            else:
                grouped[record["symbol"]][stamp] = vals
        if n != manifest["rows"] or invalid:
            raise ValueError(f"{broker}: row count mismatch or {invalid} malformed selected candles")
        for name, by_ts in grouped.items():
            candles = [(ts, *prices) for ts, prices in sorted(by_ts.items())]
            selected[(broker, name)] = candles
        facts[broker] = {"manifest_rows": n, "sha256": manifest["sha256"],
                         "selected_key_duplicates": duplicates,
                         "selected_M5_counts": {name: len(grouped[name]) for name in symbols}}
    quote_manifest = verify_source(root, "FP-Markets", "quotes.csv.gz")
    quotes = {s: [] for s in SOURCES["FP-Markets"]}
    count_quotes = 0
    for row in csv.DictReader(gzip.open(root / "FP-Markets" / "quotes.csv.gz", "rt")):
        count_quotes += 1
        if row["symbol"] in quotes:
            t = int(row["observed_utc"])
            bid, ask = float(row["bid"]), float(row["ask"])
            if not 0 < bid <= ask:
                raise ValueError("negative/invalid spread")
            quotes[row["symbol"]].append((t, ask - bid))
    if count_quotes != quote_manifest["rows"]:
        raise ValueError("FP quote count mismatch")
    for s in quotes:
        quotes[s].sort()
    facts["FP-Markets"]["quote_sha256"] = quote_manifest["sha256"]
    facts["FP-Markets"]["quote_rows"] = count_quotes
    return selected, quotes, facts


def build_examples(candles: dict) -> tuple[list[dict], dict]:
    train = []
    test = []
    detail = {}
    for (broker, symbol), bars in candles.items():
        n_gap = 0
        examples = []
        segment_start = 0
        for i in range(len(bars) - 1):
            current, nxt = bars[i], bars[i + 1]
            if i and current[0] - bars[i-1][0] != 300:
                segment_start = i
                n_gap += 1
            if nxt[0] - current[0] != 300:
                continue
            if i - segment_start + 1 < 30:
                continue
            w = bars[max(segment_start, i - 127):i + 1]
            close = [b[4] for b in w]
            hi = [b[2] for b in w]
            lo = [b[3] for b in w]
            rets = returns_from_closes(close)
            x = build_feature_vector(closes=close, highs=hi, lows=lo,
                                     **rets)
            entry = nxt[1]
            if not all(math.isfinite(t) for t in x) or entry <= 0:
                raise ValueError(f"Nonfinite {broker}/{symbol}")
            label = int(nxt[4] > current[4])
            r = {"broker": broker, "symbol": symbol,
                 "signal_ts": current[0]+300, "entry_ts": nxt[0],
                 "features": x, "label": label, "entry": entry,
                 "exit": nxt[4], "high": nxt[2], "low": nxt[3],
                 "prev_close": current[4]}
            examples.append(r)
        early = [r for r in examples if r["entry_ts"] < TEST_START]
        late = [r for r in examples if r["entry_ts"] >= TEST_START]
        train.extend(early)
        test.extend(late)
        detail[f"{broker}/{symbol}"] = {"M5_bars":len(bars),
                 "gaps":n_gap,"train_examples":len(early),"test_examples":len(late),
                 "first_utc":datetime.fromtimestamp(bars[0][0],timezone.utc).isoformat(),
                 "last_utc":datetime.fromtimestamp(bars[-1][0],timezone.utc).isoformat()}
    train.sort(key=lambda r: (r["entry_ts"], r["broker"], r["symbol"]))
    test.sort(key=lambda r: (r["entry_ts"], r["broker"], r["symbol"]))
    if len(train) < 500 or len(test) < 100:
        raise ValueError("Insufficient native records for an independent holdout")
    if max(r["entry_ts"] for r in train) >= min(r["entry_ts"] for r in test):
        raise ValueError("Training/test time leakage")
    return (train, test), detail


def apex_predictions(candles: dict, workspace: Path) -> dict[tuple, str]:
    # APEX is an offline baseline, not a writer; isolate its local state.
    os.makedirs(workspace / "apex_sandbox", exist_ok=True)
    previous_dir = os.getcwd()
    os.chdir(workspace / "apex_sandbox")
    signals = {}
    try:
        for (broker, symbol), bars in candles.items():
            bridge_symbol = symbol + ".r" if broker == "FP-Markets" else symbol
            is_deriv = broker == "Deriv"
            manifest = dict(symbol=bridge_symbol,
                broker="deriv" if is_deriv else "fp_markets",
                asset_class="deriv_synthetic" if is_deriv else "fx_index",
                tick_size=0.01 if is_deriv else 0.00001,
                spread_bps=1.0, commission_bps=0.7,
                annual_vol=0.2, max_weight=0.2,
                timeframe_seconds=300)
            b = AbutronApex11SignalBridge([manifest], warmup_bars=0)
            for ts, o, h, l, c in bars:
                result = b.on_closed_candle({
                    "symbol":bridge_symbol, "ts":ts+300,
                    "open":o,"high":h,"low":l,"close":c,"volume":0.0,
                    "is_closed":True,"provenance":"authenticated_mt5"},
                    observed_at=time.time())
                signals[(broker, symbol, ts+300)] = result["status"]
    finally:
        os.chdir(previous_dir)
    return signals


def costs_bps(broker: str, symbol: str, entry_ts: int, entry: float,
              quotes: dict) -> tuple[float | None, bool]:
    if broker == "Deriv":
        return None, False
    qs = quotes.get(symbol, [])
    times = [t for t, _ in qs]
    i = bisect.bisect_right(times, entry_ts) - 1
    if i < 0 or entry_ts - qs[i][0] > 300:
        return None, False
    return qs[i][1] / entry * 1e4, True


def stats(rows: list[dict], side_field: str, broker: str,
          quotes: dict, assumed_comm_bps: float) -> dict:
    trades = []
    signals = []
    for x in rows:
        side = x[side_field]
        if side not in ("BUY", "SELL"):
            continue
        sign = 1 if side == "BUY" else -1
        ret_bps = sign * ((x["exit"] - x["entry"]) / x["entry"]) * 1e4
        mae = ((x["entry"] - x["low"]) / x["entry"] * 1e4 if sign == 1
              else (x["high"] - x["entry"]) / x["entry"] * 1e4)
        spread, real_cost = costs_bps(broker, x["symbol"], x["entry_ts"],
                                     x["entry"], quotes)
        signals.append(int(side == "BUY") == x["label"])
        trades.append((x["entry_ts"], ret_bps, spread, mae,
                       x["symbol"], real_cost))
    trades.sort()
    net_with_known_spreads = []
    pnl_research = []
    dd_peak = 1.0
    equity = 1.0
    max_dd = 0.0
    for t, gross, spread, mae, symbol, known in trades:
        # Only observed FP spread or explicit Deriv scenario may be used.
        scenario_cost = spread if spread is not None else (2.0 if broker == "Deriv" else None)
        if scenario_cost is not None:
            net = gross - scenario_cost - assumed_comm_bps
            if known:net_with_known_spreads.append(net)
            pnl_research.append(net)
            equity *= max(1e-6, 1.0 + net / 1e4)
            dd_peak = max(dd_peak, equity)
            max_dd = max(max_dd, 1.0 - equity / dd_peak)
    gross_vals = [v[1] for v in trades]
    return {
        "signals":len(trades),
        "test_bars":len(rows),
        "coverage_pct":round(len(trades)/len(rows)*100,2) if rows else None,
        "direction_accuracy_pct":round(sum(signals)/len(signals)*100,2) if signals else None,
        "mean_gross_bps_per_trade":round(statistics.mean(gross_vals),3) if gross_vals else None,
        "median_gross_bps_per_trade":round(statistics.median(gross_vals),3) if gross_vals else None,
        "mean_adverse_excursion_bps":round(statistics.mean(v[3] for v in trades),3) if trades else None,
        "quote_cost_coverage_pct":round(sum(v[5] for v in trades)/len(trades)*100,2) if trades else None,
        "observed_quote_plus_assumed_commission_net_mean_bps":(
             round(statistics.mean(net_with_known_spreads),3)
             if net_with_known_spreads else None),
        "observed_quote_net_sample_trades":len(net_with_known_spreads),
        "commission_bps_round_trip_assumption":assumed_comm_bps,
        "deriv_roundtrip_spread_bps_scenario": 2.0 if broker == "Deriv" else None,
        "research_scenario_max_drawdown_pct":round(max_dd*100,3) if pnl_research else None,
        "research_scenario_net_mean_bps":round(statistics.mean(pnl_research),3) if pnl_research else None,
        "research_scenario_compounded_return_pct":round((equity-1)*100,3) if pnl_research else None,
        "research_scenario_cost_eligible_trades":len(pnl_research),
        "entry_timing":"next M5 candle open (hypothetical, no tick fill, no SL/TP)"
    }


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--source",default="/tmp/abutron-market-export-20261114")
    ap.add_argument("--out",required=True)
    ap.add_argument("--epochs",type=int,default=16)
    ap.add_argument("--commission-bps",type=float,default=0.7,
      help="ASSUMED roundtrip not validated broker commission")
    args=ap.parse_args()
    if not os.environ.get("APEX_DATA_DIR"):
        raise RuntimeError("APEX_DATA_DIR must target an isolated writable research directory")
    root=Path(args.source)
    output=Path(args.out)
    output.mkdir(parents=True,exist_ok=True)
    t0=time.time()
    candles, quotes, sources=load_selected(root)
    (train, test), series=build_examples(candles)
    print("SOURCE_VERIFIED", json.dumps(sources), flush=True)
    print("SPLIT",len(train),"train+validation",len(test),"untouched_test",flush=True)
    # MLP.forward() draws dropout from a module-level NumPy generator;
    # seed it here so the same archive and CLI args yield identical weights.
    neural.RNG = np.random.default_rng(20261114)
    ens=default_ensemble(seed=20261114)
    report=walk_forward_train(ens,
        [TrainingSample(features=r["features"],label=r["label"]) for r in train],
        epochs=args.epochs,batch_size=128,val_fraction=0.25,seed=20261114)
    print("TRAIN_DONE",json.dumps(report.to_dict()),flush=True)
    # Never write to V9's active 'data/brain' path; this is only an offline candidate.
    model_dir=output/"candidate_model_not_deployed"
    save_brain(str(model_dir),ens,report)
    X=np.array([r["features"] for r in test],dtype=float)
    probs=sum(float(m.weight)*m.predict(X) for m in ens.members)/sum(float(m.weight) for m in ens.members)
    for record, pp in zip(test, probs):
        record["nexus_side"]="BUY" if pp[1]>pp[0] else ("SELL" if pp[0]>pp[1] else "HOLD")
    baseline=apex_predictions(candles, output)
    for record in test:
        record["apex_side"]=baseline.get((record["broker"], record["symbol"], record["signal_ts"]), "HOLD")
    metrics={}
    for broker in SOURCES:
        br=[r for r in test if r["broker"]==broker]
        metrics[broker]={}
        for side in ("nexus_side","apex_side"):
            metrics[broker]["NexusBrain" if side=="nexus_side" else "APEX_1_1"] = stats(br,side,broker,quotes,args.commission_bps)
        for symbol in SOURCES[broker]:
            sr=[r for r in br if r["symbol"]==symbol]
            metrics[f"{broker}/{symbol}"]={
                "NexusBrain":stats(sr,"nexus_side",broker,quotes,args.commission_bps),
                "APEX_1_1":stats(sr,"apex_side",broker,quotes,args.commission_bps)}
    outcome={"state":"RESEARCH_ONLY_NOT_DEPLOYED",
             "source_type":"source-manifest-hashed broker export, not independently broker-signed",
             "train_val_cutoff_utc":datetime.fromtimestamp(TEST_START,timezone.utc).isoformat(),
             "split":"expanding chronological train then chronological calibration, untouched final test",
             "source_files":sources, "series":series,
             "train_val_samples":len(train),"heldout_samples":len(test),
             "training":report.to_dict(),"metrics":metrics,
             "limitations":[
                 "One 5-minute next-open to next-close price movement, not MT5 fill PnL or stop execution",
                 "Model and APEX each evaluated on same time windows; APEX may abstain/HOLD",
                 "Deriv spread unavailable in archive; 2bps round-trip spread is an arbitrary scenario, not actual",
                 "FP spread is prior recent historical quote, no look-ahead, max quote age 300s",
                 "Commission 0.7bps/roundtrip is research assumption only, not verified account fee",
                 "Spread+commission drawdown on equal-notional compounded trade sequence is not portfolio margin, risk or broker reconciliation",
                 "Historical M5 candle provenance from archive/manifest, not independently verified against live broker signatures"
             ],
             "wall_seconds":round(time.time()-t0,3)}
    path=output/"nexus_vs_apex_heldout.json"
    path.write_text(json.dumps(outcome,indent=2,sort_keys=True))
    print("EVALUATION_SAVED",str(path),flush=True)
    for broker in SOURCES:
        print("BROKER",broker,json.dumps(metrics[broker],sort_keys=True),flush=True)
    print("OFFLINE_COMPLETE",round(time.time()-t0,2),"seconds",flush=True)


if __name__=="__main__":
    main()
