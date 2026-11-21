"""Tests for Mt5TerminalManager /config StartupExpert support.

Verifies that the startup-expert parameter produces a valid MT5 /config
file with a [Startup] Expert section, and that the launch command includes
/config <path> when the parameter is provided (and omits it otherwise).
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

    def test_writes_ini_with_startup_expert(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        # Use a prefix directory that looks like a Wine prefix
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config("cid123", prefix,
                                         "AbutronFPMarketsNativeFeedParallelV305")

        assert os.path.isfile(path)
        content = Path(path).read_text(encoding="utf-8")
        assert "[Startup]" in content
        assert "Expert=AbutronFPMarketsNativeFeedParallelV305" in content

    def test_config_path_is_inside_wine_prefix(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config("cid123", prefix,
                                         "MyEA")

        assert path.startswith(prefix)
        assert "MQL5" in path
        assert "Config" in path

    def test_rejects_path_separators_in_expert_name(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        for bad in ["../evil", "sub/dir/EA", "sub\\dir\\EA"]:
            try:
                mgr._write_startup_config("cid123", prefix, bad)
                assert False, f"Should have rejected {bad}"
            except Mt5Error:
                pass

    def test_rejects_empty_expert_name(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix, "")
            assert False, "Should have rejected empty expert name"
        except Mt5Error:
            pass


class TestLaunchCommandConstruction:
    """The launch() method must include /config when startup_expert is set."""

    def _make_manager(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        return mgr

    @patch("abutron.feeds.mt5_terminal.subprocess.Popen")
    @patch.object(Mt5TerminalManager, "_stage_worker_files")
    def test_launch_with_startup_expert_includes_config(self, mock_stage, mock_popen, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.ensure_prefix = MagicMock(return_value=str(tmp_path / "prefix-test"))
        os.makedirs(str(tmp_path / "prefix-test" / "drive_c" / "Program Files" /
                        "MetaTrader 5" / "MQL5" / "Files"), exist_ok=True)

        mgr.launch("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                   startup_expert="AbutronFPMarketsNativeFeedParallelV305")

        # Verify Popen was called with a command containing /config
        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        assert "/config" in cmd
        # The config path should be the element after /config
        idx = cmd.index("/config")
        config_path = cmd[idx + 1]
        assert os.path.isfile(config_path)
        content = Path(config_path).read_text(encoding="utf-8")
        assert "Expert=AbutronFPMarketsNativeFeedParallelV305" in content

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
        assert "/config" not in cmd

    @patch.object(Mt5TerminalManager, "launch")
    def test_restart_passes_startup_expert(self, mock_launch, tmp_path):
        mgr = self._make_manager(tmp_path)
        # Manually insert a terminal entry so restart can find it
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        str(tmp_path / "prefix-test"),
                        str(tmp_path / "export"))
        term.proc = None  # not running
        mgr._terminals["cid123"] = term

        mgr.restart("cid123", startup_expert="AbutronFPMarketsNativeFeedParallelV305")

        mock_launch.assert_called_once()
        _, kwargs = mock_launch.call_args
        assert kwargs.get("startup_expert") == "AbutronFPMarketsNativeFeedParallelV305"
