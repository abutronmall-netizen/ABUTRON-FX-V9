# ABUTRON V9 issue #12 — UnifiedBrain-to-APEX candidate handoff

Verified against merged parent `a88f373001ee1b8fcc934a1e3d068ed5b90f947b` on Ubuntu, 16 November 2026.

## Implemented in this source branch

- The 26-symbol trained UnifiedBrain supplies the final BUY/SELL/HOLD direction in `ApexIntegration.process_candle`, after a bounded causal signal feature pass. A loss-pattern HOLD creates no candidate.
- Deriv's default manifest contains all 13 originally restored Boom/Crash/Volatility instruments. FP Markets instruments use the correct `fx_index` asset class.
- A scoped V9 candidate-review contract retains the pinned APEX native evidence, geometry and DEMO boundaries but supports all 13 native Deriv symbols, the trained FP symbol suffixes, and account-specific percent risk and portfolio caps. Older vendor code remains unchanged.
- Candidate `risk_fraction` is passed through from the account-configured review, not hardcoded 0.1%.
- The V9 manager no longer returns synthetic broker order acknowledgments/ticket numbers. An unattached writer explicitly rejects order handoff.
- Added deterministic tests for these repaired contracts.

## Validation

Isolated Python 3.12 runtime against the pinned merged checkpoint and local APEX vendor: **247 passed, 1 dependency warning**, with separate test data and no production broker orders.
Prior installed trained model SHA-256: `2667997b1498aae316c4103ec1f3d0bd2efb805f034836455ed10019284302df`.

## What is still NOT connected

- The existing V9 writer IPC currently has an operation/signature mismatch and no authenticated broker OrderSend transport to the production MT5 manager. A modification to that transport was blocked by tool safety controls; it was **not** bypassed. No real broker receipts have been established.
- The existing production customer risk state is `BLOCKED_INCOMPLETE_EXPOSURE`, including broker position and receipt evidence. The existing Deriv 6207 native dispatcher service remains failed.
- The standalone new V9 order application has NOT been started or declared execution-ready. Existing LIVE account switches remain OFF.
- Real per-account quote/contract geometry, current broker position exposure, entry stop/take-profit and trailing modifications must arrive through the actual MT5 account-bound writer and receipts. Static placeholder geometry is not broker proof.

## Safe rollout / rollback

This branch is a source repair and test release, not a LIVE switch. Test in an isolated deployment with the pinned APEX vendor, then integrate with a genuinely authenticated MT5 single-writer interface; only native broker tickets/deals and protected position requery constitute execution proof. Existing scheduled read-only full-signal model service remains on the pinned merged a88f373 release until a complete handoff is proven.
