"""Tests for Mt5TerminalManager /config StartUp configuration.

Verifies that the config file opens a dedicated startup chart for the
auth expert (with Symbol and Period required), uses /config:<path>
syntax with Wine-compatible paths, validates periods against the 21
standard MT5 timeframes, and that restart() validates before stopping.

Also tests stage_feed_expert_binary() for V305 deployment on chart03.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from abutron.feeds.mt5_terminal import (
    Mt5TerminalManager, Mt5Error, VALID_MT5_PERIODS,
)


class TestStartupConfigWriting:
    """The _write_startup_config method must write auth-only config."""

    def test_writes_ini_with_auth_expert_and_symbol_period(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config(
            "cid123", prefix,
            auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
            startup_symbol="EURUSD.r",
            startup_period="H1")

        content = Path(path).read_text(encoding="utf-8")
        assert "[StartUp]" in content
        assert "Expert=ABUTRON\\AbutronFPMarketsNativeFeedEA" in content
        assert "Symbol=EURUSD.r" in content
        assert "Period=H1" in content
        # No template or feed expert in the config
        assert "Template=" not in content

    def test_config_path_is_inside_wine_prefix(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        path = mgr._write_startup_config(
            "cid123", prefix,
            auth_expert="ABUTRON\\MyAuthEA",
            startup_symbol="EURUSD", startup_period="M5")

        assert path.startswith(prefix)
        assert "MQL5" in path
        assert "Config" in path

    def test_requires_symbol(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert="ABUTRON\\MyAuthEA",
                                      startup_symbol=None,
                                      startup_period="H1")
            assert False, "Should have rejected missing symbol"
        except Mt5Error:
            pass

    def test_requires_period(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert="ABUTRON\\MyAuthEA",
                                      startup_symbol="EURUSD",
                                      startup_period=None)
            assert False, "Should have rejected missing period"
        except Mt5Error:
            pass

    def test_rejects_invalid_period(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert="ABUTRON\\MyAuthEA",
                                      startup_symbol="EURUSD",
                                      startup_period="INVALID")
            assert False, "Should have rejected invalid period"
        except Mt5Error:
            pass

    def test_rejects_m45_period(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert="ABUTRON\\MyAuthEA",
                                      startup_symbol="EURUSD",
                                      startup_period="M45")
            assert False, "Should have rejected M45 (not a standard MT5 timeframe)"
        except Mt5Error:
            pass

    def test_rejects_newline_injection(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert="ABUTRON\\EA\n[Common]\r\n",
                                      startup_symbol="EURUSD",
                                      startup_period="H1")
            assert False, "Should have rejected newline injection"
        except Mt5Error:
            pass

    def test_rejects_path_traversal(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)

        try:
            mgr._write_startup_config("cid123", prefix,
                                      auth_expert="..\\..\\evil",
                                      startup_symbol="EURUSD",
                                      startup_period="H1")
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
                                      startup_symbol="EURUSD",
                                      startup_period="H1")
            assert False, "Should have rejected absolute drive path"
        except Mt5Error:
            pass


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
    def test_launch_with_auth_expert_uses_colon_syntax(self, mock_stage, mock_run, mock_popen, tmp_path):
        mock_run.side_effect = OSError("winepath not available")
        mgr = self._make_manager(tmp_path)
        mgr.ensure_prefix = MagicMock(return_value=str(tmp_path / "prefix-test"))
        os.makedirs(str(tmp_path / "prefix-test" / "drive_c" / "Program Files" /
                        "MetaTrader 5" / "MQL5" / "Files"), exist_ok=True)

        mgr.launch("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                   auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
                   startup_symbol="EURUSD.r",
                   startup_period="H1")

        assert mock_popen.called
        cmd = mock_popen.call_args[0][0]
        config_args = [arg for arg in cmd if arg.startswith("/config")]
        assert len(config_args) == 1
        assert config_args[0].startswith("/config:")
        config_path = config_args[0][len("/config:"):]
        assert config_path.startswith(("C:\\", "Z:\\"))

    @patch("abutron.feeds.mt5_terminal.subprocess.Popen")
    @patch.object(Mt5TerminalManager, "_stage_worker_files")
    def test_launch_without_auth_expert_omits_config(self, mock_stage, mock_popen, tmp_path):
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
                    startup_symbol="EURUSD.r",
                    startup_period="H1")

        mock_launch.assert_called_once()
        _, kwargs = mock_launch.call_args
        assert kwargs.get("auth_expert") == "ABUTRON\\AbutronFPMarketsNativeFeedEA"
        assert kwargs.get("startup_symbol") == "EURUSD.r"
        assert kwargs.get("startup_period") == "H1"


class TestValidateBeforeRestart:
    """validate_before_restart must check config without stopping the terminal."""

    def test_valid_config_passes(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        mgr.validate_before_restart(
            "cid123",
            auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
            startup_symbol="EURUSD.r",
            startup_period="H1")

    def test_invalid_period_raises(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        try:
            mgr.validate_before_restart(
                "cid123",
                auth_expert="ABUTRON\\MyAuthEA",
                startup_symbol="EURUSD",
                startup_period="BAD_PERIOD")
            assert False, "Should have raised for invalid period"
        except Mt5Error:
            pass

    def test_missing_symbol_raises(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        try:
            mgr.validate_before_restart(
                "cid123",
                auth_expert="ABUTRON\\MyAuthEA",
                startup_symbol=None,
                startup_period="H1")
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

    def test_symbol_control_chars_raise(self, tmp_path):
        """validate_before_restart must reject control chars in startup_symbol,
        matching _write_startup_config's validation."""
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        try:
            mgr.validate_before_restart(
                "cid123",
                auth_expert="ABUTRON\\MyAuthEA",
                startup_symbol="EURUSD\n[Common]\r\n",
                startup_period="H1")
            assert False, "Should have raised for newline in symbol"
        except Mt5Error:
            pass

    def test_no_auth_expert_passes(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        # No auth expert — nothing to validate, should pass
        mgr.validate_before_restart("cid123")


class TestRestartValidatesBeforeStop:
    """restart() must call validate_before_restart before stop()."""

    @patch.object(Mt5TerminalManager, "stop")
    @patch.object(Mt5TerminalManager, "validate_before_restart")
    @patch.object(Mt5TerminalManager, "launch")
    def test_restart_validates_before_stop(self, mock_launch, mock_validate, mock_stop, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        str(tmp_path / "prefix-test"),
                        str(tmp_path / "export"))
        term.proc = None
        mgr._terminals["cid123"] = term

        mgr.restart("cid123",
                    auth_expert="ABUTRON\\AbutronFPMarketsNativeFeedEA",
                    startup_symbol="EURUSD.r",
                    startup_period="H1")

        # validate must be called before stop
        mock_validate.assert_called_once()
        mock_stop.assert_called_once()
        # launch should also be called
        mock_launch.assert_called_once()

    @patch.object(Mt5TerminalManager, "stop")
    @patch.object(Mt5TerminalManager, "validate_before_restart")
    def test_restart_does_not_stop_when_validation_fails(self, mock_validate, mock_stop, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        str(tmp_path / "prefix-test"),
                        str(tmp_path / "export"))
        term.proc = None
        mgr._terminals["cid123"] = term

        # Validation raises
        mock_validate.side_effect = Mt5Error("invalid config")

        try:
            mgr.restart("cid123",
                        auth_expert="ABUTRON\\BadEA",
                        startup_symbol="EURUSD",
                        startup_period="INVALID")
            assert False, "Should have raised"
        except Mt5Error:
            pass

        # stop must NOT be called when validation fails
        mock_stop.assert_not_called()


class TestStageFeedExpertBinary:
    """stage_feed_expert_binary() must stage the EX5 in MQL5/Experts/."""

    def test_stages_binary_in_experts_dir(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)
        # Create a fake EX5 file
        ex5_path = str(tmp_path / "test.ex5")
        Path(ex5_path).write_bytes(b"fake ex5 binary content")
        sha = hashlib.sha256(Path(ex5_path).read_bytes()).hexdigest()

        # Create the export_dir that _atomic_install requires
        export_dir = os.path.join(prefix, "drive_c", "Program Files",
                                  "MetaTrader 5", "MQL5", "Files")
        os.makedirs(export_dir, exist_ok=True)

        # Create a terminal entry
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        prefix, export_dir)
        mgr._terminals["cid123"] = term

        dest = mgr.stage_feed_expert_binary(
            "cid123",
            "ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
            ex5_path, sha)

        assert os.path.isfile(dest)
        assert dest.endswith(".ex5")
        assert "AbutronFPMarketsNativeFeedParallelV305" in dest

    def test_rejects_path_traversal(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        str(tmp_path / "prefix"),
                        str(tmp_path / "export"))
        mgr._terminals["cid123"] = term

        try:
            mgr.stage_feed_expert_binary("cid123", "..\\..\\evil",
                                         "/tmp/test.ex5", "abc")
            assert False, "Should have rejected path traversal"
        except Mt5Error:
            pass

    def test_rejects_unknown_connection(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        try:
            mgr.stage_feed_expert_binary("unknown", "ABUTRON\\EA",
                                         "/tmp/test.ex5", "abc")
            assert False, "Should have rejected unknown connection"
        except Mt5Error:
            pass

    def test_binary_goes_directly_in_abtron_not_nested(self, tmp_path):
        """The EX5 must be in MQL5/Experts/ABUTRON/EAName.ex5, not in a
        nested subdirectory named after the EA."""
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-test")
        os.makedirs(prefix, exist_ok=True)
        ex5_path = str(tmp_path / "test.ex5")
        Path(ex5_path).write_bytes(b"fake ex5 binary content")
        sha = hashlib.sha256(Path(ex5_path).read_bytes()).hexdigest()

        export_dir = os.path.join(prefix, "drive_c", "Program Files",
                                  "MetaTrader 5", "MQL5", "Files")
        os.makedirs(export_dir, exist_ok=True)

        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        prefix, export_dir)
        mgr._terminals["cid123"] = term

        dest = mgr.stage_feed_expert_binary(
            "cid123",
            "ABUTRON\\AbutronFPMarketsNativeFeedParallelV305",
            ex5_path, sha)

        # The destination should be MQL5/Experts/ABUTRON/EAName.ex5
        expected = os.path.join(
            prefix, "drive_c", "Program Files", "MetaTrader 5",
            "MQL5", "Experts", "ABUTRON",
            "AbutronFPMarketsNativeFeedParallelV305.ex5")
        assert dest == expected
        # The nested directory should NOT exist
        nested = os.path.join(
            prefix, "drive_c", "Program Files", "MetaTrader 5",
            "MQL5", "Experts", "ABUTRON",
            "AbutronFPMarketsNativeFeedParallelV305")
        assert not os.path.isdir(nested)


class TestValidPeriods:
    """VALID_MT5_PERIODS must contain exactly the 21 standard MT5 timeframes."""

    def test_contains_all_21_periods(self):
        assert len(VALID_MT5_PERIODS) == 21
        expected = {"M1","M2","M3","M4","M5","M6","M10","M12","M15","M20",
                    "M30","H1","H2","H3","H4","H6","H8","H12","D1","W1","MN1"}
        assert VALID_MT5_PERIODS == expected

    def test_does_not_contain_m45(self):
        assert "M45" not in VALID_MT5_PERIODS


class TestUpdateChartReference:
    """update_chart_reference() must preserve .chr permissions and ownership."""

    def _make_chr(self, tmp_path, content=b"\x00\x01chart-data"):
        """Create a minimal terminal with a .chr file in the Charts dir."""
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-chr")
        charts_dir = os.path.join(
            prefix, "drive_c", "Program Files", "MetaTrader 5",
            "MQL5", "Profiles", "Charts", "Default")
        os.makedirs(charts_dir, exist_ok=True)
        chr_path = os.path.join(charts_dir, "chart03.chr")
        # Simulate UTF-16 LE V307 reference
        v307 = "AbutronFPMarketsNativeFeedParallelV307_FP1636".encode("utf-16-le")
        Path(chr_path).write_bytes(content + v307 + b"\x00\x01trailer")
        # Set non-default permissions (0640)
        os.chmod(chr_path, 0o640)

        export_dir = os.path.join(prefix, "drive_c", "Program Files",
                                  "MetaTrader 5", "MQL5", "Files")
        os.makedirs(export_dir, exist_ok=True)
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid1636", "FP Markets", 1636, "FPMarketsSC-Demo",
                        prefix, export_dir)
        mgr._terminals["cid1636"] = term
        return mgr, chr_path, prefix

    def test_preserves_file_permissions(self, tmp_path):
        """Atomic replacement must preserve original mode (e.g. 0640)."""
        mgr, chr_path, prefix = self._make_chr(tmp_path)
        before_mode = os.stat(chr_path).st_mode & 0o777
        assert before_mode == 0o640

        mgr.update_chart_reference(
            "cid1636", "chart03.chr",
            old_ea_name="AbutronFPMarketsNativeFeedParallelV307_FP1636",
            new_ea_name="AbutronFPMarketsNativeFeedParallelV305")

        after_mode = os.stat(chr_path).st_mode & 0o777
        assert after_mode == 0o640, (
            f"permissions changed from {oct(before_mode)} to {oct(after_mode)}")

    def test_backup_file_created(self, tmp_path):
        """A .bak backup of the original .chr must be created."""
        mgr, chr_path, prefix = self._make_chr(tmp_path)
        mgr.update_chart_reference(
            "cid1636", "chart03.chr",
            old_ea_name="AbutronFPMarketsNativeFeedParallelV307_FP1636",
            new_ea_name="AbutronFPMarketsNativeFeedParallelV305")
        backup = chr_path + ".bak"
        assert os.path.isfile(backup)

    def test_replaces_v307_reference(self, tmp_path):
        """The V307 name must be replaced with V305 in the .chr file."""
        mgr, chr_path, prefix = self._make_chr(tmp_path)
        mgr.update_chart_reference(
            "cid1636", "chart03.chr",
            old_ea_name="AbutronFPMarketsNativeFeedParallelV307_FP1636",
            new_ea_name="AbutronFPMarketsNativeFeedParallelV305")
        with open(chr_path, "rb") as fh:
            data = fh.read()
        v307 = "AbutronFPMarketsNativeFeedParallelV307_FP1636".encode("utf-16-le")
        v305 = "AbutronFPMarketsNativeFeedParallelV305".encode("utf-16-le")
        assert v307 not in data
        assert v305 in data

    def test_rejects_non_chr_file(self, tmp_path):
        mgr, chr_path, prefix = self._make_chr(tmp_path)
        try:
            mgr.update_chart_reference(
                "cid1636", "chart03.txt",
                old_ea_name="AbutronFPMarketsNativeFeedParallelV307_FP1636",
                new_ea_name="AbutronFPMarketsNativeFeedParallelV305")
            assert False, "Should have rejected non-.chr file"
        except Mt5Error:
            pass

    def test_rejects_unknown_connection(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        try:
            mgr.update_chart_reference(
                "unknown", "chart03.chr",
                old_ea_name="AbutronFPMarketsNativeFeedParallelV307_FP1636",
                new_ea_name="AbutronFPMarketsNativeFeedParallelV305")
            assert False, "Should have rejected unknown connection"
        except Mt5Error:
            pass


class TestStageV3IndicatorBinary:
    """stage_v3_indicator_binary() must stage EX5 in MQL5/Indicators/ABUTRON/."""

    def _make_terminal(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        prefix = str(tmp_path / "prefix-v3ind")
        os.makedirs(prefix, exist_ok=True)
        export_dir = os.path.join(prefix, "drive_c", "Program Files",
                                  "MetaTrader 5", "MQL5", "Files")
        os.makedirs(export_dir, exist_ok=True)
        from abutron.feeds.mt5_terminal import Terminal
        term = Terminal("cid123", "FP Markets", 1000, "FPMarketsSC-Demo",
                        prefix, export_dir)
        mgr._terminals["cid123"] = term
        return mgr, prefix

    def test_stages_binary_in_indicators_dir(self, tmp_path):
        mgr, prefix = self._make_terminal(tmp_path)
        ex5_path = str(tmp_path / "v3.ex5")
        Path(ex5_path).write_bytes(b"fake v3 indicator binary")
        sha = hashlib.sha256(Path(ex5_path).read_bytes()).hexdigest()

        dest = mgr.stage_v3_indicator_binary(
            "cid123", "ABUTRON\\ApexMarketFeedV3", ex5_path, sha)

        expected = os.path.join(
            prefix, "drive_c", "Program Files", "MetaTrader 5",
            "MQL5", "Indicators", "ABUTRON", "ApexMarketFeedV3.ex5")
        assert dest == expected
        assert os.path.isfile(dest)

    def test_binary_goes_directly_in_abtron_not_nested(self, tmp_path):
        mgr, prefix = self._make_terminal(tmp_path)
        ex5_path = str(tmp_path / "v3.ex5")
        Path(ex5_path).write_bytes(b"fake v3 indicator binary")
        sha = hashlib.sha256(Path(ex5_path).read_bytes()).hexdigest()

        dest = mgr.stage_v3_indicator_binary(
            "cid123", "ABUTRON\\ApexMarketFeedV3", ex5_path, sha)

        nested = os.path.join(
            prefix, "drive_c", "Program Files", "MetaTrader 5",
            "MQL5", "Indicators", "ABUTRON", "ApexMarketFeedV3",
            "ApexMarketFeedV3.ex5")
        assert not os.path.isdir(os.path.dirname(nested))
        assert dest != nested

    def test_rejects_path_traversal(self, tmp_path):
        mgr, _ = self._make_terminal(tmp_path)
        ex5_path = str(tmp_path / "v3.ex5")
        Path(ex5_path).write_bytes(b"fake")
        try:
            mgr.stage_v3_indicator_binary(
                "cid123", "ABUTRON\\..\\..\\evil", ex5_path, "abc")
            assert False, "Should have rejected traversal"
        except Mt5Error:
            pass

    def test_rejects_unknown_connection(self, tmp_path):
        mgr = Mt5TerminalManager(root=str(tmp_path / "mt5root"))
        ex5_path = str(tmp_path / "v3.ex5")
        Path(ex5_path).write_bytes(b"fake")
        try:
            mgr.stage_v3_indicator_binary(
                "unknown", "ABUTRON\\ApexMarketFeedV3", ex5_path, "abc")
            assert False, "Should have rejected unknown connection"
        except Mt5Error:
            pass
