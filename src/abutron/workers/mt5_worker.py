"""MT5 worker — per-account Wine terminal supervisor.

Runs inside the systemd template `abutron-mt5-worker@<connection_id>`.
Launches/supervises the Wine/MT5 terminal for one account and keeps the
native exports flowing. Does NOT place orders (the EA is the single
writer); the worker only feeds data.
"""
from __future__ import annotations

import os
import sys
import time

from ..config import DATA_ROOT
from ..events import EventLog
from ..feeds.mt5_terminal import terminal_manager
from ..db import engine_db
from ..exposure.publisher import publish_exposure


def connection_id() -> str:
    cid = os.getenv("ABUTRON_CONNECTION_ID", "")
    if cid:
        return cid
    accts = engine_db.list_accounts()
    return accts[0]["connection_id"] if accts else ""


def main() -> None:
    cid = connection_id()
    if not cid:
        sys.exit("no connection id and no accounts registered")
    acct = engine_db.get_account(cid)
    if acct is None:
        sys.exit(f"account {cid} not found")

    log = EventLog(os.path.join(DATA_ROOT, "events.jsonl"))
    # Record run-start time to remove stale-file ambiguity (Issue #38
    # STALE_NATIVE_FILE): only snapshots produced for this invocation
    # are eligible for certification.
    run_start = time.time()
    demo_data_dir = os.getenv("ABUTRON_DEMO_MT5_DATA_DIR", "").strip()
    if demo_data_dir:
        # Bind to the existing DEMO terminal's exports without spawning a
        # competing process, modifying a chart or enabling order submission.
        # The source is UNVERIFIED until CRM checks the actual native HMAC.
        if str(acct.get("environment", "")).lower() != "demo":
            sys.exit("external native evidence binding requires DEMO account")
        term = terminal_manager.bind_demo_data_source(
            connection_id=cid, broker=acct["broker"],
            login=acct["mt5_login"], server=acct.get("server", ""),
            data_dir=demo_data_dir, environment="demo")
        event = "worker.native_source_bound"
    else:
        term = terminal_manager.launch(
            connection_id=cid, broker=acct["broker"],
            login=acct["mt5_login"], server=acct.get("server", ""))
        event = "worker.process_started"

    log.append(__import__("abutron.events", fromlist=["make_event"]).make_event(
        event, connection_id=cid, broker=acct["broker"],
        mt5_login=acct["mt5_login"],
        execution_state="UNVERIFIED",
        reason_code="NATIVE_BROKER_IDENTITY_NOT_YET_VERIFIED"))

    try:
        last_publish = 0.0
        while True:
            if not term.is_running and not term.external_evidence_only:
                terminal_manager.invalidate_native_authentication(cid)
                term = terminal_manager.restart(cid)
                log.append(__import__("abutron.events",
                                      fromlist=["make_event"]).make_event(
                    "worker.restart", connection_id=cid,
                    execution_state="UNVERIFIED",
                    reason_code="PROCESS_EXITED_NO_NATIVE_PROOF"))
            # Issue #36: publish broker-native exposure from the terminal's
            # snapshot on each export cycle, so certification is derived
            # from real broker evidence — never a manually supplied flag.
            if time.time() - last_publish >= 30:
                try:
                    certified = publish_exposure(cid, log=log, since=run_start)
                    terminal_manager.mark_native_authenticated(cid, certified)
                    last_publish = time.time()
                except Exception as exc:  # noqa: BLE001 - log, never crash the loop
                    # Revoke prior proof immediately; an old success is not
                    # authorization after the native feed has failed.
                    from ..crm.controls import crm
                    crm.invalidate_exposure(cid)
                    terminal_manager.invalidate_native_authentication(cid)
                    log.append(__import__("abutron.events",
                                          fromlist=["make_event"]).make_event(
                        "exposure.publish_error", connection_id=cid,
                        execution_state="UNCERTIFIED",
                        reason_code="BROKER_SNAPSHOT_UNAVAILABLE",
                        error=str(exc)))
            time.sleep(10)
    except KeyboardInterrupt:
        terminal_manager.stop(cid)


if __name__ == "__main__":
    main()
