# ABUTRON V9 — Deriv DEMO 6207 native ledger recovery audit

Verified on Ubuntu 2026-11-15 SAST. This is staging-only engineering, not an execution deployment.

## Why this phase was necessary

The native Deriv DEMO 6207 router reported LEDGER_BROKER_POSITION_COUNT_MISMATCH. A fresh live broker feed contained one protected open BUY position but the execution-ledger.sqlite3 file contained zero active positions for that same connection. This difference must not be silently zero-filled, ignored, or converted into a new order.

## Implemented

- src/abutron/execution/deriv_6207_recovery_audit.py is a READ-ONLY inspector with strict CRM owner/connection/broker DEMO binding, worker health and nonce freshness, V3 EA readiness marker, native account feed, and existing SQLite attempts query with mode=ro.
- It verifies a candidate native protected position against the current broker feed; matching native ten-line ACK receipt and 19-line native protected-position proof; native 14-line Option C risk-intent file; identical signal key, login, server and nonce; and reconstructs the exact M5 bar from the CRM owner/connection, symbol, side and receipt timestamp.
- It separately assesses the native stop cash risk against the existing 0.10% APEX contract, and reports whether the signal/ticket already exists in the ledger. A position above that limit is not declared compliant merely because V9 runtime has a 0.15% setting.
- The command-line read-only entrypoint is scripts/v9_deriv_6207_ledger_recovery_ro.py, which requires an explicit account connection ID and never writes to broker terminal, CRM database, existing order ledger, files, services, or client controls.
- Tests include 17 fixtures for receipt impersonation, wrong deal/nonce/owner/signal, unprotected/stale broker position, intent risk geometry, double counting and compliant/noncompliant policy outcomes.

## Verified current evidence (point-in-time)

- Deriv DEMO 6207 CRM: account connected, desired ON, effective ON.
- Native V3 EA: current matching account/nonce heartbeat, one open broker-protected position, no pending orders, EA native order capability OFF.
- Execution ledger: zero active positions; the current open protected native ticket/signal is not recorded.
- Native order ACK receipt, current protected position, and native Option C intent all match the owner-bound M5 signal for the 2026-11-14 23:45 UTC candle.
- Broker-native stop-risk estimate approximately 0.108% of equity at entry. It exceeds the current separate 0.10% native contract even though it is below the deployed V9 0.15% environment setting.
- Read-only result: RECONSTRUCTION_REQUIRES_POLICY_REVIEW; orders_submitted=0; ledger_modified=false; execution_authorized=false.
- Outstanding account-specific codes: NATIVE_POSITION_RISK_EXCEEDS_0_10_PERCENT_CONTRACT, BROKER_POSITION_MISSING_FROM_APEX_LEDGER, LEDGER_BROKER_POSITION_COUNT_MISMATCH, NATIVE_EA_ORDER_CAPABILITY_DISABLED.
- Full isolated V9 pytest suite: 236 passing tests, one pytest-asyncio warning.

## Production constraints and next engineering decision

The APEX order ledger is an active execution authority; inserting a missing OPEN_PROTECTED row could alter capacity for new orders. Therefore this audit never performs a ledger migration or unpauses trading.

Before controlled recovery, independently validate broker exposure risk including all linked accounts and daystart/history evidence, reconcile the native 0.10% contract with approved risk policy without raising limits simply to pass, and make a transactional guarded ledger-reconstruction plan with SQLite rollback and no new order trigger. Existing broker-position protection must be independently reconfirmed at application time.

Other V9 blockers remain: no V9-linked accounts, missing trained active NexusBrain, no compiled V9 writer, disconnected V9-native MT5 handoff, and customer-wide BLOCKED_INCOMPLETE_EXPOSURE. Retired LIVE 0178 / FP 3492 must remain excluded.

No production service, broker order, CRM switch, native risk policy, or ledger state was changed. This checkpoint only adds audit source, tests and documentation to isolated Git staging.
