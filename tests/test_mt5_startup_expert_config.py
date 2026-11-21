"""Tests for Mt5TerminalManager /config StartUp configuration.

Verifies that the config file preserves the auth expert and attaches
the feed expert via a chart template, uses /config:<path> syntax with
Wine-compatible Z:\\ paths, validates periods, and supports
validate_before_restart.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from abutron.feeds.mt5_terminal import (
    Mt5TerminalManager, Mt5Error, VALID_MT5_PERIODS,
)


class TestStartupConfigWriting:
    """The _write_startup_config method must preserve auth + attach feed."""

    def test_preserves_auth_expert_and_attaches_feed_via_template(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config(
            "cid123", prefix,
            auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
            feed_expert="ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
            feed_symbol="EURUSD.r",
            feed_period="H1")

        content = Path(path).read_text(encoding="utf-8")
        assert "[StartUp]" in content
        # Auth expert preserved as Expert
        assert "Expert=ABUTRON\\AbutronFPMarketsNativeFeedEA" in content
        # Feed expert attached via template
        assert "Symbol=EURUSD.r" in content
        assert "Period=H1" in content
        assert "Template=" in content
        # Feed expert name is NOT in the Expert= line
        assert "Expert=ABUTRON\\AbutronFPMarketsNativeFeedParallelV305" not in content

    def test_config_path_is_inside_wine_prefix(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config(
            "cid123", prefix,
            auth_expert="ABUTRON\\MyAuthEA",
            feed_expert="ABUTRON\\MyFeedEA",
            feed_symbol="EURUSD", feed_period="M5")

        assert path.startswith(prefix)
        assert "MQL5" in path
        assert "Config" in path

    def test_auth_only_without_feed(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config(
            "cid123", prefix,
            auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
            feed_expert=None, feed_symbol=None, feed_period=None)

        content = Path(path).read_text(encoding="utf-8")
        assert "Expert=ABUTRON\\AbutronFPMarketsNativeFeedEA" in content
        assert "Template=" not in content
        assert "Symbol=" not in content

    def test_feed_requires_symbol(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert=None,
                                      feed_expert="ABUTRON\\MyFeedEA",
                                      feed_symbol=None, feed_period="H1")
            assert False, "Should have rejected missing feed_symbol"
        except Mt5Error:
            pass

    def test_feed_requires_period(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert=None,
                                      feed_expert="ABUTRON\\MyFeedEA",
                                      feed_symbol="EURUSD", feed_period=None)
            assert False, "Should have rejected missing feed_period"
        except Mt5Error:
            pass

    def test_rejects_invalid_period(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert=None,
                                      feed_expert="ABUTRON\\MyFeedEA",
                                      feed_symbol="EURUSD", feed_period="INVALID")
            assert False, "Should have rejected invalid period"
        except Mt5Error:
            pass

    def test_rejects_newline_injection_in_expert(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        for field in ("auth_expert", "feed_expert"):
            try:
                kwargs = {"auth_expert": None, "feed_expert": None,
                          "feed_symbol": None, "feed_period": None}
                kwargs[field] = "ABUTRON\\EA\n[Common]\r\n"
                if field == "feed_expert":
                    kwargs["feed_symbol"] = "EURUSD"
                    kwargs["feed_period"] = "H1"
                mgr._write_startup_config("cid123", prefix, **kwargs)
                assert False, f"Should have rejected newline in {field}"
            except Mt5Error:
                pass

    def test_rejects_path_traversal(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert="..\\..\\evil",
                                      feed_expert=None, feed_symbol=None,
                                      feed_period=None)
            assert False, "Should have rejected path traversal"
        except Mt5Error:
            pass

    def test_rejects_absolute_drive_path(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert="C:\\Windows\\evil.ex5",
                                      feed_expert=None, feed_symbol=None,
                                      feed_period=None)
            assert False, "Should have rejected absolute drive path"
        except Mt5Error:
            pass


class TestChartTemplateWriting:
    """The _write_feed_chart_template method must create a .tpl file."""

    def test_creates_template_file(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        name = mgr._write_feed_chart_template(
            "cid123", prefix,
            "ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
            "EURUSD.r", "H1")

        assert name == "ABUTRON_AbutronFPMarketsNativeFeedParallelV305"
        template_dir = os.path.join(
            prefix, "drive_c", "Program Files",
            "MetaTrader 5", "MQL5", "Profiles", "Templates")
        template_path = os.path.join(template_dir, name + ".tpl")
        assert os.path.isfile(template_path)
        # Verify binary structure
        data = Path(template_path).read_bytes()
        assert len(data) >= 12  # version + count + type


class TestWinePathConversion:
    """The _convert_to_wine_path method must produce Windows-compatible paths."""

    def test_converts_prefix_path_to_c_drive(self, tmp_path):
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)
        linux_path = os.path.join(
            prefix, "drive_c", "Program Files",
            "MetaTrader 5", "MQL5", "Config", "test.cfg")
        os.makedirs(os.path.dirname(linux_path), exist_ok=True)

        result = Mt5TerminalManager._convert_to_wine_path(linux_path, prefix)
        assert result.startswith("C:\\")
        assert "MetaTrader 5" in result

    def test_converts_outside_path_to_z_drive(self, tmp_path):
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)
        linux_path = str(tmp_path / "outside" / "file.cfg")
        os.makedirs(os.path.dirname(linux_path), exist_ok=True)

        result = Mt5TerminalManager._convert_to_wine_path(linux_path, prefix)
        assert result.startswith("Z:\\")


class TestLaunchCommandConstruction:
    """The launch() method must use /config:<wine_path> syntax."""

    def _make_manager(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        return mgr

    @patch("abutron.feeds.mt5_terminal.subprocess.Popen")
    @patch("abutron.feeds.mt5_terminal.subprocess.run")
    @patch.object(Mt5TerminalManager, "_stage_worker_files")
    def test_launch_with_auth_and_feed_uses_colon_syntax(self, mock_stage, mock_run, mock_popen, tmp_path):
        # winepath is not available in the test environment; let the
        # fallback path conversion handle it.
        mock_run.side_effect = OSError("winepath not available")
        mgr = self._make_manager(tmp_path)
        mgr.ensure_prefix = MagicMock(return_value=str(tmp_path / "prefix-test"))
        os.makedirs(str(tmp_path / "prefix-test" / "drive_c" / "Program Files" /
                        "MetaTrader 5" / "MQL5" / "Files"), exist_ok=True)

        mgr.launch("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                   auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
                   feed_expert="ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
                   feed_symbol="EURUSD.r",
                   feed_period="H1")

        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        config_args = [arg for arg in cmd if arg.startswith("/config")]
        assert len(config_args) == 1
        assert config_args[0].startswith("/config:")
        # The path after /config: should be a Windows-style path (C:\ or Z:\)
        config_path = config_args[0][len("/config:"):]
        assert config_path.startswith(("C:\\", "Z:\\"))

    @patch("abutron.feeds.mt5_terminal.subprocess.Popen")
    @patch.object(Mt5TerminalManager, "_stage_worker_files")
    def test_launch_without_experts_omits_config(self, mock_stage, mock_popen, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.ensure_prefix = MagicMock(return_value=str(tmp_path / "prefix-test"))
        os.makedirs(str(tmp_path / "prefix-test" / "drive_c" / "Program Files" /
                        "MetaTrader 5" / "MQL5" / "Files"), exist_ok=True)

        mgr.launch("cid123", "FP Markets", 1000, "FPMarketsSC-Demo")

        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        assert not any(arg.startswith("/config") for arg in cmd)

    @patch.object(Mt5TerminalManager, "launch")
    def test_restart_passes_all_params(self, mock_launch, tmp_path):
        mgr = self._make_manager(tmp_path)
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        str(tmp_path / "prefix-test"),
                        str(tmp_path / "export"))
        term.proc = None
        mgr._terminals["cid123"] = term

        mgr.restart("cid123",
                    auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
                    feed_expert="ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
                    feed_symbol="EURUSD.r",
                    feed_period="H1")

        mock_launch.assert_called_once()
        _, kwargs = mock_launch.call_args
        assert kwargs.get("auth_expert") == "ABUTRON\\AbutronFPMarketsNativeFeedEA"
        assert kwargs.get("feed_expert") == "ABUTRON\\AbutronFPMarketsNativeFeedParallelV305"
        assert kwargs.get("feed_symbol") == "EURUSD.r"
        assert kwargs.get("feed_period") == "H1"


class TestValidateBeforeRestart:
    """validate_before_restart must check config without stopping the terminal."""

    def test_valid_config_passes(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        mgr.validate_before_restart(
            "cid123",
            auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
            feed_expert="ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
            feed_symbol="EURUSD.r",
            feed_period="H1")
        # No exception raised

    def test_invalid_period_raises(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        try:
            mgr.validate_before_restart(
                "cid123",
                feed_expert="ABUTRON\\MyFeedEA",
                feed_symbol="EURUSD",
                feed_period="BAD_PERIOD")
            assert False, "Should have raised for invalid period"
        except Mt5Error:
            pass

    def test_missing_symbol_raises(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        try:
            mgr.validate_before_restart(
                "cid123",
                feed_expert="ABUTRON\\MyFeedEA",
                feed_symbol=None,
                feed_period="H1")
            assert False, "Should have raised for missing symbol"
        except Mt5Error:
            pass

    def test_control_chars_raise(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        try:
            mgr.validate_before_restart(
                "cid123",
                auth_expert="ABUTRON\\EA\n[Common]\r\n")
            assert False, "Should have raised for newline injection"
        except Mt5Error:
            pass


class TestValidPeriods:
    """VALID_MT5_PERIODS must contain exactly the 21 MT5 timeframes."""

    def test_contains_all_periods(self):
        # MT5 supports 22 timeframes (PERIOD_ enum)
        assert len(VALID_MT5_PERIODS) == 22
        expected = {"M1","M2","M3","M4","M5","M6","M10","M12","M15","M20",
                    "M30","M45","H1","H2","H3","H4","H6","H8","H12","D1","W1","MN1"}
        assert VALID_MT5_PERIODS == expected
