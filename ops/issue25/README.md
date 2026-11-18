# ABUTRON V9 — Issue #25 audited host-source handoff

**Review-only source snapshot, 18 November 2026. No production deployment.**

This directory contains individually reviewable copies of current ABUTRON V9 historical trade learning, M5 reconstruction, prospective model evaluation, and relevant systemd services/timers. It supersedes the **unreviewable pre-fix** archive in closed PR #26.

## Contents

- `scripts/`: ten root-installed service scripts represented as Python source files.
- `systemd/`: fourteen systemd service/timer texts captured from the authorized Ubuntu host.
- `sanitized-source-manifest.json`: SHA-256 of the corresponding host originals **at staging time**, SHA-256 of review copies, and whether sanitization modified a file.
- `test_issue25_review.py`: deterministic offline tests for broker profile lookups, failure on wrong LIVE/DEMO scope, native ticket collision resistance, and private profile ID exclusion.

## Sanitization differences (do not automatically deploy)

Three code files intentionally differ from their installed host counterparts:

1. `native_trade_archive.py`: changed a synthetic unit-test fixture containing a real full broker account number to a synthetic value that retains the relevant final four digits.
2. `verified_native_hcc_m5.py`: replaced four hardcoded CRM connection-profile IDs with read-only runtime discovery of the exact two Deriv DEMO and two FP DEMO connections; rejects missing or wrong-mode profiles.
3. `fp_native_m5_collector.py`: replaced four hardcoded FP DEMO profile IDs with read-only CRM discovery; rejects duplicate accounts or wrong modes.

These changes passed isolated offline tests, but **they are NOT proof of live deployment compatibility**, filesystem access, native broker execution, or sufficient model accuracy. The source snapshots have file names ending in `.py` for code review; systemd units retain the existing host `/usr/local/sbin/` paths. An installer must map review paths back to the correct destination and re-run full preflight.

## Conservative installation and rollback

1. Verify the live process/systemd state, native account roster, SL/TP controls, live-account disabled state, owner-risk limits, current backups, and rollback plan.
2. Compare `sanitized-source-manifest.json` source hashes with *live* host binaries. An unexpected hash mismatch blocks automated installation; reconcile manually instead of overwriting.
3. Install in staging with the same explicit owner-scoped read-only environment and run all offline tests, then run native account-source preflight. Verify source-only code does not submit orders, activate LIVE, or promote a candidate.
4. Explicitly authorize any production change after independent tests. Roll back changed scripts and unit files together; do not mix old and new event-key formats.
5. Keep statements, per-owner model checkpoints, account IDs, cached native price histories, SQLite ledgers, receipts, secret provisioning and recovery files outside public Git.

## Limitations

- This PR is a **source migration** only; it does not certify model performance, complete historical equity peaks, restore missing original EA ACKs, or fix broker OrderSend.
- A successful unit test or GitHub CI run is not a live broker acceptance.
- The old `issue25-code.tgz` blob in closed PR #26 remains in Git history; cleaning/revoking old refs requires a separate coordinated history operation, not merely `.gitignore`.
- Protect the existing 20% portfolio drawdown ceiling. FP LIVE x9392 remains OFF; no order submission or automatic promotion is authorized by this source handoff.
