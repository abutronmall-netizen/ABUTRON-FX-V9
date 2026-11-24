"""ABUTRON FX V9 — global configuration.

All secrets are injected via environment variables (see .env.example).
Nothing here may contain broker credentials or API keys by default.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

ENV = os.getenv("ABUTRON_ENV", "demo").lower()
APP_NAME = "ABUTRON FX V9"
ENGINE_NAME = "APEX"
ENGINE_VERSION = "1.1"

# ---------------------------------------------------------------------------
# Filesystem layout (all state lives under DATA_ROOT, kept out of Git)
# ---------------------------------------------------------------------------
DATA_ROOT = os.getenv("ABUTRON_DATA_ROOT", os.path.abspath("data"))
NATIVE_ROOT = os.path.join(DATA_ROOT, "native")            # MT5 native exports
DECISIONS_ROOT = os.path.join(DATA_ROOT, "decisions")      # APEX journals
RECEIPTS_ROOT = os.path.join(DATA_ROOT, "receipts")        # native receipts
LEDGER_ROOT = os.path.join(DATA_ROOT, "ledger")            # SQLite ledger
STATE_ROOT = os.path.join(DATA_ROOT, "state")              # reservations / locks
PORTAL_DB = os.path.join(DATA_ROOT, "portal.db")
ENGINE_DB = os.path.join(DATA_ROOT, "engine.db")

# External, non-secret datasets linked by scripts/link_all.sh. They are never
# committed to this repository; only the trained brain artifact is versioned.
MARKET_DATA_DIR = os.getenv(
    "ABUTRON_MARKET_DATA_DIR", os.path.join(DATA_ROOT, "links", "market-data"))
TRADE_HISTORY_DIR = os.getenv(
    "ABUTRON_TRADE_HISTORY_DIR", os.path.join(DATA_ROOT, "links", "trade-history"))
TICK_TRADES_PATH = os.getenv("ABUTRON_TICK_TRADES", "")
DEPTH_SNAPSHOTS_PATH = os.getenv("ABUTRON_DEPTH_SNAPSHOTS", "")
RUNTIME_RULES_PATH = os.getenv("ABUTRON_RUNTIME_RULES", "")

for _d in (DATA_ROOT, NATIVE_ROOT, DECISIONS_ROOT, RECEIPTS_ROOT,
           LEDGER_ROOT, STATE_ROOT):
    os.makedirs(_d, exist_ok=True)

# ---------------------------------------------------------------------------
# Trading policy (blueprint-defined defaults; per-account overrides in CRM)
# ---------------------------------------------------------------------------
DEFAULT_PORTFOLIO_RISK_CAP_PCT = float(os.getenv("ABUTRON_RISK_CAP_PCT", "0.20"))
DEFAULT_PER_TRADE_RISK_PCT = float(os.getenv("ABUTRON_PER_TRADE_PCT", "0.0015"))
DEFAULT_TRAILING_ATR_MULT = float(os.getenv("ABUTRON_TRAIL_ATR_MULT", "2.0"))
DEFAULT_MIN_BROKER_RISK_BUFFER_USD = float(os.getenv("ABUTRON_MIN_RISK_BUFFER", "0.0"))
# Daily loss cap — maximum loss allowed per calendar day as a fraction
# of the day-start equity. 0.02 = 2% daily loss cap.
DEFAULT_DAILY_LOSS_CAP_PCT = float(os.getenv("ABUTRON_DAILY_LOSS_CAP_PCT", "0.02"))

# ---------------------------------------------------------------------------
# NexusBrain — aggressive self-learning decision brain (signal authority)
# ---------------------------------------------------------------------------
# Aggressive mode: thresholds are informational, NOT gates. The brain
# commits to a direction on every edge; conviction drives position sizing.
# Fail-closed abstention has been removed per the new design directive.
BRAIN_MIN_CONFIDENCE = float(os.getenv("ABUTRON_BRAIN_MIN_CONFIDENCE", "0.0"))
BRAIN_MAX_UNCERTAINTY = float(os.getenv("ABUTRON_BRAIN_MAX_UNCERTAINTY", "1.0"))
BRAIN_MAX_CONVICTION = float(os.getenv("ABUTRON_BRAIN_MAX_CONVICTION", "1.0"))
BRAIN_SIZE_CAP = float(os.getenv("ABUTRON_BRAIN_SIZE_CAP", "1.0"))
BRAIN_MC_SAMPLES = int(os.getenv("ABUTRON_BRAIN_MC_SAMPLES", "16"))
BRAIN_ENSEMBLE_SEED = int(os.getenv("ABUTRON_BRAIN_SEED", "0"))
# The checked-in trained unified master brain is the default attachment.
# Override with ABUTRON_BRAIN_DIR for a separately mounted checkpoint.
BRAIN_MODEL_DIR = os.getenv("ABUTRON_BRAIN_DIR",
                            os.path.join(DATA_ROOT, "brains", "unified_m5"))

# LIVE trading is ENABLED by default (aggressive-mode design directive:
# DEMO and LIVE both trade once a broker session is CONNECTED). Set
# ABUTRON_LIVE_TRADING_ENABLED=false to gate LIVE connects behind
# LIVE_NOT_APPROVED. This matches README.md and tests/test_client_connect.py.
LIVE_TRADING_ENABLED = os.getenv("ABUTRON_LIVE_TRADING_ENABLED", "true").lower() in (
    "1", "true", "yes")

# Decision timing
DECISION_TIMEFRAMES = ("M5", "M15", "H1")
PRIMARY_TIMEFRAME = "M5"
CANDLE_OPEN_SECONDS = 300          # M5
ENGINE_TICK_SECONDS = int(os.getenv("ABUTRON_ENGINE_TICK", "30"))
HEARTBEAT_STALE_SECONDS = int(os.getenv("ABUTRON_HEARTBEAT_STALE", "90"))

# Issue #36: exposure certifications expire if not refreshed by the broker
# worker. A stale certification is treated as uncertified (fail-closed), so
# the dispatcher blocks on BLOCKED_INCOMPLETE_EXPOSURE until the worker
# publishes a fresh authenticated broker snapshot.
EXPOSURE_CERT_TTL_SECONDS = int(os.getenv("ABUTRON_EXPOSURE_CERT_TTL", "300"))

# Issue #36: a broker exposure snapshot must be fresh. The nonce embeds a
# unix timestamp; a snapshot older than this window is rejected (replay
# protection), so a captured old snapshot cannot certify exposure.
EXPOSURE_SNAPSHOT_FRESH_SECONDS = int(os.getenv("ABUTRON_SNAPSHOT_FRESH", "60"))

# ---------------------------------------------------------------------------
# Wine / MT5 terminal runtime (Ubuntu host)
# ---------------------------------------------------------------------------
MT5_ROOT = os.getenv("ABUTRON_MT5_ROOT", os.path.join(DATA_ROOT, "mt5"))
WINE_PREFIX = os.getenv("ABUTRON_WINE_PREFIX", os.path.join(MT5_ROOT, "wineprefix"))
MT5_EXE = os.getenv("ABUTRON_MT5_EXE", "Terminal64.exe")
MT5_EXPORT_DIR = os.getenv("ABUTRON_MT5_EXPORT", os.path.join(MT5_ROOT, "MQL5/Files"))
MANAGER_SOCK = os.getenv("ABUTRON_MANAGER_SOCK", os.path.join(DATA_ROOT, "manager.sock"))

# ---------------------------------------------------------------------------
# Brokers
# ---------------------------------------------------------------------------
BROKERS = ("deriv", "fpmarkets")

DERIV_SERVERS = tuple(
    s.strip() for s in
    os.getenv("ABUTRON_DERIV_SERVERS", "Deriv-Demo,synthetic-demo-server").split(",")
    if s.strip()
)
FP_SYMBOL_SUFFIX_HINT = os.getenv("ABUTRON_FP_SYMBOL_SUFFIX", ".r")

# ---------------------------------------------------------------------------
# Gemma LLM advisory (optional; advisory only — never on the order path)
# ---------------------------------------------------------------------------
GEMMA_ENABLED = os.getenv("ABUTRON_GEMMA_ENABLED", "true").lower() in ("1", "true", "yes")
GEMMA_BASE_URL = os.getenv("ABUTRON_GEMMA_BASE_URL", "http://127.0.0.1:11434/v1")
GEMMA_MODEL = os.getenv("ABUTRON_GEMMA_MODEL", "gemma2:9b")
GEMMA_TIMEOUT_S = float(os.getenv("ABUTRON_GEMMA_TIMEOUT", "20"))
GEMMA_MAX_RETRIES = int(os.getenv("ABUTRON_GEMMA_MAX_RETRIES", "2"))

# Multi-agent debate layer (TradingAgents pattern; advisory only)
# Bull/bear debate + risk debators + research-manager synthesis.
DEBATE_ENABLED = os.getenv("ABUTRON_DEBATE_ENABLED", "true").lower() in (
    "1", "true", "yes")
DEBATE_ROUNDS = int(os.getenv("ABUTRON_DEBATE_ROUNDS", "1"))
DEBATE_MAX_CALLS = int(os.getenv("ABUTRON_DEBATE_MAX_CALLS", "8"))

# Agentic decision engine: when enabled, the agentic multi-agent system
# (sense -> think -> gate -> act -> monitor -> recover) replaces the
# vendored apex engine as the authoritative production decision path in
# ApexIntegration.process_candle. Default OFF (shadow/staging) — the
# platform keeps using the apex path until the agentic system is
# certified. When ON, the RiskGate (which delegates to the existing
# RiskGuard) enforces the hard invariants before any candidate is
# produced. The fail-closed posture is preserved through all phases.
AGENTIC_ENGINE_ENABLED = os.getenv("ABUTRON_AGENTIC_ENGINE", "true").lower() in (
    "1", "true", "yes")

# Hardened pre-trade RiskGuard (kweinmeister pattern; fail-closed, opt-in)
# When enabled, an independent hard-limit gate runs on every READY candidate
# before it is stored for the single-writer dispatcher.
RISKGUARD_ENABLED = os.getenv("ABUTRON_RISKGUARD_ENABLED", "true").lower() in (
    "1", "true", "yes")

# Portal
PORTAL_HOST = os.getenv("ABUTRON_PORTAL_HOST", "0.0.0.0")
PORTAL_PORT = int(os.getenv("ABUTRON_PORTAL_PORT", "19743"))

# Audit
AUDIT_HASH_ALGO = "sha256"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_now() -> str:
    return _now()


@dataclass
class AccountSpec:
    """A broker account as known by the platform (no secrets here)."""
    connection_id: str
    broker: str                      # 'deriv' | 'fpmarkets'
    mt5_login: int
    display_name: str
    owner_id: str
    environment: str = "demo"        # 'demo' | 'live'
    server: str = ""
    per_trade_risk_pct: Optional[float] = None
    portfolio_cap_pct: Optional[float] = None
    enabled: bool = True            # CRM desired ON
    symbols: List[str] = field(default_factory=list)

    def effective_per_trade_risk_pct(self) -> float:
        return self.per_trade_risk_pct or DEFAULT_PER_TRADE_RISK_PCT

    def effective_portfolio_cap_pct(self) -> float:
        return self.portfolio_cap_pct or DEFAULT_PORTFOLIO_RISK_CAP_PCT
