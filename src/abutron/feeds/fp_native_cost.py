
"""FP Markets V305 native cost intake for the existing DEMO MT5 feed.

Source-only integration: reads the live data-only EA's MQL5/Files exports,
authenticates their account/session/nonce against the CRM and heartbeat, and
atomically materializes the root-owned JSON already required by V9.
No terminal configuration, EA order call or account activation occurs here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

EXPECTED_SCHEMA = "abutron.fpmarkets.native.cost_snapshot.v1"
NATIVE_TTL_SECONDS = 65
CATALOG_TTL_SECONDS = 100
HEARTBEAT_TTL_SECONDS = 30
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
REQUIRED_SPEC = (
    "tick_size", "point", "contract_size", "volume_min", "volume_step",
    "volume_max", "stops_level", "freeze_level", "digits", "trade_mode",
    "bid", "ask", "tick_epoch", "buy_probe_native_ordercalcprofit_usd",
    "sell_probe_native_ordercalcprofit_usd",
)


class FPMarketCostError(ValueError):
    """Actual broker cost report is absent, foreign or unusable."""


def _read_regular(path: Path, *, ttl: int, max_bytes: int = 1500000) -> tuple[str, bytes]:
    try:
        st = path.lstat()
    except OSError as exc:
        raise FPMarketCostError("FP_NATIVE_COST_SOURCE_MISSING") from exc
    if (not stat.S_ISREG(st.st_mode) or st.st_size < 3
            or st.st_size > max_bytes or st.st_mode & 0o022
            or not 0 <= time.time() - st.st_mtime <= ttl):
        raise FPMarketCostError("FP_NATIVE_COST_SOURCE_NOT_CURRENT_OR_REGULAR")
    data = path.read_bytes()
    st_after = path.lstat()
    if st.st_mtime_ns != st_after.st_mtime_ns or st.st_size != len(data):
        raise FPMarketCostError("FP_NATIVE_COST_SOURCE_CHANGED_DURING_READ")
    try:
        return data.decode("utf-8-sig"), data
    except UnicodeDecodeError as exc:
        raise FPMarketCostError("FP_NATIVE_COST_INVALID_UTF8") from exc


def _crm_fp_demo(db_path: str, cid: str) -> tuple[str, str, str]:
    if not NONCE_RE.fullmatch(cid):
        raise FPMarketCostError("FP_CRM_CONNECTION_ID_INVALID")
    with sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True) as db:
        rows = db.execute(
            """SELECT account_reference, broker_server, environment
               FROM apex_mt5_connections
               WHERE id=? AND broker='FP Markets' AND environment='DEMO'
                     AND status='CONNECTED'""", (cid,)).fetchall()
    if len(rows) != 1:
        raise FPMarketCostError("FP_CRM_NATIVE_ACCOUNT_NOT_CONNECTED_DEMO")
    return str(rows[0][0]), str(rows[0][1]), str(rows[0][2])


def _positive(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FPMarketCostError("FP_NATIVE_COST_NUMERIC_FIELD_INVALID")
    x = float(value)
    if not math.isfinite(x):
        raise FPMarketCostError("FP_NATIVE_COST_NONFINITE")
    return x


def read_native_fp_cost(
    *,
    feed_files: str | Path,
    crm_db: str,
    connection_id: str,
    expected_suffix: str,
) -> dict:
    """Read authentic FP DEMO cost data, without writing anything.

    The terminal heartbeat determines the expected 32-hex export cycle;
    native cost, catalog, quote and heartbeat identities must coincide.
    The session owner token is not a CRM ID and is compared only between
    MT5-native exports. The real login/server bind to CRM separately.
    """
    login, server, environment = _crm_fp_demo(crm_db, connection_id)
    if not login.endswith(expected_suffix) or environment != "DEMO":
        raise FPMarketCostError("FP_NATIVE_COST_ACCOUNT_SUFFIX_NOT_MATCHED")
    root = Path(feed_files)
    if not root.is_dir() or root.is_symlink():
        raise FPMarketCostError("FP_NATIVE_COST_TERMINAL_PATH_INVALID")
    heartbeats = list(root.glob("ABUTRON_SESSION_HEARTBEAT_V1_*.txt"))
    if len(heartbeats) != 1:
        raise FPMarketCostError("FP_NATIVE_COST_HEARTBEAT_NOT_UNIQUE")
    heartbeat = heartbeats[0]
    nonce = heartbeat.name.removeprefix("ABUTRON_SESSION_HEARTBEAT_V1_").removesuffix(".txt")
    if not NONCE_RE.fullmatch(nonce):
        raise FPMarketCostError("FP_NATIVE_COST_NONCE_INVALID")
    hb_text, _ = _read_regular(heartbeat, ttl=HEARTBEAT_TTL_SECONDS, max_bytes=512)
    hb = hb_text.splitlines()
    if (len(hb) != 8 or hb[0] != "ABUTRON_MT5_NATIVE_HEARTBEAT_V1"
            or hb[1] != nonce or len(hb[2]) != 36
            or hb[3] != "FP_MARKETS" or hb[4] != server
            or hb[5:] != ["DEMO", "CONNECTED", "NO_ORDER_FUNCTIONS"]):
        raise FPMarketCostError("FP_NATIVE_COST_HEARTBEAT_IDENTITY_INVALID")
    owner = hb[2]

    cat_text, _ = _read_regular(
        root / f"ABUTRON_FP_CATALOG_V305_{nonce}.txt",
        ttl=CATALOG_TTL_SECONDS)
    catalog = cat_text.splitlines()
    if (len(catalog) < 20 or
            catalog[:6] != ["ABUTRON_FP_CATALOG_V1", nonce, owner,
                            server, login, "DEMO"] or
            abs(time.time() - int(catalog[6])) > CATALOG_TTL_SECONDS):
        raise FPMarketCostError("FP_NATIVE_COST_CATALOG_IDENTITY_INVALID")
    mappings = {}
    for line in catalog[8:]:
        bits = line.split("\t")
        if len(bits) != 9 or bits[0] != "M":
            continue
        if bits[3] == "MAPPED" and bits[2] and bits[2] not in mappings:
            mappings[bits[2]] = bits
    if len(mappings) != 13:
        raise FPMarketCostError("FP_NATIVE_COST_CATALOG_INCOMPLETE")

    quotes_text, _ = _read_regular(
        root / f"ABUTRON_FP_QUOTES_V305_{nonce}.txt",
        ttl=CATALOG_TTL_SECONDS)
    quotes = quotes_text.splitlines()
    if (len(quotes) < 7 or
            quotes[:6] != ["ABUTRON_FP_QUOTES_V1", nonce, owner,
                           server, login, "DEMO"]):
        raise FPMarketCostError("FP_NATIVE_COST_QUOTES_IDENTITY_INVALID")

    cost_text, cost_bytes = _read_regular(
        root / f"ABUTRON_FP_COST_V1_{nonce}.json",
        ttl=NATIVE_TTL_SECONDS)
    try:
        cost = json.loads(cost_text)
    except json.JSONDecodeError as exc:
        raise FPMarketCostError("FP_NATIVE_COST_JSON_MALFORMED") from exc
    if not isinstance(cost, dict):
        raise FPMarketCostError("FP_NATIVE_COST_NOT_OBJECT")
    if (cost.get("schema") != EXPECTED_SCHEMA
            or cost.get("source") != "MT5_OrderCalcProfit"
            or cost.get("nonce") != nonce
            or cost.get("heartbeat_owner") != owner
            or cost.get("mt5_login") != login
            or cost.get("broker_server") != server
            or cost.get("account_suffix") != expected_suffix
            or cost.get("environment") != "DEMO"
            or cost.get("account_currency") != "USD"
            or cost.get("native_bid_ask_and_ordercalcprofit_verified") is not True
            or cost.get("verified_instrument_count") != 13
            or cost.get("expected_instrument_count") != 13):
        raise FPMarketCostError("FP_NATIVE_COST_PROVENANCE_NOT_VERIFIED")
    sample = _positive(cost.get("sample_epoch"))
    if not 0 <= time.time() - sample <= NATIVE_TTL_SECONDS:
        raise FPMarketCostError("FP_NATIVE_COST_SAMPLE_STALE")
    # Native MT5 tick timestamps are broker-server clock values.
    # The same source records broker and GMT clocks for normalization.
    broker_epoch = _positive(cost.get("broker_server_epoch"))
    broker_offset = _positive(cost.get("broker_clock_offset_seconds"))
    if (abs(broker_offset) > 14 * 3600 or
            abs((broker_epoch - sample) - broker_offset) > 10):
        raise FPMarketCostError("FP_NATIVE_COST_BROKER_CLOCK_NOT_BOUND")
    equity = _positive(cost.get("account_equity_usd"))
    if equity <= 0:
        raise FPMarketCostError("FP_NATIVE_COST_EQUITY_INVALID")
    instruments = cost.get("instruments")
    if not isinstance(instruments, dict) or set(instruments) != set(mappings):
        raise FPMarketCostError("FP_NATIVE_COST_SYMBOL_SET_NOT_MATCHED")
    for symbol, spec in instruments.items():
        if not isinstance(spec, dict) or spec.get("symbol") != symbol:
            raise FPMarketCostError("FP_NATIVE_COST_SYMBOL_SPEC_MISMATCH")
        if not all(k in spec for k in REQUIRED_SPEC):
            raise FPMarketCostError("FP_NATIVE_COST_REQUIRED_SPEC_MISSING")
        nums = {key: _positive(spec[key]) for key in REQUIRED_SPEC}
        if (nums["tick_size"] <= 0 or nums["point"] <= 0
                or nums["contract_size"] <= 0 or nums["volume_min"] <= 0
                or nums["volume_step"] <= 0
                or nums["volume_max"] < nums["volume_min"]
                or nums["bid"] <= 0 or nums["ask"] <= nums["bid"]
                or nums["buy_probe_native_ordercalcprofit_usd"] >= 0
                or nums["sell_probe_native_ordercalcprofit_usd"] >= 0
                or int(nums["trade_mode"]) != 4
                or not 0 <= broker_epoch - nums["tick_epoch"] <= 90
                or not 0 <= time.time() - (nums["tick_epoch"] - broker_offset) <= 90):
            raise FPMarketCostError("FP_NATIVE_COST_BROKER_PROBE_INVALID")
        mapped = mappings[symbol]
        if (int(mapped[4]) != int(nums["trade_mode"])
                or abs(float(mapped[5]) - nums["contract_size"]) > 1e-5
                or abs(float(mapped[6]) - nums["volume_min"]) > 1e-5
                or abs(float(mapped[7]) - nums["volume_step"]) > 1e-5
                or int(mapped[8]) != int(nums["digits"])):
            raise FPMarketCostError("FP_NATIVE_COST_CATALOG_PROBE_MISMATCH")
    checked = dt.datetime.fromtimestamp(sample, tz=dt.timezone.utc).isoformat()
    return {
        "schema": "abutron.fpmarkets.v9.native_execution_cost.v1",
        "source": EXPECTED_SCHEMA,
        "account_suffix": expected_suffix,
        "environment": "DEMO",
        "account_equity_usd": equity,
        "native_bid_ask_and_ordercalcprofit_verified": True,
        "native_sample_utc": checked,
        "worker_nonce": nonce,
        "broker_source_sha256": hashlib.sha256(cost_bytes).hexdigest(),
        "instruments": instruments,
    }


def refresh_fp_v9_cost_report(
    *, feed_files: str | Path, crm_db: str,
    connection_id: str, expected_suffix: str,
    target: str | Path,
) -> dict:
    """Create the account-scoped consumer JSON only from validated native MT5.

    This is invoked only when the FP runner is explicitly configured with
    ABUTRON_FP_FEED_FILES; no background service, order call or EA change.
    """
    result = read_native_fp_cost(
        feed_files=feed_files, crm_db=crm_db,
        connection_id=connection_id, expected_suffix=expected_suffix)
    dest = Path(target)
    if not dest.parent.is_dir() or dest.parent.is_symlink() or dest.is_symlink():
        raise FPMarketCostError("FP_NATIVE_COST_TARGET_DIRECTORY_NOT_PREPARED")
    if dest.exists() and not dest.is_file():
        raise FPMarketCostError("FP_NATIVE_COST_TARGET_NOT_REGULAR")
    fd, temporary = tempfile.mkstemp(prefix=".fp-cost-", dir=str(dest.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, dest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result
