# ABUTRON FX V9 — PR #18 native validation checkpoint

**Verification:** 2026-11-16 23:19 UTC (2026-11-17 01:19 SAST)  
**Branch:** `fix/pr17-native-auth-fileio-20261117`  
**PR:** [#18](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/pull/18) stacked on [#17](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/pull/17)  
**Status:** SOURCE COMPILATION VERIFIED; NATIVE HMAC AND DEMO BROKER EXECUTION NOT CERTIFIED.

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
