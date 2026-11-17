# ABUTRON V9 — Automatic trade learning and hourly-loss research (17 Nov 2026)

## Verified live host state
- Ubuntu remote host inspected 2026-11-17 12:28 UTC (14:28 SAST).
- `abutron-v9-realtrade-candidate.timer` triggers broker-verified FP DEMO causal candidate training every 5 minutes. Candidate only; **NO model promotion** and no orders.
- `abutron-v9-paper-candidate.timer` triggers isolated paper candidate training daily at 00:30 UTC (02:30 SAST). Candidate only.
- Existing pinned V9 model had 582 saved loss signatures; isolated realtrade candidate had 609. **These counts do not establish effectiveness.**
- Verified FP DEMO native selected windows: 503 paired closed positions, of which 143 exited after pinned training cutoff; 56 new events trained cumulatively and 86 not aligned to 30 pre-entry completed M5 bars (numbers at inspection, moving inputs).
- Root filesystem usage 95% / 17 GiB remaining, so avoid large unbounded archives.

## New hour-loss research
`ops/session_loss_research.py` is an account-bound, **read-only** report generator.
It reuses the current production root-owned native FP V101 receipt verifier
(`/usr/local/sbin/abutron-v9-broker-closed-feedback-ro.py`), rather than
trusting arbitrary CSVs or dashboard status. It evaluates **entry hour** in
`Africa/Johannesburg` (UTC+2), not exit hour or broker-server-local hour.

Hours are grouped by FP DEMO account; setup diagnostics are further split by
account, symbol and side. The first 70% of distinct chronological entry dates
form training history; the remaining dates are held out. A losing-hour
**shadow candidate** requires >=12 training and >=6 validation trades, >=2
distinct dates on each side, negative average **net USD** PnL in both sets,
and a win rate <50% in both. This is an exploratory threshold, not a
certification of real-money profitability. Account-level and setup-level
counts remain separate. No loss pattern is blocked solely because it lost
once.

**Verified first report, 2026-11-17:** 503 authenticated closed trades,
45 account/hour groups, 33 setup/hour diagnostics, **one** hour flagged:
FP DEMO 0512, **19:00–19:59 SAST** (16 train + 6 validation trades).
This is not a broker-acknowledged execution veto.

Output: `/var/lib/abutron-v9-session-loss/latest.json`
(schema `abutron.v9.broker_hour_loss_shadow.v1`, mode 0600, root-owned).

Service: `abutron-v9-session-loss-research-ro.service`
Timer: `abutron-v9-session-loss-research-ro.timer`
Runs every five minutes, verified service exit 0 and timer enabled/active.
Script installed at `/usr/local/lib/abutron-v9-realtrade-feedback/session_loss_research.py`,
SHA256 `b9376bcff337e2ac36383edfd6eb59e9d8547b6187c1ead80f217d84195d9aaf`.
6 focused unit tests passed on Ubuntu. Output includes
`production_veto_enabled=false`, `model_promotion_allowed=false`,
`broker_orders=0`.

### Deploy/rollback
- Deploy only to the authenticated FP DEMO host, as root, preserving restrictive
  service privileges and root-only output directory.
- Verify: `systemctl show abutron-v9-session-loss-research-ro.service -p Result -p ExecMainStatus`;
  `systemctl is-active abutron-v9-session-loss-research-ro.timer`.
- Rollback: `systemctl disable --now abutron-v9-session-loss-research-ro.timer`.
  Existing brain model, writers, CRM, risk and activation do not depend on
  this report. Never silently modify LIVE activation.

### Outstanding
- Before routing hourly candidates into BUY/SELL/HOLD, evaluate expectancy
  after costs, volatility/regime drift, hour-selection multiple testing,
  risk-normalized R multiples, and forward shadow behavior.
- Extend broker-verified closed-trade learning to Deriv using an independent
  account-verified native feedback source. This report currently covers
  FP Markets DEMO 0512 and 1636 only.
- Fix the missing full pre-entry M5 windows so currently excluded verified
  closed trades can actually train the 49-feature neural candidate.
- No autonomous production model promotion or LIVE learning-to-execution
  has been verified; PR #19 and native owner-exposure proof remain open.
