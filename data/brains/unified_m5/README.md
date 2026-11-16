# Unified Master Brain — Installation & Attachment Guide

**Artifact:** `nexus_brain.json` — the trained unified master brain for ABUTRON FX-V9.
**Trained:** 2026-11-16
**Timeframe:** M5 (with H1 multi-timeframe confluence)
**Universe:** 26 symbols (13 Deriv + 13 FP-Markets)

---

## What this brain contains

Trained on **every signal source** the platform provides:

| Signal source | How it's trained in |
|---|---|
| Market data (OHLCV candles) | Offline base + online per-bar causal updates |
| Order flow (imbalance, delta, absorption) | Causal candle-derived proxies via `signals.py` |
| Volume footprint (VA-imbalance, POC share) | Causal candle-derived proxies via `signals.py` |
| Gemma / agent scores (trend, meanrev, breakout, carry) | Deterministic APEX agents encoded as features; Gemma advisory wired |
| Regime (trend_up, trend_down, high_vol, range) | One-hot features |
| Trade history (875 real broker trades) | Online + offline training, 0 skipped |
| Sniper entry (LIMIT/CONFIRM, ATR TP/SL) | Applied at decision time; outcomes feed back |
| Trailing stop (Chandelier) | Applied to open positions; outcomes feed back |
| Engine + execution + reconciliation | Per-bar online learning in the backtest engine |
| Equity / risk state | Risk-managed PnL informs trade labels |
| Risk (1% DD-cap, daily loss cap, lot sizing) | Execution rules |
| Backtest / forward / paper-trade results | Paper-trade feedback stage (`train_from_paper.py`): 2,792 paper-trade outcomes fed back online+offline — the brain learns from its own paper-trade losses and avoids repeating them |

## How to install / attach the brain

### Option A: Backtest / paper-trade CLI

```bash
python3 -m venv /tmp/abvenv
/tmp/abvenv/bin/pip install --break-system-packages numpy pandas scipy requests

PYTHONPATH=src /tmp/abvenv/bin/python -u -m abutron.backtest.run \
    --data /path/to/market-data \
    --broker fpmarkets --symbol "EURUSD.r" --timeframe M5 --htf H1 \
    --equity 1000 --risk-pct 0.01 \
    --brain data/brains/unified_m5 --json
```

The `--brain data/brains/unified_m5` flag loads `nexus_brain.json` from that directory.

### Option B: Load in Python (APEX live engine)

```python
import sys; sys.path.insert(0, "src")
from abutron.brain.unified import load_unified, unified_brain_dir

brain = load_unified(unified_brain_dir())  # loads data/brains/unified_m5/nexus_brain.json
proposal = brain.decide(symbol="EURUSD.r", closes=closes, highs=highs, lows=lows,
                        orderflow=of, footprint=fp, agent_scores=scores, regime=regime)
```

### Option C: Drop-in replacement

```bash
mkdir -p /path/to/brain_dir
cp nexus_brain.json /path/to/brain_dir/nexus_brain.json
```

The codebase looks for `nexus_brain.json` inside the directory passed to `--brain` or `load_unified()`.

## Retraining

```bash
PYTHONPATH=src python -u scripts/train_unified.py --epochs 80 --data /tmp/market-data
PYTHONPATH=src python -u scripts/train_online.py --data /tmp/market-data
PYTHONPATH=src python -u scripts/train_from_trades.py --trades '/tmp/abutron-pay/*/*/deals.csv.gz' --data /tmp/market-data --mode online
PYTHONPATH=src python -u scripts/train_from_trades.py --trades '/tmp/abutron-pay/*/*/deals.csv.gz' --data /tmp/market-data --mode offline --epochs 20
PYTHONPATH=src python -u scripts/train_from_paper.py --data /tmp/market-data --mode both --epochs 15
```

## Required data

- **Market data:** `abutronmall-netizen/market-data`
- **Trade history:** `abutronmall-netizen/abutron-pay`

## Dependencies

Python 3.11+, numpy, pandas, scipy, requests, and the `abutron` package (in `src/`).
