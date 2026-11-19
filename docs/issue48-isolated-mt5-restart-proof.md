# Issue #48: FP Markets DEMO broker-native reconnect evidence

**Verified:** 19 November 2026. Related: [PR #47](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/pull/47), [Issue #48](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/issues/48), [PR #45](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/pull/45).

## What failed

A read-only, broker-authenticated MT5 Expert successfully exported the first native positions and `HistorySelect(0)` ledger. Relaunching MT5 with the **same Wine prefix** then ended before producing another authenticated native snapshot. This did not prove any broker position had disappeared.

A separate startup issue was identified: even with the original broker positions present, MT5 could initially report **zero historical deals** on reconnect, although the first snapshot had 893 deals. Treating that response as a complete history would have produced false certification.

## Verified isolated recovery procedure

1. Create a **private isolated MT5 portable data folder** and authenticate only to the designated **FP Markets DEMO** account with the existing systemd-encrypted credential loader. Trade-send and SLTP operations must be absent from the Expert and disabled at terminal startup.
2. Start the official MetaEditor-compiled **read-only** `AbutronFPPositionRestartSnapshotRO.ex5` under a **unique Wine runtime prefix**. Export the original authenticated account/session/nonce-bound native position and history snapshot. Save its original bytes under a root-private file and retain SHA-256.
3. Stop the isolated terminal process and its **isolated** wineserver. Do not stop, detach, kill or change production MT5, which runs under a separate prefix and systemd service.
4. Restart **the same MT5 portable data directory** with a **new unique Wine runtime prefix**. Set the QA-only `MinBrokerDealsToPublish` native input to the independently observed pre-restart history deal count. The read-only Expert must refuse to publish a snapshot until that minimum broker-history count is loaded.
5. Verify both immutable broker-native snapshots using `ops/issue44/compare_native_restart_ro.py`: exact account/session/nonce, unique position and deal tickets, protective SL/TP geometry, no widening of stops, no unexpected pending orders, no missing position absent broker-native exit evidence, and original historical deal ticket IDs present after reconnect. Recheck the official EX5 checksum and that **zero orders** and **zero trailing modifications** were issued by the QA.

**Observed proof:** the final isolated QA systemd service exited **0**. Same MT5 data folder, fresh Wine runtime for each launch, **7/7 protected positions**, **893/893 native history deal IDs**, **0 pending orders**, **42 seconds** between snapshots, **0 QA order/stop sends**. Signed read-only MetaEditor EX5 SHA-256: `1d28a74009bcdd733046f242faf3981e011501ce1a0b291842e66679d4e8e3cf`; [signed compile run](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/actions/runs/44580608420) had 0 errors and 0 warnings. Public source contract: 16 focused tests passed. Full immutable native source bodies are **private to the authorized Ubuntu server**; do not commit account IDs, login, ticket/deal IDs, session nonces, client history or secrets to this public repository.

## Important boundaries

- This verifies **same MT5 portable data folder with a NEW Wine prefix per start**, not the original broken second launch of an identical Wine prefix.
- It is an **isolated read-only DEMO restart**. The live production FP DEMO worker was **never restarted**, its EA and chart were **not** promoted, and this QA does **not** establish that a production rollout with open positions and all dependent services is recoverable. The flat-only production preflight must stay enabled until independent production-risk and rollback criteria are met.
- `HistorySelect(0)` returning 893 broker deals **does not prove true inception-to-present lifetime coverage**, historical account day-start/peak equity, or portfolio owner certification. Those require independently authenticated broker records.
- A protective SL/TP read-back is **not** a new Chandelier trailing amendment. A genuine risk-reducing `TRADE_ACTION_SLTP` result and separately authenticated native SL/TP read-back remain outstanding.
- Do **not** change owner-risk, V9 certification, CRM or LIVE execution flags based on this isolated proof.

## Remaining engineering work

The next controlled rollout must include an account-specific SHA-pinned dependency manifest, exact prior-state immutable backup, measured proof that open positions remain protected while the original production trade worker is replaced, independent broker-native identity/SLTP reconciliation after every step, and an automatic rollback if any invariant fails. Do not duplicate order writers or blindly retry an unknown broker request. Issues #44, #48 and #42 remain open until genuine operational proof is obtained.
