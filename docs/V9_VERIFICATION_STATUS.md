# V9 Comprehensive Verification Status

_Last updated: 2026-11-17_

This document records the **true verified state** of every V9 component.
Each item is labelled with one of:

| Label | Meaning |
|-------|---------|
| **VERIFIED** | Confirmed by code, tests, and/or broker evidence |
| **CODE-INSTALLED** | Code exists and is tested, but live broker-side proof is not yet complete |
| **PROXY** | Implemented as a candle-derived proxy — NOT true exchange depth / tick flow |
| **PARTIAL** | Partially verified — gaps remain |
| **NOT-CERTIFIED** | Explicitly not certified; requires live broker verification |

---

## Phase 1 — Risk & Lot-Size Mathematics

**Status: CODE-INSTALLED / BROKER-NATIVE CERTIFICATION PENDING (310 tests passed in isolated Ubuntu checkout)**

| Component | Status | Evidence |
|-----------|--------|----------|
| Tick size / point | REGISTRY-ONLY | Registry entries are estimates for model testing, not same-account MT5 SymbolInfo evidence. `verify_contract()` now reports non-certification without a native `OrderCalcProfit` proof. |
| Point value (USD/point/lot) | REGISTRY-ONLY | Broker-native tick value and account-currency conversion remain outstanding for execution certification. |
| Account currency | CODE-INSTALLED | Native sizing requires verified USD account currency; a USD default is not broker proof. |
| Minimum lot | CODE-INSTALLED | Native sizing validates broker minimum/maximum/step and refuses minimum-lot risk overruns; latest account-bound MT5 specs must still be provided. |
| Lot step | CODE-INSTALLED | `native_contract_sizing.py` floors to verified broker lot step; same-account contract attestation required. |
| Max loss at SL | NOT-CERTIFIED | Registry formula is an estimate. Production must supply same-account MT5 `OrderCalcProfit(1 lot, entry, SL)` before accepting a lot size. |
| Daily loss cap | PARTIAL | V9 source defaults to 2%; prior Ubuntu production risk report declared 3%. Native day-start equity and authoritative policy reconciliation remain outstanding. |
| Owner-wide exposure | NOT-CERTIFIED | The dispatcher now requires `RiskEngine.aggregate_owner_risk().owner_risk_certified is True`, and the risk engine rejects incomplete broker-native position/risk evidence. Production still reports `BLOCKED_INCOMPLETE_EXPOSURE`. |
| Risk-policy mismatch (2% vs 20%) | PARTIAL | The 0.15% default is aligned in code; 2% aggregate-open stop risk and 20% portfolio drawdown are distinct measures, not the same limit. Daily 2% source default vs 3% production report needs reconciliation. |

**Tests:** `tests/test_contract_verify.py` (7 tests), `tests/test_risk.py` (8 tests), `tests/test_riskguard.py` — all pass.

---

## Phase 2 — V9-to-APEX Execution Integration

**Status: CODE-INSTALLED (native broker-side proof still pending)**

| Component | Status | Evidence |
|-----------|--------|----------|
| Brain direction → order path | VERIFIED | Candidate flow: brain → APEX engine → dispatcher → writer gateway; 12-field canonical HMAC signing covers direction |
| Sniper geometry → order path | CODE-INSTALLED | `entry_type`, `trigger_price`, `stop_loss`, `take_profit` carried in canonical payload; `ApexNativeWriter.mq5` places LIMIT/CONFIRM orders at trigger |
| Position sizing → order path | CODE-INSTALLED / NOT-CERTIFIED | Strict native `OrderCalcProfit` sizing exists but current production account-bound dynamic proof is incomplete. No production lot sizing certification claimed. |
| Account-bound order path | VERIFIED | `SingleWriterGateway.send_order()` holds per-account lock, verifies SHA256 pin, provisions HMAC secret |
| Broker receipts (ACK) | CODE-INSTALLED | `ReceiptManager.record_native_receipt()` records ACK receipts; `_deliver_to_ea()` returns ACK/UNKNOWN/REJECTED (never fabricates) |
| Protective SL/TP on DEMO | CODE-INSTALLED | `ReceiptManager.verify_protective_stops()` verifies broker SL/TP vs requested geometry; **NOT yet proven on live DEMO** |
| `BLOCKED_INCOMPLETE_EXPOSURE` guard | VERIFIED | Implemented in `execution/state.py` + `dispatcher.py`: pre-flight exposure verification blocks all RESERVED candidates when owner exposure is unverified or cap exceeded |
| Native broker-order reconciliation | NOT-CERTIFIED | `ReceiptManager` + state machine exist; **native terminal reconciliation cycle not yet proven on live DEMO** |

---

## Phase 3 — Feedback Learning & Trailing-Stop Verification

**Status: CODE-INSTALLED (broker-side proof pending)**

| Component | Status | Evidence |
|-----------|--------|----------|
| Closed-trade learning → brain | CODE-INSTALLED | `learning/feedback.py:learn_from_closed_trades()` feeds broker-confirmed CLOSED/SETTLED outcomes into `brain.online_learn()` with realised return; saves updated brain |
| Feedback loop closure | CODE-INSTALLED | `collect_closed_trades_from_receipts()` reads closed receipts from DB; **production cycle not yet proven end-to-end** |
| Chandelier trailing stop (code) | CODE-INSTALLED | `risk_engine.trailing_stop()` / `_apply_trail()` computes trail; canonical payload carries `trail_k` / `trail_stop`; `ApexNativeWriter.mq5` modifies broker SL |
| Chandelier trailing (broker-side) | NOT-CERTIFIED | `ReceiptManager.verify_chandelier_trail()` verifies broker SL == trail stop; **no fresh broker-side trailing modification verified on live DEMO** |

---

## Full V9 Component Matrix

| # | Component | Status | Notes |
|---|-----------|--------|-------|
| 1 | Market data (offline OHLCV, 26 instruments) | VERIFIED | `data/links/market-data`, offline replay confirmed |
| 2 | Online broker-market updates | VERIFIED | Deriv + FP DEMO 0512/1636 running |
| 3 | Order flow / imbalance / delta / absorption | **PROXY** | Candle-derived proxies — NOT true tick/order-book flow. `nexus_brain.json` records `true_tick_bars: 0, depth_bars: 0, proxy_bars: N` for all symbols |
| 4 | Volume footprint / POC | **PROXY** | Candle-derived proxies — no market-depth verification. Same honest_limitations entry |
| 5 | Gemma / APEX agent scores | **PARTIAL** | APEX deterministic scores trained and verified. Gemma NOT used in verified training run (`GEMMA_NOT_REQUESTED_FOR_THIS_PASS`). Gemma gateway exists but is optional |
| 6 | Regime classification | VERIFIED | trend_up, trend_down, high_vol, range — in signal features |
| 7 | 875 real broker trades | VERIFIED | `brain_provenance.json`: `trades_loaded=875, trained=875, 0 skipped`; 9 broker/account MT5 deal logs |
| 8 | Sniper entry | CODE-INSTALLED | Code installed in `apex/sniper.py` + MQL5 EA; **NOT proven live broker-entry authority** |
| 9 | Chandelier trailing stop | CODE-INSTALLED | Code installed; **NO fresh broker-side trailing modification verified** |
| 10 | Engine / execution / reconciliation | **PARTIAL** | `BLOCKED_INCOMPLETE_EXPOSURE` guard + dispatcher + receipts implemented; native broker-order reconciliation cycle not yet proven on live DEMO |
| 11 | Equity / risk learning | CODE-INSTALLED | `learning/feedback.py` implements closed-trade feedback; **account-specific continuous learning unproven in production** |
| 12 | Drawdown / daily loss / lot sizing | CODE-TESTED / NOT-CERTIFIED | Strict native sizing module and regressions pass; production MT5 contract proof, day-start equity, daily cap consistency and DEMO sizing authority remain incomplete. |
| 13 | 2,792 paper-trade outcomes | **FIXED** | README corrected to verified 327 replayed paper outcomes (AUDUSD.r); full 26-symbol eval ran 9,267 trades. `brain_provenance.json` is source of truth |
| 14 | Owner-wide risk (`BLOCKED_INCOMPLETE_EXPOSURE`) | **NOT-CERTIFIED** | Fail-closed native exposure guard repaired in PR17, but Ubuntu owner-wide risk remains blocked due independent native proof and equity history gaps. |
| 15 | Risk-policy mismatch (2% vs 20%) | **PARTIAL** | Per-trade default aligned; aggregate-open 2%, drawdown 20%, and daily-loss policy require explicit measure-by-measure certification. |

---

## Explicitly NOT Certified

These items require **live broker-side verification** before production deployment expansion:

1. **Native broker-order execution** — PR18 writer stack is staged on the branch (`writer_gateway.py`, `dispatcher.py`, `dispatcher_worker.py`, `ApexNativeWriter.mq5`), but the DEMO ACK → protective SL/TP → Chandelier stop → closed-trade feedback cycle has **not been proven end-to-end on a live DEMO account**.

2. **Broker-side Chandelier trailing** — No fresh broker-side trailing stop modification has been verified. `verify_chandelier_trail()` exists in code but has not been exercised against a live DEMO position.

3. **Account-specific continuous equity/risk learning** — The feedback learning pipeline (`learning/feedback.py`) is implemented and tested, but has not been proven running continuously against live account equity in production.

4. **Native broker-order reconciliation** — The state machine and receipt manager exist, but the full reconciliation cycle (native terminal export → receipt → state transition → feedback) has not been proven on live DEMO.

---

## Verification Commands

```bash
# Phase 1: Contract verification
PYTHONPATH=src python3 scripts/verify_contracts.py --equity 10000 --stop-points 26

# Full test suite
PYTHONPATH=src python3 -m pytest tests/ -v

# Paper-trade feedback
PYTHONPATH=src python3 scripts/train_from_paper.py --data data/links/market-data
```
