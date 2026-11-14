# ABUTRON FX V9 — non-disruptive native integration staging checkpoint

Date: 2026-11-15 SAST (2026-11-14 UTC)
Staging branch: integration/v9-native-safe-path-20261115
Base commit: e83fde1
Upstream GitHub V9 application ancestor: 55a7a76 on arena/862683c0-abutron-fx-v9
Production checkout: /opt/abutron-fx-v9-web, unchanged
Staging worktree: /var/tmp/abutron-v9-integration-20261115

## What was actually changed (staging only)

1. Removed a placeholder manager socket order/order_handoff handler that fabricated an ACK and ticket. Until an authenticated broker-native handoff is implemented, it explicitly reports UNKNOWN. A fake local response is not a broker receipt.
2. SingleWriterGateway now blocks requests when desired/effective client ON are not both true; broker/environment don't match DEMO; retired 0178/3492 suffixes are linked; EA binary pin/activation revision aren't attested; activation secret is missing; or the entry, stop, take-profit, direction or volume is invalid. It now sends kind=order_handoff with an HMAC matching the local manager protocol.
3. Accepting an application-level ACK now additionally requires a consistent broker deal, ticket, account binding, broker-reported fill, broker-reported protected stops and acknowledgement flag. Such an envelope is **not** independent broker-native proof: only MT5 receipt, deal history and independently reconciled open positions can certify a real fill.
4. NativeDispatcher blocks queued candidates when either client switch is OFF. Its recorded fill, SL, TP, commission and swap use the validated response rather than the original Sniper trigger/assumed zero costs.
5. Fixed ApexIntegration.store_ready_candidate's invalid relative import; a broker-reviewed candidate persists the exact reviewed native entry price as the conservative CONFIRM entry, without inventing a LIMIT trigger. No pre-fill trailing stop is passed off as a position-specific trail.
6. The internal/legacy ApexEngine now respects account desired/effective OFF. This engine remains separate from the deployed ApexIntegration path; no alternate order writer was enabled.
7. Tests changed to reject non-attested mocks; new negative tests cover live/retired/OFF, invalid stops, missing secret, fake ACK, cross-account ACK, manager placeholder, broker-reviewed price and legacy OFF.

## Test evidence

- Complete staged unit suite, isolated temporary DB and APEX_DATA_DIR:
  175 passed, 1 pytest-asyncio deprecation warning, ~16.57 seconds.
- No genuine broker order was submitted by this staging change.
- Production V9 service stays active on its unchanged commit e83fde1.
- The main V9 health endpoint still reports brain_model_ready=false; trained model was **not** promoted.
- This is **not** proof of native DEMO execution or profitable NexusBrain trading.

## Remaining integration blockers

1. V9 manager socket does not yet forward orders to authenticated per-account native MT5 terminals. Wire and prove a genuine native broker request, broker OrderCheck, deal ticket, broker-accepted protective stop and subsequent independent reconciliation. Do not convert the placeholder into a simulated ACK.
2. Production gate/geometry from main._gate_overrides_for/_native_geometry_for default to fail-closed, and V9 has zero linked broker accounts. Their actual owner/account, broker exposure and native session evidence require verified wiring.
3. V9 Sniper LIMIT trigger and protective invalidation require a fresh broker-native OrderCheck/OrderCalcProfit for the exact intended entry geometry, not a guess from the closed-bar price. Current staging deliberately retains conservative broker-reviewed CONFIRM geometry; the Sniper engine is **not** yet a live execution entry authority.
4. TrailingStop currently computes from pre-fill history in the legacy engine. Real post-fill trailing requires authenticated position and high/low history **since the actual entry**, and may only tighten broker-accepted stops. No live trailing changes made.
5. Per-trade risk policy is inconsistent: this V9 bridge contract accepts hardcoded 0.10% while the deployed V9 environment is 0.15%; native portfolio reconciliation reports aggregate_open_pct=2.0%, max_drawdown_pct=20.0%, separate from the owner 20% ceiling. Reconcile units and controls **without raising** limits or falsely certifying exposure.
6. The native-risk auditor still reported BLOCKED_INCOMPLETE_EXPOSURE; this branch does not clear, ignore or overwrite that blocker.
7. NexusBrain trained candidate remains offline because its historical results did not prove net trading edge. V9 active model is unavailable.
8. The vendored APEX dependency is symlinked **only in staging**, pointing to the production-local untracked vendor tree; no vendor files were added to Git. Pin/reproduce its exact source before a portable deployment.
9. Never reintroduce the retired Deriv LIVE 0178 or FP Markets 3492 account(s).
10. No LIVE promotion or live trading change authorized by this checkpoint.

## Rollback / containment

- Production working tree, systemd units, trading state, deployed database and service are unchanged.
- No changes need rolling back in production.
- This work exists on a separate Git worktree/branch. Remove only that worktree and integration branch if it is rejected; do **not** reset the running checkout.
- Do not merge or deploy without independent production comparison, production-readiness review, DEMO-native broker proof and account risk acceptance.
