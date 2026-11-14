# ABUTRON V9 NexusBrain → APEX 1.1 integration checkpoint
Verified: 2026-11-14 UTC (Ubuntu service inspection and tests)

## Scope implemented
- `src/abutron/brain/apex_authority.py`: exclusive V9 NexusBrain decision adapter, per-account/symbol M5 feature history, model artifact validation, fail-safe no-candidate outcomes for missing/invalid models.
- `src/abutron/apex_integration.py`: APEX bridge remains candle/manifest validator and context engine; the V9 brain alone selects BUY/SELL/HOLD; candidate identity contains the model digest and side; original APEX vote is recorded only as `apex_context_status`.
- APEX candidate review remains separate, with broker-native risk and CRM/writer attestation. No unconditional order submission has been introduced.
- `src/abutron/main.py` and `src/abutron/portal/app.py` expose `signal_authority` and `brain_model_ready` at `/api/health`.
- Tests in `tests/test_apex_integration.py` and `tests/test_nexus_apex_authority.py`.

## Verified result
- Full V9 suite: 156 passed, 3 warnings (pytest configuration warnings).
- V9 service `abutron-v9-hermes.service` active/running after restart; `http://127.0.0.1:19843/api/health` HTTP 200.
- Health currently reports `signal_authority=V9_NEXUS_V1`, `brain_model_ready=false`.
- Isolated V9 database has **zero linked broker accounts**.
- No persisted model `/var/lib/abutron-fx-v9-hermes/data/brain/nexus_brain.json` found.
- No native broker orders submitted or verified by this change. Existing production APEX order timer and broker control stack were not modified.
- Existing APEX candidate review in vendored source uses nominal **0.10%/trade** and **20% portfolio** limits, DEMO-only. This must be reconciled with current approved account-specific risk policy before production cutover.

## Remaining blockers
1. Obtain genuine broker-native historical candles and prepare a time-ordered, reproducible walk-forward train/validation split.
2. Save a calibrated model artifact with training metrics; verify trading performance, cost sensitivity, and OOS drift independently. A format-valid training report is NOT evidence of profitable trading.
3. Validate V9 model direction versus APEX and broker fills on DEMO accounts. Keep APEX canonical single-writer, verified SL/TP, CRM permissions and 20% portfolio limit.
4. Connect the V9 candidate stream to the actual production APEX native execution routing only after adapter compatibility and broker-native end-to-end evidence are proven.
5. Legacy production blocker `BLOCKED_INCOMPLETE_EXPOSURE` and account/market-data health are separate issues. Do not relink retired Deriv LIVE 0178 or FP Markets 3492 without new authorization.

## Restore
Pre-change originals: `/var/backups/20261114T2215Z-v9-nexus-authority/`
- `apex_integration.py`, `test_apex_integration.py`, `main.py`, `portal_app.py`.
- Restore the four files to their corresponding repo paths, remove the new `src/abutron/brain/apex_authority.py` module, and restart `abutron-v9-hermes.service`.
- Before restoring, verify no further changes were made in those files; do not blindly overwrite new work.
