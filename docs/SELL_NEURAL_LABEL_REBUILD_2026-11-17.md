# V9 SELL neural-target repair and broker-verified candidate rebuild — 2026-11-17

## Root cause
Both the repository training script `scripts/train_from_trades.py` and its Ubuntu production candidate-specific copy at `/usr/local/lib/abutron-v9-realtrade-feedback/train_from_trades_causal.py` converted SELL price returns to the wrong neural label.

Original:
```python
trade_return = exit_price - entry_price
return trade_return if direction == "BUY" else -trade_return
```

The neural output is the **market direction** UP/DOWN rather than trade-relative profit. A profitable short on a falling market must train DOWN (0), not UP (1). For trades with actual prices, the correct label uses `exit_price - entry_price` **regardless of side**. When prices are missing, only then can the signed net PnL proxy be converted from trade-relative to market direction by side.

Note that `LossPatternGuard.learn(..., pnl=...)` already consumes net broker PnL separately. This change corrects only neural targets; it must not be mistaken for broker-order or strategy-validation proof.

## Ubuntu verification
- Native production candidate timer temporarily stopped to prevent learning while patching.
- Root copy of previous trainer retained: `/var/backups/abutron-trade-label-before-fix-20261117T1430Z.py`.
- Corrected production trainer SHA256: `0bd9340094b49363008cb7b13c71fab449a964671f61c38e468feb14068ec598`.
- Six focused directional test cases passed for winning and losing BUY, winning and losing SELL, and SELL missing-price fallback.
- Previous isolated candidate manifest archived within root-owned feedback directory as `candidate-manifest-before-sell-label-rebuild-20261117T1434Z.json`. Previous model files were retained.
- Rebuilt a **new isolated candidate from the pinned baseline** using authenticated FP DEMO broker-closed receipts and causal native M5 windows. Verified 154/154 new broker-closed trade events learned, 0 M5 exclusions, candidate model SHA256 `ebb11519d3f2f286fd0b...` (see live manifest for complete digest); `online_updates=164066`.
- Candidate `model_promotion_allowed=false`, `broker_orders=0`; no CRM, native dispatch, production model or LIVE modifications.
- Five-minute `abutron-v9-realtrade-candidate.timer` restored; enabled and active; service exit 0 at 2026-11-17 14:32:39 UTC.
- New repository regression tests: `tests/test_trade_history_market_direction.py`.

## Important remaining scope
The pinned baseline model itself was already trained before this correction; this rebuild repairs **subsequent closed-trade candidate updates**, not an unverified complete retrain of the historical pinned model. This candidate is not automatically suitable for promotion.

The isolated candidate and hourly loss shadow remain separate from live BUY/SELL decisions. Before automatic model promotion, require genuinely future out-of-sample broker outcomes, train-vs-test leakage checks, realistic costs/slippage and rollback. Underlying Deriv DEMO 6207 dispatcher still reports `6207_CRM_EFFECTIVE_ON_OWNER_AND_ACK_UNVERIFIED`; this model repair must not bypass broker risk/activation evidence.
