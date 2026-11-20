# FP Markets DEMO 0512 — V9 Native-Writer Integration Plan

**Status:** Code complete and tested (804 tests, 4 skipped). PR #59
corrections merged. **Not yet deployed.** This plan covers the
Ubuntu-side steps to establish the first FP DEMO V9 trading writer
on account **0512**, then verify a genuine broker-acknowledged order
before expanding to 1636, 1622, 2582.

**Scope:** DEMO only. LIVE 9392 is never touched by this plan.

## Background

FP Markets already has a working **feed/data layer**:

- `mt5_session_manager.py` + `worker_feed_v8_fp_handoff.py` manage
  FP MT5 terminal sessions running `AbutronFPMarketsNativeFeedV2.ex5`
  (no-order feed EA).
- FP DEMO accounts (0512, 1636, 1622, 2582) are connected with active
  feed workers and CRM trade controls ON.
- An existing FP execution/reconciliation service runs at
  `/var/lib/abutron-fp-execution/` (schema
  `abutron.fpmarkets.execution.native_reconciliation.readonly.v1`).

The **V9 ApexNativeWriter** (`b6b63afe…`) is a **new, separate**
trading writer. It must be deployed to a **dedicated** FP trading
terminal — it does not replace or compete with the existing feed/order
bridge. The V9 runner (`production_fp_demo.py`) drives the
decision-to-execution cycle on top of the existing CRM controls and
broker-native data.

## Prerequisites (verify on Ubuntu before deploying)

1. **CRM trade control for 0512 is ON.** The runner checks
   `apex_trade_controls` (desired_enabled=1, effective_enabled=1).
   The PR #59 correction uses this existing table — no new activation
   table is needed.

2. **FP broker-native instrument specs exist.** The runner derives
   FP symbols from the cost report's `instruments` dict (PR #59
   correction). Verify the FP cost report contains instrument
   specifications with `tick_size` for the symbols to be traded.

3. **The `b6b63afe…` EX5 artifact is available** on the Ubuntu host
   (the same artifact deployed to Deriv DEMO — the EA is
   broker-agnostic).

## Deployment steps (one account at a time: 0512 first)

### Step 1 — Establish the FP 0512 V9 trading terminal

Create a dedicated FP trading terminal (analogous to Deriv's QA
terminal). This is separate from the FP feed terminal.

```
QA_ROOT=/var/lib/private/abutron-pr37-direct-writer-fp-demo0512-qa
```

Under `QA_ROOT/terminal/`:

1. **Deploy ApexNativeWriter.ex5** to
   `MQL5/Experts/ABUTRON/ApexNativeWriter.ex5`. Verify SHA-256:
   `b6b63afeb7c4939a2bea1f40cfad58d8288af4009d8dc4e2534d7196162b91f3`

2. **Provision the activation secret** at
   `MQL5/Files/ABUTRON_ACTIVATION_SECRET.txt` (the EA refuses to
   start without it). Use the existing FP activation secret/seed.

3. **Create an account-bound preset** at
   `MQL5/Presets/PR37WriterExposureOnly.set` with the REAL 0512
   values:
   - `InpConnectionId` = 0512's CRM connection ID (32-char)
   - `InpBrokerServer` = 0512's broker server (e.g. `FPMarkets-Demo`)
   - `InpActivationSeed` = the FP activation seed
   - `InpActivationSecret` = (or file-provisioned)
   - `InpExposureOnly=false` (enables dispatch)
   - `InpTrailingEnabled=true`, `InpTradeMagic=20261120`,
     `InpTrailingAtrMult=2.0`, `InpRiskFraction=0.15`

   **Do NOT use the blank template.** The enablement script preserves
   existing account-bound values; apply a real preset first.

4. **Create the MQL5/Files directory** for native exports.

5. **Verify the terminal authenticates** and the EA starts (check
   `issue38-health.json` and signed native exposure files appearing in
   `MQL5/Files/`).

### Step 2 — Establish the V9 data directory and reports

```
DATA_ROOT=/var/lib/abutron-v9-production-fp-0512
```

1. Create `DATA_ROOT/` (the runner writes `status.json` here).

2. **FP cost report** at the configured path (default:
   `/var/lib/abutron-fp-v4-0512/v4-native-execution-cost-0512.json`).
   Must contain:
   - `account_suffix`: "0512"
   - `environment`: "DEMO"
   - `native_bid_ask_and_ordercalcprofit_verified`: true
   - `instruments`: {symbol: {tick_size: ...}, ...}
   - `account_equity_usd`: <current equity>

3. **FP owner report** at the configured path (default: shared
   supervisor report). Must contain owner equity and per-connection
   equity for 0512.

   Both paths are configurable via `ABUTRON_FP_COST_REPORT` and
   `ABUTRON_FP_OWNER_REPORT` env vars.

### Step 3 — Run preflight (shadow decision pass, no orders)

```
cd /opt/abutron-v9-execution-repair/V9-10794e2
sudo env PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/opt/abutron-v9-execution-repair/V9-10794e2/src \
python3 -B -m abutron.agent.production_fp_demo \
    --account 0512 --mode preflight
```

**Verify:** `status.json` shows
`BRAIN_DECISION_EVALUATED_EXECUTION_NOT_INVOKED` with candle/quote
counts and candidate count. No orders are placed in preflight.

### Step 4 — Run the enablement script (verifies, then enables)

```
cd /opt/abutron-v9-execution-repair/V9-10794e2
sudo env PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/opt/abutron-v9-execution-repair/V9-10794e2/src \
python3 -B ops/v9-cutover/enable_trading_fp_demo.py
```

The script (PR #59 corrected) now:
1. **Verifies all accounts first** — CRM binding, account-bound
   preset, data root, EX5 SHA — before writing ANY preset.
2. **Updates execution flags only** — preserves all account-bound
   inputs and file mode (0600).
3. **Runs one execute cycle per account** — checks subprocess exit
   code, rejects stale/wrong-account status.json.

**If 0512 is not ready, the script fails before touching any
account's preset** — no partial configuration.

### Step 5 — Verify a genuine broker-acknowledged order

After the execute cycle, inspect:

1. **`status.json`** for 0512:
   - `status`: `V9_NATIVE_DISPATCH_EVALUATED`
   - `native_orders_sent`: count of ACK receipts
   - `native_unknown_receipts`: must be 0
   - `dispatch_blocked`: false

2. **Broker-native receipts** in the CRM/engine receipt store —
   verify each order has a genuine broker ACK with correct ticket,
   symbol, volume, SL, and TP.

3. **Protective stops** — verify the broker position's SL/TP match
   the dispatched order (via `reconcile_engine_receipts`).

4. **Trailing stops** — if a position is open, verify the trailing
   stop management is active (the EA's `ManageAgenticTrailingStops`).

5. **Order-flow provenance** — inspect the order-flow export
   (`ABUTRON_ORDER_FLOW_V2_*.txt`) to determine whether FP Markets
   provides genuine `trade_ticks` or falls back to `candle_derived`.

## Expansion criteria (1636, 1622, 2582)

Only after 0512 passes Step 5 verification:

1. Repeat Steps 1–5 for **1636**, then **1622**, then **2582**,
   one account at a time.
2. Each account gets its own QA terminal, data directory, cost
   report, and account-bound preset.
3. The enablement script handles all four in sequence, but each
   must pass verification before the next is touched.

## LIVE 9392 — separate path, not this plan

LIVE 9392 requires:
- Explicit LIVE authorization (real money).
- A `live_intent` record with `authorized=1` in `live_intents`.
- Broker-side trailing verification on FP Markets LIVE.
- A separate LIVE deployment path (the current scripts are DEMO-only).

**9392 is connected for data but trading remains OFF.** This plan
does not touch it.

## Rollback

If any step fails or verification does not pass:

1. Stop the V9 runner for the affected account.
2. The existing FP feed/execution infrastructure is unaffected —
   the V9 ApexNativeWriter is a separate terminal.
3. Revert the preset execution flags to `InpExposureOnly=true`
   (export-only) if needed.
4. The Deriv DEMO accounts (6207, 7593, 9244) are unaffected.
