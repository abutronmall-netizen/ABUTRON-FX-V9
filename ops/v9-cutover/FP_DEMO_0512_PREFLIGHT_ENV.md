# FP Markets DEMO 0512 — Verified Preflight Environment (Source Only)

**Date:** 20 November 2026. **Mode:** preflight (shadow decision; no order dispatch). **Scope:** account 0512 only. **Production state:** NOT READY. The account-bound V9 writer, FP-native execution-cost report and V9 data root are not yet installed. Running the preflight as though they exist would be misleading. LIVE 9392 remains OFF.

## Exact source and environment bindings

The following path is a **verified offline source checkout**, not the currently installed production runner:

```bash
SOURCE=/opt/abutron-v9-migration-qa-20261120/v9-fp-risk-units-b6104c1
QA=/var/lib/private/abutron-pr37-direct-writer-fp-demo0512-qa
DATA=/var/lib/abutron-v9-production-fp-0512
COST=/var/lib/abutron-fp-v4-0512/v4-native-execution-cost-0512.json
OWNER=/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json
```

The QA/data/cost paths are **planned destinations, not installed components**. Do not create fake cost reports, bypass the existing FP 0512 order bridge, or install a second order-capable writer before an explicitly authorized one-writer cutover.

## Read-only prerequisite checks

These commands do not update server files or services:

```bash
sudo test -f "$SOURCE/src/abutron/agent/production_fp_demo.py" || echo "MISSING: FP V9 runner"
sudo test -f "$SOURCE/data/brains/unified_m5/nexus_brain.json" || echo "MISSING: V9 model"
sudo test -f "$QA/terminal/MQL5/Experts/ABUTRON/ApexNativeWriter.ex5" || echo "MISSING: FP 0512 EA"
sudo test -f "$QA/terminal/MQL5/Presets/PR37WriterExposureOnly.set" || echo "MISSING: account-bound FP 0512 preset"
sudo test -d "$DATA" || echo "MISSING: 0512 V9 data root"
sudo test -s "$COST" || echo "MISSING: genuine 0512 FP broker cost report"
sudo test -s "$OWNER" || echo "MISSING/STALENESS: owner risk report"
```

Also confirm the dedicated terminal is actually authenticated to FP Markets **DEMO 7410512** via its current CRM account reference/server and native signed exports. Do not print or publish the activation seed, secret or account identifiers. The intended EX5 is the verified push-CI build with SHA-256:

`b6b63afeb7c4939a2bea1f40cfad58d8288af4009d8dc4e2534d7196162b91f3`

The isolated source checkout contains the expected V9 model SHA-256:
`aa7ba415c5aeb4ff745670632323b203c578e198ee51b15e746577dee09e1d5c`.

## Correct preflight invocation — **NOT AUTHORIZED TO RUN UNTIL PREREQUISITES EXIST**

Running this produces `$DATA/status.json`, even in preflight mode. It is **not purely read-only** and should be executed only within an expressly authorized 0512 staging/deployment action:

```bash
sudo env \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$SOURCE/src" \
  ABUTRON_FP_QA_ROOT="$QA" \
  ABUTRON_FP_DATA_ROOT="$DATA" \
  ABUTRON_DATA_ROOT="$DATA" \
  ABUTRON_FP_OWNER_REPORT="$OWNER" \
  ABUTRON_FP_COST_REPORT="$COST" \
  ABUTRON_FP_PER_TRADE_PCT=0.10 \
  ABUTRON_FP_PORTFOLIO_CAP_PCT=20.0 \
  ABUTRON_PORTFOLIO_CAP_PCT=0.20 \
  /opt/abutron-v9-brain/.venv-a88f373/bin/python -B \
  -m abutron.agent.production_fp_demo \
  --account 0512 --mode preflight
```

**Parameter semantics (verified from actual code):**

| Setting | Value | Meaning |
|---|---|---|
| `ABUTRON_FP_PER_TRADE_PCT` | `0.10` | 0.10 percentage points; the V9 RiskGate fraction becomes `0.001`; the EA should receive `InpRiskFraction=0.1` |
| `InpRiskFractionPct` | `10` | MQL5 **integer** display value in basis points; 10 = 0.10% |
| `ABUTRON_FP_PORTFOLIO_CAP_PCT` | `20.0` | Runner expects percentage points and divides by 100 to obtain `0.20` portfolio risk fraction |
| `ABUTRON_PORTFOLIO_CAP_PCT` | `0.20` | CRM account snapshot uses the `0.20` fraction; this is **not** a substitute for the separately authorized 20% portfolio *drawdown* control |
| `ABUTRON_FP_COST_REPORT` | `$COST` | Current, authenticated FP account-specific execution costs, not a handwritten certification |
| `ABUTRON_DATA_ROOT` | `$DATA` | Single-account status/events root, not the existing Deriv default |

`ABUTRON_FP_PORTFOLIO_CAP_PCT=0.20` would be wrong: in the old source it became `0.002`, a 0.2% fraction, not 20%.

**Verification:** require exit code 0, `status=BRAIN_DECISION_EVALUATED_EXECUTION_NOT_INVOKED`, fresh 0512 `status.json`, real nonzero native bars/quotes, no new orders, matching authenticated FP broker and symbol mappings. A shadow/preflight success is not a real broker ACK, SL/TP or trailing-stop proof.

Do **not** run `enable_trading_fp_demo.py --account 0512` until the correct account-bound trading writer, native cost evidence, one-writer cutover and new release are verified and separately authorized. Omit `--account` only if all four DEMO accounts were individually authorized for one command; the default targets all four.

LIVE FP 9392, Deriv 6207/7593/9244, original FP feed and existing FP 0512 router must stay unchanged during these preparatory checks.

