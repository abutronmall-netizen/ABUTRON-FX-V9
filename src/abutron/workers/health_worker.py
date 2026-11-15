"""Health worker — periodic operations health snapshot.

Runs as `abutron-health.service` (timer-driven every 30 min). Walks every
registered account, writes a health snapshot event into the chained audit
journal, and exits non-zero when any account reports a non-HEALTHY overall
status so systemd records the failure.

This is the operator-observability counterpart of the portal's
`GET /api/command/health`: the portal answers on demand, the timer leaves a
durable trail in the event log even when nobody is looking at the UI.
"""
from __future__ import annotations

import os
import sys

from ..config import DATA_ROOT
from ..db import engine_db
from ..events import EventLog, make_event
from ..ops.health import HealthMonitor


def main() -> None:
    log = EventLog(os.path.join(DATA_ROOT, "events.jsonl"))
    monitor = HealthMonitor(log)

    accounts = engine_db.list_accounts()
    if not accounts:
        log.append(make_event(
            "health.worker", execution_state="UNKNOWN",
            reason_code="NO_ACCOUNTS_REGISTERED"))
        print("abutron-health: no accounts registered")
        return

    unhealthy = []
    for acct in accounts:
        snap = monitor.snapshot_account(acct["connection_id"])
        print(f"{acct['connection_id']}: {snap.overall} "
              f"faults={','.join(snap.faults) or 'none'}")
        if snap.overall not in ("HEALTHY", "OK", "CONNECTED"):
            unhealthy.append(acct["connection_id"])

    log.append(make_event(
        "health.worker", execution_state="UNHEALTHY" if unhealthy else "HEALTHY",
        reason_code=";".join(unhealthy) or "OK",
        payload={"accounts": len(accounts), "unhealthy": unhealthy}))

    if unhealthy:
        # Non-zero exit so systemd/ops alerting can see the failure.
        sys.exit(1)


if __name__ == "__main__":
    main()
