# ABUTRON V9 — broker-native reconciliation checkpoint (2026-11-15)

## Verified staging changes

Integration branch: integration/v9-native-safe-path-20261115. Production Ubuntu checkout untouched.

- Manager IPC now obtains the activation secret from the configured environment rather than an embedded fallback, rejects missing/default secret and mismatched account signatures.
- Native reconciliation requires exact account login, broker ticket and deal, matching symbol/volume, protected entry/stop/target geometry, and nonwidening stops before recording a matched broker receipt.
- Duplicate native tickets are left unresolved. UNKNOWN broker sends are no longer resolved by matching just a symbol and lot size. Independent candidate/deal identity and stops are required.
- Added staged negative tests for MT5 identity, deals, widened stops, duplicate tickets, unknown orders and IPC signature binding.
- Full isolated V9 suite: 190 passed, one external pytest warning. No genuine orders sent.

## Read-only production audit (2026-11-14 23:39 UTC)

Read-only script: scripts/v9_native_readiness_ro.py

- V9 linked accounts: 0
- Active NexusBrain trained model: absent
- Compiled V9 ApexNativeWriter.ex5: absent
- V9 manager to MT5 native order handler: not connected (returns UNKNOWN)
- Customer-wide native risk: BLOCKED_INCOMPLETE_EXPOSURE, due to unverified daystart equity peak, incomplete broker exit ledger, pending broker-order ACK reconciliation, and missing native OrderCalcProfit per position.
- Runtime is DEMO, LIVE disabled, configured per-trade 0.15 versus vendored contract 0.10 percent; the native 2% aggregate exposure policy is distinct from the 20% max drawdown policy.

## Critical native EA architecture finding

The tracked mql5/ApexNativeWriter.mq5 contains pseudocode-like or incompatible MQL5 calls (SetTradeAllowed, SetTradeExecution, req.magnitude, Zero(req), FileWrite used with path strings and unsupported MarketInfo member access). It has NOT been compile-verified and no matching compiled V9 writer was found. Do not treat it as functional broker execution.

The established Deriv production APEX bridge uses a distinct native V2/V3 protocol, file request format, activation and receipt identity. Never send unreviewed V9 JSON into those native folders or activate a second writer.

## Outstanding work

1. Implement and compile a genuinely native MQL5 writer or a rigorously verified adapter to the existing single writer, with nonce and account binding, OrderCheck and OrderCalcProfit.
2. Independently authenticate native export provenance before accepting a NativeHistoryEntry. Caller-supplied objects are NOT independently broker certified.
3. Resolve the real customer-wide risk ledger and link eligible DEMO accounts. Keep 0178 and 3492 retired.
4. Validate Sniper LIMIT and post-fill trailing only against genuine broker-native execution and protected position receipts.
5. Calibrate cost-aware NexusBrain from separate broker-native out-of-sample periods before promoting a trained model.

No production changes, broker orders, LIVE activation or user account-switch changes occurred. Rollback: abandon only the staging branch/worktree; leave production checkout untouched.
