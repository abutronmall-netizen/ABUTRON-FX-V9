# FP Markets DEMO 0512 — V9 Native Writer Integration Plan

**Status: NOT DEPLOYED.** Corrected against read-only Ubuntu evidence on **20 November 2026 (22:39 SAST)**. The source branch contains commit `5f0dfea` and the merged PR #59 fixes; the reported 804 tests passing / 4 skipped are **source-level results**, not broker execution proof.

**Scope:** FP Markets **DEMO 0512 first**, then 1636 → 1622 → 2582 individually. Existing FP feed services, order bridges, CRM controls, original broker receipts and Deriv DEMO are to remain intact. **FP LIVE 9392 remains OFF and unchanged.** Editing this document does not authorize changes on Ubuntu.

## 1. Verified existing FP infrastructure

| Account | CRM / existing switch | Current terminal / new V9 writer |
|---|---|---|
| DEMO 0512 | CONNECTED, desired ON, effective ON | Existing active `abutron-client-mt5-<CRM CID>/terminal` feed; **new V9 writer absent** |
| DEMO 1636 | CONNECTED, desired ON, effective ON | Active FP feed; **new V9 writer absent** |
| DEMO 1622 | CONNECTED, desired ON, effective ON | Active FP feed; **new V9 writer absent** |
| DEMO 2582 | CONNECTED, desired ON, effective ON | Active FP feed; **new V9 writer absent** |
| LIVE 9392 | CONNECTED, desired OFF, effective OFF | Data feed only; outside this plan |

Resolve account-bound CRM IDs at runtime from `/var/lib/apex-signup/accounts.sqlite3`. Do **not** commit account connection IDs, seeds, passwords, or keys.

The existing execution environment includes:

- Feed worker `/opt/apex-client-mt5/worker_feed_v8_fp_handoff.py`. For 0512, the actual terminal is under `/var/lib/private/abutron-client-mt5-<0512 CRM CID>/terminal/`. This is **not** an installation target for the new V9 EA; do not overwrite the live feed chart or EA.
- Existing 0512 writer/router `/opt/apex-client-mt5/fpmarkets-execution/fp_signal_order_router_apex11_0512.py`, activation `fp_demo_activation_ack.py`, and reconciliation `fp_native_reconcile_readonly.py`. Systemd units `fp-demo-single-writer-preflight.service`, `fp-demo-native-activation-ack.service`, and `fp-native-reconcile-readonly.service` are scheduled **oneshots**, commonly inactive between successful timer runs.
- Evidence directories `/var/lib/abutron-fp-execution/activation/`, `order-router/`, `reconciliation/`, and `dual-account-reconciliation/`; FP broker-native history `/var/lib/apex/operator-live/fp-native-history.json`.
- Shared owner exposure `/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json`. Use current authentic records; do not copy old values into new reports.

**Absent at last check:** proposed `/var/lib/private/abutron-pr37-direct-writer-fp-demo0512-qa`, `/var/lib/abutron-v9-production-fp-0512`, and `/var/lib/abutron-fp-v4-0512/v4-native-execution-cost-0512.json`. These are **proposed future paths**, not existing deployed components. The other three DEMO accounts also lack their proposed V9 writer/data-root paths.

## 2. Source/runtime discrepancies that must be corrected

1. **Wrong production import path.** `/opt/abutron-v9-execution-repair/V9-10794e2/src/abutron/agent/production_fp_demo.py` is **not installed**. The new module exists in the source worktree. Do not run the previous preflight command from that older Deriv checkout; it cannot establish FP readiness.
2. **The enablement script is not 0512-only.** `ops/v9-cutover/enable_trading_fp_demo.py` hardcodes all four DEMO accounts. Its all-account precheck and subsequent preset writes cannot implement the requested **0512 → 1636 → 1622 → 2582** one-account-at-a-time rollout. Add an explicit account selector and test that only the authorized account is changed before using it.
3. **CRM activation uses existing controls.** FP uses `apex_trade_controls` (both desired and effective state). The Deriv-style `abutron_v4_0512_activation_revision` table **does not exist** in FP. PR #59 correctly distinguishes them. Do not create parallel FP activation tables or re-toggle customer switches.
4. **Native execution cost is not history.** The FP cost-report default above does not exist. `fp-native-history.json` and read-only native reconciliation are useful evidence but do not, by themselves, satisfy `DemoNativeCRM`'s contract for current account equity, verified bid/ask + OrderCalcProfit, instrument `tick_size`, `account_suffix` and `environment`. Adapt the existing FP-native source or implement the missing cost producer using genuine current values. **Never fabricate a verification flag.**
5. **FP symbol mapping.** Use the current account's broker-native `instruments` mapping, symbol spelling, tick/lot steps and supported asset classes; never use Deriv's `Volatility 50 Index.0` on FP. PR #59 fixes source symbol selection, but real instrument reports and quote coverage are not yet verified on the new writer.
6. **Risk settings need precise continuity.** The new FP runner specifies `RiskGate(per_trade_pct=0.0015, portfolio_cap_pct=0.02)` while the existing 0512 activation code references a **0.10% per-trade** configuration. A **2% aggregate open stop-risk** number is not interchangeable with a **20% portfolio drawdown ceiling**. Preserve the actually authorized FP position-sizing and protective SL/TP/trailing settings; do not silently change them by applying a generic Deriv preset.
7. **Per-account runtime context.** The FP runner reads `ABUTRON_DATA_ROOT` for its event log. The engine DB, receipt ledger, signed writer and cost/owner paths must all belong to 0512. Verify the trained V9 model and its SHA in the **actual** runtime checkout before executing. Do not reuse another account's engine or secrets.
8. **Order-writer ownership.** The existing FP 0512 router/order bridge may already own order dispatch. Before attaching an order-capable `ApexNativeWriter`, inspect current routing and agree on a single execution writer for 0512. A second terminal does not inherently prevent duplicate dispatch; do not add a new supervisory/veto architecture, and do not disable the established writer blindly.
9. **Release integrity.** The specific push-CI EX5 SHA-256 is `b6b63afeb7c4939a2bea1f40cfad58d8288af4009d8dc4e2534d7196162b91f3`. Use the exact verified binary, not a separately recompiled file with an assumed equal hash. Account-binding and secret provisioning must be proven from the actual FP DEMO session, without publishing secrets.

## 3. 0512-first integration sequence

### A. Offline corrections (current permitted scope)

- Read the existing 0512 native order-router, bridge, activation publisher and reconciliation output; identify the present order-writing authority.
- Connect `production_fp_demo.py` to genuine FP native instrument/cost evidence and existing CRM controls. Preserve the existing FP account and owner risk policy.
- Provide a **single-account** 0512 option for the enablement runner; verify the FP preset and release SHA before any write; reject stale or foreign-account status. The source tests alone do not establish that an MT5 terminal is ready.
- Verify isolated FP data root/engine paths, model availability and the current owner-account association. Keep the original feed and LIVE settings unchanged.

### B. Account-specific staging on Ubuntu (requires explicit production-change authorization)

1. Record the existing 0512 worker/bridge/preset/binary/receipt state and rollback procedure. Do not overwrite the active FP feed terminal.
2. Use `/var/lib/private/abutron-pr37-direct-writer-fp-demo0512-qa` for a **new dedicated terminal only if confirmed necessary** for the agreed one-writer topology. Bind it to 0512's real CRM ID and broker server, provision its own authorized secret securely, and use a mode-`0600` account-bound preset. Never use the blank template.
3. Establish account-specific runtime state under `/var/lib/abutron-v9-production-fp-0512` and a current FP native cost report from real broker data. Do not treat creating a directory as operational readiness.
4. Start the new writer only within the agreed 0512 execution routing arrangement. Verify fresh signed account, bars, quotes, position snapshots, native owner exposure, and the actual release SHA.
5. Execute **0512 preflight only** from the correctly installed FP source and matching `ABUTRON_DATA_ROOT`. Its result must explicitly state `BRAIN_DECISION_EVALUATED_EXECUTION_NOT_INVOKED` with real candle/quote counts and **no orders**.

### C. 0512 DEMO execution evidence

- Use the **0512-only**, tested execution path. Do not invoke the current four-account enablement script. No forced test trades. A `V9_NATIVE_DISPATCH_EVALUATED` cycle with zero orders is not broker-fill proof.
- If a genuine V9 signal results in an order, reconcile the exact CRM/request identity and broker ACK/REJECTED with ticket/deal, FP symbol, volume and protective SL/TP. Preserve true UNKNOWN receipts without blind retries.
- Verify a broker-confirmed trailing SL modification only after a genuine qualifying DEMO position. Compilation alone does not verify trailing.
- Read `ABUTRON_ORDER_FLOW_V2_<nonce>.txt` only for the authenticated account/cycle. Record `trade_ticks` only if actual FP trade-side flags and volumes were exported; otherwise label `candle_derived` as an estimate.

### D. Subsequent DEMO accounts

After 0512 has independently verified native order/protection evidence, repeat the same process **1636 → 1622 → 2582**, one at a time. Each has distinct existing worker and order-writer history. Reuse the tested code, **not** 0512's identity, credentials, EA state, receipts or cost assertions.

## 4. LIVE 9392 — excluded

FP LIVE 9392 remains **CONNECTED for data, desired OFF, effective OFF**. No LIVE account, service, EA, owner-risk limit or control change belongs in this 0512 plan. LIVE execution is a separately authorized task. Do not assert a particular `live_intents` table or future deployment gate exists without checking the current production schema.

## 5. Rollback and evidence standard

For any subsequently authorized 0512 deployment, preserve exact before/after EA and preset hashes, the existing router's authority, native receipt/engine state and service configuration. If the new path fails, undo **only** the new 0512 V9 integration, leaving established FP feeds and history intact. Keep Deriv DEMO and LIVE unchanged. No fabricated broker confirmations, no unrequested trading veto layers.

**Documentation updated does not mean trading deployed:** As of this audit, FP 0512 still lacks the new dedicated V9 writer, V9 data root and native cost-report integration. The first operational milestone is a verified single-writer 0512 integration; only then proceed to genuine DEMO execution evidence.
