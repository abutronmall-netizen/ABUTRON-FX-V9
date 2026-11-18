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

  The vendored tree lives in-repo at `vendor/abutron-apex/` (`apex11/` +
  `risk_guard/`) and is loaded by `apex_integration._load_apex`, which
  registers both import roots itself — no `PYTHONPATH` setup is required.
  When the tree is absent the platform degrades cleanly to the NexusBrain
  path and reports `APEX_VENDOR_MISSING` per candle instead of crashing.
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

  The plan is computed by `ApexIntegration.build_entry_plan` on the live
  decision path (it consumes a rolling per-symbol OHLC history), merged into
  the broker-native review geometry by `review_geometry`, attached to the
  READY candidate, persisted on the candidate row (`entry_type`,
  `trigger_price`, `invalidation`, `entry_quality`, `sniper_plan`, `trail_k`,
  `trail_stop`, `trail_plan`), and forwarded to the EA by the dispatcher.
- **Brain lot sizing + trailing stop + loss-learning**
  (`src/abutron/brain/brain.py`, `src/abutron/apex/trailing.py`) — the AI
  brain performs ALL the calculation; the EA only executes. **Lot size**
  is computed from client equity + per-trade risk + the sniper stop
  distance, scaled by entry quality, and sent as the exact lot. A
  **trailing stop** (Chandelier: high/low ± 3·ATR) tightens in favour to
  lock profit. The executed lot is **clamped to the volume the candidate
  contract validated** against the 0.10% per-trade risk policy and the 20%
  portfolio ceiling, so quality-scaled sizing can never breach a limit the
  contract just proved safe. **Self-learning from losses**: every settled trade feeds
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
- **Multi-tenant MT5 API gateway** — a MetaApi-style REST + WebSocket
  bridge (`src/abutron/gateway/`) with API keys, scopes, rate limits,
  idempotent trading, signed webhooks and a NAT-friendly MQL5 bridge EA.
  See [`docs/MT5_GATEWAY.md`](docs/MT5_GATEWAY.md).
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

## MT5 API gateway (multi-tenant)

A second product surface ships alongside the autotrader: a **MetaApi-style
MT5 API bridge** (`src/abutron/gateway/`) that gives each client an API key
and lets them read market data, positions and history, trade, and stream
events over REST/WebSocket — with no shared writer and full tenant
isolation.

```
client bots ── REST/WS (API keys) ──▶ API tier
                                      │ per-account bridge token
                                      ▼
                              bridge EA / simulated transport
                                      │ single writer
                                      ▼
                              MT5 terminal (Wine or Windows)
```

- **REST + WebSocket** — accounts, symbols/quotes/candles, positions,
  orders, deals, market/pending orders, modify, partial close, cancel,
  `/v1/stream` event feed, signed webhooks, audit.
- **Tenant isolation** — hashed `abk_...` keys, `read`/`trade`/`manage`
  scopes, per-key rate limits, cross-tenant ids return 404.
- **One writer per account** — `control_mode: api` vs `read_only`;
  provisioning refuses an account APEX automation is currently driving
  (`409 AUTOMATION_WRITER_ACTIVE`), and accounts the platform already
  automates are mirrored **read-only** (`market_data_mode: engine`) so
  positions, deals, quotes and candles are visible through the same API.
- **Honest execution** — `200 EXECUTED` only on a transport ACK; a slow
  bridge yields `202 QUEUED` + a command id, never a fabricated fill.
- **Runs without Wine** — a deterministic simulated transport powers the
  preview and the 35 gateway tests; the MQL5 bridge
  (`mql5/AbuGatewayBridge.mq5`) connects real terminals over outbound HTTP
  (NAT-friendly pull/push).
- **Part of the platform, not a vendor** — `./run.sh` starts the portal
  (`:19743`) and the gateway (`:19744`) in one process. No MetaApi account,
  SDK or outbound third-party call is involved; broker access is the MT5
  terminal's own login.

```bash
./run-gateway.sh
# → http://localhost:19744/docs      (Swagger)
# → http://localhost:19744/          (demo key + quickstart)

export ABUTRON_GATEWAY_KEY=abk_demo_0123456789abcdef0123456789abcdef
python examples/gateway_client.py --demo
```

Full design, API reference, bridge protocol and scaling notes:
[`docs/MT5_GATEWAY.md`](docs/MT5_GATEWAY.md).

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

# The platform starts the multi-tenant MT5 API gateway too
# → http://localhost:19744/docs   (ABUTRON_GATEWAY_ENABLED=true)
# Standalone instead: ./run-gateway.sh
```

## Tests

```bash
.venv/bin/python -m pytest            # platform + gateway tests
.venv/bin/python -m pytest tests/test_gateway.py tests/test_gateway_platform.py -q   # 35 gateway tests
.venv/bin/python -m pytest vendor/abutron-apex/apex11/tests   # 210 vendored APEX tests
```

`pytest.ini` registers `src/` on the path, so no `PYTHONPATH` is needed.

**202 tests** covering feed quality, the **NexusBrain** decision brain
(neural backprop, ensemble uncertainty, conviction-based sizing,
walk-forward training, engine integration), decision logic, account auth,
risk sizing/caps, single-writer execution, client OFF, order flow +
footprint, reconciliation, the **real APEX integration** (instrument
manifest → signal bridge → advisory review → READY candidate), the **AI
entry pipeline** (sniper geometry → trailing stop → brain lot sizing) and
the **backtest / forward-test / training** pipeline.

The vendored APEX source also ships its own **210-test** suite, which the
repo ships a `conftest.py` bootstrap for so it runs unmodified:

```bash
.venv/bin/python -m pytest vendor/abutron-apex/apex11/tests -q   # 210 passed
```

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
