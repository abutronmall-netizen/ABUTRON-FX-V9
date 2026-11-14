"""Dispatcher worker — drains RESERVED candidates to native orders.

Runs as `abutron-dispatch.service`. For each effective-ON account it
invokes the native dispatcher, which sends orders through the single-writer
gateway and records native receipts (ACK / UNKNOWN / REJECTED).
"""
from __future__ import annotations

import os
import time

from ..config import DATA_ROOT, ENGINE_TICK_SECONDS
from ..events import EventLog, make_event
from ..db import engine_db
from ..execution.dispatcher import NativeDispatcher
from ..execution.receipts import ReceiptManager
from ..execution.writer_gateway import SingleWriterGateway


def main() -> None:
    log = EventLog(os.path.join(DATA_ROOT, "events.jsonl"))
    receipt_mgr = ReceiptManager(log)
    gateway = SingleWriterGateway(log)
    dispatcher = NativeDispatcher(log, gateway, receipt_mgr)

    while True:
        for acct in engine_db.list_accounts():
            if acct["effective_on"] and acct["desired_on"]:
                try:
                    result = dispatcher.dispatch_account(
                        acct["connection_id"])
                    if result.get("ack"):
                        log.append(make_event(
                            "dispatch.run", connection_id=acct["connection_id"],
                            broker=acct["broker"],
                            execution_state="ACK",
                            payload=result))
                except Exception as e:
                    log.append(make_event(
                        "dispatch.error", connection_id=acct["connection_id"],
                        broker=acct["broker"], execution_state="UNKNOWN",
                        reason_code=f"DISPATCH_ERROR:{type(e).__name__}"))
        time.sleep(ENGINE_TICK_SECONDS)


if __name__ == "__main__":
    main()
