# Issue #38: DEMO-native MT5 evidence and writer QA

Checkpoint verified on 2026-11-19. Parent PR #37 compiled native MetaEditor with zero errors and zero warnings.

The previous pinned QA EX5 SHA-256 is 290ed85164ee165e37c297541842b66504c8689002fbbd006be0b5df9f5e6138. This branch changes the MQL5 source to use InpExposureOnly=true by default, so a NEW build and NEW pin are required. Do not attach the previous EX5.

Deriv DEMO 6207's existing independent read-only sidecar was authenticated and the prior QA binary was staged in an ABUTRON-PR37-QA Experts folder. It was NOT chart-attached, did not send orders, and did not change activation secrets or terminal profiles.

## Source and worker behavior

- stage_demo_writer_qa requires an existing terminal data folder with MQL5/Files and MQL5/Experts, DEMO account identity and exact compiled EX5 SHA-256. It writes ONLY the QA Experts binary, refuses differing overwrites and does not modify any chart or secret.
- bind_demo_data_source binds existing DEMO native exports without launching a second terminal. Initial status is SOURCE_BOUND_UNVERIFIED, not broker CONNECTED.
- The publisher and CRM verify a fresh signed native snapshot, connection ID, account login, server, complete broker positions and stop-risk before reporting CONNECTED.
- Worker publication failure immediately invalidates broker authentication and CRM exposure certification.
- New native EA exposure-only mode defaults on; the writer does not call ProcessOrderRequests while enabled.

## Operational procedure

1. Identify the exact registered DEMO CRM account and actual terminal DATA folder from independent broker evidence, not the path name alone.
2. Confirm ABUTRON_ENV=demo, ABUTRON_AGENTIC_ENGINE=false, ABUTRON_LIVE_TRADING_ENABLED=false; inspect existing attached EA, secrets, order authority and rollback options.
3. Run scripts/stage_issue38_demo_native_qa.py with --connection-id, --terminal-data-dir, --ex5, --sha256 and --dry-run. It reads the registered DEMO account and verifies the exact file pin; it never attaches or sends orders.
4. After the read-only preflight and rollback checks pass, run the same script with --stage. The result QA_STAGED_NOT_ATTACHED means copied, NOT trading or broker verified.
5. Independently attach the new exposure-only EA on a dedicated DEMO chart/profile, never on an existing read-only sidecar chart or LIVE session; verify terminal's actual account/server and secure activation-secret provisioning. This step is NOT completed by staging.
6. Require an actual EA-produced ABUTRON_NATIVE_EXPOSURE_V2 snapshot and independent HMAC, nonce, account, equity, native ticket and stop-risk reconciliation. Test verified-flat, native-open, source-loss, stale and replayed cases.
7. Separately validate a real DEMO OrderSend/broker deal and protective SL/TP, trailing, independent ACK and CRM reconciliation, preserving the 20% portfolio limit. Do not enable LIVE.

## Rollback

Remove only the QA-staged EX5 after matching its SHA-256. Do not remove other EAs, MT5 profiles, source broker snapshots, historical evidence or backups. Keep existing read-only workers running.

## Evidence not yet claimed

Real EA HMAC/JSON round-trip on attached DEMO, native owner-risk certification, actual broker ACK, protective stops, trailing and agentic production rollout remain unverified.
