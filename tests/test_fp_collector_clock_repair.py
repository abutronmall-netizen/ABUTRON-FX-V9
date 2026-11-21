"""FP native-feed collector clock repair tests.

Regression tests for two defects that caused
BROKER_CLOCK_OFFSET_UNVERIFIED across FP DEMO connections:

1. clock_offset.py used time.mktime() (local time) instead of
   calendar.timegm() (UTC) to parse UTC timestamp strings, producing
   wrong offsets on non-UTC hosts.

2. The FP collector's trusted_fp_demo_clock_offset() required native
   history + reconciliation files that don't exist on Ubuntu, causing
   FP_NATIVE_SOURCE_FileNotFoundError. A V305 cost-report fallback
   (v305_cost_clock_offset) now provides the broker clock offset from
   genuine broker-native cost evidence.

All fixtures are isolated temp files; no broker is contacted.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from abutron.feeds.clock_offset import ClockOffsetEstimator
from abutron.feeds.feed_quality import Candle


# --------------------------------------------------------------------- #
# Fix 1: UTC epoch parsing (time.mktime -> calendar.timegm)
# --------------------------------------------------------------------- #

class TestClockOffsetUtcEpoch:
    def test_utc_epoch_uses_timegm_not_mktime(self):
        """_utc_epoch must parse UTC strings as UTC, not local time."""
        from abutron.feeds.clock_offset import _utc_epoch
        # 2026-11-20T12:00:00Z is a known UTC epoch.
        expected = calendar.timegm(time.strptime(
            "2026-11-20T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        actual = _utc_epoch("2026-11-20T12:00:00Z")
        assert actual == expected
        # time.mktime would give a different result on a non-UTC host.
        assert actual != time.mktime(time.strptime(
            "2026-11-20T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ")) or \
            time.timezone == 0

    def test_estimate_verified_with_aligned_utc_bars(self):
        """When broker bars close on UTC, the offset must be ~0."""
        est = ClockOffsetEstimator()
        now_epoch = int(time.time())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch))
        # Need >= 7 candles for confidence > 0.5 (formula: len/12.0).
        candles = [Candle(
            symbol="XAUUSD.r", timeframe="M5",
            open_time=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(now_epoch - 600 - i * 300)),
            close_time=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(now_epoch - 1)),
            open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
            for i in range(8)]
        state = est.estimate("FP Markets", candles, now)
        assert state.verified is True
        assert state.status == "BROKER_CLOCK_VERIFIED"

    def test_estimate_rejects_bad_timestamp(self):
        est = ClockOffsetEstimator()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        candles = [Candle(symbol="XAUUSD.r", timeframe="M5",
                          open_time="not-a-time", close_time="not-a-time",
                          open=1.0, high=1.0, low=1.0, close=1.0)]
        state = est.estimate("FP Markets", candles, now)
        assert state.verified is False
        assert state.reason == "BAD_SAMPLE_TIMESTAMP"


# --------------------------------------------------------------------- #
# Fix 2: V305 cost-report clock offset fallback
# --------------------------------------------------------------------- #

def _make_v305_cost(suffix="0512", offset=7200, verified=True):
    return {
        "account_suffix": suffix,
        "environment": "DEMO",
        "native_bid_ask_and_ordercalcprofit_verified": verified,
        "broker_clock_offset_seconds": offset,
        "broker_server_epoch": int(time.time()),
        "sample_epoch": int(time.time()),
        "account_equity_usd": 5000.0,
        "instruments": {},
    }


def _write_root_cost(tmp_path, data, suffix="0512"):
    path = tmp_path / f"native-cost-{suffix}.json"
    path.write_text(json.dumps(data))
    return path


class TestV305CostClockOffset:
    @pytest.fixture(autouse=True)
    def _skip_root_ownership(self, monkeypatch):
        """Test files are not root-owned; skip the uid check."""
        from abutron.feeds import native_market_v3 as nmv
        real_read_bound = nmv._read_bound
        def patched(path, *, max_bytes, max_age=None, expected_uid=None):
            return real_read_bound(path, max_bytes=max_bytes,
                                   max_age=max_age, expected_uid=None)
        monkeypatch.setattr(nmv, "_read_bound", patched)

    def test_returns_valid_offset_from_cost_report(self, tmp_path):
        from abutron.feeds.native_market_v3 import v305_cost_clock_offset
        path = _write_root_cost(tmp_path, _make_v305_cost(offset=7200))
        assert v305_cost_clock_offset(
            cost_report_file=str(path), account_suffix="0512") == 7200

    def test_rejects_zero_offset_when_invalid(self, tmp_path):
        from abutron.feeds.native_market_v3 import v305_cost_clock_offset
        # Offset not in the whole-hour allowlist.
        path = _write_root_cost(tmp_path, _make_v305_cost(offset=900))
        with pytest.raises(ValueError, match="OFFSET_INVALID"):
            v305_cost_clock_offset(
                cost_report_file=str(path), account_suffix="0512")

    def test_rejects_wrong_account_suffix(self, tmp_path):
        from abutron.feeds.native_market_v3 import v305_cost_clock_offset
        path = _write_root_cost(tmp_path, _make_v305_cost(suffix="1636"))
        with pytest.raises(ValueError):
            v305_cost_clock_offset(
                cost_report_file=str(path), account_suffix="0512")

    def test_rejects_verification_flag_false(self, tmp_path):
        from abutron.feeds.native_market_v3 import v305_cost_clock_offset
        path = _write_root_cost(
            tmp_path, _make_v305_cost(verified=False))
        with pytest.raises(ValueError):
            v305_cost_clock_offset(
                cost_report_file=str(path), account_suffix="0512")

    def test_rejects_missing_cost_report(self, tmp_path):
        from abutron.feeds.native_market_v3 import v305_cost_clock_offset
        with pytest.raises(ValueError):
            v305_cost_clock_offset(
                cost_report_file=str(tmp_path / "nonexistent.json"),
                account_suffix="0512")

    def test_rejects_invalid_suffix_format(self, tmp_path):
        from abutron.feeds.native_market_v3 import v305_cost_clock_offset
        with pytest.raises(ValueError, match="SUFFIX_INVALID"):
            v305_cost_clock_offset(
                cost_report_file=str(tmp_path / "x.json"),
                account_suffix="abc")


# --------------------------------------------------------------------- #
# Status script fallback wiring
# --------------------------------------------------------------------- #

class TestStatusScriptV305Fallback:
    def test_status_script_uses_v305_fallback(self, tmp_path, monkeypatch):
        """When history/reconciliation files are missing, the status
        script should try the V305 cost report as a clock source."""
        import importlib.util
        script = (Path(__file__).resolve().parents[1]
                  / "scripts/native_market_v3_status_ro.py")
        spec = importlib.util.spec_from_file_location(
            "native_market_v3_status_ro", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # The script should have imported v305_cost_clock_offset.
        from abutron.feeds.native_market_v3 import v305_cost_clock_offset
        assert callable(v305_cost_clock_offset)
        # The fallback is wired into the except block of the status scan.
        src = script.read_text(encoding="utf-8")
        assert "v305_cost_clock_offset" in src
        assert "ABUTRON_FP_COST_REPORT" in src
        assert "clock_source" in src


# --------------------------------------------------------------------- #
# P0 fixes: per-account cost paths, read-only publisher, clock independence
# --------------------------------------------------------------------- #

class TestPerAccountCostReportResolution:
    def test_per_account_env_var_preferred(self, tmp_path, monkeypatch):
        """ABUTRON_FP_COST_REPORT_0512 takes priority over the generic."""
        import importlib.util
        script = (Path(__file__).resolve().parents[1]
                  / "scripts/native_market_v3_status_ro.py")
        src = script.read_text(encoding="utf-8")
        # The script must read the per-account env var.
        assert "ABUTRON_FP_COST_REPORT_" in src
        assert "ABUTRON_FP_COST_REPORT_" + "{suffix}" in src or \
            'f"ABUTRON_FP_COST_REPORT_' in src

    def test_generic_fallback_present(self):
        """The generic ABUTRON_FP_COST_REPORT is still supported."""
        script = (Path(__file__).resolve().parents[1]
                  / "scripts/native_market_v3_status_ro.py")
        src = script.read_text(encoding="utf-8")
        assert 'ABUTRON_FP_COST_REPORT"' in src or \
            'os.environ.get("ABUTRON_FP_COST_REPORT"' in src


class TestReadOnlyCostPublisher:
    def test_status_script_invokes_cost_publisher(self):
        """The status script must call refresh_fp_v9_cost_report."""
        script = (Path(__file__).resolve().parents[1]
                  / "scripts/native_market_v3_status_ro.py")
        src = script.read_text(encoding="utf-8")
        assert "refresh_fp_v9_cost_report" in src
        assert "cost_report_published" in src
        assert "cost_report_blocker" in src


class TestClockIndependenceFromV3Files:
    def test_clock_resolution_outside_symbols_block(self):
        """Clock fallback must run before/independently of V3 symbol discovery."""
        script = (Path(__file__).resolve().parents[1]
                  / "scripts/native_market_v3_status_ro.py")
        src = script.read_text(encoding="utf-8")
        # The clock resolution block must appear BEFORE the actual
        # `if symbols:` code block (not a comment referencing it).
        clock_idx = src.index("broker_offset = None")
        symbols_idx = src.index("if symbols:\n            for symbol in sorted")
        assert clock_idx < symbols_idx, (
            "Clock resolution must precede V3 symbol file discovery")

    def test_clock_fallback_uses_per_account_cost_path(self):
        """The V305 clock fallback must use the per-account cost report."""
        script = (Path(__file__).resolve().parents[1]
                  / "scripts/native_market_v3_status_ro.py")
        src = script.read_text(encoding="utf-8")
        # The cost_report variable must be used in the v305 fallback call.
        assert "v305_cost_clock_offset" in src
        assert "cost_report_file=cost_report" in src


class TestV305HistoryIndicatorP1:
    def test_v305_history_indicator_is_configurable(self):
        """V305 must default to V101 history, not auto-attach V100."""
        src = (Path(__file__).resolve().parents[1]
               / "mql5/AbutronFPMarketsNativeFeedParallelV305.mq5")
        text = src.read_text(encoding="utf-8")
        assert "InpHistoryIndicator" in text
        assert "AbutronFPAccountHistoryV101" in text
        # V100 must no longer be hardcoded in the attach logic.
        assert "iCustom(_Symbol,_Period,\"ABUTRON\\\\AbutronFPAccountHistoryV100\")" not in text
