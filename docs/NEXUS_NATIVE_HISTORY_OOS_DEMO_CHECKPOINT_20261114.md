# ABUTRON V9 NexusBrain / APEX 1.1 — broker-native research and DEMO checkpoint
Verified: 2026-11-14 (UTC). Status: RESEARCH COMPLETE, NOT PROMOTED TO DEMO EXECUTION.

## Scope and isolation
- Examined Deriv DEMO-sourced market candles for Volatility 10 Index.0 and Volatility 75 Index.0, and FP Markets native GBPUSD.r/EURUSD.r candles/quotes.
- Historical archive copies and manifests preserved under:
  `/var/lib/abutron-fx-v9-hermes/research/archives/native-export-20261114/`
- Read-only lineage: `scripts/verify_native_m5_lineage_ro.py`.
- Offline benchmark: `scripts/nexus_native_oos.py`; output:
  `/var/lib/abutron-fx-v9-hermes/research/nexus-20261114/nexus_vs_apex_heldout.json`
- Research model: `.../nexus-20261114/candidate_model_not_deployed/nexus_brain.json`.
- No copy to V9 active model location; no MT5 order sent; no broker account linked, changed, enabled or retired.

## Archive integrity and broker-native lineage
- Deriv archive: 270,570 rows, canonical gzip SHA256
  `5ad0076d56dee4f1531bf8a6f37bd0e1f8b0d878b07cdbee9d52bb4c4fc0b517`.
- FP Markets archive: 145,073 rows, SHA256
  `d6fe1bc32f64a7c49bd072c98eb7562515c1fde62dc1b23cb647136d08a3b26c`.
- FP historical quotes: 30,158 rows, SHA256
  `762d8fef15278c8d0fa9aaa05a6534a719722217b575fefc521f8963c61389e7`.
- Exact read-only native warehouse comparison: **Deriv 6,510/6,510 M5 OHLC matches** against
  `/var/lib/apex/precision-history/closed-candles.sqlite3` and **FP Markets 2,086/2,086 matches**
  against `/var/lib/abutron-quant-first/dual-broker/fp-immutable-native-candles.sqlite3`.
  Zero conflicting values; selected FP-M5 had 2,398 identical duplicate keys removed.
  Full FP archive has 78,412 identical duplicate candle keys at all timeframes.
- Native warehouses are independent local evidence, not broker-cryptographic signatures. Export files intentionally omit account identifiers.

## Chronological experiment
- Selected M5: Deriv 3,255 bars per instrument; FP 1,043 unique bars per instrument.
- Only 30+ contiguous prior bars used for initial model features, with history reset at M5 gaps.
- Feature window never uses bars after signal. Entries assumed at next M5 open, exits at that M5 close.
- Global test cutoff: 2026-11-12 00:00 UTC; no training or calibration label touches later timestamps.
- 6,616 pretest labeled observations; first 4,962 used to fit; chronologically last 1,654 used for calibration/validation; 1,620 held-out test observations (1,102 Deriv; 518 FP).
- V9 `nexus_v1` three-member MLP ensemble, 16 epochs; random/dropout seed 20261114.
- Deterministic second run from the preserved canonical archive reproduces **exactly the same model SHA256** `b3ea79093839755bb9f60f8d697cab35eaa7f2dafbbb1eb63154cde07c6a5159` and identical training metrics and broker-by-broker held-out results. Wall-clock times differ.
- **Internal validation directional accuracy 51.09%; not evidence of positive risk-adjusted return.**
- Baseline APEX 1.1.0 replayed in `signal_only` mode with the same M5 candles; APEX HOLD signals are not treated as incorrect buys or sells.

## Completely held-out comparison (2026-11-12 onward)
| Broker | Engine | BUY/SELL signals | Direction accuracy on signals | Mean gross next-bar P&L, bps | Mean next-bar adverse excursion, bps |
|---|---|---:|---:|---:|---:|
| Deriv | NexusBrain | 1,102 | 47.91% | +0.328 | 9.432 |
| Deriv | APEX 1.1 | 963 | 54.31% | +1.112 | 8.482 |
| FP Markets | NexusBrain | 518 | 54.63% | +0.104 | 1.236 |
| FP Markets | APEX 1.1 | 309 | 47.57% | -0.084 | 1.372 |

### Transaction costs and drawdown (research only)
- Deriv quote-spread time series is absent from archive. At an **illustrative 2.0-bps roundtrip spread + 0.7-bps roundtrip commission**, mean net per simulated Deriv trade is -2.372 bps NexusBrain versus -1.588 bps APEX; sequential equal-notional hypothetical research max drawdown 23.173% versus 14.311%. **These are not broker-confirmed net P&L, fill drawdowns or exposure/risk-cap compliance metrics.** The 20% production portfolio cap was not removed or adjusted.
- FP Markets quote-based spread available at or before simulated entry within 300 seconds for only 58/518 (11.20%) NexusBrain and 53/309 (17.15%) APEX signals. On these different, sparse subsets, adding an **assumed** 0.7-bps roundtrip commission gives mean net -1.736 bps NexusBrain versus -1.164 bps APEX. Actual account commission, broker fills and slippage are **not proven**; these estimates cannot establish net profitability over the whole sample.
- The model generates BUY/SELL on 100% of eligible M5 bars, unlike APEX's abstentions. This is likely costly in low-edge market conditions; evaluate model-driven HOLD/calibrated confidence and broker-cost-aware entry logic using only validation periods, not the held-out test for parameter selection.
- Next-bar entry is hypothetical: no tick-level execution, stop execution, native `OrderCheck`, broker rejection, fill slippage or live margin simulated. The backtest is a **research diagnostic**, not execution certification.

## DEMO broker read-only checkpoint
- The active V9 service returns HTTP 200, `signal_authority=V9_NEXUS_V1`, `brain_model_ready=false`. The isolated V9 account database contains zero linked accounts.
- Existing production APEX timers and DEMO accounts were not altered; no V9 model was enabled for orders.
- Existing 6207 native history and native-order ACK audit show historical broker-side execution evidence, **not a new NexusBrain order receipt**.
- At 2026-11-14 ~22:23 UTC the customer-wide read-only risk auditor still reports `BLOCKED_INCOMPLETE_EXPOSURE`. Outstanding blockers:
  `ACCOUNT_EQUITY_DAYSTART_PEAK_UNVERIFIED`;
  `COMPLETE_BROKER_HISTORY_EXIT_LEDGER_UNVERIFIED`;
  `NATIVE_BROKER_ACK_ORDER_RECONCILIATION_PENDING`;
  `NATIVE_ORDER_CALC_PROFIT_PER_POSITION_MISSING`.
- DEMO-only contract regression tests explicitly verify account OFF denies candidates, portfolio ceiling 20% denies excess nominal stop risk, missing protective stops deny candidates, and unverified broker sessions deny candidates. These are **code-level protections only**, not real broker execution proof.
- Retired accounts Deriv LIVE 0178 and FP Markets 3492 must remain excluded. No LIVE routing changes or account relinking.

## Decision: DO NOT PROMOTE THIS MODEL
The tested model does not show compelling or verified positive net edge. Avoid enabling V9 execution merely because training yielded a calibrated checkpoint. Keep researching more contiguous broker-native history and cost-aware risk/entry selection; use fresh walk-forward validation and then independent prospective DEMO shadow observation. Only if those checks succeed should a named DEMO account be bound to the existing exclusive native writer and verified through actual broker order ACK + protected SL/TP receipts + reconciled exposure and account ON/OFF.

## Repeatability, recovery and safety
- Script execution requires `APEX_DATA_DIR` to point to writable **research-only** storage. Never point it to production APEX state.
- Use the preserved market archives, selected broker-native warehouses and exact source hashes to reproduce.
- No change to live V9 Brain model directory or execution services is made by the research scripts. No trading switch, portfolio ceiling, broker account or stop settings modified.
- Rollback for new code: remove the research-only scripts and test file from the V9 branch; preserve data/model/JSON research evidence until archived elsewhere. Existing V9 integration rollback files remain `/var/backups/20261114T2215Z-v9-nexus-authority/`.
