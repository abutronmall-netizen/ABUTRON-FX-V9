# ABUTRON FX V9 — Fully Automated AI Trading Platform

A fully automated, AI/ML-driven trading platform for **FP Markets** and
**Deriv**, running **MetaTrader 5 on Ubuntu via Wine**. It integrates the
authoritative **ABUTRON / APEX 1.1.0** decision engine (vendored from
`abutronmall-netizen/abutron-apex`) with native broker single-writer
execution, broker-native reconciliation, and three operator surfaces
(client, admin, command center).

> **Evidence discipline** — a green portal, an active timer, a published
> BUY/SELL, or a requested ON flag is **not** an accepted trade. The only
> completion event is an authenticated broker ticket/deal with SL/TP and a
> reconciled position lifecycle.

## What's included

- **Real APEX 1.1.0 decision engine** — vendored from the authoritative
  `abutron-apex` repository (`vendor/abutron-apex/`). A portfolio-weight
  risk governor: RegimeAgent, Trend/MeanReversion/Breakout/Carry signal
  agents, PortfolioAgent (regime-conditional trust + online reputation),
  RiskAgent (per-instrument cap, asset-class cap, correlation clusters,
  net/gross leverage, portfolio vol target, VaR, drawdown/daily-loss kill
  switch). The `abutron_signal_only` bridge computes signal-only proposals.
- **NexusBrain — advanced uncertainty-aware decision brain**
  (`src/abutron/brain/`) — the **signal authority**, rebuilt from scratch
  (not the legacy rule-based agent voting). A pure-NumPy ensemble of diverse
  neural encoders (snapshot + sequence MLPs) trained with real backprop +
  Adam. It is an **always-trading signal authority**:
  **ensemble + MC-dropout uncertainty**, **calibrated confidence**,
  **regime conditioning**, and **conviction-scaled position sizing**, with
  **online adaptation** (forgetting gate + out-of-sample check) to avoid
  catastrophic forgetting. Confidence and uncertainty are reported for
  transparency but never gate the decision — the brain commits to a
  BUY/SELL direction on every non-zero edge. Order-flow / footprint /
  indicator features feed the encoders; the legacy agents are retained as
  feature contributors + diagnostics. A deterministic rule-based fallback
  keeps the platform running when untrained. The brain is **signal-only** —
  it proposes direction + conviction + confidence + uncertainty and never
  places orders. Train offline with walk-forward validation
  (`prepare_samples` / `walk_forward_train` / `save_brain`).
- **Sniper entry — entry precision controls drawdown**
  (`src/abutron/apex/sniper.py`) — no market-at-close fills. Every entry
  is a precise, trigger-based plan: a **LIMIT pullback** to a structural
  level (swing / EMA / Donchian) where the edge is intact, or a **CONFIRM**
  breakout entry when price is extended. Each carries a **tight
  invalidation** (0.3 ATR) and a **tight take-profit** (1.2 ATR) so a
  wrong entry loses tiny and a winner locks profit fast. An **entry-quality
  score** ranks entries, and **size scales with entry precision** — a
  sniper-grade entry takes full size, a stretched entry takes less. The
  geometry flows through the dispatcher to the native EA, which places a
  LIMIT order at the trigger price. Still always-trade — every edge is
  entered — but drawdown is minimised by entering precisely.
- **Brain lot sizing + trailing stop + loss-learning**
  (`src/abutron/brain/brain.py`, `src/abutron/apex/trailing.py`) — the AI
  brain performs ALL the calculation; the EA only executes. **Lot size**
  is computed from client equity + per-trade risk + the sniper stop
  distance, scaled by entry quality, and sent as the exact lot. A
  **trailing stop** (Chandelier: high/low ± 3·ATR) tightens in favour to
  lock profit. **Self-learning from losses**: every settled trade feeds
  the brain's `TradeMemory`; a loss raises the confirm-bias and
  entry-quality floor so the brain prefers CONFIRM entries and waits for
  a cleaner setup — it learns to never enter too early. Wins decay the
  bias. Settlement is wired through the reconciler.
- **True self-learning AI** (`src/abutron/selflearn/`) — a live model-weight
  learner that observes closed-bar outcomes and updates its own weights every
  bar (online SGD edge predictor over a 15-feature canonical order-flow /
  footprint / regime / agent-score vector, plus a per-(agent,symbol,regime)
  EWMA skill learner with bounded trust). Adaptive learning rate, state
  persisted to `data/selflearn_state.json` across restarts. Advisory only:
  the learner adjusts conviction **weights** via clamped overlay multipliers
  and never gains execution authority (invariant-enforced), so it cannot
  place or modify orders.
- **Multi-agent LLM debate** (`src/abutron/apex/debate.py`) — upgrades the
  Gemma advisor to a TradingAgents-style **bull/bear debate + risk-debator
  consensus**: bull & bear researchers argue opposing cases, aggressive /
  conservative / neutral risk debators challenge the deterministic agents'
  direction, and a research manager synthesises a signal/confidence/narrative
  advisory (full debate history journaled). Bounded rounds + LLM-call budget;
  degrades to `NO_ADVISORY` or a consensus aggregate on any failure.
  Advisory only — never on the order path.
- **Hardened RiskGuard + paper simulator** (`src/abutron/risk/`) — a
  self-contained pre-trade risk check (kweinmeister pattern) that
  evaluates every candidate against risk limits (per-trade risk cap,
  max position size, per-symbol concentration, total exposure) and reports
  a machine-readable verdict. **Advisory only** — the verdict is journaled
  for transparency but never blocks a trade (fail-closed gating removed).
  Ships with an offline **paper-trading simulator** (trend+volatility price
  stream + risk-checked paper portfolio) to validate strategies pre-deployment
  without touching DEMO/LIVE. Opt-in via `ABUTRON_RISKGUARD_ENABLED`.
- **Portal ops — runs, analytics, leaderboard** (`src/abutron/ops/portal_ops.py`)
  — operator observability (AgenticTrading dashboard pattern): every decision
  cycle is recorded as a traceable **run** (APEX decision → review →
  candidate → dispatch → receipt); **analytics** aggregates candidates,
  receipts, trades, win-rate and P&L from reconciled native facts; a
  **leaderboard** ranks owners by realized P&L. A **model-provider registry**
  stores only non-secret LLM config (provider, base URL, model) — secrets are
  injected via environment variables, never stored.
- **Multi-user client connect flow** (`src/abutron/crm/sessions.py`,
  `start_broker_link` / `verify_broker_identity`) — per-client HttpOnly-cookie
  sessions with hashed tokens and server-side tenant isolation (one customer
  cannot reach another owner's accounts by ID). The client **Connect Trading
  Account** flow (blueprint C11) is split into enroll (no password) + verify:
  enrollment opens a per-account MT5 terminal in `CONNECTING`, then the
  broker password is submitted **only** to an isolated `Mt5Verifier` for a
  one-shot native login — never persisted or returned. A verified identity
  sets a worker binding and `CONNECTED`. **For a new LIVE account, once the
  portal connect succeeds the MT5 terminal is launched and the live session
  is activated (CONNECTED) automatically** — the terminal IS the broker
  session. Trading ON grants trading **immediately** for both DEMO and LIVE:
  `request_on` sets `desired_on=1 / effective_on=1` together (no pending
  broker-ACK gate). LIVE is **enabled by default**
  (`ABUTRON_LIVE_TRADING_ENABLED=true`) — DEMO and LIVE both trade, for
  both brokers (FP Markets + Deriv).
- **Order flow** — tick-level aggressor flow, cumulative delta, imbalance,
  absorption detection (overlay context fed to the decision journal).
- **Volume footprint** — volume profile by price, POC, HVN/LVN, value area,
  cell-level buy/sell imbalance.
- **Advisory candidate review** — the `candidate_contract.review_candidate`
  check: account binding, native EA attestation, single-writer exclusivity,
  per-trade risk, portfolio ceiling, broker calc-profit/order-check. Produces
  a READY order candidate — never an order. The review verdict is recorded
  for traceability but never blocks a trade (fail-closed gating removed).
- **Native execution** — per-account single-writer MQL5 EA
  (`ApexNativeWriter`), signed activation revisions, binary SHA256 pins,
  ACK / UNKNOWN / REJECTED receipts, at-most-once reservation on the
  canonical signal_id.
- **Broker-native reconciliation** — ticket/deal/SL/TP matching, trailing
  stops, UNKNOWN resolution, no zero-filling of missing exposure.
- **Three portals** — client, admin CRM, APEX command center (FastAPI).
- **Ubuntu/Wine deployment** — systemd units, per-account MT5 worker
  template, install script.

## Architecture

```
                      ┌───────────────────────────────────────────┐
   Native MT5 (Wine)  │  MT5 terminal per account (Wine on Ubuntu) │
   terminal exports   │  ApexNativeWriter.mq5 (SINGLE writer)      │
   bars / identity /  │  exports: identity, bars, order-flow,      │
   order-flow / receipts│  receipts · receives signed order reqs    │
                       └──────────────┬────────────────────────────┘
                                      │ Unix socket (manager.sock)
                       ┌──────────────▼────────────────────────────┐
                       │  APEX 1.1.0 (vendored, signal-only)        │
                       │  Regime → Signals → Portfolio → Risk       │
                       │  → NexusBrain (signal authority)          │
                       │  → candidate_contract.review (advisory)   │
                       │  → READY order candidate (NOT an order)    │
                       └──────────────┬────────────────────────────┘
                                      │ reserved candidate (signal_id)
                       ┌──────────────▼────────────────────────────┐
                       │  Native dispatcher (single-writer lock)    │
                       │  ACK / UNKNOWN / REJECTED receipts        │
                       └──────────────┬────────────────────────────┘
                                      │
                       ┌──────────────▼────────────────────────────┐
                       │  Reconciliation (broker-native authority)  │
                       │  ticket/deal/SL/TP · trailing · ledger    │
                       └────────────────────────────────────────────┘

   Self-Learning AI loop: closed-bar outcome → observe_outcome (SGD + skill
   update) → produce_overlay (clamped conviction multiplier, weight-only)
   → fed back as advisory input to APEX. Never executes.

   Portal: /client  /admin  /command   (FastAPI, port 19743)
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full blueprint.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env

# Run the portal + engine
./run.sh
# → http://localhost:19743/client  (client)
# → http://localhost:19743/admin   (admin CRM)
# → http://localhost:19743/command (command center)
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

145 tests covering feed quality, the **NexusBrain** decision brain
(neural backprop, ensemble uncertainty, conviction-based sizing,
walk-forward training, engine integration), decision logic, account auth,
risk sizing/caps, single-writer execution, client OFF, order flow +
footprint, reconciliation, the **real APEX integration** (signal bridge
→ advisory review → READY candidate), and the **backtest / forward-test /
training** pipeline. The vendored APEX source also ships its own **210-test**
suite (run from `vendor/abutron-apex/apex11`).

## Deploying to Ubuntu

```bash
sudo bash deploy/scripts/install.sh
sudo systemctl start abutron-portal.service
sudo systemctl start abutron-mt5-worker@<connection_id>
```

The installer sets up Wine, the Python environment, data directories, and
all systemd units. Native evidence (receipts, history) is root-owned.

## Backtest & forward test

The platform can backtest and forward-test the full AI pipeline
(brain → sniper entry → trailing stop → lot sizing) over the public
`abutronmall-netizen/market-data` dataset.

```bash
# list available symbols / timeframes
PYTHONPATH=src .venv/bin/python -m abutron.backtest.run \
    --data /path/to/market-data --list

# backtest Deriv Boom 1000 on M5
PYTHONPATH=src .venv/bin/python -m abutron.backtest.run \
    --data /path/to/market-data --broker deriv \
    --symbol "Boom 1000 Index.0" --timeframe M5 --equity 10000 --risk-pct 0.05

# forward test (trains the brain on the first 80%, evaluates the held-out 20%)
PYTHONPATH=src .venv/bin/python -m abutron.backtest.run \
    --data /path/to/market-data --broker deriv \
    --symbol "Boom 1000 Index.0" --timeframe M5 --forward --forward-frac 0.2
```

Point sizes are auto-detected from each instrument's price precision.
`--brain <dir>` loads a trained model instead of the untrained ensemble.

**Multi-timeframe confluence** — wait for timeframes to agree before
trading. Pass `--htf <timeframe>` (e.g. `--htf H1` when trading M5): the
engine only enters when the higher-timeframe direction agrees with the
lower-timeframe trigger. Disagreeing / not-yet-formed HTF directions are
skipped (counted as "filtered"). Omit `--htf` to trade every trigger.

**High-precision entry filters + win-rate optimisation** — win rate is
driven by TP distance and entry quality. The sniper geometry is
configurable and a set of entry filters drop low-probability setups to
push win rate up while keeping expectancy positive:

- `--tp-atr <n>` / `--sl-atr <n>` — sniper take-profit / stop-loss in ATR
  (a tighter TP raises win rate; the optimizer finds the tradeoff).
- `--min-conviction <0..1>` — skip entries below a brain-conviction floor.
- `--require-trend` — only enter in a trending regime.
- `--require-momentum` — only trade *with* momentum (BUY needs RSI>50,
  SELL needs RSI<50); drops counter-trend triggers.
- `--min-atr-pct` / `--max-atr-pct` — volatility band (skip dead / wild
  markets).

Filtered entries are counted and shown as `entry filters: N filtered`.

**Win-rate optimizer** — sweeps TP/SL geometry and the entry filters over
a train window, ranks combos by win rate subject to a positive-expectancy
guard, then validates the winner on a held-out test window:

```bash
PYTHONPATH=src .venv/bin/python -m abutron.backtest.optimize \\
    --data /path/to/market-data --broker deriv \\
    --symbol "Boom 1000 Index.0" --timeframe M5 --htf H1 \\
    --equity 10000 --risk-pct 0.05
```

On Deriv Boom 1000 M5 (untrained brain) this lifts win rate from ~68%
to **~81%** with profit factor ~7.9 and ~4% max drawdown, validated on
held-out bars. The expectancy guard prevents the degenerate "92% win
rate at ~0.3:1 reward:risk" that loses money — it maximises win rate
*subject to positive expectancy*.

**All-symbol sweep** — optimize every Deriv and FP-Markets symbol at
once and rank by held-out win rate:

```bash
PYTHONPATH=src .venv/bin/python -m abutron.backtest.optimize_all \\
    --data /path/to/market-data --broker all --timeframe M5 --htf H1 \\
    --equity 10000 --risk-pct 0.05 --grid fast --out data/opt_all.json
```

`--grid fast` sweeps ~16 combos/symbol (quick overview); `--grid full`
sweeps ~96 combos including tight TPs (0.3–0.5 ATR) and conviction
floors for refining the best instruments. Across the full dataset the
top symbols reach **~82% held-out win rate** (Boom 1000, EURUSD) — the
expectancy-guarded ceiling for this geometry; a trained brain is the
lever that lifts it further.

## Training the brain

Train the NexusBrain on historical candles and save the model:

```bash
# single walk-forward split
PYTHONPATH=src .venv/bin/python -m abutron.backtest.train \
    --data /path/to/market-data --broker deriv \
    --symbol "Boom 1000 Index.0" --timeframe M5 \
    --epochs 120 --out data/brains/boom1000_m5

# multi-fold walk-forward for a robust out-of-sample accuracy estimate
PYTHONPATH=src .venv/bin/python -m abutron.backtest.train \
    --data /path/to/market-data --broker deriv \
    --symbol "Boom 1000 Index.0" --timeframe M5 \
    --epochs 120 --folds 5 --out data/brains/boom1000_m5
```

The trainer reports **out-of-sample direction accuracy** (the honest
measure of a trained brain) and persists weights + calibration to
`data/brains/<symbol>_<tf>/nexus_brain.json`. Load it back with
`load_brain(...)` and pass it to `make_brain(ensemble=...)` for backtest,
forward-test, or live use. More candles = more stable OOS accuracy.

## Broker scope

| Broker | Accounts | Notes |
|--------|----------|-------|
| Deriv | DEMO (6207, 7593, 9244) | Synthetic indices via MT5 |
| FP Markets | DEMO (0512, 1636, 1622, 2582) | FX/CFD, `.r` symbol suffix |

DEMO and LIVE are both enabled for both brokers. request_on grants
trading immediately in either environment — no separate LIVE activation or
promotion step. The only requirement to trade is a CONNECTED broker session
and an active license.

## Security

- Secrets via environment injection only (`.env`); never in Git.
- Root-owned native evidence; portal cannot impersonate owners.
- One native writer per account; binary SHA256 pins; signed activation.
- UNKNOWN sends block resubmission until reconciled.
