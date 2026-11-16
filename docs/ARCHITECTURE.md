# ABUTRON FX V9 — Technical Architecture

This document maps the rebuilt platform to the **ABUTRON / APEX 1.1
Technical Blueprint** (13 Nov 2026). It is an engineering reference, not a
deployment certificate.

## 1. Actors and trust boundaries

| Actor | Role | Boundary |
|-------|------|----------|
| Client | Link DEMO accounts, view holdings/signals/trades, request ON/OFF | Cannot impersonate owners or write native receipts |
| Admin / operator | Licenses, entitlements, MT5 linkage, diagnosis | Human authority separate from trading engine |
| APEX signal processor | Closed-candle decisions | Signal-only; no broker credentials or ACK |
| MT5 manager / worker | Per-account terminal sessions | Native artifacts bound to one login/server |
| Native execution writer | Reserve/deduplicate, place broker order | Exactly one writer per account/candidate |
| Reconciler / auditor | Receipts vs native history | May correct evidence; never invents fills |

**System of record**: broker-native MT5 tickets, deals, SL/TP, position
history and owner-bound ledger. APEX signal journals are **not** authority.

## 2. Module map

```
src/abutron/
├── config.py            global config + AccountSpec
├── events.py            canonical chained audit event envelope
├── db.py                SQLite: accounts, candidates, receipts, ledger, portal
├── crypto.py            SHA256 pins, HMAC signing, activation revisions
├── feeds/
│   ├── feed_quality.py  feed-quality contract (closed candles, provenance)
│   ├── mt5_terminal.py  Wine/MT5 terminal supervisor (per-account prefix)
│   ├── broker_feeds.py  Deriv + FP Markets native readers
│   └── clock_offset.py  broker clock offset estimation
├── analytics/
│   ├── orderflow.py     tick flow: delta, imbalance, absorption
│   └── footprint.py     volume profile: POC, HVN/LVN, value area
├── apex_integration.py  REAL APEX 1.1.0 bridge + candidate review wiring
├── apex/                (local indicator reference + journal helpers)
├── selflearn/           TRUE self-learning AI (model-weight updates)
│   ├── features.py      15-feature canonical order-flow/footprint vector
│   ├── online_model.py  online SGD edge predictor (weight self-update)
│   ├── skill_learner.py per-(agent,symbol,regime) EWMA skill + trust
│   ├── guardrails.py    weight-only overlay clamps + safety invariant
│   └── engine.py        SelfLearningEngine: observe→overlay→save/load
├── execution/
│   ├── state.py         RESERVED→SEND→ACK→OPEN→PROTECTED→CLOSED machine
│   ├── writer_gateway.py single-writer lock + signed order send
│   ├── dispatcher.py    per-account native dispatch
│   ├── receipts.py      native receipt recording (ACK/UNKNOWN)
│   └── ipc.py           manager Unix-socket protocol
├── reconciliation/reconciler.py  broker-native matching
├── risk/risk_engine.py  position risk, owner cap, trailing stops
├── risk/riskguard.py    hardened pre-trade gate (hard-limit rules)
├── risk/simulator.py    paper-trading simulator (pre-deployment)
├── crm/controls.py      accounts, licenses, ON/OFF state machine
├── ops/health.py        health + fault-code registry
├── ops/portal_ops.py    runs/traces, analytics, leaderboard, providers
├── workers/             mt5_worker, dispatcher_worker, reconcile_worker
└── portal/              FastAPI app + client/admin/command templates
mql5/ApexNativeWriter.mq5   native single-writer EA
vendor/abutron-apex/     OPTIONAL vendored APEX 1.1.0 source
                         (clone via scripts/link_all.sh --vendor; the
                         platform runs without it on NexusBrain + apex/)
  apex11/apex/           regime/trend/meanreversion/breakout/portfolio/risk
                         agents, engine, covariance, memory, llm advisor
  apex11/abutron_replacement/  signal-only bridge + candidate_contract +
                         exclusive native writer + transactional activation
  apex11/config/         abutron_signal_only.yaml, default.yaml, deriv_only.yaml
deploy/                   systemd units + install script
```

## 2a. Decision engine — APEX bridge (optional vendor) + NexusBrain

The decision engine is **not** a reconstruction: when the real APEX 1.1.0
source is vendored under `vendor/abutron-apex/` (clone via
`scripts/link_all.sh --vendor`), it is driven through
`abutron/apex_integration.py`. The vendor is **optional** — without it the
platform runs on the NexusBrain + `abutron/apex/engine.py`, and
`ApexIntegration` degrades gracefully. The flow per closed M5 candle:

1. **Feed** — native MT5 exports are validated by the feed-quality contract
   (closed candle, provenance nonce, geometry).
2. **APEX bridge** — `AbutronApex11SignalBridge.on_closed_candle` runs the
   full engine: Regime → Trend/MeanReversion/Breakout → Portfolio (regime
   trust + online reputation + vol targeting) → **NexusBrain** (the signal
   authority, always committing to BUY/SELL with conviction-scaled sizing).
   Output is a signal-only proposal.
3. **Advisory review** — `candidate_contract.review_candidate` checks
   attestation (account binding, native EA, single-writer, entitlement,
   per-trade risk, portfolio ceiling, broker calc-profit/order-check). The
   verdict is journaled but never blocks — a proposal becomes a READY
   `order_candidate` — never an order.
4. **Reserve** — the READY candidate is stored with the canonical signal_id
   as the replay key (at-most-once).
5. **Execute + reconcile** — the single-writer dispatcher sends the native
   order; native receipts are recorded; the reconciler matches
   broker ticket/deal/SL/TP.

## 2b. NexusBrain — the advanced decision brain (signal authority)

The **signal authority** is the NexusBrain (`src/abutron/brain/`), rebuilt
from scratch — **not** the legacy rule-based multi-agent voting. It replaces
the agent-vote direction/conviction decision with an uncertainty-aware
ensemble of neural encoders. It is the **always-trading signal authority**:
confidence and uncertainty are computed and reported for transparency, but
never gate the decision.

- **Architecture** — a pure-NumPy ensemble of diverse members (snapshot +
  sequence MLPs) trained with real backprop + Adam + gradient clipping.
  Sequence awareness comes from a lightweight attention encoder over the
  trailing price window, so the brain consumes the recent *path*, not a
  static snapshot.
- **Features** — a versioned 23-feature vector combining price-sequence
  indicators (returns, momentum z, realised-vol z, EMA cross, RSI, ATR,
  Bollinger width), order-flow (imbalance, delta, absorption), footprint
  (VA imbalance, POC share), regime one-hots, and the legacy agent scores
  (kept as features, not authority).
- **Uncertainty** — fused from **ensemble disagreement** and **MC-dropout**
  spread, giving the brain a principled sense of when it does not know.
- **Calibration** — a Platt-sigmoid pass on a held-out slice so stated
  confidence is meaningful.
- **Always-trade** — the brain commits to a BUY/SELL direction on every
  non-zero edge; confidence and uncertainty are reported but never gate the
  decision (fail-closed abstention removed). Conviction scales position
  size (`0.25 + 0.75·conviction`, capped by `BRAIN_SIZE_CAP`).
- **Online adaptation** — a bounded scalar edge model updated per bar with a
  **forgetting gate** (EMA on loss) and an **out-of-sample gate** that
  rejects updates that worsen the running loss (anti-catastrophic-forgetting).
- **Fallback** — when untrained, a bounded deterministic rule-based edge is
  used so the platform never breaks.
- **Training** — offline walk-forward (`prepare_samples` →
  `walk_forward_train` → `save_brain`), train/val split, calibration,
  persisted to `data/brain/nexus_brain.json`.

The brain is **signal-only**: it proposes direction + conviction +
confidence + uncertainty. It never places orders or sets stops. The legacy
agents are retained as feature contributors and diagnostics, and the
Gemma/debate advisory remains advisory-only. The RiskGuard / candidate
contract / single-writer layers are retained as advisory checks and the
single-writer execution path — they report but never block.

## 2c. True self-learning AI — model-weight self-training

The vendored APEX ships adaptive learning (EWMA reputation + causal
reflection memory) but **does not retrain model weights**. ABUTRON FX V9
adds a genuine self-learning layer at `src/abutron/selflearn/` that
observes live closed-bar outcomes and updates its own parameters every bar.

**Learner components**

- **`online_model.py` — `OnlineLinearModel`.** An online SGD edge predictor
  over the 15-feature canonical vector (`features.py`): order-flow
  imbalance, footprint imbalance, regime one-hot, agent scores, momentum_z,
  realised-vol_z. Per-feature online mean/var normalization and an adaptive
  EWMA learning rate. Update rule:
  `w ← w + lr·(y − ŷ)·x − l2·lr·w`, where `y` is the realized forward edge
  (signed return vs horizon).
- **`skill_learner.py` — `SkillLearner`/`SkillCell`.** Per-(agent, symbol,
  regime) EWMA of the signal's Information Coefficient (IC), with a
  t-statistic. Derives a bounded **trust** in `[0.5, 1.6]`; well-performing
  agents earn higher conviction weight, noise agents decay toward the floor.
- **`guardrails.py` — `SelfLearnGuardrails`.** `build_overlay()` clamps the
  global multiplier to `[0.5, 1.5]` and per-symbol to `[0.5, 1.5]`.
  `apply_to_weights()` **multiplies** convictions only — it preserves signal
  direction and never introduces new symbols. The static
  `execution_safety_assertion()` invariant asserts the learner holds no
  execution authority.
- **`engine.py` — `SelfLearningEngine`.** `produce_overlay()` maps predicted
  edge + skill trust to a bounded conviction multiplier; `apply_overlay()`
  applies it weight-only; `observe_outcome()` runs the SGD weight update +
  skill update on the settled bar. `save()`/`load()` persist state to
  `data/selflearn_state.json` so learned weights survive restarts.

**Causal wiring** — `ApexIntegration._run_selflearn()` runs on every closed
candle: it first settles the *prior* bar's outcome (`observe_outcome`), then
computes an overlay and adjusts the research-only target weight as
**advisory** input. The overlay is clamped and weight-only — it can never
touch the candidate, place, or modify an order, and never raises risk beyond
the bounds reported by the advisory review. `main.engine_tick_loop`
persists self-learn state each tick.

**Supervision** — the command center surfaces a **Self-Learning AI** card
(`GET /api/command/selflearn`, `POST /api/command/selflearn/save`) showing
model updates, observations, skill cells, MSE, the last overlay
multiplier, and the `execution_authority: false` badge.

## 2d. Sniper entry — precise, low-drawdown entries

**Entry precision is what controls drawdown.** A correct direction entered
at a bad price still draws down before it runs, so the platform does not
market-enter at the close. Every entry is a *sniper entry* computed by
`apex/sniper.py` (`SniperEntryEngine.plan`) from the market's own structure:

- **LIMIT (pullback) entry** — enter at a structural level (recent swing,
  EMA21, Donchian band) where the signal edge is still intact. Filling at
  the level means the trade starts near its edge, so adverse excursion to
  invalidation is minimal. Levels must be on the correct side of price and
  within `PULLBACK_ATR_MAX` (4 ATR) to be a practical pullback target.
- **CONFIRM (breakout) entry** — when price is gapped/extended and no
  practical pullback level exists, enter on a continuation trigger (the
  current close) rather than chasing a stretched level.
- **Tight invalidation** — a stop just beyond the trigger
  (`INVALIDATION_ATR_FRAC` = 0.3 ATR), so a wrong entry exits with a tiny
  loss instead of a full ATR.
- **Tight take-profit** (`TP_ATR_MULT` = 1.2 ATR) — locks profit fast,
  reducing time-in-trade drawdown.
- **Entry quality score** (0..1) — ranks how clean the entry is, combining
  level proximity, order-flow alignment, footprint value-area alignment,
  regime alignment, conviction, and an RSI overextension penalty for
  CONFIRM entries.
- **Sniper sizing** — position size is scaled by entry quality
  (`0.4 + 0.6·quality`): a sniper-grade entry takes full size; a stretched
  entry takes less. This is sizing by entry precision — still always-trade
  (every edge is entered), but drawdown is minimised by entering precisely
  and sizing down when the entry is imperfect.

The sniper geometry (`entry_type`, `trigger_price`, `invalidation`,
`entry_quality`, `sniper_plan`) is attached to every `Candidate`, persisted
to the `candidates` table, and carried through the dispatcher into the
native order request: the EA places a **LIMIT order at the trigger price**
for LIMIT entries, or a market order for CONFIRM entries, with the tight
invalidation as the stop. The recorded fill price is the sniper trigger.

## 2e. Lot sizing, trailing stop, and self-learning

**The AI brain performs ALL the calculation; the EA only executes.**

- **Lot sizing from client equity** — `NexusBrain.size_position()` computes
  the lot size for every trade from client equity + per-trade risk + the
  sniper stop distance: `risk_usd = equity · risk_pct`;
  `risk_per_lot = stop_distance_points · point_value`; `lots = risk_usd /
  risk_per_lot`, rounded to the broker lot step and clamped to
  `[lot_min, lot_max]`. Sniper entry quality scales the risk (a cleaner
  entry risks more → a larger lot). The computed lot is carried on the
  candidate and sent to the EA, which places exactly that lot — it does no
  sizing of its own.
- **Trailing stop** — `apex/trailing.py` (`TrailingStop.plan`) computes a
  Chandelier-style trail leg: for a long, `trail = highest_high − k·ATR`;
  for a short, `trail = lowest_low + k·ATR` (`k = 3.0` by default). The
  trail only tightens (never widens) and never worsens the initial
  invalidation. The trail leg (`trail_k`, `trail_stop`) is attached to the
  candidate and sent in the order request so the EA trails the broker stop
  in favour, locking in profit as the trade moves.
- **Self-learning from losses — never enter too early** — the brain's
  `TradeMemory` records every settled trade per (symbol, regime). A **loss**
  raises `confirm_bias` and `quality_floor`: the sniper entry engine then
  prefers **CONFIRM** entries over LIMIT pullbacks (it waits for price to
  confirm the direction instead of entering early) and floors the entry
  quality so the brain demands a cleaner setup. A **win** decays the biases
  back toward zero. The settlement hook lives in the reconciler: when a
  native trade is matched as `CLOSED`, `Reconciler._match_receipt` calls
  `brain.learn_from_trade(...)` with the realised PnL, closing the loop
  from loss → learned entry-timing bias → no early entry. `ApexIntegration`
  owns the brain so the decision path and the settlement path share it.

## 3. Closed-candle decision and order chain

1. **Capture** — native MT5 exports account identity + M5 bars; reader
   validates feed-quality contract (closed candle, provenance, nonce).
2. **APEX signal** — the vendored engine computes regime + ensemble +
   paper-risk proposal (signal-only; no order authority).
3. **Advisory review** — the NexusBrain is the signal authority; the
   candidate contract validates and reports a READY order candidate (the
   verdict is journaled but never blocks a trade).
4. **Validate** — CRM desired/effective ON, licensing, owner binding,
   signal identity, broker clock, spread/cost, native positions, portfolio
   cap.
5. **Reserve** — at-most-once reservation on the canonical `signal_id`.
6. **Execute + reconcile** — single-writer sends; native receipt recorded;
   reconciler matches ticket/deal/SL/TP.

## 4. Execution evidence state machine

`RESERVED → SEND → UNKNOWN | ACK → OPEN | PROTECTED → CLOSED → SETTLED`

UNKNOWN is **never** silently upgraded. It blocks resubmission until the
reconciler resolves it against native history.

## 5. Order flow + footprint

- **Order flow** (`analytics/orderflow.py`): aggregates aggressor trades
  per closed bar → delta, cumulative delta, imbalance ratio, max
  imbalance, absorption (large blocks holding price).
- **Volume footprint** (`analytics/footprint.py`): buckets trades by price
  into tick cells → volume profile, POC, HVN/LVN, 70% value area, cell
  buy/sell imbalance. Features feed the APEX context and command-center
  visualization.

## 6. Gemma advisory — multi-agent debate (TradingAgents pattern)

`apex/gemma.py` is the single-advisor fallback; `apex/debate.py` upgrades it
to a **multi-agent bull/bear debate + risk-debator consensus** (the
TradingAgents pattern), all calling the same Gemma endpoint (LiteLLM/Ollama/
Google AI). Per decision:

1. **Bull & bear researchers** argue opposing cases from the regime, agent
   opinions, order-flow and footprint features.
2. **Aggressive / conservative / neutral risk debators** challenge the
   deterministic agents' direction, weighing the bull and bear cases.
3. **Research manager** synthesises the debate into one signal/confidence/
   narrative advisory (journaled with the full debate history).

The debate runs a bounded number of rounds and total LLM calls
(`ABUTRON_DEBATE_ROUNDS`, `ABUTRON_DEBATE_MAX_CALLS`) so it does not
dominate loop latency. **Advisory only** — a disabled, failing, refusing, or
budget-exhausted debate degrades to `NO_ADVISORY` (or, if the manager call
fails but debaters succeeded, a simple consensus aggregate). It never
changes anything about execution, and never touches the order path.

## 7. Risk controls

- Per-trade risk: default 50% (owner-configurable via
  `ABUTRON_PER_TRADE_PCT`).
- Owner aggregate cap: 100% of equity in USD stop-risk (owner-configurable
  via `ABUTRON_RISK_CAP_PCT`).
- Stops: broker-side SL/TP; stop geometry validated (tick size, point
  value, ATR width); trailing stops tighten in-favor only.
- Multitrade: new + existing positions satisfy independent protection +
  aggregate exposure.
- ON/OFF: client OFF blocks new entries; protective management continues.

**RiskGuard + paper simulator** (`risk/riskguard.py`,
`risk/simulator.py`, kweinmeister pattern, FX-adapted) — an optional
*pre-trade* risk check distinct from the portfolio `RiskEngine`.
`RiskGuard.check_trade()` evaluates a proposed trade against risk limits:
per-trade risk cap, max position size (lots), per-symbol concentration,
and total gross exposure — returning a machine-readable verdict. **Advisory
only**: when `ABUTRON_RISKGUARD_ENABLED` is on, `ApexIntegration` runs the
guard on every READY candidate and journals the verdict, but the verdict
never blocks the candidate (fail-closed gating removed).

The **paper simulator** replays a trend+volatility price stream
(`MarketSimulator`) against a risk-gated paper portfolio
(`PaperTradingEngine`) with a strategy callback, recording trades, P&L and an
equity curve — a pre-deployment validation harness that never references
DEMO/LIVE, the broker, or the writer. Exposed via
`GET /api/command/riskguard` and `POST /api/command/simulate`.

## 8. Fault codes

`ops/health.py` maps blueprint fault codes to domains and first checks:
`CRM_EFFECTIVE_ON_OWNER_AND_ACK_UNVERIFIED`, `FP_SINGLE_WRITER_DISARMED`,
`BINARY_SOURCE_MISMATCH`, `BROKER_CLOCK_OFFSET_UNVERIFIED`,
`CANDLE_ALREADY_CONSUMED`, `SEND_UNKNOWN`, `OPEN_UNPROTECTED`,
`PROCESS_EXITED`.

## 9. Four evidence statuses

- **CONNECTED** — worker session/heartbeat (not EA armed).
- **PUBLISHING** — closed-candle decisions exist (not an order sent).
- **EFFECTIVE ON** — CRM execution authority passed (not a fill).
- **BROKER CONFIRMED** — native ticket/deal with SL/TP + ledger match.

## 9a. Portal ops — runs, analytics, leaderboard, model providers

`ops/portal_ops.py` (AgenticTrading dashboard pattern, ABUTRON-adapted)
provides operator observability and configuration on top of the existing
SQLite engine DB and audit EventLog:

- **Runs / traces** — every decision cycle in
  `ApexIntegration.process_candle` is recorded as a `runs` row (run_id,
  symbol, status, decision, signal_id, regime, conviction, gemma signal/
  confidence, riskguard result, blockers), so a signal can be traced
  end-to-end. Exposed via `GET /api/command/runs`.
- **Analytics** — aggregates candidates (by execution state), receipts (by
  execution/reconciled state), closed trades, win rate, realized P&L,
  commission, swap and net P&L, derived from reconciled native facts.
  `GET /api/command/analytics`.
- **Leaderboard** — ranks owners by realized P&L, trade count and win rate.
  `GET /api/command/leaderboard`.
- **Model-provider registry** — stores only non-secret LLM provider config
  (provider, base_url, model, enabled, env-secret var name). The actual
  secret is injected via environment variables — never stored in the repo or
  DB. CRUD via `GET/POST/DELETE /api/command/model-providers`.

## 9b. Multi-user client connect flow

`crm/sessions.py` + `CrmService.connect_broker_account` (blueprint Identity
layer + C10–C16) turn the client portal into a per-tenant surface:

- **Sessions** — login issues an HttpOnly `abutron_session` cookie whose
  token is stored only as a SHA-256 hash (`sessions` table). 7-day TTL,
  revocable on logout. `POST /api/client/login` / `/logout`,
  `GET /api/client/me`.
- **Tenant isolation** — every client API resolves the session's `owner_id`
  and scopes queries/mutations to it; a connection ID belonging to another
  owner returns 404 (no ID guessing). C15.
- **Connect Trading Account — split enroll / verify** — `POST
  /api/client/connect-account` enrolls a connection in `CONNECTING` with
  **no password** (rate-limited, duplicate-checked per owner, DEMO/LIVE
  gated). The client's MT5 terminal is launched via `Mt5TerminalManager.launch`.
  `POST /api/client/accounts/{id}/verify` then submits the broker password
  **only** to the isolated `Mt5Verifier`, which performs a one-shot native
  DEMO login and returns a boolean + reason code. The password is never
  persisted or returned.
- **Identity proof ≠ persistent connection** — a successful native verify
  records `NATIVE_DEMO_IDENTITY_PROVED` and sets a `worker_binding`, then
  marks `CONNECTED`. It does **not** grant trading or orders. `CONNECTED`
  requires a worker binding; the account row tracks `broker_verified_at`.
  `POST /api/client/accounts/{id}/disconnect` sets `DISCONNECTED` (blocked
  while an active session exists).
- **Seven connection statuses** — `NOT_LINKED → CONNECTING → AUTHENTICATING →
  CONNECTED → DEGRADED → DISCONNECTED`, terminal `ERROR`/`REVOKED`, persisted
  on the account row (`conn_status`). A verification failure sets `ERROR`
  with the reason code. `GET /api/client/accounts/{id}/status`.
- **ON/OFF — immediate grant** — `request_on` grants trading **immediately**
  for both DEMO and LIVE: it sets `desired_on=1 / effective_on=1` together
  (no pending broker-ACK gate). `confirm_effective_on` is retained as an
  advisory confirmation that never blocks. `request_off` sets
  `desired_on=0` immediately (blocks new entries) while the worker confirms
  effective OFF; protective SL/TP for existing positions are preserved.
- **DEMO/LIVE — both enabled** — LIVE is **enabled by default**
  (`ABUTRON_LIVE_TRADING_ENABLED=true`). A `live` enroll is accepted
  directly; no separate approval or auto-promotion step. Both brokers
  (FP Markets + Deriv) trade in DEMO and LIVE.

## 10. Validation matrix

| Layer | Scenario | Evidence |
|-------|----------|----------|
| Feed | closed/stale/future candles, envelope | rejection with reason code |
| Decision | trend/range/high-vol, conviction ranking | stable replay keys |
| Auth | wrong owner, bad pin, duplicate | denied, no order |
| Risk | stop too wide, cap exceeded, cost drift | no entry or reduced volume |
| Writer | binary mismatch, duplicate writer, UNKNOWN | no double order |
| Reconcile | ticket/deal/SL/TP, trailing, restart | matched, no UNKNOWN |
| Client OFF | one account off, others active | no new entries |

All implemented in `tests/test_platform.py` (26 tests).
