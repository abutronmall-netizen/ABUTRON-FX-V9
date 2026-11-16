# ABUTRON V9 — broker operator report adapter and MT5 feed identity fixes

Verified: 2026-11-15 SAST, 2026-11-14 UTC.
Staging only: integration/v9-native-safe-path-20261115
Production checkout: /opt/abutron-fx-v9-web, unchanged.

## Implemented and verified

1. Introduced a READ-ONLY adapter at src/abutron/execution/native_report_adapter.py for existing root-owned FP DEMO 0512/1636 native reconciliation reports, the Deriv native router inspection report, and Deriv's native receipt audit.
2. Validates protected file ownership, no symlinks, file size, freshness, UTC timestamp, expected schema, report intent, exact allowed FP account set, connection identities and report consistency. Does not read trading passwords or submit orders.
3. Keeps all upstream reports classified as SECONDARY operator reports, not direct MT5 broker-native fill proof. A valid report NEVER sets V9 execution_authorized true or bypasses customer-wide exposure checks.
4. Wired the adapter into scripts/v9_native_readiness_ro.py. This now displays grounded existing broker report status alongside independent V9 execution blockers.
5. Fixed four sites that treated MT5 Terminal.is_running as a property instead of calling the method: health, export read, verifier, worker restart, and terminal re-launch handling.
6. Fixed native broker feed validator to use the actual identity object exported from the terminal rather than passing a wrapper and overwriting the native broker claim with the expected value.
7. Fixed a false credential-authentication success: a matching login number inside a native file is not a verified broker challenge/response. Until a true isolated MT5 login probe is wired, verification returns INCONCLUSIVE rather than authenticated.
8. Added 27 fixture tests for native report tampering/retirement/freshness/authorization and MT5 health, identity and login verification boundaries.

## Evidence

- Full isolated pytest run: 219 passed, one pytest-asyncio warning, about 18.75 seconds.
- Read-only live Ubuntu adapter output: SECONDARY_REPORTS_VALID_READONLY; execution_authorized=false.
- FP 0512: RECONCILED_READONLY, effective_on=false, 12 positions, 0 pending; FP 1636: RECONCILED_READONLY, effective_on=false, 1 position, 0 pending. Counts are a point-in-time read and must never be assumed current without freshness.
- Deriv router: READ_ONLY inspect mode; zero accounts authorized for native dispatch; Deriv receipt audit: READ_ONLY.
- V9 readiness: NOT_READY_FOR_V9_AUTOMATIC_DEMO_EXECUTION, zero linked V9 accounts, no active trained NexusBrain model, no compiled native V9 writer, no actual V9 MT5 handoff.
- Customer-wide native risk: BLOCKED_INCOMPLETE_EXPOSURE (incomplete daystart peak, broker exit ledger, ACK reconciliation, OrderCalcProfit per position).
- V9 production and existing broker services not touched; no live or demo orders submitted by this integration.

## Limitations / next work

- Secondary reports establish operator-level native reconciliation only. Actual broker-native claims need source authentication, account nonce, broker OrderCheck/OrderCalcProfit, and signed or pinned receipt lineage.
- Existing Deriv and FP order writers use DIFFERENT native protocols. The V9 code cannot yet send orders to either. Do not activate multiple writers or let an operator report act as an order approval.
- Connect valid DEMO accounts to the V9 account store only after verifying owner, broker environment, CRM entitlements and existing account switching; keep retired accounts 0178 and 3492 excluded.
- The checked-in V9 ApexNativeWriter.mq5 is not compile-verified. Avoid deploying it or claiming its broker ACKs are genuine.
- Risk environment per-trade 0.15 differs from vendored candidate contract 0.10. Preserve existing 2% native aggregate risk cap and separate 20% drawdown/portfolio limits.
- No trained NexusBrain model was promoted. Confirm cost-adjusted out-of-sample broker results before promotion.

Rollback: stage changes exist only on independent Git worktree; no production rollback necessary.
