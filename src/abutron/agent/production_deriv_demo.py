"""V9 Deriv DEMO multi-account decision-to-native-MT5 execution runner.

Generalized from the proven 6207 runner to support any Deriv DEMO
account (6207, 7593, 9244, ...). Each account has its own QA root,
activation authority, and cost report, but shares the same V9 brain,
market data adapter, dispatcher, and native writer infrastructure.

Production account and activation source: the existing CRM.
Broker identity, data, and owner position risk: genuine native MT5.
Never enables or submits on LIVE.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

from .crm_native_binding import DemoNativeCRM
from ..risk.owner_native_authority import CurrentBrokerOwnerRisk
from ..events import EventLog
from ..feeds.mt5_terminal import Mt5TerminalManager
from .market_data import BrokerNativeMarketDataAdapter, run_shadow_decision_pass
from .production import AgenticDecisionEngine
from .substrate import BrainSubstrate
from .risk_gate import RiskGate

CRM_DB = "/var/lib/apex-signup/accounts.sqlite3"
MODEL_SHA = "aa7ba415c5aeb4ff745670632323b203c578e198ee51b15e746577dee09e1d5c"


def _account_paths(account_suffix: str) -> dict:
    """Derive the standard path layout for a Deriv DEMO account."""
    return {
        "qa_root": Path(f"/var/lib/private/abutron-pr37-direct-writer-demo{account_suffix}-qa"),
        "owner": f"/var/lib/abutron-v4-broker-crm-supervisor-{account_suffix}/customer-wide-risk-evidence.json",
        "cost": f"/var/lib/abutron-v4-broker-crm-supervisor-{account_suffix}/v4-native-execution-cost-{account_suffix}.json",
        "data_root": f"/var/lib/abutron-v9-production-{account_suffix}",
    }


def resolve_crm_demo_connection(account_suffix: str) -> str:
    """Find the exact current CRM Deriv DEMO account for a suffix."""
    with sqlite3.connect("file:" + CRM_DB + "?mode=ro", uri=True) as db:
        rows = db.execute(
            """SELECT id FROM apex_mt5_connections
               WHERE broker='Deriv' AND environment='DEMO'
                     AND status='CONNECTED' AND account_reference LIKE '%' || ?""",
            (account_suffix,)).fetchall()
    if len(rows) != 1 or len(str(rows[0][0])) != 32:
        raise ValueError(f"CRM_DERIV_DEMO_{account_suffix}_CONNECTION_NOT_UNIQUE")
    return str(rows[0][0])


def write_status(path: Path, body: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(body, f, sort_keys=True, allow_nan=False, default=str)
        f.flush()
        os.fsync(f.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)


def execution_session_enabled(
        account_suffix: str, *,
        qa_terminal: "Path | None" = None,
        connection_id: "str | None" = None,
        crm_db: "str | None" = None) -> bool:
    """Check that the MT5 chart has execution enabled and the native
    HMAC-signed broker exposure is current.

    The ``qa_terminal``, ``connection_id`` and ``crm_db`` overrides are
    used by the backward-compatible ``production_6207`` wrapper so its
    module-level attributes remain monkeypatchable in tests.
    """
    paths = _account_paths(account_suffix)
    if qa_terminal is None:
        qa_terminal = paths["qa_root"] / "terminal"
    else:
        qa_terminal = Path(qa_terminal)
    preset = qa_terminal / "MQL5/Presets/PR37WriterExposureOnly.set"
    if not preset.is_file() or preset.is_symlink():
        return False
    raw = preset.read_text(encoding="utf-8").lower()
    if "inpexposureonly=false" not in raw or "inptrailingenabled=true" not in raw:
        return False
    # Read live HMAC-authenticated native broker exposure directly.
    from ..execution.writer_gateway import SingleWriterGateway
    from ..reconciliation.native_crm_6207 import signed_native_exposure
    if crm_db is None:
        crm_db = CRM_DB
    try:
        cid = connection_id if connection_id is not None \
            else resolve_crm_demo_connection(account_suffix)
        with sqlite3.connect("file:" + crm_db + "?mode=ro", uri=True) as db:
            rows = db.execute(
                """SELECT account_reference,broker_server FROM apex_mt5_connections
                   WHERE id=? AND broker='Deriv' AND environment='DEMO'""",
                (cid,)).fetchall()
        if len(rows) != 1:
            return False
        files = str(qa_terminal / "MQL5/Files")
        signed_native_exposure(
            files, cid=cid, login=int(rows[0][0]), server=str(rows[0][1]),
            secret=SingleWriterGateway.read_existing_secret(files), ttl=70)
        return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False


def run(account_suffix: str, mode: str) -> dict:
    if mode not in ("preflight", "execute"):
        raise ValueError("MODE_NOT_SUPPORTED")
    paths = _account_paths(account_suffix)
    cid = resolve_crm_demo_connection(account_suffix)
    from .. import db
    binding = DemoNativeCRM(
        account_suffix=account_suffix, connection_id=cid,
        crm_db=CRM_DB,
        native_owner_report=paths["owner"],
        native_cost_report=paths["cost"],
        qa_data_dir=str(paths["qa_root"] / "terminal"),
        qa_health=str(paths["qa_root"] / "issue38-health.json"))
    snapshot = binding.snapshot()
    acct = snapshot["account"]
    assert acct["environment"] == "demo"
    source = Path(__file__).resolve().parents[3]
    model = source / "data/brains/unified_m5/nexus_brain.json"
    if hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA:
        raise ValueError("V9_TRAINED_MODEL_RELEASE_HASH_CHANGED")
    brain = BrainSubstrate(brain_dir=str(model.parent))
    if brain.is_available() is not True:
        raise ValueError("V9_TRAINED_BRAIN_NOT_AVAILABLE")
    engine = AgenticDecisionEngine(
        brain_substrate=brain,
        risk_gate=RiskGate(per_trade_pct=0.0015, portfolio_cap_pct=0.02),
        live=False)
    manager = Mt5TerminalManager(root=str(paths["qa_root"]))
    manager.bind_demo_data_source(
        connection_id=cid, broker="Deriv",
        login=acct["mt5_login"], server=acct["server"],
        data_dir=str(paths["qa_root"] / "terminal"))
    adapter = BrokerNativeMarketDataAdapter(
        manager=manager, connection_id=cid, broker="Deriv",
        login=acct["mt5_login"], server=acct["server"],
        min_bars=30, max_stale_seconds=155)
    result = {
        "checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "V9_DEMO_NATIVE_" + mode.upper(),
        "account_suffix": account_suffix,
        "crm_account_connected": True,
        "native_owner_current_risk_verified": True,
        "native_owner_current_stop_risk_usd": snapshot["owner_open_risk_usd"],
        "owner_equity_usd": snapshot["owner_equity_usd"],
        "crm_demo_global_flag": snapshot["global_demo_flag"],
        "trained_v9_brain_loaded": True,
        "native_writer_ex5_sha": snapshot["writer"]["ea_sha256"],
        "native_orders_sent": 0,
    }
    if mode == "preflight":
        output = run_shadow_decision_pass(
            engine, adapter, connection_id=cid, broker="Deriv",
            owner_id=acct["owner_id"], account=acct,
            equity=float(acct["equity"]))
        result.update(
            status="BRAIN_DECISION_EVALUATED_EXECUTION_NOT_INVOKED",
            native_m5_bar_count=output["candle_count"],
            native_quote_count=output["quote_count"],
            candidates_generated=output["candidates_generated"])
        return result
    if not execution_session_enabled(account_suffix):
        result["status"] = "NATIVE_MT5_EXECUTION_MODE_NOT_ACTIVE"
        return result
    db.engine_db.upsert_account(acct)
    from .runtime import run_agentic_pass
    from ..reconciliation.native_crm_6207 import reconcile_engine_receipts
    cost_ticks = {symbol: value["tick_size"]
                  for symbol, value in snapshot["native_specs"].items()
                  if "tick_size" in value}
    receipt_before = reconcile_engine_receipts(
        engine_db=db.engine_db, crm_db=CRM_DB, cid=cid,
        login=acct["mt5_login"], server=acct["server"],
        files_dir=snapshot["writer"]["mql5_files_dir"],
        tick_sizes=cost_ticks)
    outcome = run_agentic_pass(
        cid, broker="Deriv", owner_id=acct["owner_id"], engine=engine,
        adapter=adapter, native_specs=snapshot["native_specs"],
        owner_open_risk_usd=snapshot["owner_open_risk_usd"],
        owner_equity_usd=snapshot["owner_equity_usd"],
        native_writer=snapshot["writer"],
        owner_native_authority=binding.current_risk,
        event_log=EventLog(os.path.join(os.environ["ABUTRON_DATA_ROOT"],
                                        "native-events.jsonl")))
    receipt_after = reconcile_engine_receipts(
        engine_db=db.engine_db, crm_db=CRM_DB, cid=cid,
        login=acct["mt5_login"], server=acct["server"],
        files_dir=snapshot["writer"]["mql5_files_dir"],
        tick_sizes=cost_ticks)
    result.update(
        status="V9_NATIVE_DISPATCH_EVALUATED",
        crm_native_protected_receipts=receipt_after["protected"],
        crm_native_receipts_waiting=receipt_after["unresolved"],
        candidates_generated=outcome.candidates_generated,
        native_m5_bar_count=outcome.candle_count,
        native_quote_count=outcome.quote_count,
        candidate_reservations=len(outcome.reserved_candidate_ids),
        native_orders_sent=sum(
            1 for x in outcome.dispatch_results if x.get("outcome") == "ACK"),
        native_unknown_receipts=sum(
            1 for x in outcome.dispatch_results if x.get("outcome") == "UNKNOWN"),
        dispatch_blocked=outcome.dispatch_blocked,
        dispatch_reason=outcome.dispatch_reason,
    )
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True,
                        help="Account suffix (e.g. 6207, 7593, 9244)")
    parser.add_argument("--mode", choices=("preflight", "execute"),
                        default="preflight")
    args = parser.parse_args(argv)
    paths = _account_paths(args.account)
    root = Path(os.environ.get("ABUTRON_DATA_ROOT", paths["data_root"]))
    if not root.is_dir() or root.is_symlink():
        raise ValueError("V9_PRODUCTION_DATA_ROOT_NOT_PREPARED")
    try:
        status = run(args.account, args.mode)
        exitcode = 0
    except Exception as exc:
        status = {
            "checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "mode": "V9_DEMO_NATIVE_" + args.mode.upper(),
            "account_suffix": args.account,
            "status": "PRODUCTION_INPUT_OR_EXECUTION_ERROR",
            "reason_code": type(exc).__name__ + ":" + str(exc)[:120],
            "native_orders_sent": 0}
        exitcode = 1
    write_status(root / "status.json", status)
    print(json.dumps({k: v for k, v in status.items()
                      if k not in ("connection_id", "owner_id")},
                     sort_keys=True))
    return exitcode


if __name__ == "__main__":
    raise SystemExit(main())
