# FP Markets DEMO — Issue #21 read-only rotating receipt auditor

This is an **observability and independent receipt-coverage companion**, not a native MT5 order bridge. It does not create ACKs or EA position-evidence files and does not submit orders, alter risk, or restart a terminal.

## Why it exists

Native account receipts can exceed 250 entries. The historical order-bridge EA scanned only the first 250 and consequently starved later evidence. The corrected MQL5 candidate resides in PR #17 but was **not hot-attached** to the production MT5 chart at the 2026-11-17 verification checkpoint.

This Python companion enumerates all receipt names, verifies positional 10-line immutable receipt schema and account/session binding, examines up to 250 per run with a persisted modulo cursor, and checks 19-line native position evidence. It reads the **separate** broker-native reconciliation record to distinguish broker-verified ACKs from missing EA evidence. It never fills missing evidence using synthetic records.

## Private configuration (never commit real values)

In a root-only `/etc/abutron/fp-issue21-auditor.env` (mode 0600), configure:

```sh
ABUTRON_ISSUE21_CONNECTION_ID=<32-lowercase-hex-connection-id>
ABUTRON_ISSUE21_ACCOUNT_LOGIN=<broker-demo-login-digits>
ABUTRON_ISSUE21_BROKER_SERVER=FPMarketsSC-Demo
```

The Python auditor uses the existing worker, broker history and reconciliation paths of the FP DEMO account. Install the script as `/usr/local/lib/abutron-fp0512-issue21-scanner/audit.py` and use the paired sample service and timer. No trading command is in this program. Keep account IDs, login references, broker credentials, session nonces and actual receipt contents out of this public repository.

## Verified production checkpoint (2026-11-17)

An account-bound private-configured version of the same rotating algorithm was installed and enabled on Ubuntu at 16:56 UTC under `abutron-fp0512-issue21-receipt-audit.timer`. Consecutive systemd runs processed all **279 of 279** actual native receipts in two 250-file batches, independently observed **215 broker-verified ACKs**, and reported **one missing native EA evidence file** without generating it. MT5 session nonce, production V114 EA, CRM controls, and LIVE account settings remained unchanged.

The source here is a **generic account-configured portable projection**; it is not byte-identical to the already running account-bound script. It passed source redaction checks; the on-server deployed build passed its full account-native replay. Do not treat this code or timer as proof the corrected V115 MQL5 binary is chart-attached.

## Still outstanding for Issue #21

Deploy and attach the corrected native V115 EA to the single authorized DEMO chart without losing session-locked historical receipts, or implement a separately verified historical ledger for rotated sessions. Only an actual chart attachment, new EA-created `ABUTRON_FP/scan_cursor.txt` and native broker evidence for the remaining ACK close the original EA scanner defect. Retain the 20% owner portfolio risk ceiling and separate LIVE activation.

Check runtime with `systemctl list-timers --all` and read `/var/lib/abutron-fp0512-issue21-scanner/state.json`. The field `cycle_missing_native_evidence_count` intentionally remains nonzero until authentic EA evidence arrives; broker reconciliation cannot be substituted for EA-generated evidence.
