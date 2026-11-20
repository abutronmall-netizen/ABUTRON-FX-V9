#!/usr/bin/python3
"""Pinned ABUTRON PR37 agentic engine + trained brain readiness, offline shadow only.
No order dispatch, EA, CRM writes, broker sockets, or certification overrides.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from abutron.agent.market_data import MarketDataError

SOURCE = Path("/opt/abutron-pr37-native-m5-head978-qa")
OUT = Path("/var/lib/abutron-v9-agentic-shadow/agentic-readiness.json")
PROBE = Path("/var/lib/abutron-issue38-exposure-readonly/latest.json")
RISK = Path("/var/lib/abutron-v9-native-risk-audit/latest.json")
MODEL = SOURCE / "data/brains/unified_m5/nexus_brain.json"
EXPECTED_MODEL_SHA = "aa7ba415c5aeb4ff745670632323b203c578e198ee51b15e746577dee09e1d5c"
EXPECTED_SOURCE_COMMIT = "0197f0c488d6b46fb1d81c558cc22c47cc2b61ad"
PROBE_SCHEMA = "abutron.issue38.deriv6207.signed_native_exposure_reconcile.readonly.v1"
QA_ROOT = Path("/var/lib/private/abutron-pr37-direct-writer-demo6207-qa")
QA_HEALTH = QA_ROOT / "issue38-health.json"
OWNER_JOIN = Path("/var/lib/abutron-customer-native-evidence-join/latest.json")
QA_EX5_SHA = "9d4f6701993865d32fcd5d9e9781c7803e5f1a688bd8f1ca63e85b8eed173b86"
NATIVE_COST = Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/v4-native-execution-cost-6207.json")
OWNER_EXPOSURE = Path("/var/lib/abutron-v4-broker-crm-supervisor-6207/customer-wide-risk-evidence.json")


def trusted_json(path, max_size=500000):
    if path.is_symlink():
        raise ValueError("SOURCE_SYMLINK_DENIED")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022 or st.st_size < 4 or st.st_size > max_size:
            raise ValueError("UNTRUSTED_SOURCE")
        raw = os.read(fd, max_size + 1)
        after = os.fstat(fd)
        if len(raw) != st.st_size or st.st_mtime_ns != after.st_mtime_ns or st.st_ino != after.st_ino:
            raise ValueError("READ_RACE")
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("SCHEMA_INVALID")
        return d, st
    finally:
        os.close(fd)


def main():
    now = time.time()
    result = {
        "schema":"abutron.v9.agentic_shadow_runtime.readonly.v1",
        "checked_at_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
        "pinned_commit":EXPECTED_SOURCE_COMMIT,
        "agentic_engine_type":"AgenticDecisionEngine",
        "mode":"SHADOW_READONLY",
        "live_trading_enabled":False,
        "broker_order_send_available":False,
        "broker_network_access":False,
        "native_orders_sent":0,
        "crm_changed":False,
        "live_account_changed":False,
        "readiness":"NOT_VERIFIED",
    }
    try:
        # Do not depend on cwd and never trust unreviewed source/model changes.
        if hashlib.sha256(MODEL.read_bytes()).hexdigest() != EXPECTED_MODEL_SHA:
            raise ValueError("MODEL_SHA_CHANGED")
        pinned_agent_sources = {
            "production.py":"aa82229e0771982d4e11234bc453224dd9670a56f783a02d5a9f1afb4dc6a609",
            "risk_gate.py":"a8dd1cb5ff42b4d4ed46de6e2a118ad5e3202fcf9f1c10a722c47280d8b7ece8",
            "substrate.py":"78728bf90bc673bbf84e05eaa59586723326ddd53aee19b50ef9bb3a6277da79",
            "__init__.py":"d43d79e13d3c47c089aa32f0f776443cef14e2938f5caeb14adbdfecf82ab95f",
        }
        for fname, expected in pinned_agent_sources.items():
            actual = hashlib.sha256((SOURCE/"src/abutron/agent"/fname).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError("AGENTIC_SOURCE_HASH_MISMATCH")
        if os.getenv("ABUTRON_LIVE_TRADING_ENABLED") != "false":
            raise ValueError("LIVE_TRADING_MUST_BE_FALSE")
        if os.getenv("ABUTRON_RISKGUARD_ENABLED") != "true":
            raise ValueError("RISK_GUARD_MUST_BE_TRUE")
        if os.getenv("ABUTRON_ENV") != "demo":
            raise ValueError("DEMO_MODE_REQUIRED")
        # External history/certification monitors are telemetry, NOT a
        # veto on market inference. Native quote and broker identity are
        # validated independently by the DEMO adapter below.
        probe, _ = trusted_json(PROBE)
        risk, _ = trusted_json(RISK)
        if not isinstance(probe.get("native_equity_usd"), (int,float)):
            raise ValueError("BROKER_EQUITY_NOT_NUMERIC")
        from abutron.agent.production import AgenticDecisionEngine
        from abutron.agent.substrate import BrainSubstrate
        from abutron.agent.risk_gate import RiskGate

        substrate=BrainSubstrate(brain_dir=str(MODEL.parent))
        if substrate.is_available() is not True:
            raise ValueError("TRAINED_BRAIN_NOT_LOADED")
        gate=RiskGate(per_trade_pct=0.0015, portfolio_cap_pct=0.02)
        engine=AgenticDecisionEngine(brain_substrate=substrate,risk_gate=gate,live=False)
        from abutron.feeds.mt5_terminal import Mt5TerminalManager
        from abutron.agent.market_data import (
            BrokerNativeMarketDataAdapter, run_shadow_decision_pass)
        health, hst = trusted_json(QA_HEALTH)
        if (not 0 <= now-hst.st_mtime <= 155 or
            health.get("status")!="SIGNED_NATIVE_EXPOSURE_VERIFIED" or
            health.get("signature_verified") is not True or
            health.get("native_probe_sha256")!=QA_EX5_SHA or
            health.get("automated_trading_configured") is not False):
            raise ValueError("NATIVE_DEMO_QA_SOURCE_NOT_READY")
        owner_report, ost = trusted_json(OWNER_JOIN)
        matches = [x for x in owner_report.get("accounts", [])
                   if x.get("account_suffix")=="6207"]
        if len(matches)!=1 or not matches[0].get("owner_hash_prefix"):
            raise ValueError("NATIVE_DEMO_OWNER_NOT_BOUND")
        owner_id = "ownerhash:" + matches[0]["owner_hash_prefix"]
        cid = __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_6207"]
        mgr = Mt5TerminalManager(root=str(QA_ROOT))
        mgr.bind_demo_data_source(connection_id=cid, broker="Deriv",
            login=int(__import__("os").environ["ABUTRON_DERIV_DEMO_LOGIN"]), server="Deriv-Demo",
            data_dir=str(QA_ROOT / "terminal"))
        adapter = BrokerNativeMarketDataAdapter(manager=mgr,
            connection_id=cid, broker="Deriv", login=int(__import__("os").environ["ABUTRON_DERIV_DEMO_LOGIN"]),
            server="Deriv-Demo", min_bars=30, max_stale_seconds=155)
        shadow = run_shadow_decision_pass(engine, adapter,
            connection_id=cid, broker="Deriv", owner_id=owner_id,
            account={"equity":probe["native_equity_usd"]},
            equity=probe["native_equity_usd"])
        candidates = shadow["candidates"]
        # Build broker-executable lot/SL/TP geometry separately from the
        # AI's raw position_size. No order file or broker request is written.
        from types import SimpleNamespace
        from abutron.execution.native_agentic_sizing import (
            plan_native_agentic_order, NativeSizingError)
        native_plans = []
        native_plan_state = "NO_DIRECTIONAL_CANDIDATE" if not candidates else "NO_CURRENT_BROKER_COST"
        try:
            cost, cst = trusted_json(NATIVE_COST)
            owner_live, ovst = trusted_json(OWNER_EXPOSURE)
            if not (0 <= now-cst.st_mtime <= 65 and
                    0 <= now-ovst.st_mtime <= 65):
                raise ValueError("NATIVE_QUOTE_OR_OWNER_SOURCE_STALE")
            matches = [g for g in owner_live.get("owners", {}).values()
                       if any(str(a.get("account_masked", "")).endswith("6207")
                              for a in g.get("connections", []))]
            if len(matches)!=1:
                raise ValueError("OWNER_EXPOSURE_GROUP_NOT_UNIQUE")
            group = matches[0]
            owner_equity = sum(float(a["snapshot"]["equity_usd"])
                for a in group["connections"])
            native_plan_state = "NO_DIRECTIONAL_CANDIDATE" if not candidates else "BROKER_NATIVE_PLANNED"
            for c in candidates:
                try:
                    plan = plan_native_agentic_order(
                        SimpleNamespace(**c), cost["instruments"][c["symbol"]],
                        equity_usd=min(float(probe["native_equity_usd"]),
                                       float(cost["account_equity_usd"])),
                        owner_equity_usd=owner_equity,
                        owner_open_risk_usd=group["current_open_stop_risk_usd"])
                    plan["candidate_id"] = c["candidate_id"]
                    native_plans.append(plan)
                except (NativeSizingError, KeyError, TypeError, ValueError) as exc:
                    native_plan_state = "NATIVE_SPEC_NOT_ORDER_READY:"+str(exc)[:75]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            native_plan_state = "NATIVE_ORDER_PLAN_SOURCE_UNAVAILABLE:"+str(exc)[:65]
        if engine.gate.portfolio_cap_pct!=0.02:
            raise ValueError("RISK_POLICY_CHANGED")
        result.update({
            "readiness":"SHADOW_NATIVE_M5_EVALUATED_BROKER_PROOFS_PENDING",
            "trained_model_loaded":True,
            "trained_model_sha256":EXPECTED_MODEL_SHA,
            "signed_exposure_reconciled":True,
            "native_current_positions":probe.get("native_position_count"),
            "risk_audit_status":risk["status"],
            "risk_audit_blockers":list(risk.get("blockers", [])),
            "risk_limit_per_trade_pct":0.15,
            "risk_limit_aggregate_open_pct":2.0,
            "risk_limit_drawdown_pct":20.0,
            "candidates_generated":len(candidates),
            "candidate_details":candidates,
            "native_order_plans":native_plans,
            "native_order_plan_state":native_plan_state,
            "evaluated_symbol_count":shadow["evaluated_symbol_count"],
            "native_m5_candle_count":shadow["candle_count"],
            "native_quote_count":shadow["quote_count"],
            "broker_native_source_hash":shadow["source_hash"],
            "agentic_decision_evaluated":True,
            "decision_mode":"BROKER_NATIVE_M5_SHADOW_PASS",
            "broker_native_signal_certified":False,
        })
    except (OSError,ValueError,TypeError,KeyError,ImportError,RuntimeError,
            OverflowError,MarketDataError) as exc:
        result["readiness"]=("MARKET_INPUT_UNAVAILABLE"
                             if isinstance(exc,MarketDataError) else "NOT_VERIFIED")
        result["reason_code"]=(str(exc)[:96]
                               if isinstance(exc,(ValueError,MarketDataError))
                               else type(exc).__name__)
    OUT.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if OUT.parent.stat().st_uid != 0 or OUT.parent.stat().st_mode & 0o077:
        raise SystemExit("SHADOW_OUTPUT_DIR_UNTRUSTED")
    fd, tmp = tempfile.mkstemp(prefix=".agentic-",dir=OUT.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"w") as f:
            json.dump(result,f,sort_keys=True,separators=(",",":"),allow_nan=False)
            f.write("\n");f.flush();os.fsync(f.fileno())
        os.replace(tmp,OUT)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print("AGENTIC_V9_NATIVE_M5",result["readiness"],result.get("reason_code","OK"),
          "ORDERS",0,flush=True)

if __name__ == "__main__":
    main()
