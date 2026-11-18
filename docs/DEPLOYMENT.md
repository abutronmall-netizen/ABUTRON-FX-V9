# ABUTRON FX V9 — Install, Deploy & Live Operations

This document covers installing ABUTRON FX V9 on an Ubuntu host, deploying
the **agentic decision engine as the live authority (overriding apex)**,
connecting a DEMO broker, and operating the dispatcher behind the
RiskGate.

The agentic multi-agent system is the **primary operating layer** on V9.
The vendored apex engine is **legacy** — the agentic engine overrides it
as the decision authority. The dispatcher is enabled in live mode; the
**RiskGate + exposure guard remain** as the unbypassable money-safety
floor (0.15%/trade, 20% portfolio ceiling, R:R ≥ 1.50, exposure
certification). Fail-closed is the build/test posture, not the permanent
operating mode — this guide deploys the agentic system live.

---

## 1. Prerequisites

- Ubuntu 24.04 LTS host (x86_64) with `sudo`.
- Wine 64-bit (for the MT5 terminal).
- Python 3.11+.
- A broker DEMO account with MT5 terminal access (FP Markets or Deriv).
- The trained unified brain checkpoint (`data/brains/unified_m5/nexus_brain.json`,
  26-symbol universe, val_accuracy ≈ 0.55). Ships in the repo; retrain
  with `scripts/train_unified.py` if you retrain.

## 2. Install

The repo ships an installer that sets up Wine + MT5, a Python venv,
data directories and systemd units. It never embeds broker credentials.

```bash
git clone <repo-url> ABUTRON-FX-V9
cd ABUTRON-FX-V9

# Optional: link training datasets (market-data + trade-history).
# Datasets are git-ignored; the checked-in brain works without them.
bash scripts/link_all.sh

# Install system packages, venv, deps, data dirs, systemd units.
sudo bash deploy/scripts/install.sh
```

The installer:
- installs `wine64 wine32 wine-binfmt` + Python,
- creates `/opt/abutron-fx-v9` (app) and `/var/lib/abutron-fx-v9/data` (data),
- installs Python deps from `requirements.txt` into a venv,
- copies the systemd units and enables the portal + dispatcher +
  reconcile timer + health timer.

Verify the install:

```bash
PYTHONPATH=src /opt/abutron-fx-v9/.venv/bin/python -m compileall -q src/abutron
PYTHONPATH=src /opt/abutron-fx-v9/.venv/bin/python -m pytest tests/test_agent_system tests/test_agentic_integration -q
```

## 3. Deploy — override apex with the agentic engine

The agentic engine replaces the vendored apex engine as the decision
authority in `ApexIntegration.process_candle`. This is controlled by one
environment flag.

### Activate the agentic decision path (override apex)

```bash
# In the systemd unit (or .env): activate the agentic engine.
export ABUTRON_AGENTIC_ENGINE=true
```

When `ABUTRON_AGENTIC_ENGINE=true`:
- `ApexIntegration` builds the `AgenticDecisionEngine` (sense → think →
  gate → act → monitor → recover) using the loaded unified brain as the
  signal authority.
- `process_candle()` routes through `_run_agentic_decision()` **before**
  the apex availability check — the agentic engine does **not** need the
  vendored apex source at all. Apex is bypassed.
- The `RiskGate` (which delegates to the V9 `RiskGuard`) enforces the
  hard invariants before any candidate is produced.

To apply the flag to the running services, add it to the environment of
the portal + dispatcher units, then reload:

```bash
# Add to /etc/systemd/system/abutron-portal.service and
# /etc/systemd/system/abutron-dispatch.service under [Service]:
#   Environment=ABUTRON_AGENTIC_ENGINE=true
sudo systemctl daemon-reload
sudo systemctl restart abutron-portal.service abutron-dispatch.service
```

Or set it in `/opt/abutron-fx-v9/.env` (read by `python-dotenv`) and
restart. The flag **defaults to `false`** (shadow/staging) in the repo;
setting it to `true` is the deployment action that overrides apex and
deploys the agentic system live.

### Other deployment switches

| Variable | Default | Purpose |
|---|---|---|
| `ABUTRON_AGENTIC_ENGINE` | `false` | **Deploy switch**: `true` activates the agentic decision path (overrides apex). |
| `ABUTRON_ENV` | `demo` | `demo` or `live`. |
| `ABUTRON_LIVE_TRADING_ENABLED` | `true` | Dispatcher trades once a broker session is CONNECTED. |
| `ABUTRON_RISKGUARD_ENABLED` | `false` | Independent hard-limit `RiskGuard` on every READY candidate. Enable for production. |
| `ABUTRON_RISK_CAP_PCT` | `0.20` | Portfolio ceiling (20%). |
| `ABUTRON_PER_TRADE_PCT` | `0.0015` | Per-trade risk (0.15%). |
| `ABUTRON_GEMMA_ENABLED` | `false` | Optional LLM advisory (never on the order path). |
| `ABUTRON_DEBATE_ENABLED` | `false` | Optional multi-agent debate layer. |

> **No staging/certification veto.** Setting `ABUTRON_AGENTIC_ENGINE=true`
> deploys the agentic system as the live decision authority — there is no
> certification gate that holds it in shadow. The staging/certification
> harnesses (`scripts/validate_live_staging.py`,
> `scripts/validate_live_certification.py`) are **validation tools**, not
> runtime gates; once certification has passed, setting the flag deploys
> live immediately.

## 4. Connect the DEMO broker

A broker connection requires four things: **MT5 terminal, connection ID,
account (MT5 login), and equity.** Register the account through the CRM,
then launch the per-account MT5 worker.

### 4.1 Register the account (portal / CRM)

```python
from abutron.crm.controls import crm

crm.register_account(
    connection_id="<your-connection-id>",   # unique, e.g. fp-demo-001
    broker="fpmarkets",                      # or "deriv"
    mt5_login=<MT5_LOGIN>,                   # the DEMO account login number
    owner_id="<owner-id>",
    environment="demo",
    server="synthetic-demo-server",          # Deriv: "Deriv-Demo"
    symbols=["EURUSD.r", "GBPUSD.r", "XAUUSD.r"],
)
```

This creates the account with `desired_on=0, effective_on=0` (not yet
trading). Connection ID, MT5 login, broker, and symbols are the **DEMO
broker connection details** you supply from your broker.

### 4.2 Bind the EA pin + activation revision

```python
crm.link_broker_identity(
    "<connection-id>",
    ea_sha256="<sha256-of-compiled-ApexNativeWriter.ex5>",
    server="synthetic-demo-server",
)
```

The compiled MQL5 EA (`mql5/ApexNativeWriter.mq5`) is the single-writer
native order transport. Its SHA256 pin is verified before any order is
sent.

### 4.3 Launch the per-account MT5 worker

The MT5 worker supervises the Wine/MT5 terminal for one account and keeps
native exports flowing. It does **not** place orders (the EA is the single
writer); it only feeds data.

```bash
sudo systemctl start abutron-mt5-worker@<connection-id>
```

The worker reads `ABUTRON_CONNECTION_ID` from the unit and launches the
MT5 terminal with the account's broker/login/server. It writes native
exports to `data/native/<connection-id>/`.

### 4.4 Certify exposure (broker worker sets equity + native_positions_verified)

The exposure guard (`RiskEngine.aggregate_owner_risk`) requires the
account to carry **broker-native evidence**: `equity > 0`, `account_currency
== "USD"`, and `native_positions_verified`. Until these are set, the
dispatcher blocks all RESERVED candidates with `BLOCKED_INCOMPLETE_EXPOSURE`
(fail-closed). The broker worker sets them after a reconciled native
positions snapshot + MT5 balance are verified:

```python
from abutron.crm.controls import crm

crm.certify_exposure(
    "<connection-id>",
    equity=<account-equity-usd>,   # from the MT5 balance export
    account_currency="USD",
    native_positions_verified=True,
)
```

This stores `equity`, `account_currency`, and `native_positions_verified`
on the account. The exposure guard then certifies the owner (aggregate
stop-risk ≤ 20% ceiling) and the dispatcher proceeds. **This is the gate
that blocks unverified exposure** — a broker worker (or the portal after
reconciliation) must set it from real broker evidence.

### 4.5 Turn the account ON

```python
# Set desired_on + effective_on once the broker session is CONNECTED and
# exposure is certified. effective_on requires a fresh native ACK bound to
# the current activation revision.
acct = crm.engine.get_account("<connection-id>")
acct["desired_on"] = 1
acct["effective_on"] = 1
acct["conn_status"] = "CONNECTED"
crm.engine.upsert_account(acct)
```

The dispatcher and engine tick only act on accounts with
`effective_on and desired_on`.

## 5. Run the services

```bash
sudo systemctl start abutron-portal.service       # portal + engine tick loop
sudo systemctl start abutron-dispatch.service      # drains RESERVED candidates
sudo systemctl start abutron-mt5-worker@<connection-id>
sudo systemctl enable abutron-reconcile.timer abutron-health.timer
```

- **`abutron-portal.service`** (`abutron.main`): runs the portal + the
  engine tick loop. Each tick reads native exports, feeds closed M5
  candles through `process_candle` (agentic decision), stores READY
  candidates, and dispatches them.
- **`abutron-dispatch.service`** (`abutron.workers.dispatcher_worker`):
  drains RESERVED candidates to native orders through the single-writer
  gateway, records ACK/UNKNOWN/REJECTED receipts.
- **`abutron-mt5-worker@<cid>`**: supervises the MT5 terminal + native
  exports.
- **`abutron-reconcile.timer`** (every 15 min): reconciles receipts
  against broker-native history, writes ledger rows, feeds the brain.

The portal is at `http://<host>:19743/`.

## 6. The live cycle (what happens per closed candle)

```
MT5 native export (certified feed)
  -> ApexRuntime.engine_tick
  -> ApexIntegration.process_candle  (AGENTIC decision engine — overrides apex)
      -> AgentOrchestrator: sense (brain) -> think (reasoning) -> gate (RiskGate)
      -> gate-approved Candidate (RiskGuard: 0.15%/trade, 20% ceiling, R:R>=1.50)
  -> store_ready_candidate  (RESERVED, at-most-once via replay_key)
  -> NativeDispatcher.dispatch_account
      -> owner exposure guard (aggregate_owner_risk must be certified)
      -> SingleWriterGateway.send_order  (HMAC-signed, EA SHA256-pin verified)
      -> MQL5 EA places the native order -> ACK / UNKNOWN / REJECTED
  -> ReceiptManager  (native receipts: DB + files + audit event log)
  -> Reconciler.reconcile_account  (match receipts vs native history -> ledger)
```

The **RiskGate remains unbypassable** behind the agentic engine. The brain
predicts direction; the RiskGate + exposure guard validate money-safety.
Agents and workers stay deterministic and auditable.

## 7. Verify the deployment

Run the certification harness against the live config (it exercises the
real `ApexRuntime` with a simulated native EA):

```bash
PYTHONPATH=src python3 scripts/validate_live_certification.py
```

17/17 checks confirm: agentic authority, broker-worker exposure
certification, engine tick, READY→RESERVED, dispatch ACK, receipt,
reconciliation → ledger, candidate lifecycle, audit trail, and fail-closed
safety gates.

## 8. Rollback

To roll back to the apex decision path, set `ABUTRON_AGENTIC_ENGINE=false`
and restart the portal + dispatcher. The vendored apex engine (legacy)
takes over the decision path. All candidate/dispatch/receipt contracts are
identical, so no downstream changes are needed.

## 9. Safety invariants (never bypassed)

These are enforced regardless of the deployment mode:

- **Per-trade risk**: 0.15% of equity (`ABUTRON_PER_TRADE_PCT`).
- **Portfolio ceiling**: 20% aggregate stop-risk (`ABUTRON_RISK_CAP_PCT`).
- **Reward:risk floor**: R:R ≥ 1.50.
- **Max position**: 50 lots.
- **Exposure certification**: the dispatcher blocks until the broker
  worker sets `native_positions_verified` + equity from broker-native
  evidence.
- **At-most-once**: the replay_key unique constraint prevents duplicate
  order submission for the same candle.
- **Fail-closed**: no candidate when the gate rejects or the brain signal
  is weak; UNKNOWN receipts block resubmission until reconciliation.
