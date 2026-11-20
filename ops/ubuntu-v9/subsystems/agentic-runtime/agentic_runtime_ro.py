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

SOURCE = Path("/opt/abutron-v9-agentic-release-review/pr37")
OUT = Path("/var/lib/abutron-v9-agentic-shadow/agentic-readiness.json")
PROBE = Path("/var/lib/abutron-issue38-exposure-readonly/latest.json")
RISK = Path("/var/lib/abutron-v9-native-risk-audit/latest.json")
MODEL = SOURCE / "data/brains/unified_m5/nexus_brain.json"
EXPECTED_MODEL_SHA = "aa7ba415c5aeb4ff745670632323b203c578e198ee51b15e746577dee09e1d5c"
EXPECTED_SOURCE_COMMIT = "b2cc75c3af8a8b5e8e5cc52713125f5f10e2eb7c"
PROBE_SCHEMA = "abutron.issue38.deriv6207.signed_native_exposure_reconcile.readonly.v1"


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
        probe, fst = trusted_json(PROBE)
        risk, rst = trusted_json(RISK)
        if not (0 <= now-fst.st_mtime <= 140 and 0 <= now-rst.st_mtime <= 140):
            raise ValueError("NATIVE_RISK_OR_SIGNED_PROBE_STALE")
        if (probe.get("schema")!=PROBE_SCHEMA or
            probe.get("status")!="SIGNED_NATIVE_EXPOSURE_RECONCILED_READONLY" or
            probe.get("signature_verified") is not True or
            probe.get("dual_native_crosscheck_verified") is not True or
            probe.get("trading_authorized") is not False or
            probe.get("customer_wide_risk_certified") is not False):
            raise ValueError("ISSUE38_NATIVE_EXPOSURE_NOT_VERIFIED")
        if (risk.get("status")!="NOT_CERTIFIED_FOR_NEW_ORDERS" or
            risk.get("new_entry_authorized_by_this_audit") is not False or
            risk.get("live_trading_changed") is not False):
            raise ValueError("RISK_AUDIT_STATUS_UNEXPECTED")
        from abutron.agent.production import AgenticDecisionEngine
        from abutron.agent.substrate import BrainSubstrate
        from abutron.agent.risk_gate import RiskGate

        substrate=BrainSubstrate(brain_dir=str(MODEL.parent))
        if substrate.is_available() is not True:
            raise ValueError("TRAINED_BRAIN_NOT_LOADED")
        gate=RiskGate(per_trade_pct=0.0015, portfolio_cap_pct=0.02)
        engine=AgenticDecisionEngine(brain_substrate=substrate,risk_gate=gate,live=False)
        # Empty candle array proves the complete decision-engine contract and
        # avoids fabricating any broker-native market data or candidate.
        candidates=engine.run_decision_pass(
            connection_id="issue38-deriv-demo-6207", broker="Deriv",
            owner_id="no-dispatch-shadow", candles=[], quotes=[],
            account={"equity":probe["native_equity_usd"]}, equity=probe["native_equity_usd"])
        if candidates or engine.ledger.count != 0 or engine.gate.portfolio_cap_pct != 0.02:
            raise ValueError("SHADOW_RUNTIME_CONTRACT_CHANGED")
        result.update({
            "readiness":"SHADOW_ENGINE_LOADED_BROKER_PROOFS_PENDING",
            "trained_model_loaded":True,
            "trained_model_sha256":EXPECTED_MODEL_SHA,
            "signed_exposure_reconciled":True,
            "native_current_positions":probe.get("native_position_count"),
            "risk_audit_status":risk["status"],
            "risk_audit_blockers":list(risk.get("blockers", [])),
            "risk_limit_per_trade_pct":0.15,
            "risk_limit_aggregate_open_pct":2.0,
            "risk_limit_drawdown_pct":20.0,
            "candidates_generated":0,
            "decision_mode":"NO_CANDLES_RUNTIME_SMOKE_ONLY",
            "broker_native_signal_certified":False,
        })
    except (OSError,ValueError,TypeError,KeyError,ImportError,RuntimeError,OverflowError) as exc:
        result["readiness"]="NOT_VERIFIED"
        result["reason_code"]=str(exc)[:96] if isinstance(exc,ValueError) else type(exc).__name__
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
    print("AGENTIC_V9_SHADOW",result["readiness"],result.get("reason_code","OK"),
          "ORDERS",0,flush=True)

if __name__ == "__main__":
    main()
