# ABUTRON FX V9 — PR #18 native validation checkpoint

**Verification:** 2026-11-16 23:19 UTC (2026-11-17 01:19 SAST)  
**Branch:** `fix/pr17-native-auth-fileio-20261117`  
**PR:** [#18](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/pull/18) stacked on [#17](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/pull/17)  
**Status:** SOURCE COMPILATION AND NATIVE HMAC VERIFIED ON FP MARKETS DEMO; BROKER ORDER EXECUTION NOT CERTIFIED.

## Exact native build evidence

The compiled source is `mql5/ApexNativeWriter.mq5` on this branch.

| Artifact | Verification |
|---|---|
| Git source blob | `3da75c5222716f1b2d32d0a89e06116515e2f7dd` |
| Source SHA-256 | `cb658b57530f5757be0fd05b8616d57cd2633e3953e2d77f9c47fe824cbfd9f7` |
| Compiled EX5 SHA-256 | `a1955645652b9b233bbd27026dcc3b78a61937676ce717da66d9bf2a56b4befe` |
| Compiled EX5 size | 41,158 bytes |
| MetaEditor report | **0 errors, 0 warnings** |
| Target | X64 Regular |
| MetaEditor elapsed | 1,523 ms |
| Runtime | Wine 11.20 (Staging) invoked through an isolated transient systemd service |

### Compiler excerpt

```text
Z:\\var\\tmp\\abutron-pr18-native-validation-20261117\\ApexNativeWriter.mq5 : information: compiling ...
: information: code generated
Result: 0 errors, 0 warnings, 1523 ms elapsed, cpu='X64 Regular'
```

Source, EX5, and UTF-16 compiler log remain in the host's isolated directory:
`/var/tmp/abutron-pr18-native-validation-20261117/`.

The original attempt from the remote shell failed at a seccomp `Bad system call`. The successful attempt ran under `systemd-run` in an isolated Wine prefix, using the server's existing Wine 11.20 runtime and MetaEditor64 (no production MT5 terminal).

## Python test evidence

- `tests/test_audit_fixes.py`
- `tests/test_risk.py`
- `tests/test_platform.py`

**54/54 focused tests passed** after the native source corrections.

The complete Python suite was previously interrupted by a missing trained-brain test fixture in the isolated checkout (87 passed, 4 skipped before that failure). Do not claim full-suite green status from these checks.

## Native HMAC smoke-test attempt

A separate MQL5 script was generated from the exact `HmacSha256Hex` / `Sha256Hex` helpers and compiled using MetaEditor:

- **0 errors, 0 warnings**.
- Public test vectors: ASCII key/message, >64-byte key with multiblock message, UTF-8 key/message.
- The isolated MT5 terminal initialized from `[StartUp]` config but exited before writing the result file.
- **No verified MQL5 runtime signatures** are available yet; the script compile itself is not cross-language HMAC proof.

No broker credentials or order authority were supplied to the smoke test.

## Release blockers

1. Execute the HMAC known-answer script inside an authorized isolated DEMO MT5 runtime, compare every actual MQL5 digest against Python `hmac` digests.
2. Confirm activation-secret provisioning and activation-revision handoff in the **same verified MT5 account** without exposing secrets or arming a different account.
3. Broker-confirmed DEMO order lifecycle: accepted/rejected retcode, ticket/deal, SL/TP, protected-risk/owner exposure reconciliation, exact one-writer ownership.
4. Implement and test LIMIT order transport independently; current EA refuses unsupported LIMIT requests rather than silently changing them to MARKET.
5. Reconcile remaining V9/FP native market feed freshness issues; production 13/13 inference and order proof are separate from this source compilation.

**No LIVE trading settings changed, no production EA installed, no PR merged, and no broker order submitted during this validation.**

## 2026-11-16 23:42:50 UTC — authenticated FP DEMO native HMAC proof

**VERIFIED 3/3 Python-to-MQL5 known-answer vectors.** The authorized FP Markets DEMO 7410512 MT5 worker was connected and its fresh account identity confirmed. The test-only \`ABUTRON_PR18_HMAC_0512_SAFE.ex5\` was compiled with **0 errors / 0 warnings**, installed only under the connected DEMO terminal's \`MQL5/Scripts/ABUTRON\` path, and run once from the Navigator. The test source was explicitly restricted to DEMO login 7410512, contains no order-sending APIs and does not call \`TerminalClose\`.

The script wrote its own broker-terminal-native output file \`MQL5/Files/ABUTRON_PR18_HMAC_KAT_V1.txt\`, owner-only mode \`0600\`. Its contents were independently compared with Python \`hashlib/hmac\` reference values, matching in all 3 cases:

| Known-answer case | Native vs Python SHA-256 HMAC | Result |
|---|---|---|
| ASCII key + standard text | \`f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8\` | **PASS** |
| Long 65-byte key + multiblock order message | \`778986646c5c0481245bda8b1184801934beefd2e78470ad64a8111232492662\` | **PASS** |
| UTF-8 key and Unicode message | \`1fb50011299db8a824a7b72b3ca789d5b9e5304786779ea58c87c1a47b042189\` | **PASS** |

- Native test result SHA-256: \`ad3bbbed7588020aa75fb9a6da015541b63dfb70f958d0cbb5b9ad54ddae3d4b\`
- Exact 0512-safe test binary SHA-256: \`e30e55442742ef0428ece94a2c60c7ce8b762be0520bd9dc62eb68df0f1bbd1e\`
- Root-only source, compiled EX5, compiler log, Python reference JSON and native results backed up at: \`/var/backups/abutron-pr18-hmac-fp0512-proof-20261116T234250Z/\`
- Reusable, non-disruptive DEMO-only HMAC source compiled separately at **0 errors / 0 warnings**, Git blob \`55310e60ef8b8bf67bbe925e080c857715e958cd\`: [\`mql5/tests/ABUTRON_PR18_HMAC_KAT.mq5\`](../mql5/tests/ABUTRON_PR18_HMAC_KAT.mq5).
- The FP DEMO worker heartbeat remained fresh. No LIVE configuration, order authority or broker orders were changed by the test.

**Conclusion:** The actual native SHA-256 HMAC helper matches Python for all three coverage cases, including multi-block input and UTF-8. The previous isolated-terminal startup failure is superseded for this HMAC check. This result **does not** prove the complete JSON request parser, activation revision and secret provisioning, broker fill retcode, SL/TP, owner-risk or LIVE readiness. Those remain separate engineering checks.

**Post-test cleanup:** The unique one-shot `ABUTRON_PR18_HMAC_0512_SAFE.ex5` was removed from the active FP DEMO terminal after its byte-identical compiled binary and result were archived. The native result file remains available in the test terminal's `MQL5/Files`, and the root-only evidence copy is preserved. The MT5 worker remained connected. This removes an unnecessary test executable from the active trading environment without changing an existing EA or any order settings.

## 2026-11-17 00:12 UTC — true FP DEMO 0512 13-field JSON handoff test

**VERIFIED: both native file-handoff cases passed inside FP Markets DEMO 7410512**. A test-only MQL5 script with no \`OrderSend\`, no \`WebRequest\`, no \`TerminalClose\` and an exact DEMO-account guard read the following *actual terminal sandbox* files:

- \`ABUTRON_PR18_HANDOFF_GOOD.json\`: 13-field canonical signed request with signed expiry, actual account binding, and all symbol/volume/SL/TP/trailing fields → **GOOD|PASS**.
- \`ABUTRON_PR18_HANDOFF_TAMPER.json\`: same signature and order intent but altered symbol (\`EURUSD.r\` → \`GBPUSD.r\`) → **TAMPER|PASS**, meaning tampering was rejected.

The native test harness compiled via MetaEditor64 with **0 errors / 0 warnings** and produced \`ABUTRON_PR18_HANDOFF_KAT_RESULT.txt\` from the genuine connected DEMO terminal. Both result rows were independently checked against the input JSON and expected signature behavior.

Root-only immutable evidence directory:
\`/var/backups/abutron-pr18-native-handoff-fp0512-20261117T001201Z/\` (native result, both JSON inputs, exact test source/EX5, build log, SHA-256s and \`VERIFIED_CHECKPOINT.json\`).

The test executable and both input files were removed from the active MT5 terminal after copying and verifying the evidence. Existing account EA and portfolio risk settings were not modified.

## 2026-11-17 — PR18 signed-file delivery and expiry repair

- \`src/abutron/execution/writer_gateway.py\` now writes an account-bound pending JSON directly to \`MQL5/Files/ABUTRON_REQ_PENDING.json\`, which the native writer actually consumes; it no longer sends order payloads into the incompatible \`MANAGER_SOCK\` protocol.
- 13-field Python/MQL5 canonical HMAC now includes signed \`expires_at\` (30 seconds by default), with native time-based expiry rejection. This is **in addition to** already-verified HMAC known-answer vectors.
- Account-bound \`mt5_login\` is checked against the real MT5 login; \`InpBrokerServer\` and connection id are enforced in native initialization.
- Per-request immutable \`ABUTRON_NATIVE_RECEIPT_<request_id>.json\` receipts and replay checks replace time-varying receipt filenames; the Python gateway will not infer an ACK from an absent or mismatched receipt.
- Account-wide file handoff uses a cross-process lock, atomic no-clobber pending creation, 0600 files and the actual MT5 worker UID/GID. If the caller cannot provision the private worker directory, arming fails; the 0700 terminal permissions are **not weakened**.
- \`tests/test_pr18_account_handoff.py\` exercises simulated native ACKs, account mismatch, missing receipts, pending collision and expiry-HMAC binding. Existing sniper tests were corrected to reject an unarmed fake ACK, rather than asserting that a disarmed writer can trade.
- **70/70 focused Python tests passed**.
- Exact updated EA source Git blob \`044f0c5ae1cd34320d3e04213ae8ca8fb3a9fc9d\` compiled with **0 errors / 0 warnings** under MetaEditor64, producing 42,530 bytes, EX5 SHA-256 \`f967a48a951ee9bea63d22ea1889e40ce2a49d8ac11250c5244a530197b3c067\`.
- Root-only **staged, not attached** deployment with manifest and source pins: \`/var/lib/abutron-v9-pr18-staging/20261117-native-handoff-044f0c5a/\`.

**Production integrity status:** The operating FP Markets DEMO V101 order bridges were **not replaced**, PR18 is **not attached to a trading chart**, and there was **no PR18 broker order**. FP native V9 inference subsequently reached **13/13 current symbols on both 0512 and 1636**, including GER40 recovery from genuine new broker ticks/M5 bars. The separate broker-native reconciliation can temporarily report \`FP_BRIDGE_BROKER_EXPOSURE_MISMATCH\` while positions are changing; it later returned \`RECONCILED_READONLY\` for both accounts. The strict owner-wide exposure gate and broker SL/TP proof must be rechecked at the moment of any writer switchover.

**Not certified:** privileged root-to-MT5 worker handoff in the actual admin process, unique writer/chart attachment vs V101, LIVE readiness, signed request to *new* PR18 broker-acknowledged fill, SL/TP + trailing confirmation, native LIMIT support, and end-to-end owner-risk controls. This PR must remain **draft** until those are established.
