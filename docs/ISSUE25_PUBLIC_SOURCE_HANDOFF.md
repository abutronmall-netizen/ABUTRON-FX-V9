# Issue #25 — Public-source handoff and broker evidence boundary

This file concerns **source control**, not broker execution authorization or production model promotion.

## Verified release blockers (18 November 2026)

- PR #26 has a single tracked binary bundle, `issue25-code.tgz`, instead of independently reviewable scripts and unit files. It predates the broker-ticket identity, deduplication recovery and dependency repairs already installed on Ubuntu.
- The archive contains runtime profile identifiers. Do not use that package as a public source-of-truth release. Neither `.gitignore` nor closing a pull request deletes a blob from previous Git commits.
- The arena branch is separate from PR #26 and diverges from `main`. Its `.gitignore` must not replace main's carefully re-included `data/brains/unified_m5/` artifacts wholesale.

## Accepted source-control procedure

1. Keep native broker statements, original receipts, account/owner IDs, market evidence, checkpoint and order ledgers in the root-private production evidence archive or authorized private backups — never the public repository.
2. Create a **new branch from current `main`**, and commit each sanitized, code-reviewed executable and systemd unit as a readable text file with tests. Replace hard-coded runtime connection-profile IDs with authenticated read-only CRM lookups; pin broker/account identity independently. Do not copy production credentials or original customer trade rows.
3. Include tests for broker-native ticket identity and distinct economically identical positions, stable reconnect-independent deduplication, partial-exit lifecycle rejection, true pre-entry completed M5, source SHA/revision fences, per-owner isolation, and duplicate-learning retries.
4. Review the final diff, run CI and isolated tests, and document rollback. Only after source parity with the deployed, verified host state should a reviewed source PR supersede #26. **Do not deploy from the old binary PR.**
5. Historical account-inception certification, current broker-native order execution and forward model evaluation are separate domains; merging source code is not equivalent to certification.

The `abutron-public-artifact-guard` workflow scans `git ls-files` (the tracked index) and rejects host-only evidence paths or formats. It does not retroactively purge prior Git history; use an explicit history-cleanup process if a previously committed artifact requires removal from public reach.

Production constraints remain unchanged: no automatic model promotion, no live-account activation, no changes to the authorized 20% drawdown policy, and no native orders from source-control tooling.
