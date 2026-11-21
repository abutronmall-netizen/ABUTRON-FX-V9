"""Authenticated broker-native MT5 V3 tick/DOM reader.

V2 account fills / last bid-ask quotes NEVER constitute market-wide prints.
This reader requires an isolated DEMO worker identity, pinned compiled EA,
strict account+symbol binding, recent native heartbeat and immutable JSONL rows.
It reports observed prints, but broker certification also needs chart-attached
attestation and a representative duration of live data capture.
No order API or write to the MT5 terminal exists in this module.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

from ..analytics.orderflow import Trade

SCHEMA="abutron.v9.mt5.market_microstructure.readonly.v3"
NAME=re.compile(r"ABUTRON_NATIVE_MARKET_V3_[A-Za-z0-9._-]+_\d{8}\.jsonl\Z")

def _sanitize_file_symbol(symbol: str) -> str:
    """Convert a broker symbol to its filename-safe variant.

    Mirrors the MQL5 ApexMarketFeedV3 SanitizeFile(): spaces become
    underscores, all other characters are kept if alphanumeric, dot,
    dash, or underscore. Used for globbing V3 output files whose names
    use the sanitized symbol while JSON content preserves the original.
    """
    return symbol.replace(" ", "_")
HEX32=re.compile(r"[0-9a-f]{32}\Z")
HEX64=re.compile(r"[0-9a-f]{64}\Z")
# MQL5 MqlTick.flags: LAST=8, BUY=32, SELL=64.
TICK_FLAG_LAST=8
TICK_FLAG_BUY=32
TICK_FLAG_SELL=64
MAX_FILE_BYTES=26*1024*1024

@dataclass
class NativeMarketV3Result:
    account: str
    symbol: str
    accepted_market_prints: int=0
    accepted_quotes: int=0
    accepted_dom_snapshots: int=0
    source_files: int=0
    identity_verified: bool=False
    pinned_binary_verified: bool=False
    native_chart_attested: bool=False
    verified_native_quote_ticks: bool=False
    verified_native_dom_levels: bool=False
    latest_heartbeat_utc: str=""
    largest_record_gap_seconds: float=0
    quote_only: bool=True
    execution_enabled_by_importer: bool=False
    verified_broker_orderflow: bool=False
    status: str="NOT_CERTIFIED"
    blockers: list[str]=field(default_factory=list)
    trade_bars: dict[str,list[Trade]]=field(default_factory=dict)
    depth_snapshots: list[dict[str,Any]]=field(default_factory=list)
    source_sha256: list[str]=field(default_factory=list)

    def summary(self) -> dict[str,Any]:
        return {k:getattr(self,k) for k in (
            "account","symbol","accepted_market_prints","accepted_quotes",
            "accepted_dom_snapshots","source_files","identity_verified",
            "pinned_binary_verified","native_chart_attested",
            "verified_native_quote_ticks","verified_native_dom_levels",
            "latest_heartbeat_utc",
            "largest_record_gap_seconds","quote_only",
            "execution_enabled_by_importer","verified_broker_orderflow",
            "status","blockers","source_sha256")}

def _read_bound(path: Path, *, max_bytes: int, max_age: int | None=None,
                expected_uid: int | None=None) -> bytes:
    if path.is_symlink():
        raise ValueError("NATIVE_SOURCE_SYMLINK")
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>max_bytes or st.st_size<2:
            raise ValueError("NATIVE_SOURCE_INODE_INVALID")
        if st.st_mode & 0o022:
            raise ValueError("NATIVE_SOURCE_WRITABLE_BY_OTHERS")
        if expected_uid is not None and st.st_uid!=expected_uid:
            raise ValueError("NATIVE_SOURCE_OWNER_MISMATCH")
        if max_age is not None and not -5<=time.time()-st.st_mtime<=max_age:
            raise ValueError("NATIVE_SOURCE_STALE")
        blob=os.read(fd,max_bytes+1)
        tail=os.fstat(fd)
        if len(blob)!=st.st_size or (st.st_ino,st.st_dev,st.st_mtime_ns,st.st_size)!=(
                tail.st_ino,tail.st_dev,tail.st_mtime_ns,tail.st_size):
            raise ValueError("NATIVE_SOURCE_CHANGED_DURING_READ")
        return blob
    finally:
        os.close(fd)

def _trusted_fp_demo_clock_offset_once(
    *, native_history_file: str, native_reconciliation_file: str,
    account_suffix: str
) -> int:
    """Return independently reconciled MT5 broker clock offset (seconds).

    V101 native broker history must match the independent current receipt /
    position reconciler generation. Never infer UTC offset from the system
    locale or from a possibly forged V3 market-data file.
    """
    if not re.fullmatch(r"[0-9]{4}",account_suffix):
        raise ValueError("FP_CLOCK_ACCOUNT_SUFFIX_INVALID")
    hist=json.loads(_read_bound(Path(native_history_file),
        max_bytes=3_000_000,max_age=150))
    recon=json.loads(_read_bound(Path(native_reconciliation_file),
        max_bytes=2_000_000,max_age=150))
    expected="••••"+account_suffix
    if (hist.get("schema")!="abutron.fpmarkets.native.account_history.readonly.v1"
        or hist.get("history_verified_from_mt5") is not True
        or hist.get("broker")!="FP Markets" or hist.get("environment")!="DEMO"
        or hist.get("account_masked")!=expected
        or not re.fullmatch(r"[0-9a-f]{64}",
                            str(hist.get("native_file_sha256","")))
        or recon.get("broker")!="FP Markets" or
        recon.get("environment")!="DEMO" or
        recon.get("account_masked")!=expected or
        recon.get("status")!="RECONCILED_READONLY" or
        recon.get("history_sha256")!=hist.get("native_file_sha256") or
        recon.get("native_broker_open_risk_verified") is not True or
        recon.get("bridge_proof_matches_broker") is not True or
        recon.get("blockers")):
        raise ValueError("FP_CLOCK_NATIVE_BROKER_RECONCILIATION_UNVERIFIED")
    offset=hist.get("broker_clock_offset_seconds")
    if type(offset) is not int or offset not in (0,3600,7200,10800,14400):
        raise ValueError("FP_CLOCK_NATIVE_OFFSET_INVALID")
    return offset


def trusted_fp_demo_clock_offset(
    *, native_history_file: str, native_reconciliation_file: str,
    account_suffix: str
) -> int:
    """Read until one exact fresh broker-generation reconciles or fail closed.

    Both FP V101 history and independent position/ACK proof are periodically
    written. A SHA race can exist for a fraction of a second. Every attempt
    repeats the COMPLETE native identity, risk and SHA validation; no cached
    success or fallback inferred offset is accepted.
    """
    for attempt in range(12):
        try:
            return _trusted_fp_demo_clock_offset_once(
                native_history_file=native_history_file,
                native_reconciliation_file=native_reconciliation_file,
                account_suffix=account_suffix)
        except ValueError as exc:
            if (str(exc)!="FP_CLOCK_NATIVE_BROKER_RECONCILIATION_UNVERIFIED"
                or attempt==11):
                raise
            time.sleep(0.25)
    raise ValueError("FP_CLOCK_NATIVE_RECONCILIATION_EXHAUSTED")


def v305_cost_clock_offset(
    *, cost_report_file: str, account_suffix: str
) -> int:
    """Return the broker clock offset from a V305 native cost report.

    This is a fallback clock source for FP Markets DEMO accounts when
    the native history / reconciliation files are unavailable (e.g.
    FP_NATIVE_SOURCE_FileNotFoundError). The V305 exporter measures the
    broker-server clock against UTC and publishes the offset in its
    cost report JSON. The offset must be a whole number of hours and
    within the allowed range; anything else is rejected fail-closed.

    The cost report must be root-owned, regular, fresh (65s TTL), and
    must carry a genuine broker-probe verification flag.
    """
    if not re.fullmatch(r"[0-9]{4}", account_suffix):
        raise ValueError("FP_CLOCK_ACCOUNT_SUFFIX_INVALID")
    cost_path = Path(cost_report_file)
    try:
        blob = _read_bound(cost_path, max_bytes=1_500_000, max_age=65,
                           expected_uid=0)
    except OSError as exc:
        raise ValueError("FP_CLOCK_V305_COST_NOT_AVAILABLE: "
                         + type(exc).__name__) from exc
    try:
        cost = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FP_CLOCK_V305_COST_NOT_AVAILABLE") from exc
    if (cost.get("account_suffix") != account_suffix
            or cost.get("environment") != "DEMO"
            or cost.get("native_bid_ask_and_ordercalcprofit_verified") is not True
            or "broker_clock_offset_seconds" not in cost):
        raise ValueError("FP_CLOCK_V305_COST_NOT_AVAILABLE")
    offset = cost["broker_clock_offset_seconds"]
    if type(offset) is not int or offset not in (0, 3600, 7200, 10800, 14400):
        raise ValueError("FP_CLOCK_V305_OFFSET_INVALID")
    return offset


def native_mt5_chart_attached(worker_root: Path, symbol: str) -> bool:
    """Independent native terminal-log chart-load proof (never self-attested by V3).

    A successful MT5 indicator-load log is evidence that *MetaTrader* loaded the
    pinned indicator. Coupled with currently streaming account-bound V3 data
    this proves the chart is still running. No GUI assumption or mock STATUS
    row alone can prove attachment.
    """
    terminal=worker_root/"terminal"
    logs=terminal/"logs"
    if not logs.is_dir() or logs.is_symlink():
        return False
    owner=terminal.stat().st_uid
    today=datetime.now(timezone.utc).date()
    candidates=[logs/(d.strftime("%Y%m%d")+".log") for d in
        (today, today-timedelta(days=1))]
    event_re=re.compile(
        r"Indicators\tcustom indicator ApexMarketFeedV3 \("+
        re.escape(symbol)+r",(M1|M5|M15|M30|H1|H4|D1)\) loaded succesfully")
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        for attempt in range(3):
            try:
                raw=_read_bound(path,max_bytes=4_000_000,
                                max_age=2*86400,expected_uid=owner)
                break
            except ValueError as e:
                if str(e)!="NATIVE_SOURCE_CHANGED_DURING_READ" or attempt==2:
                    return False
                time.sleep(0.05)
        if not raw.startswith(b"\xff\xfe"):
            continue
        text=raw.decode("utf-16",errors="strict")
        matches=[line for line in text.splitlines()
                 if "ApexMarketFeedV3" in line]
        if not matches:
            continue
        last=matches[-1]
        if event_re.search(last) and not any(x in last.lower()
                         for x in ("failed","unloaded","removed","error")):
            return True
    return False


def read_v3(
    *, worker_root: str, connection_id: str, login: str,
    server: str, symbol: str, approved_binary_sha256: str,
    max_age_seconds: int=240, broker_clock_offset_seconds: int | None=None
) -> NativeMarketV3Result:
    result=NativeMarketV3Result(account=str(login)[-4:],symbol=symbol)
    try:
        if not HEX32.fullmatch(connection_id) or not HEX64.fullmatch(approved_binary_sha256):
            raise ValueError("NATIVE_CONNECTION_OR_BINARY_PIN_INVALID")
        if (type(broker_clock_offset_seconds) is not int or
            broker_clock_offset_seconds not in (0,3600,7200,10800,14400)):
            raise ValueError("NATIVE_V3_BROKER_CLOCK_OFFSET_UNVERIFIED")
        if not str(login).isdigit() or not server or not symbol:
            raise ValueError("NATIVE_ACCOUNT_IDENTITY_MISSING")
        root=Path(worker_root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("NATIVE_WORKER_ROOT_NOT_TRUSTED")
        health=json.loads(_read_bound(root/"worker-health.json",max_bytes=150_000,max_age=35))
        if (health.get("id")!=connection_id or
            health.get("broker") not in ("FP Markets","Deriv") or
            health.get("mode")!="DEMO" or health.get("server")!=server or
            health.get("status")!="CONNECTED" or
            health.get("heartbeat_fresh") is not True or
            not HEX32.fullmatch(str(health.get("nonce","")))):
            raise ValueError("NATIVE_DEMO_WORKER_SESSION_UNVERIFIED")
        result.identity_verified=True
        binary=root/"terminal/MQL5/Indicators/ABUTRON/ApexMarketFeedV3.ex5"
        if not binary.is_file() or binary.is_symlink() or binary.stat().st_size<1000:
            raise ValueError("NATIVE_V3_COMPILED_EXPORTER_NOT_INSTALLED")
        if hashlib.sha256(_read_bound(binary,max_bytes=1_000_000)).hexdigest()!=approved_binary_sha256:
            raise ValueError("NATIVE_V3_BINARY_SHA256_NOT_PINNED")
        result.pinned_binary_verified=True
        files=root/"terminal/MQL5/Files/ABUTRON_V9_NATIVE"
        if not files.is_dir() or files.is_symlink():
            raise ValueError("NATIVE_V3_EXPORT_DIRECTORY_MISSING")
        expected_uid=(root/"terminal/MQL5/Files").stat().st_uid
        if files.stat().st_uid!=expected_uid:
            raise ValueError("NATIVE_EXPORT_DIRECTORY_OWNER_MISMATCH")
        target=files.glob("ABUTRON_NATIVE_MARKET_V3_"+_sanitize_file_symbol(symbol)+"_*.jsonl")
        paths=sorted(target)
        if not paths or len(paths)>7:
            raise ValueError("NATIVE_V3_SOURCE_FILES_MISSING_OR_TOO_MANY")
        seen=set()
        record_times=[]
        latest_status_time=0
        last_status=None
        for path in paths:
            if not NAME.fullmatch(path.name):
                raise ValueError("NATIVE_V3_FILE_NAME_INVALID")
            blob=_read_bound(path,max_bytes=MAX_FILE_BYTES,max_age=2*86400,
                            expected_uid=expected_uid)
            result.source_sha256.append(hashlib.sha256(blob).hexdigest())
            result.source_files+=1
            if not blob.endswith(b"\n"):
                raise ValueError("NATIVE_V3_UNTERMINATED_JSONL_RECORD")
            for raw in blob.splitlines():
                if not raw:
                    continue
                d=json.loads(raw)
                if not isinstance(d,dict):
                    raise ValueError("NATIVE_V3_ROW_MALFORMED")
                if (d.get("schema")!=SCHEMA or
                    _sanitize_file_symbol(d.get("symbol",""))!=_sanitize_file_symbol(symbol) or
                    d.get("account_login")!=str(login) or
                    d.get("broker_server")!=server or
                    d.get("environment")!="DEMO"):
                    raise ValueError("NATIVE_V3_ROW_IDENTITY_MISMATCH")
                kind=d.get("record_type")
                ms=d.get("time_msc")
                if type(ms) is not int:
                    raise ValueError("NATIVE_V3_MILLISECOND_CLOCK_INVALID")
                # MQL5 CopyTicks on this broker uses broker-server epoch.
                # STATUS/DOM use TimeGMT() (actual UTC). Normalize only
                # market ticks using independently reconciled history offset.
                utc_ms=(ms-broker_clock_offset_seconds*1000
                        if kind in ("QUOTE","MARKET_PRINT") else ms)
                if not 1600000000000<=utc_ms<=int(time.time()*1000)+120000:
                    raise ValueError("NATIVE_V3_MILLISECOND_CLOCK_INVALID")
                if utc_ms<int(time.time()*1000)-(3*86400*1000):
                    raise ValueError("NATIVE_V3_EXPIRED_RECORD")
                if kind=="STATUS":
                    if ms>=latest_status_time:
                        latest_status_time=ms
                        last_status=d
                    continue
                if kind=="DOM":
                    levels=d.get("levels")
                    if not isinstance(levels,list) or not 1<=len(levels)<=80:
                        raise ValueError("NATIVE_V3_DOM_LEVELS_INVALID")
                    for level in levels:
                        if (not isinstance(level,dict) or
                            type(level.get("book_type")) is not int or
                            not math.isfinite(float(level.get("price",0))) or
                            float(level.get("price",0))<=0 or
                            float(level.get("volume_real",0))<0 or
                            float(level.get("volume",0))<0):
                            raise ValueError("NATIVE_V3_DOM_LEVEL_INVALID")
                    result.depth_snapshots.append({"time_msc":ms,"symbol":symbol,
                                                   "levels":levels})
                    result.accepted_dom_snapshots+=1
                    continue
                if kind not in ("QUOTE","MARKET_PRINT"):
                    raise ValueError("NATIVE_V3_RECORD_KIND_UNRECOGNIZED")
                flags=d.get("flags")
                if type(flags) is not int or flags<0 or flags>65535:
                    raise ValueError("NATIVE_V3_TICK_FLAGS_INVALID")
                bid=float(d.get("bid",0))
                ask=float(d.get("ask",0))
                if not all(math.isfinite(v) and v>=0 for v in (bid,ask)):
                    raise ValueError("NATIVE_V3_QUOTE_INVALID")
                key=(ms,flags,d.get("price"),d.get("volume_real"),kind,d.get("aggressor_side"))
                if key in seen:
                    continue
                seen.add(key)
                record_times.append(ms)
                if kind=="QUOTE":
                    if d.get("aggressor_side")!="NONE":
                        raise ValueError("NATIVE_V3_QUOTE_WITH_FALSE_SIDE")
                    result.accepted_quotes+=1
                    continue
                side=d.get("aggressor_side")
                buy=bool(flags&TICK_FLAG_BUY)
                sell=bool(flags&TICK_FLAG_SELL)
                if not(flags&TICK_FLAG_LAST) or buy==sell or (
                    side!=("BUY" if buy else "SELL")):
                    raise ValueError("NATIVE_V3_MARKET_PRINT_FLAGS_UNVERIFIED")
                price=float(d.get("price",0))
                volume=float(d.get("volume_real",0))
                if volume<=0:
                    volume=float(d.get("volume",0))
                if not math.isfinite(price) or not math.isfinite(volume) or price<=0 or volume<=0:
                    raise ValueError("NATIVE_V3_MARKET_PRINT_INVALID")
                bar_open=utc_ms//300000*300
                bar_key=datetime.fromtimestamp(bar_open,timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                result.trade_bars.setdefault(bar_key,[]).append(Trade(
                    price=price,size=volume,side=side.lower(),
                    time=str(utc_ms//1000),order_id=0))
                result.accepted_market_prints+=1
        # Reconfirm worker session identity; market data must not cross a
        # profile/account nonce switch while a multi-MB file was read.
        tail_health=json.loads(_read_bound(
            root/"worker-health.json",max_bytes=150_000,max_age=35))
        if (tail_health.get("id")!=connection_id or
            tail_health.get("nonce")!=health.get("nonce") or
            tail_health.get("server")!=server or
            tail_health.get("mode")!="DEMO" or
            tail_health.get("heartbeat_fresh") is not True):
            raise ValueError("NATIVE_V3_WORKER_SESSION_CHANGED_DURING_READ")
        result.quote_only=(result.accepted_market_prints==0)
        if record_times:
            times=sorted(record_times)
            result.largest_record_gap_seconds=max((b-a)/1000 for a,b in zip(times,times[1:])) if len(times)>1 else 0
        if latest_status_time==0:
            raise ValueError("NATIVE_V3_TERMINAL_HEARTBEAT_MISSING")
        age=(time.time()*1000-latest_status_time)/1000
        if not -10<=age<=max_age_seconds:
            raise ValueError("NATIVE_V3_TERMINAL_HEARTBEAT_STALE")
        result.latest_heartbeat_utc=datetime.fromtimestamp(
            latest_status_time/1000,timezone.utc).isoformat()
        if last_status.get("dropped_records",0)!=0:
            raise ValueError("NATIVE_V3_DROPPED_RECORDS_BREAK_CONTINUITY")
        # Cross-check independent MT5 terminal log AND live, identity-bound
        # rows. A broker-origin log entry alone or a generated STATUS alone
        # cannot prove active attachment.
        result.native_chart_attested=native_mt5_chart_attached(root,symbol)
        if not result.native_chart_attested:
            result.blockers.append("NATIVE_V3_CHART_ATTACHMENT_ATTESTATION_PENDING")
        result.verified_native_quote_ticks=bool(
            result.identity_verified and result.pinned_binary_verified and
            result.native_chart_attested and result.accepted_quotes>0)
        result.verified_native_dom_levels=bool(
            result.identity_verified and result.pinned_binary_verified and
            result.native_chart_attested and result.accepted_dom_snapshots>0)
        if result.accepted_market_prints==0:
            result.blockers.append("NATIVE_V3_GENUINE_AGGRESSOR_PRINTS_UNAVAILABLE")
        result.verified_broker_orderflow=bool(
            result.identity_verified and result.pinned_binary_verified and
            result.accepted_market_prints>0 and not result.blockers)
        result.status=("VERIFIED_BROKER_MARKET_PRINTS"
            if result.verified_broker_orderflow else
            "BROKER_NATIVE_PRINTS_OBSERVED_NOT_CERTIFIED"
            if result.accepted_market_prints else
            "BROKER_NATIVE_QUOTE_AND_DOM_VERIFIED_NO_MARKET_PRINT"
            if result.verified_native_quote_ticks and result.verified_native_dom_levels else
            "BROKER_NATIVE_QUOTES_VERIFIED_NO_MARKET_PRINT"
            if result.verified_native_quote_ticks else
            "NATIVE_QUOTE_OR_DOM_ONLY_PROVENANCE_PARTIAL")
    except (OSError,ValueError,TypeError,KeyError,IndexError,json.JSONDecodeError) as err:
        result.blockers.append(str(err)[:110])
        result.status="NOT_CERTIFIED"
    return result
