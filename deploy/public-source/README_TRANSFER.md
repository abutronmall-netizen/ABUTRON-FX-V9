# Issue #25 — Sanitized Source Transfer

This directory receives sanitized, reviewable production source files for
public GitHub publication. It is **not** a production deployment directory.

## Privacy boundary

Per `docs/ISSUE25_PUBLIC_SOURCE_HANDOFF.md`:

- Native broker statements, original receipts, account/owner IDs, market
  evidence, checkpoints and order ledgers stay in the root-private
  production evidence archive — **never** in the public repository.
- Each sanitized executable and systemd unit is committed as a readable
  text file with tests.
- Hard-coded runtime connection-profile IDs are replaced with authenticated
  read-only CRM lookups; broker/account identity is pinned independently.
- No production credentials or original customer trade rows are copied.

## Transfer procedure

1. **New branch from `main`** — create a dedicated branch for the
   sanitized source transfer. Do not mix with unrelated changes.

2. **Sanitize each file** before committing:
   - Replace hardcoded 32-char connection profile IDs with `_resolve_accounts()`
     or equivalent CRM read-only lookup.
   - Replace hardcoded broker logins with environment variables or CRM lookup.
   - Remove any original customer trade rows, broker statements, or
     private evidence references.
   - Ensure all files are plain text (no binary archives).

3. **Add tests** for:
   - Broker-native ticket identity and distinct economically identical positions
   - Stable reconnect-independent deduplication
   - Partial-exit lifecycle rejection
   - True pre-entry completed M5
   - Source SHA/revision fences
   - Per-owner isolation
   - Duplicate-learning retries

4. **Validate before PR:**
   - Run `python3 tools/ci/verify_public_repo_artifacts.py` — must PASS.
   - Run `python3 -m pytest tests/test_sanitized_source_contract.py -v` — must pass.
   - Run the full test suite.
   - Review the final diff for any private identifiers.

5. **Open PR** — the `abutron-public-artifact-guard` GitHub Action scans
   `git ls-files` and rejects host-only evidence paths or formats.

## Staged files (Ubuntu host)

The reviewer staged **28 reviewable text files** at
`/tmp/abutron-issue25-reviewable-public-source-20261118` on the authorized
Ubuntu host:

| Category | Count | Notes |
|---|---|---|
| V9 Python source scripts | 10 | Includes production 8-account archive and all-owner causal trainer |
| Systemd unit/timer files | 14 | Production service definitions |
| Source SHA manifest | 1 | SHA-256 of each file for verification |
| README | 1 | Transfer documentation |
| Identity/privacy test suite | 1 | Tests for sanitized source contract |
| Intended CI workflow | 1 | GitHub Actions workflow for sanitized source |

**These files have NOT been transferred to GitHub yet.** The transfer
requires an approved, audited source transfer with secret review.

## Validation checklist

Before committing sanitized files, verify each:

- [ ] No 32-char hex connection profile IDs
- [ ] No 7-12 digit broker login numbers (except in CRM lookup context)
- [ ] No private key or API credential signatures
- [ ] No references to host evidence files (.sqlite3, .xlsx, .tgz, etc.)
- [ ] All files are plain text
- [ ] `verify_public_repo_artifacts.py` passes
- [ ] `test_sanitized_source_contract.py` passes
- [ ] No LIVE account identifiers
- [ ] Read-only monitoring preserved (no CRM updates, broker orders, model promotion)

## Current status

| Item | Status |
|---|---|
| Privacy guard CI | ✅ Merged (PR #27) |
| `.gitignore` host-only patterns | ✅ Updated |
| `native_market_v3_status_ro.py` sanitization | ✅ Committed |
| CRM fallback repair | ✅ Merged (PR #28) |
| Sanitized source contract tests | ✅ 12 tests passing |
| 28-file transfer | ⏳ Pending Ubuntu host transfer |
| Replacement source PR | ⏳ Blocked pending transfer |
| Production-model promotion | Not performed |
| Broker execution/exposure repairs | Separate outstanding work (#12, #22) |
