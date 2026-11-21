"""Tests for Mt5TerminalManager /config StartUpExpert support.

Verifies that the startup-expert parameter produces a valid MT5 /config
file with a [StartUp] section containing Expert, Symbol, and Period,
and that the launch command uses the documented /config:<path> syntax.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from abutron.feeds.mt5_terminal import Mt5TerminalManager, Mt5Error


class TestStartupConfigWriting:
    """The _write_startup_config method must produce a valid MT5 config."""

    def test_writes_ini_with_start_up_section(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config(
            "cid123", prefix,
            "ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
            "EURUSD.r", "H1")

        assert os.path.isfile(path)
        content = Path(path).read_text(encoding="utf-8")
        assert "[StartUp]" in content
        assert "Expert=ABUTRON\\AbutronFPMarketsNativeFeedParallelV305" in content
        assert "Symbol=EURUSD.r" in content
        assert "Period=H1" in content

    def test_config_path_is_inside_wine_prefix(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config(
            "cid123", prefix,
            "ABUTRON\\MyEA", "EURUSD", "M1")

        assert path.startswith(prefix)
        assert "MQL5" in path
        assert "Config" in path

    def test_rejects_absolute_path_in_expert_name(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        for bad in ["..\\..\\evil", "/etc/passwd", "C:\\Windows\\evil.ex5"]:
            try:
                mgr._write_startup_config("cid123", prefix, bad, "EURUSD", "H1")
                assert False, f"Should have rejected {bad}"
            except Mt5Error:
                pass

    def test_rejects_newline_injection_in_expert_name(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      "ABUTRON\\EA\n[Common]\r\n",
                                      "EURUSD", "H1")
            assert False, "Should have rejected newline injection"
        except Mt5Error:
            pass

    def test_rejects_control_chars_in_expert_name(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      "ABUTRON\\EA\x00bad",
                                      "EURUSD", "H1")
            assert False, "Should have rejected control characters"
        except Mt5Error:
            pass

    def test_rejects_missing_symbol(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      "ABUTRON\\MyEA", None, "H1")
            assert False, "Should have rejected missing symbol"
        except Mt5Error:
            pass

    def test_rejects_missing_period(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      "ABUTRON\\MyEA", "EURUSD", None)
            assert False, "Should have rejected missing period"
        except Mt5Error:
            pass

    def test_rejects_newline_in_symbol(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      "ABUTRON\\MyEA",
                                      "EURUSD\nPeriod=M5", "H1")
            assert False, "Should have rejected newline in symbol"
        except Mt5Error:
            pass

    def test_rejects_empty_expert_name(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix, "", "EURUSD", "H1")
            assert False, "Should have rejected empty expert name"
        except Mt5Error:
            pass


class TestLaunchCommandConstruction:
    """The launch() method must use /config:<path> syntax when startup_expert is set."""

    def _make_manager(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        return mgr

    @patch("abutron.feeds.mt5_terminal.subprocess.Popen")
    @patch.object(Mt5TerminalManager, "_stage_worker_files")
    def test_launch_with_startup_expert_uses_colon_syntax(self, mock_stage, mock_popen, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.ensure_prefix = MagicMock(return_value=str(tmp_path / "prefix-test"))
        os.makedirs(str(tmp_path / "prefix-test" / "drive_c" / "Program Files" /
                        "MetaTrader 5" / "MQL5" / "Files"), exist_ok=True)

        mgr.launch("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                   startup_expert="ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
                   startup_symbol="EURUSD.r",
                   startup_period="H1")

        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        # Must use /config:<path> colon syntax, not /config <path>
        config_args = [arg for arg in cmd if arg.startswith("/config")]
        assert len(config_args) == 1
        assert config_args[0].startswith("/config:")
        # The path after /config: should exist
        config_path = config_args[0][len("/config:"):]
        assert os.path.isfile(config_path)
        content = Path(config_path).read_text(encoding="utf-8")
        assert "[StartUp]" in content
        assert "Expert=ABUTRON\\AbutronFPMarketsNativeFeedParallelV305" in content
        assert "Symbol=EURUSD.r" in content
        assert "Period=H1" in content

    @patch("abutron.feeds.mt5_terminal.subprocess.Popen")
    @patch.object(Mt5TerminalManager, "_stage_worker_files")
    def test_launch_without_startup_expert_omits_config(self, mock_stage, mock_popen, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.ensure_prefix = MagicMock(return_value=str(tmp_path / "prefix-test"))
        os.makedirs(str(tmp_path / "prefix-test" / "drive_c" / "Program Files" /
                        "MetaTrader 5" / "MQL5" / "Files"), exist_ok=True)

        mgr.launch("cid123", "FP Markets", 1000, "FPMarketsSC-Demo")

        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        assert not any(arg.startswith("/config") for arg in cmd)

    @patch.object(Mt5TerminalManager, "launch")
    def test_restart_passes_all_startup_params(self, mock_launch, tmp_path):
        mgr = self._make_manager(tmp_path)
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        str(tmp_path / "prefix-test"),
                        str(tmp_path / "export"))
        term.proc = None
        mgr._terminals["cid123"] = term

        mgr.restart("cid123",
                    startup_expert="ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
                    startup_symbol="EURUSD.r",
                    startup_period="H1")

        mock_launch.assert_called_once()
        _, kwargs = mock_launch.call_args
        assert kwargs.get("startup_expert") == "ABUTRON\\AbutronFPMarketsNativeFeedParallelV305"
        assert kwargs.get("startup_symbol") == "EURUSD.r"
        assert kwargs.get("startup_period") == "H1"

    @patch.object(Mt5TerminalManager, "launch")
    def test_restart_without_startup_expert(self, mock_launch, tmp_path):
        mgr = self._make_manager(tmp_path)
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        str(tmp_path / "prefix-test"),
                        str(tmp_path / "export"))
        term.proc = None
        mgr._terminals["cid123"] = term

        mgr.restart("cid123")

        mock_launch.assert_called_once()
        _, kwargs = mock_launch.call_args
        assert kwargs.get("startup_expert") is None
        assert kwargs.get("startup_symbol") is None
        assert kwargs.get("startup_period") is None
