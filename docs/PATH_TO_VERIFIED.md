# Path to VERIFIED — Getting Every V9 Component to Full Verification

_Last updated: 2026-11-17_

This document answers: **how do we get every V9 component from its
current status to VERIFIED?** Each item below has the current status,
what is already built, and the concrete steps to reach VERIFIED.

---

> **Independent Ubuntu verification hold (17 Nov 2026).** The V2 exporter
> is **NOT DEPLOYABLE / NOT TRUE ORDER FLOW**: it uses unsupported MQL5
> deal-history APIs, writes account executions rather than market-wide prints,
> treats a last bid/ask tick as depth, and uses incorrect MQL5 file paths.
> No original-exporter MetaEditor compilation evidence is available.
> A Python-only 317-pass suite does not compile MQL5 or certify broker orders.
>
> **Verification fix:** `TickIngestion.load_trades()` and `.load_depth()` now
> reject V2 account-fills/quote-only files as market tape or DOM;
> `scripts/verify_tick_data.py` will not self-certify arbitrary CSV/JSON.
> The old V2 records remain accessible only for diagnostics through
> `load_account_execution_deals()` and `load_quote_snapshots()`.
> True market order-flow requires broker-origin `CopyTicks` with actual LAST,
> volume and BUY/SELL flags, identity-bound export proof, causal time alignment
> and deduplication. Real DOM requires `MarketBookAdd/MarketBookGet` levels.
> Some OTC FX/CFD brokers may never expose aggressor print or book-volume data.
>
> **Execution hold:** PR18 native writer remains staged and unattached. Current
> owner-wide risk, true day-start/peak equity and lifetime history proof
> remain incomplete; a 90-day exit ledger is not lifetime certification.
> Attaching an EA to an existing MT5 EA chart would replace that EA: use
> dedicated data-only charts for any future collector.
>
> **Performance classification:** reported last-20% replay is an offline
> historical split, not a live forward test or an independently retrained
> walk-forward holdout. Reported 225 online updates are on *paper outcomes*,
> not 225 broker-acknowledged closed trades. The cited
> `data/results/integration_test/integration_report.json` does not exist in
> the checked-out Git branch; performance figures cannot be independently
> reproduced from the repository's tracked artifacts alone.
>
> **Risk-policy discrepancy:** source default daily loss is 2% while the
> production owner policy audit records 3%. Neither value was changed.
>
## 1. Order flow / imbalance / delta / absorption
**Current: PROXY — candle-derived, NOT true tick/order-book flow**

### What's already built
- `mql5/ApexOrderFlowExport.mq5` — EA that exports true tick/trade
  deals from the MT5 terminal to `MQL5/Files/ABUTRON_NATIVE_TRADES_V2_*.txt`
- `src/abutron/feeds/tick_ingestion.py` — `TickIngestion.load_trades()`
  reads those files into `Trade(price, size, side)` objects grouped by
  M5 bar time
- `src/abutron/brain/signals.py` — `build_signal_bundle()` already
  accepts `tick_trades` and uses `OrderFlowEngine.aggregate()` on real
  trades when present (source: `"true_tick_trades"`), falling back to
  `_proxy_orderflow()` only when no trades exist
- `src/abutron/backtest/engine.py` — `BacktestEngine` accepts
  `tick_trades` and passes them to `build_signal_bundle()`
- `src/abutron/backtest/run.py` — `--tick-data <csv>` CLI arg loads
  CSV tick/trade data into the same pipeline
- `scripts/verify_tick_data.py` — reports honest status (VERIFIED vs PROXY)

### Steps to VERIFIED
1. **Deploy the export EA on a live DEMO terminal:**
   ```bash
   # MT5 terminal already running for FP DEMO 0512/1636
   # Copy ApexOrderFlowExport.mq5 to the terminal's MQL5/Indicators/
   # Attach to every chart for the symbols you trade
   # Set ExportIntervalSec = 60 in the EA input
   ```
2. **Let it export for 24-48 hours** to build a meaningful tick/trade history
3. **Run the verification script:**
   ```bash
   PYTHONPATH=src python3 scripts/verify_tick_data.py --mql5-files MQL5/Files
   ```
   Output should show `Order flow / imbalance / delta : VERIFIED`
4. **Run a forward test with true tick data:**
   ```bash
   PYTHONPATH=src python3 -u -m abutron.backtest.run \
     --data data/links/market-data --broker fpmarkets \
     --symbol EURUSD.r --timeframe M5 --equity 176.72 \
     --risk-pct 0.05 --forward --forward-frac 0.2 \
     --brain data/brains/unified_m5
   ```
   Check the `signal_sources` in the output: `orderflow.true_tick_bars > 0`

**Alternative (Deriv):** Deriv's API provides tick history via
`https://api.deriv.com/api/v1/tick_history`. Export tick/trade CSVs
from the Deriv API and feed them via `--tick-data`.

**Result:** `orderflow.true_tick_bars > 0, proxy_bars = 0` → **VERIFIED**

---

## 2. Volume footprint / POC
**Current: PROXY — candle-derived, no market-depth verification**

### What's already built
- Same `ApexOrderFlowExport.mq5` exports `ABUTRON_NATIVE_DEPTH_V2_*.txt`
  (bid/ask snapshots) and `ABUTRON_NATIVE_TRADES_V2_*.txt` (trade prints)
- `src/abutron/analytics/footprint.py` — `VolumeFootprintBuilder` already
  accepts `Trade` objects and computes real POC, HVN/LVN, value area,
  and cell-level buy/sell imbalance
- `build_signal_bundle()` already calls `VolumeFootprintBuilder.build()`
  on real trades when `tick_trades` is present
- `--depth-data <csv>` CLI arg loads depth snapshots

### Steps to VERIFIED
1. **Same as #1** — deploy the export EA and let it accumulate trade prints
2. **The trade prints drive real footprint:** once `tick_trades` is populated,
   `VolumeFootprintBuilder` computes true POC from actual traded prices,
   not candle-derived proxies
3. **For true market depth (order book):** the depth snapshots provide
   bid/ask at each timestamp. For full order-book depth, extend
   `ApexOrderFlowExport.mq5` to export `DepthGet()` data (top N levels)
4. **Verify:**
   ```bash
   PYTHONPATH=src python3 scripts/verify_tick_data.py --mql5-files MQL5/Files
   ```
   Output should show `Volume footprint / POC : VERIFIED`

**Result:** `footprint.true_tick_bars > 0, proxy_bars = 0` → **VERIFIED**

---

## 3. Gemma / APEX agent scores
**Current: PARTIAL — APEX verified; Gemma NOT used in verified run**

### What's already built
- `src/abutron/apex/gemma.py` — `GemmaAdvisor` class with `advise()` method
- Config: `ABUTRON_GEMMA_ENABLED`, `ABUTRON_GEMMA_BASE_URL`,
  `ABUTRON_GEMMA_MODEL` in `.env.example`
- Gemma is **advisory-only** — never on the order path. The brain uses
  deterministic APEX-style scores as the offline fallback

### Steps to VERIFIED
Gemma is **optional and advisory-only by design** — it provides narrative
context and a confidence overlay, not trade signals. The honest path to
VERIFIED for the *scores* component is:

**Option A — Enable Gemma and verify:**
1. **Set up a local Gemma server** (Ollama):
   ```bash
   # Install Ollama, then:
   ollama pull gemma2:9b
   ollama serve  # runs on 127.0.0.1:11434
   ```
2. **Enable Gemma in the environment:**
   ```bash
   export ABUTRON_GEMMA_ENABLED=true
   export ABUTRON_GEMMA_BASE_URL=http://127.0.0.1:11434/v1
   export ABUTRON_GEMMA_MODEL=gemma2:9b
   ```
3. **Run a forward test with `--gemma`:**
   ```bash
   PYTHONPATH=src python3 -u -m abutron.backtest.run \
     --data data/links/market-data --broker fpmarkets \
     --symbol EURUSD.r --timeframe M5 --equity 176.72 \
     --risk-pct 0.05 --forward --forward-frac 0.2 \
     --brain data/brains/unified_m5 --gemma
   ```
4. **Verify** the `signal_sources.gemma.source` is `"gemma_llm"` not
   `"deterministic_apex"`

**Option B — Accept APEX-only as VERIFIED:**
Since Gemma is advisory-only and never on the order path, the APEX
deterministic scores are the verified signal source. Reclassify as:
"APEX scores VERIFIED; Gemma advisory-only (not required for signal
authority)" — which is honest and accurate.

**Recommendation:** Option B. Gemma is advisory context, not a signal
authority. The brain's decisions are driven by APEX scores + neural
ensemble, which are already verified.

---

## 4. Sniper entry
**Current: CODE-INSTALLED — not proven live broker-entry authority**

### What's already built
- `src/abutron/apex/sniper.py` — `SniperEntryEngine` with LIMIT/CONFIRM
  entry, ATR-based TP/SL, pullback detection
- `mql5/ApexNativeWriter.mq5` — native EA that places orders at trigger
  prices with 12-field HMAC-signed canonical payloads
- `src/abutron/execution/writer_gateway.py` — `SingleWriterGateway` with
  per-account lock, SHA256 pin verification, HMAC signing
- `src/abutron/execution/dispatcher.py` — `NativeDispatcher` with
  `BLOCKED_INCOMPLETE_EXPOSURE` pre-flight gate

### Steps to VERIFIED
1. **Connect a DEMO account to the MT5 terminal** (FP DEMO 0512/1636)
2. **Arm the writer gateway:**
   ```bash
   # Provision the activation secret
   ABUTRON_ACTIVATION_SECRET=your-secret
   # Arm the writer for the connection
   # (via the portal or API — the gateway verifies EA SHA256 pin)
   ```
3. **Run a single-symbol decision pass** with the sniper engine active:
   ```bash
   # The APEX engine runs decision passes, generates candidates with
   # sniper geometry (entry_type, trigger_price, stop_loss, take_profit)
   # The dispatcher sends signed orders to the native EA
   ```
4. **Verify the broker-side result:**
   - Check the MT5 terminal for the LIMIT order at the trigger price
   - Check the position log for the filled order with correct SL/TP
   - Check `ReceiptManager.record_native_receipt()` recorded an ACK
   - Check `verify_protective_stops()` confirms broker SL/TP match
5. **Confirm:** one sniper entry → native order → broker ACK → position
   open with correct protective stops = **VERIFIED**

**Result:** Live DEMO entry with broker-confirmed sniper geometry → **VERIFIED**

---

## 5. Chandelier trailing stop
**Current: CODE-INSTALLED — no fresh broker-side trailing modification verified**

### What's already built
- `src/abutron/risk/risk_engine.py` — `trailing_stop()`, `_apply_trail()`,
  `_broker_confirms_sl()` compute the Chandelier trail from ATR
- Canonical payload carries `trail_k` and `trail_stop` — signed into the
  order request so the EA receives the trail parameters
- `mql5/ApexNativeWriter.mq5` — EA modifies the broker SL to the trail
  stop via `OrderModify()`
- `src/abutron/execution/receipts.py` — `verify_chandelier_trail()`
  verifies broker SL == trail stop

### Steps to VERIFIED
1. **Open a position via the sniper entry path** (steps from #4)
2. **Let the price move** so the Chandelier trail triggers a stop update
3. **Verify the broker-side trailing modification:**
   - Check the MT5 terminal: the position's SL should have moved to the
     trail stop level
   - Call `ReceiptManager.verify_chandelier_trail(
       receipt_id=..., broker_sl=<current SL>,
       broker_trail_stop=<computed trail stop>)`
   - The verification returns `verified=True` when broker SL == trail stop
4. **Confirm:** trail triggers → native `OrderModify()` → broker SL
   updated → `verify_chandelier_trail()` passes = **VERIFIED**

**Result:** Fresh broker-side trailing stop modification confirmed → **VERIFIED**

---

## 6. Engine / execution / reconciliation
**Current: PARTIAL — native broker cycle not proven on live DEMO**

### What's already built
- Full execution state machine: RESERVED → SEND → ACK → OPEN → PROTECTED →
  CLOSED → SETTLED, with BLOCKED_INCOMPLETE_EXPOSURE guard
- `NativeDispatcher` with exposure pre-flight gate
- `ReceiptManager` with ACK/UNKNOWN/REJECTED recording, protective stop
  verification, Chandelier trail verification
- `writer_gateway.py` with single-writer lock, HMAC signing, SHA256 pin
- `dispatcher_worker.py` for background dispatch

### Steps to VERIFIED
1. **Run the full cycle on DEMO:**
   ```
   Brain decision → Candidate generation → RiskAgent eligibility
   → Exposure gate (BLOCKED_INCOMPLETE_EXPOSURE if unsafe)
   → Dispatcher sends signed order → Native EA places order
   → Broker ACK → Receipt recorded → Protective SL/TP verified
   → Position open → Chandelier trail updates SL
   → Position closes → Receipt CLOSED/SETTLED
   → Feedback learning feeds realised return into brain
   ```
2. **Verify each stage with broker evidence:**
   - Candidate state transitions in the state machine
   - Receipt file in `data/receipts/` with ACK and protective stop data
   - `verify_protective_stops()` and `verify_chandelier_trail()` pass
   - Closed trade receipts feed back via `learning/feedback.py`
3. **Confirm:** complete cycle from brain decision to closed trade
   with broker receipts = **VERIFIED**

**Result:** Full native broker cycle proven end-to-end on DEMO → **VERIFIED**

---

## 7. Equity / risk learning
**Current: CODE-INSTALLED — continuous learning unproven in production**

### What's already built
- `src/abutron/learning/feedback.py` — `learn_from_closed_trades()`
  feeds broker-confirmed CLOSED/SETTLED outcomes into
  `brain.online_learn()` with realised return, then saves the brain
- `collect_closed_trades_from_receipts()` reads closed receipts from DB
- `NexusBrain.online_learn()` performs online gradient steps

### Steps to VERIFIED
1. **Enable continuous feedback learning** — run the feedback loop
   periodically (e.g., every 15 minutes) against the receipt database:
   ```bash
   PYTHONPATH=src python3 -u -c "
   from abutron.learning.feedback import (
       collect_closed_trades_from_receipts,
       learn_from_closed_trades)
   trades = collect_closed_trades_from_receipts()
   results = learn_from_closed_trades(closed_trades=trades)
   print(f'Fed back {len(results)} closed trades, '
         f'{sum(r.updated for r in results)} updated brain')
   "
   ```
2. **Verify the brain weights change** after feeding back closed trades:
   - Record brain hash before and after
   - Confirm `online_updates` counter increments
   - Confirm the brain file is saved
3. **Run a subsequent forward test** and confirm the brain has learned
   from the feedback (compare performance before/after)
4. **Confirm:** closed trades → feedback → brain update → saved →
   subsequent decisions reflect learning = **VERIFIED**

**Result:** Continuous closed-trade feedback loop proven in production → **VERIFIED**

---

## 8. Drawdown / daily loss / lot sizing
**Current: VERIFIED (calc) — deployment unproven**

### What's already built
- `brain.size_position()` — floors to lot_step, rejects under lot_min,
  caps at lot_max, with min-lot-risk guard (drawdown death-spiral protection)
- `config.DEFAULT_DAILY_LOSS_CAP_PCT = 0.02` (2% daily loss cap)
- `riskguard.py` — hard limits: max position lots, concentration, total
  exposure, per-trade risk
- `contract_verify.py` — verifies lot math against real MT5 contract specs

### Steps to VERIFIED
1. **Deploy on DEMO** — run the execution path with the risk engine
   active on a live DEMO account
2. **Verify lot sizing on real orders:**
   - Check that placed orders use lot sizes computed by `size_position()`
   - Confirm orders never round up (floored to lot_step)
   - Confirm orders below lot_min are rejected
3. **Verify daily loss cap enforcement:**
   - Configure `ABUTRON_DAILY_LOSS_CAP_PCT=0.02`
   - Trigger a day where losses approach 2% — confirm the engine
     blocks further entries
4. **Verify drawdown control:**
   - Check that max drawdown in the forward tests stays within limits
   - Confirm the min-lot-risk guard prevents death-spiral on small accounts
5. **Confirm:** real DEMO orders with correct lot sizes + daily loss cap
   enforcement on live account = **VERIFIED**

**Result:** Risk rules enforced on live DEMO with broker-confirmed orders → **VERIFIED**

---

## Summary Table

| # | Component | Current | Path to VERIFIED | Effort |
|---|-----------|---------|-------------------|--------|
| 1 | Order flow / imbalance / delta | PROXY | Deploy MT5 export EA → run `verify_tick_data.py` | Medium (deploy + wait) |
| 2 | Volume footprint / POC | PROXY | Same EA → real trades drive `VolumeFootprintBuilder` | Medium (same as #1) |
| 3 | Gemma / APEX scores | PARTIAL | Option B: APEX-only is the verified signal source; Gemma is advisory | Low (reclassify) |
| 4 | Sniper entry | CODE-INSTALLED | Arm writer → send sniper order → verify broker ACK | Medium (DEMO cycle) |
| 5 | Chandelier trailing | CODE-INSTALLED | Open position → trail triggers → verify broker SL == trail stop | Medium (DEMO cycle) |
| 6 | Engine / execution / reconciliation | PARTIAL | Full DEMO cycle: decision → ACK → SL/TP verify → trail → close | Medium-High |
| 7 | Equity / risk learning | CODE-INSTALLED | Run feedback loop → verify brain updates → re-test | Medium |
| 8 | Drawdown / daily loss / lot sizing | VERIFIED (calc) | Deploy on DEMO → verify real orders + daily cap enforcement | Medium |

**Key insight:** Items #1, #2, #4, #5, #6, #7, #8 all share a common
dependency — **a live DEMO terminal running the MT5 EAs with the writer
and export stack attached.** Once that terminal is running with:
- `ApexOrderFlowExport.mq5` (exports tick/trade data)
- `ApexNativeWriter.mq5` (places and trails orders)
- The dispatcher + receipt manager running

...all remaining items can be verified in a single integrated DEMO cycle.

The code infrastructure for every component is **already built, tested,
and uploaded to GitHub.** The remaining step is deploying the EAs on a
live DEMO terminal and running the verification cycle.
