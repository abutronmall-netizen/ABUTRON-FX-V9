# ABUTRON FX V9 native authority cutover (2026-11-20)

This implementation follows integration commit `34811da`. It is **staged source**, not an assertion that Ubuntu has switched services.

## Staged on the authorized Ubuntu host

- `/opt/abutron-v9-native-runtime/activation_6207.py` — V9 native 6207 CRM activation, independent of legacy APEX11/V4 supervisor findings
- `/opt/abutron-v9-native-runtime/mt5_session_manager.py` — port of the existing MT5 manager to a V9-owned path, preserving the authenticated UNIX socket and encrypted credential store
- `/opt/abutron-v9-native-runtime/workers/` — copied existing per-broker worker implementations and recovery helpers, with syntax checks
- `/opt/abutron-v9-native-runtime/native_owner_risk_publisher.py` — staged copy of current owner-native risk publisher, not activated
- V9 systemd service/timer definitions; **none enabled or started** at source-validation checkpoint

The source remains compatible with the existing per-client MT5 terminal state and CRM. The account/owner limits, signed broker snapshots, SL/TP/positions, and customer ON/OFF state remain the authority. The 6207 activation program renews a CRM revision ACK **only after** checking current signed broker-native exposure, writer SHA, native broker contract, owner position evidence and explicit client desired ON. It does not send an order or toggle LIVE.

The MT5 manager port **retains existing state paths** `/run/abutron-client-mt5-manager` and `/var/lib/abutron-client-mt5-manager`, to avoid disconnecting the portal. Its two historical live-account exception IDs are supplied through a root-only `EnvironmentFile`; do not commit the file or the values.

## Verification to date

- On Ubuntu: V9 activation `--inspect` passed using live signed Deriv DEMO 6207 evidence; negative tests for spoofed EA hash and client desired OFF passed **without DB writes**.
- Ubuntu V9 session manager static load: Deriv DEMO and FP DEMO routing, existing worker active and attested, and portal socket path confirmed.
- Owner risk publisher staged source byte-identical to original before parameterization.
- Integrated GitHub code `34811da` clean QA worktree: **650 passed, 4 skipped**, not a production release.
- The V9 activation/manager module tests are isolated and require no trading or credentials.

## Unresolved — no false completion claims

1. The old `abutron-6207-native-activation-ack.timer` is still active. It can renew or clear the old V4 revision ACK. A safe cutover must ensure only ONE renewal authority is running.
2. The old `abutron-client-mt5-manager.service` owns the portal's UNIX socket and monitors all accounts; the V9 unit is staged **inactive**. A concurrent start would steal the socket. A coordinated service handover with broker-state and portal checks is required.
3. The owner-risk publisher is staged but its private account-specific bindings still require a secure, separately tested V9 configuration. Its old evidence path remains a dependency of the current V9 runner; do not stop the existing publisher until the replacement produces matching current exposure.
4. Existing V9 6207 production checkout remains local `8a37ed2`; GitHub `34811da` is NOT deployed and has divergent runtime/dispatcher components. Reconcile those deltas and preserve the already broker-proven fill-price and position-ticket fixes before any swap.
5. V9 native execution for other DEMO accounts, broker-confirmed trailing-stop modifications, and automated production-model promotion are not proven.
6. The service cutover was not executed: a controlled DB-backed migration test was blocked by tool safety checks. This is a blocker, not approval to disable protections or bypass the restriction.

LIVE authorization, archived retired accounts, client switches and open broker positions have not been changed by this source migration. Do not activate all accounts from a single account's signed proof. Review and validate account-by-account.

## Expected release evidence

Record the production source commit/SHA, native EA checksum, fresh 6207 account and owner HMAC evidence, CRM desired/effective/revision acknowledgment at successive timer ticks, independently authenticated protected broker position and trail changes, live portal session, and rollback result. Mark migration complete only when old APEX writers are retired **and** replacement authorities continue to function.

## Verified first production cutover — 2026-11-20 11:34 UTC

The old Deriv three-account read-only reconciler was failing with `NATIVE_PROTECTED_SL_TP_PROOF_NOT_UNIQUE` on account 6207 because the V9 writer emits valid signed `ABUTRON_NATIVE_EXPOSURE_V2` HMAC evidence rather than legacy APEX-format per-position proof files. The two independent native broker feeds were directly compared for the open position: ticket, symbol, side, volume, entry, SL and TP matched exactly; stop risk was USD 9.81 with `ordercalcprofit_verified=true`.

The V9 replacement `native_deriv_reconciliation.py` passed a three-DEMO-account read-only inspection and was installed into the **existing** `abutron-deriv-three-native-reconcile-ro.service` compatibility slot via `90-v9-native-source.conf`. This retained its upstream `abutron-six-broker-native-history-reconcile.service` dependency, which otherwise restarted the old unit and canceled the parallel V9 unit. The new V9 executable was verified to run successfully (exit 0) and publish `all_verified=true` for 6207/7593/9244. The downstream owner-wide current exposure report then verified zero unknown position sets. All publisher jobs remain read-only; no order was sent, no LIVE activation was changed.

An initial new-unit start failed with a read-only `/data` path because the V9 package imports `abutron.config`, which initializes state directories. The service now sets `ABUTRON_DATA_ROOT=/var/lib/abutron-deriv-multi-native-reconciliation`, the pre-existing state directory permitted by its systemd sandbox, and explicitly sets `ABUTRON_LIVE_TRADING_ENABLED=false`. The new separate timer was disabled; the existing timer continues using the V9 executable through the drop-in. The old unit name is retained *only as a downstream dependency alias*.

**Not yet cut over:** V9 activation job and MT5 session manager remain staged/inactive; legacy APEX11 still controls CRM activation revision. The user-desired 6207 switch remains ON, but old ACK expiration intermittently blocks the V9 dispatcher. Owner-risk publisher also remains the legacy implementation, now reading the corrected Deriv native reconciliation report. Do not claim full APEX retirement.
