# Production evidence gap — 2026-11-18 UTC

Tracked in [issue #22](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/issues/22). Related historical EA scanner defect: [issue #21](https://github.com/abutronmall-netizen/ABUTRON-FX-V9/issues/21).

## Confirmed state

| Category | Native evidence currently available | Why not certified |
|---|---|---|
| Linked-account exposure | 8/8 current position sets independently reconciled; 0 unknown sets | Current risk reconciliation is **not** historical peak/day-start proof |
| Native sampled equity | Per-account samples and sampled high-water marks | No authenticated, complete broker equity history across every instant; gaps in observation and exact 00:00:00 values |
| LIVE x9392 full-range history | MT5 `HistorySelect(0)` returned 0 deals, zero positions, and cashflows matching its returned $0 balance | Broker server may only retain a subset; absent an independently dated broker-issued *inception statement*, no lifetime guarantee |
| FP DEMO x1636 ACKs | 100/100 original ACKs matched to independent broker history and protected position stop-risk | One original post-OrderSend EA position file was not preserved; broker-derived validation cannot recreate it |
| FP DEMO x0512 prior-session ACKs | 215/215 prior original ACKs independently broker-matched | One original EA evidence file missing in the historical archive; related issue #21 |

These counts are observations from the 2026-11-18 audit, not permanent invariants. Recheck native timestamps, identities, and hashes for any subsequent production change. No LIVE order activation or broker statement inference is implied.

## Required authentic source records

1. **Historical equity:** broker-issued timestamped equity records including broker day-start values and actual historical intraminute maxima; include account binding, broker timezone/UTC conversion, data retention limits and full coverage intervals. A sampled high is never a *true* high, and a reconstructed cash balance is never equivalent to peak equity.
2. **Lifetime account coverage:** broker-issued inception-to-now MT5/portal statement with opening balance, all trade entry/exit deals, orders, deposits, withdrawals, credits, adjustments, fees, swaps, reversals, closed/truncated history notices. Reconcile every cashflow type, position lifecycle, opening/closing balances, coverage boundaries and any exceptions.
3. **Original EA evidence:** search genuine archived terminal copies/offsite backups using the *exact* account/session/signal hash. Preserve recovered original bytes with content digest, session binding and provenance. If absent, keep `original_ea_file_missing=true` and independently broker-match the ACK without forging the original.

## Completion conditions

- Historical equity extrema independently proved for **each** owner account, including explicit interval coverage and all missing intervals; no use of nearest sample as exact midnight or unobserved peak.
- Lifetime broker statements independently prove account inception, all funding movements and full exit lifecycle for every linked account; partial broker `HistorySelect(0)` alone cannot satisfy this.
- Original EA evidence recovered if authentically present; if not recoverable, exception remains visible rather than being relabeled "original preserved".
- Source integrity tests reject stale files, changed hashes, bad broker identities, overlapping/doubled cashflows, unsupported account currencies, unknown deal types, wrong timezone and incomplete span.
- Preserve owner risk policy and CRM state; FP LIVE x9392 stays OFF. No inference of order execution from signals, simulation or historical chart data.

## Operator commands (read-only)

```bash
python3 scripts/check_broker_evidence_contract.py --report /path/to/owner-evidence-auto-status.json
python3 scripts/check_broker_evidence_contract.py --selftest
```

The checker reads an **already-sanitized summary**, not raw broker histories. Exit code `2` means historical certification remains incomplete and is the expected status while records are missing. It **never** changes trading or certification on the server.

**Security:** This is a public repository. No broker account logins, client identities, complete broker order/export files, session keys, credentials, private backups, recovered EA evidence bytes, or full server manifests have been uploaded.
