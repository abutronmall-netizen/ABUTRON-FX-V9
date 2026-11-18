# Affected production code / review map (2026-11-18)

**Purpose:** point a developer to the exact affected source, with the observed immutable SHA-256 on Ubuntu. The paths below are local host paths, **not** URLs or files checked into this public repository. Review code through authorized private host access and do not upload genuine account/EA session records to this public repo.

| Component / host path | SHA-256 (UTC audit) | Review action |
|---|---|---|
| `/usr/local/sbin/abutron-customer-wide-risk-readonly` | `f4d1a5ba2d1a10a745a49b097403a1f7abf10e0fae949ea4f8438417fc437748` | Current 8/8 position reconciliation is separate from historical-certified owner-wide equity and lifetime exposure |
| `/usr/local/sbin/abutron-owner-evidence-auto-status-ro` | `c200b521f8c556bf666e688aa6bac74312afe7fa6866c8e692df8f87bf499190` | Surface missing source coverage, true historical proof flags and original EA exceptions |
| `/usr/local/sbin/abutron-owner-equity-fast-observation-ro` | `edff28152139ffe81d5ca8d6af7e6b550e401a81f4c5793f84bcf4d377cd5518` | Retain observed samples but never label sampled peak as true unobserved peak |
| `/usr/local/sbin/abutron-per-account-native-equity-ro` | `598505a3687a2caf55bb0c18388e2d866a017dd8b0d745dac70b3f596db33aa8` | Quantify samples and missing intervals per account, not just per owner |
| `/usr/local/sbin/abutron-fp9392-fullrange-history-observer-ro` | `fb249325f4652dceffb54293aedd1d3b8d63595dbbc7e0f64856fc4b077720a3` | Explicitly distinguish server-returned epoch-zero query from broker-inception completeness |
| `/usr/local/sbin/abutron-fp-demo-multiaccount-native-receipt-archive-ro` | `3adaeed735ef37b25de7492fb5be8a219750f7b32bbf9ac037bce72d84d37574` | Preserve immutable originals; distinguish mutable position snapshots from immutable ACK/intent data |
| `/opt/apex-client-mt5/fpmarkets-execution/fp_native_reconcile_readonly_1636.py` | `2e263020765b47a42d6b63a80c17845be12ec9f68d32f0c55c368caf212f273d` | Genuine 100/100 broker-matched ACKs, one missing EA file, no counterfeit reconstruction |
| `/opt/apex-client-mt5/fpmarkets-execution/fp_native_reconcile_readonly.py` | `6eef3cc6be25686294b8553fae233816bd775b8ae053a57f760a7327a3d86317` | FP x0512 historical original ACK scanner/EA evidence gap, see issue #21 |

Additional relevant sources (host-specific locations; inspect before editing):

- `/usr/local/lib/abutron-fp9392-live-fullrange-v105/fp_live_9392_fullrange_parser.py` — live epoch-zero history parser and returned cashflow balance, not independent inception certificate.
- `/var/lib/abutron-v4-broker-crm-supervisor-6207/six-account-native-daystart-cash-audit.json` — **derived** day-start cash/balance reconstruction (not historical equity maxima); private evidence, do not publish.
- `/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json` — live risk aggregation; contains private owner/account evidence, do not publish.
- `/var/lib/abutron-v4-broker-crm-supervisor-6207/owner-evidence-auto-status.json` — status summary; sanitize before sharing.
- `/var/lib/abutron-fp9392-live-fullrange-proof/latest.json` — true broker query result, independently acquired inception statement still missing.
- `/var/lib/abutron-native-multiaccount-receipt-archive/` — per-session original archive; protect source bytes, don't commit.
- `/var/backups/` — recovery sources; never delete or claim original recovery without matching exact content and session provenance.

**Status from 2026-11-18:** FP DEMO x1636 repaired in production using broker evidence without fabricating an EA file. All eight current sets can reconcile. The owner-wide historical evidence gate remains `BLOCKED_INCOMPLETE_EXPOSURE` for genuine outstanding broker proof. FP LIVE x9392 remains OFF. Existing owner max drawdown ceiling remains 20%.

## Developer handoff

Submit source-code changes against `main` in a separate PR after checking the *current installed binary* and source pins. Do not compile an older MQL5 source over a newer installed EA. Test all affected broker identity, risk, source freshness and rollback invariants. Issues: [#22](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/issues/22), [#21](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/issues/21).
