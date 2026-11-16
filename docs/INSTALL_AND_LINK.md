# Install & Link — ABUTRON FX V9

This document describes how to install the platform, link the external
market-data and trade-history datasets, train the unified master brain,
optimize the non-neural runtime rules, and run backtests. It is the
companion to `README.md` (product overview) and `docs/ARCHITECTURE.md`
(blueprint).

## 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # never commit .env
```

For a production Ubuntu host (Wine + MT5 + systemd), use the installer:

```bash
sudo bash deploy/scripts/install.sh
sudo systemctl start abutron-portal.service
```

The installer copies the repo to `/opt/abutron-fx-v9`, creates the venv,
data directories, and systemd units. It does **not** clone market data —
run `scripts/link_all.sh` on the host after install (see §2).

## 2. Link external data

The brain trains on two external repositories. `scripts/link_all.sh`
clones them into `data/links/` (git-ignored, never committed):

```bash
bash scripts/link_all.sh            # first clone
bash scripts/link_all.sh --update   # refresh existing clones
bash scripts/link_all.sh --vendor   # also clone vendor/abutron-apex
```

| Env var | Default | Contents |
|---|---|---|
| `ABUTRON_MARKET_DATA_DIR` | `data/links/market-data` | `abutronmall-netizen/market-data` — Deriv + FP-Markets OHLCV gzip CSVs (`.part-*` chunks are assembled automatically by `backtest/loader.py`) |
| `ABUTRON_TRADE_HISTORY_DIR` | `data/links/trade-history` | `abutronmall-netizen/abutron-pay @ trade-history-2026-11-15` — 9 broker/account MT5 deal logs (875 closed trades) |
| `ABUTRON_BRAIN_DIR` | `data/brains/unified_m5` | Trained unified master brain checkpoint (`nexus_brain.json`) |
| `ABUTRON_RUNTIME_RULES` | _(empty = defaults)_ | Optimized non-neural sniper/trailing rules JSON (see §5) |
| `ABUTRON_TICK_TRADES` | _(empty = OHLCV proxies)_ | Optional true tick prints CSV(.gz) — see §6 |
| `ABUTRON_DEPTH_SNAPSHOTS` | _(empty = OHLCV proxies)_ | Optional depth snapshots CSV(.gz) — see §6 |
| `ABUTRON_GEMMA_ENABLED` | `false` | Optional Gemma agent-score gateway (advisory only, never on the order path) |

Verify the link:

```bash
PYTHONPATH=src .venv/bin/python -m abutron.backtest.run --list
```

## 3. Train the unified master brain

All three stages write to `data/brains/unified_m5/` (the checked-in
checkpoint is tracked in Git; new artifacts are force-added — see
`.gitignore` exceptions).

### Stage 1 — offline pooled training

```bash
PYTHONPATH=src .venv/bin/python -m scripts.train_unified \
    --data data/links/market-data --timeframe M5 \
    --epochs 12 --out data/brains/unified_m5
```

Pools all 26 symbols, builds causal signal bundles (`brain.signals`),
trains one 49-dim (23 market features + 26 symbol one-hot) ensemble, and
writes `nexus_brain.json`, `feature_audit.json`, `signal_sources.json`,
and `report.json`. The audit proves all nine formerly-dead feature slots
(order-flow ×3, footprint ×2, agent scores ×4) are populated.

### Stage 2 — causal per-bar online training

```bash
PYTHONPATH=src .venv/bin/python -m scripts.train_online \
    --data data/links/market-data --timeframe M5 \
    --in data/brains/unified_m5 --out data/brains/unified_m5
```

One causal gradient step per bar using the exact features the live engine
sees (`online_learn_from_bundle`).

### Stage 3 — learn from real settled trades

```bash
PYTHONPATH=src .venv/bin/python -m scripts.train_from_trades \
    --trades data/links/trade-history --data data/links/market-data \
    --mode online --in data/brains/unified_m5 --out data/brains/unified_m5
# optional offline refinement pass:
PYTHONPATH=src .venv/bin/python -m scripts.train_from_trades \
    --trades data/links/trade-history --data data/links/market-data \
    --mode offline --epochs 20 \
    --in data/brains/unified_m5 --out data/brains/unified_m5
```

Each trade is aligned to the market candle at entry; SELL profits are
labelled as market-down moves. Every loss updates the persistent
`LossPatternGuard` (quantized feature signature veto); wins clear it.

### Stage 4 — replay paper / backtest results

Paper trades were previously only an aggregate in the provenance file.
Now they are a real training input:

```bash
# export paper trades (with entry features) from a backtest:
PYTHONPATH=src .venv/bin/python -m abutron.backtest.run \
    --data data/links/market-data --broker fpmarkets \
    --symbol AUDUSD.r --timeframe M5 \
    --export-trades data/results/paper_trades.json

# replay them into the brain:
PYTHONPATH=src .venv/bin/python -m scripts.replay_results \
    --results data/results/paper_trades.json \
    --in data/brains/unified_m5 --out data/brains/unified_m5
```

Rows without `features` are reported and skipped — never guessed.

## 4. Backtest / forward test

```bash
PYTHONPATH=src .venv/bin/python -m abutron.backtest.run \
    --data data/links/market-data --broker deriv \
    --symbol "Boom 1000 Index.0" --timeframe M5 \
    --equity 10000 --risk-pct 0.05
```

`--brain` defaults to `ABUTRON_BRAIN_DIR`. Backtest learning is isolated
(deep copy) unless `--persist-brain` is passed. `--export-trades` writes
every closed paper trade with its entry features for `replay_results.py`.

## 5. Optimize non-neural runtime rules

Sniper TP/SL geometry, entry filters, and the Chandelier trailing
multiple are **execution rules, not neural weights**. They are optimized
on held-out paper trades and loaded from `ABUTRON_RUNTIME_RULES`:

```bash
PYTHONPATH=src .venv/bin/python -m abutron.backtest.optimize_all \
    --data data/links/market-data --broker all --timeframe M5 --htf H1 \
    --equity 10000 --risk-pct 0.05 --grid fast \
    --runtime-rules-out data/runtime_rules.json

export ABUTRON_RUNTIME_RULES=data/runtime_rules.json
```

Only symbols with positive held-out expectancy (profit factor > 1,
minimum trades) are written. The live `ApexEngine` and backtest engine
both read the same file via `apex/runtime_rules.py`.

## 6. Optional true microstructure evidence

Without tick/depth files, order-flow and footprint features are
explicitly-labelled deterministic OHLCV proxies (close-location volume,
wick absorption, volume-at-price histogram). They are **not** exchange
depth or true footprint, and the source is recorded in every bundle and
in the brain provenance.

To use true evidence, set:

- `ABUTRON_TICK_TRADES=path/to/trades.csv.gz` — columns: `symbol`,
  `bar_open_utc` (aliases `bar_open`/`bar_time`/`open_time`), `price`,
  `size` (aliases `volume`/`lots`/`qty`/`quantity`), `side`
  (aliases `aggressor_side`/`aggressor`). Times may be epoch seconds,
  epoch milliseconds, or ISO-UTC. These drive the real `OrderFlowEngine`
  and `VolumeFootprintBuilder`.
- `ABUTRON_DEPTH_SNAPSHOTS=path/to/depth.csv.gz` — columns: `symbol`,
  `bar_open_utc`, `bid_volume`, `ask_volume`, optional `price_change`.
  Depth gives real queue imbalance; footprint proxies remain unless tick
  trades are also supplied.

Pass `--ticks` / `--depth` to any training script, or `--gemma` to
attempt the optional Gemma score gateway (`ABUTRON_GEMMA_ENABLED=true`).

## 7. Optional vendored APEX source

`src/abutron/apex_integration.py` looks for the authoritative APEX 1.1.0
bridge at `vendor/abutron-apex/apex11`. It is **optional**: without it
the platform runs on the NexusBrain + `apex/engine.py`, and
`ApexIntegration` degrades gracefully. Clone it with:

```bash
bash scripts/link_all.sh --vendor
```

## 8. Provenance

Every retrain should update `data/brains/unified_m5/brain_provenance.json`
with the training stages actually run, sample counts, data commits, and
the paper-trade replay report. Do not claim paper trades trained the
brain unless `replay_results.py` has run and the report is recorded.
