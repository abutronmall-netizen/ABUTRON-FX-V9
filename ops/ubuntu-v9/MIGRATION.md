# ABUTRON FX V9 — Ubuntu source migration package

Captured from the authorized Ubuntu deployment on 2026-11-20. This branch is based on PR #37 head 588427d, not on main. It is a SOURCE UPLOAD, not an installation or certification.

## Uploaded source inventory

- 4 previously absent production Python modules in src/abutron: Deriv DEMO runner, CRM-native binding, native protected-receipt reconciliation, and owner-native risk reader.
- 50 V9 and FP archive systemd service/timer definitions in ops/ubuntu-v9/systemd/.
- 22 scripts under ops/ubuntu-v9/scripts/: causal candidate learning, native closed-trade archival, feedback, FP M5 inference, recovery, account monitors, broker evidence and notifications.
- 4 deployed-core source snapshots under ops/ubuntu-v9/production-deltas/: dispatcher.py, runtime.py, market_data.py, writer_gateway.py. These DIFFER from PR #37; snapshots MUST be reconciled rather than blindly replacing the newer branch.
- Agentic runtime, broker-native loss research, paper trainer, trained-brain signal runner and native-market V3 research utilities in ops/ubuntu-v9/subsystems/.
- Broker-native signed exposure/PR37 writer bridge, FP source-recovery and legacy risk dependency references in ops/ubuntu-v9/legacy-adapters/.

Private CRM connection identifiers and full broker account logins in selected operational scripts were replaced with required environment variables. These are MIGRATION EXPORTS, not byte-identical executable production releases. Real credentials, activation secrets, terminal profiles, signed runtime evidence, SQLite trading histories and proprietary broker data were intentionally excluded.

## Current real architecture and reason legacy APEX persists

1. /opt/apex-client-mt5/manager.py still manages the per-customer MT5 terminal and broker heartbeats. Running V9 consumes those broker-native sessions and data exports. Disabling that manager now would disconnect V9.
2. abutron-6207-native-activation-ack.timer executes an APEX11 script that updates the CRM activation revision acknowledgment. Its V4 supervisor requirements can clear the ACK even after V9 has authenticated real DEMO broker fills.
3. The MT5 manager also sets effective_enabled=0 after a short broker-heartbeat gap; merely becoming CONNECTED again does not restore the prior effective state. Desired client ON/OFF must remain separate from a transport heartbeat.
4. Deriv DEMO 6207 is the only verified V9 automated order-sending production route. The native writer and authenticated exposure snapshots have demonstrated protected DEMO positions, but multi-account V9 dispatch, universal trailing-stop modification and autonomous production-brain adoption are NOT established.
5. The current V9 owner-native risk reader consumes a report still emitted by legacy customer-wide risk infrastructure. A first-party V9 report publisher and client-MT5 manager replacement are required before removing those old components.

Do not mistake a GitHub commit, compiled EA or passing synthetic tests for a deployed broker-trading chain.

## Critical dependencies not yet replaced

- The APEX-managed per-client MT5 terminal/session manager, its Wine/Xvfb profiles and authenticated broker feed adapters.
- The APEX11 activation ACK authority and CRM update/heartbeat handling. This must be replaced with a single V9-owned activation lifecycle, preserving the requested client switch and validating current authenticated broker identity.
- The customer-wide native exposure/risk reporter, which remains an external source for V9 owner risk calculations. The old implementation contains private hardcoded user/account identifiers and was not published intact.
- V9 account-scoped order routes for Deriv DEMO 7593/9244 and FP DEMO 0512/1636/1622/2582; the source bundle DOES NOT create broker writers for those accounts.
- Production-broker MT5 trailing amendments and confirmations for each account, plus safe closed-trade model-promotion workflow.
- Migration of existing portal/CRM dependencies before retiring APEX signup, portal or DB schemas.

LIVE broker execution was not changed or enabled by this migration. Do not restore retired accounts or assume any historical routing remains current.

## Source layouts

- src/abutron/: verified server-only V9 executable modules (new, normal package paths).
- ops/ubuntu-v9/scripts/: server operational Python source, sometimes parameterized for privacy.
- ops/ubuntu-v9/systemd/: source-only snapshots of service and timer files; many contain server-specific absolute paths.
- ops/ubuntu-v9/subsystems/: auxiliary training/market data/agentic source.
- ops/ubuntu-v9/production-deltas/: exact divergent Ubuntu source files for three-way merge; NOT automatic overrides.
- ops/ubuntu-v9/legacy-adapters/: source of broker bridge dependencies to replace; some service snapshots are intentionally absent if they embed private identifiers.

## Migration implementation sequence

1. Reconcile the four production-delta modules with the current PR #37 code and tests, without resetting local server commit 8a37ed2. Preserve both the order-send actual fill price and signed historical position protection reconciliation.
2. Build a V9-native CRM session and activation authority. Separate user-desired ON/OFF from transient broker health and short-lived order authorization; ensure a legacy APEX timer cannot overwrite V9's account state.
3. Build and test a V9-owned MT5 terminal manager; preserve existing per-client terminal profiles, broker identities, history feeds, EA attachment, and single-writer isolation. Avoid duplicate EAs/order senders.
4. Replace legacy native owner-exposure/risk publisher, with independent broker-native position/stop-risk reconciliation and preserved account/portfolio risk limits.
5. Prove account-by-account V9 chain: authentic quote/M5 -> trained brain/sniper -> native sizing -> CRM user switch -> single native writer -> MT5 OrderCheck/OrderSend -> native deal -> broker SL/TP -> trailing amendment -> CRM evidence -> closed-trade learning.
6. Deploy a DEMO account one at a time with a code SHA pin, test account isolation and rollback on failure. Only after a replacement works should its specific conflicting APEX timer/service be disabled. Keep LIVE and retired accounts unchanged unless separately authorized.
7. Keep the CRM, client/admin portal and account management online until their V9 equivalents are operational.

## Preserve existing trading policy

- User's 20% portfolio/drawdown ceiling is distinct from the current 2% aggregate-open stop-risk policy and is not automatically interchangeable.
- The deployed 6207 DEMO runner currently uses 0.15% per-trade risk; keep per-account configured limits and protective SL/TP with trailing logic.
- Do not introduce redundant research/certification vetoes or silently change LIVE trade permissions.
- Never add or commit secret files, private broker account statements, tokens, signed runtime receipts, private keys or credential blobs.

## Required environment (values provided securely, never committed)

See env.example. Environment-based identities must be provided to the parameterized scripts and service units before they can run. Publishing source is not proof that their live runtime dependencies are configured.

## Verification

Run python3 ops/ubuntu-v9/verify_bundle.py to check uploaded Python syntax and reject known private-literal patterns. Also run the repository's existing test and privacy CI before merging. This validator cannot prove broker connectivity, order execution, trailing or source authenticity.

This bundle deliberately DOES NOT stop, restart, enable or disable any Ubuntu service.
