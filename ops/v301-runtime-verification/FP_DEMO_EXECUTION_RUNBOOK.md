# FP DEMO V3.01 Execution Runbook (0512-first)

**Purpose:** Canonical procedure for a controlled V3.01 deployment to FP
DEMO feed charts, account 0512 first, then 1636. Includes full rollback
steps. This runbook documents the procedure executed on 21 Nov 2026 and
is the reference for any repeat rollout.

**Scope:** DEMO feed charts only. Feed-only / persistence-only pattern.
Order bridges, history, trading controls and LIVE 9392 are never touched.

## 0. Pre-deployment checks (both accounts)

Run before touching any binary:

1. **Confirm account identity and current state:**
   - 0512: FP Markets DEMO 7410512
   - 1636: FP Markets DEMO 7411636
   - Record each terminal's PID, chart list, and current binary hashes.
2. **Confirm LIVE 9392 is separate** — note its PID; it must not be
   restarted or modified.
3. **Record pre-deployment hashes** of:
   - `MQL5/Indicators/ABUTRON/ApexMarketFeedV3.ex5`
   - `MQL5/Experts/ABUTRON/AbutronFPMarketsNativeFeedParallelV305.ex5`
   - Chart01 config, order-bridge EA, V101 history binary
4. **Create the backup directory:**
   ```bash
   mkdir -p /var/backups/abutron-fp-v301-<TIMESTAMP>/
   ```
5. **Verify the target binary pins** (must match exactly):
   | Binary | SHA-256 |
   |---|---|
   | ApexMarketFeedV3.ex5 (V3.01) | `321bc0d80b783f308a23b9823e4c3da95b138ea95c2e0f595a826fdd3c6f3d29` |
   | AbutronFPMarketsNativeFeedParallelV305.ex5 | `e7e67f13a4b2d6969487c6901118247acce7b499d40f7bc77ce5b94ed8783907` |

## 1. Backup (before any change)

For **each** account, back up into the backup directory:

```bash
BACKUP=/var/backups/abutron-fp-v301-<TIMESTAMP>/<account>
mkdir -p "$BACKUP"

# Binaries
cp <terminal>/MQL5/Indicators/ABUTRON/ApexMarketFeedV3.ex5 "$BACKUP/"
cp <terminal>/MQL5/Experts/ABUTRON/AbutronFPMarketsNativeFeedParallelV305.ex5 "$BACKUP/"

# Chart config + order bridge + history
cp <terminal>/MQL5/Experts/ABUTRON/*.ex5 "$BACKUP/" 2>/dev/null
cp <terminal>/MQL5/Presets/*.chr "$BACKUP/" 2>/dev/null
cp <terminal>/MQL5/Files/ABUTRON_V9_NATIVE/*.jsonl "$BACKUP/" 2>/dev/null

# Record hashes
sha256sum "$BACKUP"/* > "$BACKUP/pre-deploy-hashes.txt"
```

## 2. Deploy 0512 (first account)

1. **Stop** the 0512 V305 feed chart process.
2. **Stage the V3.01 binaries:**
   - `ApexMarketFeedV3.ex5` → `<terminal>/MQL5/Indicators/ABUTRON/`
   - `AbutronFPMarketsNativeFeedParallelV305.ex5` → `<terminal>/MQL5/Experts/ABUTRON/`
3. **Verify staged hashes** match the pins in section 0.5. If any mismatch,
   **stop** — do not proceed.
4. **Start** the V305 feed chart. The auth startup expert is preserved;
   V3 attaches programmatically via `EnsureV3Indicator()` after broker
   auth.
5. **Confirm** the terminal log shows:
   - `ABUTRON_FP_V3_INDICATOR_ATTACHED_AFTER_BROKER_AUTH`
   - `Indicators\custom indicator ApexMarketFeedV3 (<symbol>,<tf>) loaded succesfully`
6. **Confirm** authentication: `CONNECTED`, heartbeat fresh.
7. **Confirm** trading controls unchanged (desired ON / effective OFF).
8. **Confirm** order bridge (V101) and Chart01 still loaded.

## 3. Verify 0512

1. **Wait** for streaming (or use the existing DEMO session). Allow ≥5
   minutes per symbol.
2. **Run the verifier** against the produced files:
   ```bash
   python3 scripts/verify_fp_v301_runtime.py \
       --data-dir <terminal>/MQL5/Files/ABUTRON_V9_NATIVE \
       --day <YYYYMMDD> --json
   ```
3. **Collect evidence:**
   - Verifier output (text or JSON)
   - Terminal log excerpt (attach attestation lines)
   - `sha256sum` of both staged binaries
   - Record counts per symbol
4. **Assess:**
   - **PASS:** all symbols with QUOTE records pass all assertions;
     symbols with STATUS-only are recorded as broker-data-pending, not
     failures.
   - **FAIL:** any assertion error → diagnose before proceeding to 1636.
     Do **not** proceed to 1636 with 0512 in a broken state.

## 4. Deploy 1636 (second account)

Repeat section 2 for 1636, using the same binaries and pins. Back up
1636's prior state into the same backup directory before changes.

## 5. Verify 1636

Repeat section 3 for 1636.

## 6. Post-deployment audit (both accounts)

Confirm for **each** account:

- [ ] Terminal `CONNECTED`, heartbeat fresh
- [ ] MT5 V3 indicator-load attestation in terminal log
- [ ] 13/13 native files present (STATUS records for all)
- [ ] Symbols with QUOTE records verified by the collector
- [ ] Trading controls unchanged (desired ON / effective OFF)
- [ ] Order bridge (V101) and Chart01 hashes match pre-deployment
- [ ] LIVE 9392 not restarted, PID unchanged, still OFF/OFF
- [ ] Collector installed and timer active

Save the aggregated evidence to:
```bash
/var/backups/abutron-fp-v301-<TIMESTAMP>/post-verification.json
```

## 7. Rollback (if any assertion fails or the terminal misbehaves)

Rollback is binary restoration only — no config or control changes.

1. **Stop** the V305 feed chart process for the affected account.
2. **Restore the prior binaries** from the backup:
   ```bash
   BACKUP=/var/backups/abutron-fp-v301-<TIMESTAMP>/<account>
   cp "$BACKUP/ApexMarketFeedV3.ex5" \
      <terminal>/MQL5/Indicators/ABUTRON/
   cp "$BACKUP/AbutronFPMarketsNativeFeedParallelV305.ex5" \
      <terminal>/MQL5/Experts/ABUTRON/
   ```
3. **Verify** the restored hashes match `pre-deploy-hashes.txt`.
4. **Restart** the V305 feed chart.
5. **Confirm** the terminal returns to the pre-deployment state:
   CONNECTED, attestation, controls, order bridge all as before.
6. **Do not** roll back the other account unless it is also affected.

## 8. Rechecking broker-data gaps (time-gated, no deployment)

Some symbols may have STATUS-only output because the broker does not
stream them during the current session (e.g. weekend or restricted
instrument sessions). This is **not** a rollback condition.

When the broker session for those symbols resumes:

1. Let the terminal stream for the new day.
2. Re-run the verifier against the new day's files:
   ```bash
   python3 scripts/verify_fp_v301_runtime.py \
       --data-dir <terminal>/MQL5/Files/ABUTRON_V9_NATIVE \
       --day <NEW_YYYYMMDD>
   ```
3. If the previously-missing symbols now produce QUOTE records, the
   verification closes. No binary or control change is needed.

## 9. Explicit exclusions (never touch)

- LIVE 9392 (separate account, separate PID)
- Trading switches / risk settings
- Order bridges / V101 / Chart01 (read-only confirmation only)
- V9 writer / order-capable charts
- Market-print and DOM generation (broker-dependent; absence is legitimate)

## 10. Related files

- `FP_DEMO_VERIFICATION_PROTOCOL.md` — assertions and evidence format
- `scripts/verify_fp_v301_runtime.py` — per-symbol file verifier
- `scripts/native_market_v3_status_ro.py` — read-only collector (SHA pinned to V3.01)
- `mql5/ApexMarketFeedV3.mq5` — V3.01 indicator source
- `mql5/AbutronFPMarketsNativeFeedParallelV305.mq5` — FP feed EA
