"""Wine-based MT5 terminal management on Ubuntu.

Each broker account gets an isolated Wine prefix + terminal profile. The
worker runs `wine Terminal64.exe` headless, uses the MQL5 Files directory
for native exports, and the terminal's Files are read back by the reader.

This module manages: prefix init, terminal launch/stop, file transfer to
the MQL5/Files drop zone, and health heartbeats. It does NOT place orders
— the native EA inside the terminal is the only order authority.

Startup configuration:
MT5 supports exactly one Expert per chart. The [StartUp] section of the
MT5 /config file opens a temporary startup chart with the auth expert
(``ABUTRON\\AbutronFPMarketsNativeFeedEA``) for heartbeat authentication.

V305 is deployed on a SEPARATE chart (chart03) via its existing .chr
chart-state file. The .chr file references the EA by name; when MT5
restarts, it loads the EA binary from MQL5/Experts/ by name. Deploying
V305 therefore requires: (1) staging the V305 EX5 binary in the Experts
directory, and (2) restarting the terminal. The /config file only
controls the auth expert's startup chart — it does not attach V305.

The correct production chart layout is:
  - Startup chart:  auth EA (heartbeat)  — via [StartUp] Expert
  - Chart01:        order bridge (unchanged)
  - Chart03:        V305 feed EA (via existing .chr file + staged binary)
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import (MT5_EXE, MT5_EXPORT_DIR, MT5_ROOT, WINE_PREFIX)
from ..crypto import make_nonce

# MT5 supports exactly 21 timeframes (ENUM_TIMEFRAMES). M45 is NOT
# a standard MT5 timeframe and must not be in the allowlist.
VALID_MT5_PERIODS = frozenset({
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20",
    "M30", "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1",
    "W1", "MN1",
})


class Mt5Error(Exception):
    pass


@dataclass
class Terminal:
    connection_id: str
    broker: str
    login: int
    server: str
    wine_prefix: str
    export_dir: str
    proc: Optional[subprocess.Popen] = None
    started_at: float = 0.0
    health_ts: str = ""
    external_evidence_only: bool = False
    authenticated_at: float = 0.0
    authenticated_nonce: str = ""

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class Mt5TerminalManager:
    """Launches and supervises per-account Wine/MT5 terminals."""

    def __init__(self, root: str = MT5_ROOT):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._terminals: Dict[str, Terminal] = {}

    # ------------------------------------------------------------------
    # Wine prefix lifecycle
    # ------------------------------------------------------------------
    def ensure_prefix(self, connection_id: str) -> str:
        prefix = os.path.join(self.root, f"prefix-{connection_id}")
        if not os.path.exists(os.path.join(prefix, "dosdevices")):
            self._run(["wine", "start", "/unix", prefix, "cmd", "/c",
                       "echo init"], env={"WINEPREFIX": prefix},
                      timeout=120, check=False)
        return prefix

    def _wine_env(self, prefix: str) -> Dict[str, str]:
        env = dict(os.environ)
        env["WINEPREFIX"] = prefix
        env["WINEDEBUG"] = "-all"
        env["DISPLAY"] = os.getenv("DISPLAY", "")
        return env

    def _run(self, cmd: List[str], *, env: Optional[Dict[str, str]] = None,
             timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
        try:
            cp = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                timeout=timeout)
        except FileNotFoundError as e:
            raise Mt5Error(f"command not found: {cmd[0]} (install wine)") from e
        if check and cp.returncode != 0:
            raise Mt5Error(f"command failed rc={cp.returncode}: {' '.join(cmd)}\n"
                           f"{cp.stderr[:500]}")
        return cp

    # ------------------------------------------------------------------
    # Terminal lifecycle
    # ------------------------------------------------------------------
    def launch(self, connection_id: str, broker: str, login: int,
               server: str, *, headless: bool = True,
               auth_expert: Optional[str] = None,
               startup_symbol: Optional[str] = None,
               startup_period: Optional[str] = None) -> Terminal:
        if connection_id in self._terminals and self._terminals[connection_id].is_running:
            raise Mt5Error("terminal already running")
        prefix = self.ensure_prefix(connection_id)
        export_dir = os.path.join(prefix, "drive_c", "Program Files",
                                  "MetaTrader 5", "MQL5", "Files")
        os.makedirs(export_dir, exist_ok=True)
        term = Terminal(connection_id, broker, login, server, prefix, export_dir)
        self._terminals[connection_id] = term

        # Source/config staging is not proof of an EA attachment or broker login.
        # Do not claim a session CONNECTED until a fresh signed native snapshot
        # has passed CRM verification.
        self._stage_worker_files(connection_id, export_dir)

        env = self._wine_env(prefix)
        cmd = ["wine", MT5_EXE]
        if headless:
            cmd += ["/headless", "/nosplash"]
        # The /config file opens a temporary startup chart with the auth
        # expert for heartbeat authentication. MT5 supports exactly one
        # Expert per chart — V305 is NOT attached here. V305 runs on a
        # separate chart (chart03) via its existing .chr file and the
        # staged EX5 binary in MQL5/Experts/.
        #
        # When auth_expert is set, startup_symbol and startup_period are
        # REQUIRED: they define which chart the auth expert runs on.
        # Without them, MT5 attaches the EA to the first chart of the
        # current profile, which could be an order-bridge chart.
        #
        # Command-line syntax is /config:<path> (colon, no space).
        # The path is converted to a Windows-compatible path via winepath.
        config_path = None
        if auth_expert:
            config_path = self._write_startup_config(
                connection_id, prefix, auth_expert,
                startup_symbol, startup_period)
            wine_config_path = self._convert_to_wine_path(config_path, prefix)
            cmd.append(f"/config:{wine_config_path}")
        term.proc = subprocess.Popen(cmd, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        term.started_at = time.time()
        term.health_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return term

    def stage_feed_expert_binary(self, connection_id: str,
                                 expert_name: str,
                                 ex5_path: str,
                                 expected_sha: str) -> str:
        """Stage a feed EA binary directly in MQL5/Experts/ABUTRON/.

        This is the mechanism for deploying V305 on chart03. The existing
        chart03 .chr file references the EA by name; when MT5 restarts, it
        loads the EA binary from MQL5/Experts/ by name. Staging the corrected
        EX5 binary here ensures the next terminal initialization loads V305
        instead of the older V307.

        The binary is placed directly in MQL5/Experts/ABUTRON/ as
        <EAName>.ex5 — NOT in a nested subdirectory. MT5's Experts loader
        searches MQL5/Experts/ and subdirectories, but the .chr file
        references the EA by its full relative path. Installing the binary
        in the correct location ensures MT5 loads it.

        For 1636, whose chart03 references V307
        (``AbutronFPMarketsNativeFeedParallelV307_FP1636.ex5``), use
        update_chart_reference() to change the chart reference to V305
        before or after staging.

        Args:
            connection_id: The terminal connection ID.
            expert_name: Backslash-relative expert path (e.g.
                "ABUTRON\\AbutronFPMarketsNativeFeedParallelV305").
            ex5_path: Linux path to the verified EX5 binary.
            expected_sha: Expected SHA-256 of the binary.

        Returns the destination path of the staged binary.
        """
        # Validate expert_name
        self._validate_expert_name(expert_name, "expert_name")

        term = self._terminals.get(connection_id)
        if term is None:
            raise Mt5Error(f"terminal not found: {connection_id}")

        # Resolve the Experts directory inside the terminal prefix.
        # The binary goes DIRECTLY in MQL5/Experts/ABUTRON/, NOT in a
        # nested subdirectory. The expert_name "ABUTRON\\EAName" means
        # the binary is MQL5/Experts/ABUTRON/EAName.ex5.
        experts_dir = os.path.join(
            term.wine_prefix, "drive_c", "Program Files",
            "MetaTrader 5", "MQL5", "Experts")
        # expert_name is "ABUTRON\\AbutronFPMarketsNativeFeedParallelV305"
        # — the directory part is "ABUTRON", the binary filename is the
        # basename + ".ex5". The binary sits directly in ABUTRON/, not
        # in a subdirectory named after the EA.
        dir_parts = expert_name.split("\\")
        dest_dir = os.path.join(experts_dir, *dir_parts[:-1])
        os.makedirs(dest_dir, exist_ok=True)
        ea_basename = dir_parts[-1]
        dest_path = os.path.join(dest_dir, ea_basename + ".ex5")

        self._atomic_install(ex5_path, dest_path, term.export_dir,
                             expected_sha=expected_sha)
        return dest_path

    def stage_v3_indicator_binary(self, connection_id: str,
                                  indicator_name: str,
                                  ex5_path: str,
                                  expected_sha: str) -> str:
        """Stage the V3 indicator EX5 binary in MQL5/Indicators/ABUTRON/.

        ApexMarketFeedV3 is a custom indicator that must be loaded from
        MQL5/Indicators/ for iCustom() to find it. The V305 EA attaches it
        to chart03 via ChartIndicatorAdd() using the same mechanism as
        the V101 history indicator (EnsureFPHistoryIndicator).

        The binary is placed directly in MQL5/Indicators/ABUTRON/ as
        <IndicatorName>.ex5 — NOT in a nested subdirectory. MT5's
        Indicators loader searches MQL5/Indicators/ and subdirectories.

        Args:
            connection_id: The terminal connection ID.
            indicator_name: Backslash-relative indicator path (e.g.
                "ABUTRON\\ApexMarketFeedV3").
            ex5_path: Linux path to the verified EX5 binary.
            expected_sha: Expected SHA-256 of the binary.

        Returns the destination path of the staged binary.
        """
        # Validate indicator_name (same rules as expert names)
        self._validate_expert_name(indicator_name, "indicator_name")

        term = self._terminals.get(connection_id)
        if term is None:
            raise Mt5Error(f"terminal not found: {connection_id}")

        # Resolve the Indicators directory inside the terminal prefix.
        # The binary goes DIRECTLY in MQL5/Indicators/ABUTRON/.
        indicators_dir = os.path.join(
            term.wine_prefix, "drive_c", "Program Files",
            "MetaTrader 5", "MQL5", "Indicators")
        dir_parts = indicator_name.split("\\")
        dest_dir = os.path.join(indicators_dir, *dir_parts[:-1])
        os.makedirs(dest_dir, exist_ok=True)
        ind_basename = dir_parts[-1]
        dest_path = os.path.join(dest_dir, ind_basename + ".ex5")

        self._atomic_install(ex5_path, dest_path, term.export_dir,
                             expected_sha=expected_sha)
        return dest_path

    def update_chart_reference(self, connection_id: str,
                               chart_file: str,
                               old_ea_name: str,
                               new_ea_name: str) -> None:
        """Update a chart .chr file's EA reference from old to new.

        Some accounts (e.g. 1636) have chart03 referencing a different EA
        name (e.g. ``AbutronFPMarketsNativeFeedParallelV307_FP1636.ex5``)
        rather than the standard V305 name. This method updates the
        .chr file's binary reference so MT5 loads the corrected EA on
        the next chart restoration.

        The .chr file is a binary chart-state file. This method performs
        a byte-level search-and-replace of the old EA name string with
        the new EA name. The .chr file is backed up before modification.

        Args:
            connection_id: The terminal connection ID.
            chart_file: The .chr filename (e.g. "chart03.chr").
            old_ea_name: The EA name currently referenced in the .chr.
            new_ea_name: The EA name to replace it with.
        """
        term = self._terminals.get(connection_id)
        if term is None:
            raise Mt5Error(f"terminal not found: {connection_id}")

        if not chart_file or not chart_file.endswith(".chr"):
            raise Mt5Error("chart_file must be a .chr file")

        # Validate EA names
        self._validate_expert_name(old_ea_name, "old_ea_name")
        self._validate_expert_name(new_ea_name, "new_ea_name")

        # Resolve the chart file path under MQL5/Profiles/Charts/
        charts_dir = os.path.join(
            term.wine_prefix, "drive_c", "Program Files",
            "MetaTrader 5", "MQL5", "Profiles", "Charts", "Default")
        chart_path = os.path.join(charts_dir, chart_file)

        if not os.path.isfile(chart_path):
            raise Mt5Error(f"chart file not found: {chart_path}")

        # Read the .chr file
        with open(chart_path, "rb") as fh:
            data = fh.read()

        # Check if the old EA name exists in the .chr file
        old_bytes = old_ea_name.encode("utf-16-le")
        if old_bytes not in data:
            # Also try ANSI encoding
            old_bytes = old_ea_name.encode("ascii")
            if old_bytes not in data:
                raise Mt5Error(
                    f"old EA name '{old_ea_name}' not found in {chart_file}")

        # Back up the original .chr file
        backup_path = chart_path + ".bak"
        shutil.copy2(chart_path, backup_path)

        # Replace old EA name with new EA name
        new_bytes = new_ea_name.encode("utf-16-le")
        data = data.replace(old_bytes, new_bytes)

        # Write back atomically, preserving original permissions and
        # ownership so the replacement does not tighten file mode (e.g.
        # from 0640 to 0600) or drop ownership.
        orig_stat = os.stat(chart_path)
        fd, tmp_path = tempfile.mkstemp(prefix=".chart", dir=charts_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp_path, orig_stat.st_mode & 0o777)
            os.chown(tmp_path, orig_stat.st_uid, orig_stat.st_gid)
            os.replace(tmp_path, chart_path)
            self._qa_fsync_dir(charts_dir)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    def _validate_expert_name(expert_name: str, field: str) -> None:
        """Validate an expert name for safe use in paths and config.

        Rejects control characters, path traversal, forward slashes,
        absolute paths, and drive-letter paths.

        Raises Mt5Error if the name is invalid.
        """
        if not expert_name:
            raise Mt5Error(f"{field} is required")
        if any(c in expert_name for c in ("\r", "\n", "\x00")) or any(ord(c) < 32 for c in expert_name):
            raise Mt5Error(f"{field} must not contain control characters")
        if "/" in expert_name or expert_name.startswith("\\"):
            raise Mt5Error(f"{field} must be a backslash-relative path (e.g. ABUTRON\\EAName)")
        if ".." in expert_name:
            raise Mt5Error(f"{field} must not contain path traversal (..)")
        if re.match(r"^[A-Za-z]:[\\/]", expert_name):
            raise Mt5Error(f"{field} must not be an absolute drive-letter path")

    @staticmethod
    def _validate_symbol(symbol: str, field: str) -> None:
        """Validate a symbol name for safe use in config files.

        Rejects control characters, newlines, and null bytes.

        Raises Mt5Error if the symbol is invalid.
        """
        if not symbol:
            raise Mt5Error(f"{field} is required")
        if any(c in symbol for c in ("\r", "\n", "\x00")) or any(ord(c) < 32 for c in symbol):
            raise Mt5Error(f"{field} must not contain control characters")

    @staticmethod
    def _validate_period(period: str, field: str) -> None:
        """Validate a timeframe against the MT5 ENUM_TIMEFRAMES set.

        Raises Mt5Error if the period is invalid.
        """
        if not period:
            raise Mt5Error(f"{field} is required")
        if any(c in period for c in ("\r", "\n", "\x00")) or any(ord(c) < 32 for c in period):
            raise Mt5Error(f"{field} must not contain control characters")
        if period not in VALID_MT5_PERIODS:
            raise Mt5Error(
                f"{field} '{period}' is not a valid MT5 timeframe. "
                f"Valid periods: {sorted(VALID_MT5_PERIODS)}")

    @staticmethod
    def _convert_to_wine_path(linux_path: str, prefix: str) -> str:
        """Convert a Linux filesystem path to a Wine-compatible Windows path.

        MT5 runs inside Wine and expects Windows-style paths. The Wine
        prefix maps the Linux root to ``Z:\\``. This function converts
        a Linux absolute path to its ``Z:\\...`` equivalent, using
        ``winepath`` when available for accuracy, and falling back to
        the prefix-relative mapping.

        For paths inside the Wine prefix (drive_c/...), the conversion
        maps to ``C:\\...``. For paths outside, ``Z:\\...`` is used.
        """
        try:
            cp = subprocess.run(
                ["winepath", "-w", linux_path],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "WINEPREFIX": prefix})
            if cp.returncode == 0 and cp.stdout.strip():
                wine_path = cp.stdout.strip()
                wine_path = wine_path.replace("/", "\\")
                return wine_path
        except (OSError, subprocess.SubprocessError):
            pass
        # Fallback: manual conversion.
        drive_c = os.path.join(prefix, "drive_c")
        real_drive_c = os.path.realpath(drive_c)
        real_path = os.path.realpath(linux_path)
        if real_path.startswith(real_drive_c + os.sep):
            rel = real_path[len(real_drive_c) + 1:]
            return "C:\\" + rel.replace("/", "\\")
        rel = real_path.lstrip("/")
        return "Z:\\" + rel.replace("/", "\\")

    def _write_startup_config(self, connection_id: str, prefix: str,
                              auth_expert: str,
                              startup_symbol: Optional[str],
                              startup_period: Optional[str]) -> str:
        """Write a MT5 /config file with the auth expert's startup chart.

        The config file is placed under the terminal prefix so it is
        accessible to the Wine-launched MT5 process. The [StartUp] section
        (capital U, per MT5 documentation) opens a temporary chart with
        the auth expert for heartbeat authentication.

        MT5 supports exactly one Expert per chart. This config only
        controls the auth expert's startup chart. V305 is deployed on a
        separate chart (chart03) via binary staging, not via this config.

        startup_symbol and startup_period are REQUIRED when auth_expert
        is set: they define which chart the auth expert runs on. Without
        them, MT5 attaches the EA to the first chart of the current
        profile, which could be an order-bridge chart.

        Returns the absolute path to the config file (Linux filesystem).
        """
        # Validate all inputs using shared validation functions
        self._validate_expert_name(auth_expert, "auth_expert")
        self._validate_symbol(startup_symbol, "startup_symbol")
        self._validate_period(startup_period, "startup_period")

        config_dir = os.path.join(prefix, "drive_c", "Program Files",
                                   "MetaTrader 5", "MQL5", "Config")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "abutron_startup.cfg")

        # MT5 [StartUp] section: opens a temporary chart with the auth
        # expert. This chart is not saved when MT5 closes. V305 is NOT
        # attached here — it runs on a separate chart via binary staging.
        content = (
            "[StartUp]\r\n"
            f"Expert={auth_expert}\r\n"
            f"Symbol={startup_symbol}\r\n"
            f"Period={startup_period}\r\n"
        )
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return config_path

    def validate_before_restart(self, connection_id: str,
                                auth_expert: Optional[str] = None,
                                startup_symbol: Optional[str] = None,
                                startup_period: Optional[str] = None) -> None:
        """Validate startup configuration before stopping a running terminal.

        Call this before restart() to ensure the config is valid without
        first terminating the running terminal. If validation fails, the
        terminal remains running.

        Raises Mt5Error if the configuration is invalid.
        """
        if not auth_expert:
            return  # No startup expert configured — nothing to validate

        # Use the same shared validation functions as _write_startup_config
        self._validate_expert_name(auth_expert, "auth_expert")
        self._validate_symbol(startup_symbol, "startup_symbol")
        self._validate_period(startup_period, "startup_period")

    def _stage_worker_files(self, connection_id: str, export_dir: str) -> None:
        """Legacy setup, without a false claim that an MQ5 is installed.

        The only supported compiled PR37 staging path is stage_demo_writer_qa.
        Installing source under Files never attaches an EA to a terminal.
        """
        cfg = {"connection_id": connection_id, "status": "UNVERIFIED",
               "native_ea_installed": False, "broker_authenticated": False}
        os.makedirs(export_dir, exist_ok=True)
        with open(os.path.join(export_dir, "apex_export_config.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    @staticmethod
    def _validated_terminal_data_dir(data_dir: str) -> tuple[str, str]:
        """Accept only an existing, explicit terminal data folder.

        MT5 stores sandbox files under the terminal *data folder's*
        MQL5/Files, which is not necessarily the Wine installation tree.
        Neither staging nor the evidence reader creates missing directories.
        """
        if not data_dir or not os.path.isabs(data_dir):
            raise Mt5Error("explicit absolute MT5 terminal data directory required")
        root = os.path.realpath(data_dir)
        if root in ("/", "/tmp", "/var", "/home", "/opt"):
            raise Mt5Error("unsafe terminal data root")
        files_dir = os.path.join(root, "MQL5", "Files")
        experts_dir = os.path.join(root, "MQL5", "Experts")
        for directory in (root, os.path.join(root, "MQL5"),
                          files_dir, experts_dir):
            if not os.path.isdir(directory) or os.path.islink(directory):
                raise Mt5Error("terminal MQL5/Files and MQL5/Experts must exist")
        return files_dir, experts_dir

    def bind_demo_data_source(self, *, connection_id: str, broker: str,
                              login: int, server: str,
                              data_dir: str, environment: str = "demo") -> Terminal:
        """Bind to DEMO exports only: no process spawn, EA attach or orders.

        The source is not broker-authenticated until CRM verifies the native
        snapshot's account/server binding, HMAC and complete positions.
        """
        if environment.lower() != "demo":
            raise Mt5Error("external evidence binding is DEMO-only")
        if not connection_id or not broker or not server or int(login) <= 0:
            raise Mt5Error("complete registered DEMO account identity required")
        if connection_id in self._terminals:
            raise Mt5Error("connection already bound or launched")
        files_dir, _ = self._validated_terminal_data_dir(data_dir)
        term = Terminal(connection_id, broker, int(login), server,
                        os.path.realpath(data_dir), files_dir,
                        external_evidence_only=True)
        self._terminals[connection_id] = term
        return term

    @staticmethod
    def _qa_read_checked(path: str, *, expected_sha: str = "",
                         expected_uid: Optional[int] = None) -> bytes:
        """Read a stable, regular EX5 without following a symlink."""
        import stat
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_mode & 0o022
                    or not 0 < before.st_size < 64_000_000):
                raise Mt5Error("untrusted QA binary file")
            if expected_uid is not None and before.st_uid != expected_uid:
                raise Mt5Error("QA backup owner mismatch")
            raw = os.read(fd, 64_000_000)
            after = os.fstat(fd)
            if (len(raw) != before.st_size or
                    (before.st_dev, before.st_ino, before.st_mtime_ns,
                     before.st_size) !=
                    (after.st_dev, after.st_ino, after.st_mtime_ns,
                     after.st_size)):
                raise Mt5Error("QA file changed during read")
            if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
                raise Mt5Error("QA backup content does not match expected SHA")
            return raw
        finally:
            os.close(fd)

    @staticmethod
    def _qa_fsync_dir(path: str) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _qa_backup_directory(path: str) -> None:
        import stat
        if os.path.lexists(path) and os.path.islink(path):
            raise Mt5Error("backup directory cannot be a symlink")
        os.makedirs(path, mode=0o700, exist_ok=True)
        state = os.lstat(path)
        if (not stat.S_ISDIR(state.st_mode) or os.path.islink(path)
                or state.st_uid != os.geteuid()
                or state.st_mode & 0o022):
            raise Mt5Error("backup directory ownership or mode invalid")
        os.chmod(path, 0o700)

    @staticmethod
    def _atomic_install(src: str, dest: str, files_dir: str,
                        *, expected_sha: str = "") -> None:
        """Hash-check input, fsync staged bytes, atomically replace and fsync dir."""
        import tempfile
        payload = Mt5TerminalManager._qa_read_checked(src, expected_sha=expected_sha)
        destdir = os.path.dirname(dest)
        if os.path.islink(destdir) or not os.path.isdir(destdir):
            raise Mt5Error("QA destination directory invalid")
        fd, tmp = tempfile.mkstemp(prefix=".ApexNativeWriter.", dir=destdir)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            st = os.stat(files_dir)
            if os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
            elif os.geteuid() != st.st_uid:
                raise Mt5Error("stage as the MT5 data owner or root only")
            os.chmod(tmp, 0o640)
            os.replace(tmp, dest)
            Mt5TerminalManager._qa_fsync_dir(destdir)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _write_backup_atomic(backup_dir: str, src: str, old_sha: str,
                             new_sha: str, files_dir: str) -> str:
        """Preserve each transaction, even when a prior EX5 hash repeats.

        A backup records the PREVIOUS digest and the intended NEW digest.
        Only a separately fsync'd .committed marker makes it eligible
        for rollback; an interrupted installation cannot create proof.
        """
        import secrets
        Mt5TerminalManager._qa_backup_directory(backup_dir)
        source = Mt5TerminalManager._qa_read_checked(src, expected_sha=old_sha)
        # Sequence numbers never go backwards even if the host clock moves.
        # A random suffix makes a concurrent collision fail safely under
        # O_EXCL rather than overwriting an existing backup.
        prior = []
        for candidate in os.listdir(backup_dir):
            match = re.fullmatch(
                r"ApexNativeWriter\.ex5\.(\d{19,22})\.[0-9a-f]{16}\."
                r"[0-9a-f]{64}\.[0-9a-f]{64}\.bak", candidate)
            if match:
                prior.append(int(match.group(1)))
        sequence = max(time.time_ns(), max(prior, default=0) + 1)
        ident = f"{sequence}.{secrets.token_hex(8)}"
        path = os.path.join(
            backup_dir, f"ApexNativeWriter.ex5.{ident}.{old_sha}.{new_sha}.bak")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            st = os.stat(files_dir)
            if os.geteuid() == 0:
                os.fchown(fd, st.st_uid, st.st_gid)
            elif os.geteuid() != st.st_uid:
                raise Mt5Error("backup requires MT5 owner or root")
            with os.fdopen(fd, "wb") as out:
                out.write(source)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(path, 0o640)
            Mt5TerminalManager._qa_fsync_dir(backup_dir)
        except BaseException:
            if os.path.lexists(path):
                os.unlink(path)
            raise
        return path

    @staticmethod
    def _commit_backup(path: str, old_sha: str, new_sha: str) -> None:
        """Publish a durable commit marker only AFTER the EX5 install."""
        marker = path + ".committed"
        value = ("ABUTRON_QA_ROLLBACK_V1\n" + old_sha + "\n" +
                 new_sha + "\n").encode("ascii")
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(value)
                out.flush()
                os.fsync(out.fileno())
            Mt5TerminalManager._qa_fsync_dir(os.path.dirname(marker))
        except BaseException:
            if os.path.lexists(marker):
                os.unlink(marker)
            raise

    def stage_demo_writer_qa(self, *, connection_id: str, broker: str,
                             login: int, server: str, environment: str,
                             data_dir: str, compiled_ex5: str,
                             expected_sha256: str,
                             allow_replace: bool = False) -> str:
        """Stage a pinned EX5 on DEMO only; never attach, order, or alter CRM.

        A unique backup records each replacement's old and new SHA.
        A durable commit marker distinguishes a completed transition from
        an interrupted or failed install. Rollback checks that exact edge.
        """
        if environment.lower() != "demo":
            raise Mt5Error("native writer QA staging is DEMO-only")
        if not connection_id or not broker or not server or int(login) <= 0:
            raise Mt5Error("registered DEMO account identity required")
        files_dir, experts_dir = self._validated_terminal_data_dir(data_dir)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise Mt5Error("expected SHA-256 pin must be 64 hex characters")
        if not compiled_ex5.lower().endswith(".ex5") or os.path.islink(compiled_ex5):
            raise Mt5Error("native QA staging requires a compiled EX5 regular file")
        try:
            payload = self._qa_read_checked(compiled_ex5)
        except OSError as err:
            raise Mt5Error("compiled EA source must be a regular file") from err
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_sha256.lower():
            raise Mt5Error("native EA pin mismatch")
        destdir = os.path.join(experts_dir, "ABUTRON-PR37-QA")
        if os.path.lexists(destdir) and (os.path.islink(destdir) or
                                        not os.path.isdir(destdir)):
            raise Mt5Error("native QA expert path cannot be a symlink")
        os.makedirs(destdir, mode=0o750, exist_ok=True)
        st = os.stat(files_dir)
        if os.geteuid() == 0:
            os.chown(destdir, st.st_uid, st.st_gid)
        elif os.geteuid() != st.st_uid:
            raise Mt5Error("stage as the MT5 data owner or root only")
        os.chmod(destdir, 0o750)
        dest = os.path.join(destdir, "ApexNativeWriter.ex5")
        backup_path = ""
        old_sha = ""
        if os.path.lexists(dest):
            if os.path.islink(dest):
                raise Mt5Error("native QA destination is not a regular file")
            existing = self._qa_read_checked(dest, expected_uid=st.st_uid)
            old_sha = hashlib.sha256(existing).hexdigest()
            if old_sha == actual:
                return dest
            if not allow_replace:
                raise Mt5Error("different native writer already staged: refuse overwrite")
            backup_dir = os.path.join(destdir, ".backup")
            backup_path = self._write_backup_atomic(
                backup_dir, dest, old_sha, actual, files_dir)
        try:
            self._atomic_install(compiled_ex5, dest, files_dir, expected_sha=actual)
            if backup_path:
                self._commit_backup(backup_path, old_sha, actual)
        except BaseException:
            if backup_path:
                try:
                    self._atomic_install(
                        backup_path, dest, files_dir, expected_sha=old_sha)
                except BaseException as restore_error:
                    raise Mt5Error(
                        "QA installation failed AND prior binary rollback failed"
                    ) from restore_error
            raise
        return dest

    def rollback_demo_writer_qa(self, *, data_dir: str) -> str:
        """Revert ONLY the last committed transition ending at current SHA.

        Never choose by file mtime. Reject a forged basename, missing commit
        marker, mismatched original hash, unexpected owner or symlink.
        """
        files_dir, experts_dir = self._validated_terminal_data_dir(data_dir)
        qa_dir = os.path.join(experts_dir, "ABUTRON-PR37-QA")
        dest = os.path.join(qa_dir, "ApexNativeWriter.ex5")
        backup_dir = os.path.join(qa_dir, ".backup")
        if not os.path.isdir(backup_dir) or os.path.islink(backup_dir):
            raise Mt5Error("no backup directory; nothing to roll back to")
        state = os.lstat(backup_dir)
        if state.st_uid != os.geteuid() or state.st_mode & 0o022:
            raise Mt5Error("backup directory ownership or mode invalid")
        if not os.path.isfile(dest) or os.path.islink(dest):
            raise Mt5Error("current staged writer is missing or a symlink")
        file_owner = os.stat(files_dir).st_uid
        current_sha = hashlib.sha256(
            self._qa_read_checked(dest, expected_uid=file_owner)).hexdigest()
        pattern = re.compile(
            r"^ApexNativeWriter\.ex5\.(\d{19,22})\.([0-9a-f]{16})\."
            r"([0-9a-f]{64})\.([0-9a-f]{64})\.bak$")
        transitions = []
        for name in os.listdir(backup_dir):
            if not name.endswith(".bak"):
                continue
            m = pattern.fullmatch(name)
            if not m:
                raise Mt5Error("backup filename is not an exact QA transaction")
            path = os.path.join(backup_dir, name)
            if os.path.islink(path) or not os.path.isfile(path):
                raise Mt5Error("backup symlink or non-file denied")
            marker = path + ".committed"
            if not os.path.lexists(marker):
                continue  # Incomplete install: never treat as committed.
            if os.path.islink(marker) or not os.path.isfile(marker):
                raise Mt5Error("backup commit marker invalid")
            expected_marker = ("ABUTRON_QA_ROLLBACK_V1\n" + m.group(3) +
                               "\n" + m.group(4) + "\n").encode("ascii")
            if self._qa_read_checked(marker) != expected_marker:
                raise Mt5Error("backup commit marker mismatch")
            self._qa_read_checked(path, expected_sha=m.group(3),
                                  expected_uid=file_owner)
            if m.group(4) == current_sha:
                transitions.append((int(m.group(1)), m.group(2), path,
                                    m.group(3)))
        if not transitions:
            raise Mt5Error("no committed rollback for current staged writer")
        _, _, previous_path, previous_sha = max(transitions)
        self._atomic_install(previous_path, dest, files_dir,
                             expected_sha=previous_sha)
        return dest

    def mark_native_authenticated(self, connection_id: str,
                                  certified: Dict) -> None:
        """Only call after CRM verified a fresh signed native exposure."""
        term = self._terminals.get(connection_id)
        if term is None or certified.get("native_positions_verified") != 1:
            raise Mt5Error("native snapshot is not broker certified")
        if certified.get("connection_id") != connection_id:
            raise Mt5Error("certified account does not match bound source")
        if str(certified.get("mt5_login")) != str(term.login):
            raise Mt5Error("native account login does not match bound terminal")
        if certified.get("server") != term.server:
            raise Mt5Error("native account server does not match bound terminal")
        nonce = str(certified.get("exposure_nonce") or "")
        if not nonce.startswith("ABUTRON_NATIVE_"):
            raise Mt5Error("native evidence nonce missing")
        term.authenticated_nonce = nonce
        term.authenticated_at = time.time()

    def invalidate_native_authentication(self, connection_id: str) -> None:
        term = self._terminals.get(connection_id)
        if term:
            term.authenticated_at = 0.0
            term.authenticated_nonce = ""

    def stop(self, connection_id: str) -> None:
        term = self._terminals.get(connection_id)
        if term and term.proc and term.proc.poll() is None:
            try:
                term.proc.send_signal(signal.SIGTERM)
                term.proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    term.proc.kill()
                except ProcessLookupError:
                    pass
        if term:
            term.proc = None

    def restart(self, connection_id: str,
                auth_expert: Optional[str] = None,
                startup_symbol: Optional[str] = None,
                startup_period: Optional[str] = None) -> Terminal:
        # Validate configuration before stopping the running terminal.
        # If validation fails, the terminal remains running.
        self.validate_before_restart(connection_id, auth_expert,
                                     startup_symbol, startup_period)
        self.stop(connection_id)
        term = self._terminals[connection_id]
        return self.launch(connection_id, term.broker, term.login,
                           term.server, auth_expert=auth_expert,
                           startup_symbol=startup_symbol,
                           startup_period=startup_period)

    # ------------------------------------------------------------------
    # Native export read-back
    # ------------------------------------------------------------------
    def get(self, connection_id: str) -> Optional["Terminal"]:
        """Return the live terminal for a connection, or None."""
        return self._terminals.get(connection_id)

    def read_native_export(self, connection_id: str,
                           required_nonce: Optional[str] = None) -> Optional[Dict]:
        """Read the latest native export (bars + identity) from MQL5/Files.

        Supports both the JSON format (ApexNativeWriter) and the real
        tab-delimited text format produced by the deployed Deriv MT5
        writer:

        - ``ABUTRON_NATIVE_BARS_V2_*.txt``: line 1 schema tag, line 2
          account-bound nonce, then 7-field rows:
          ``symbol<TAB>timeframe<TAB>bar_open_epoch<TAB>open<TAB>high<TAB>low<TAB>close``
        - ``ABUTRON_NATIVE_ACCOUNT_V2_*.txt``: either JSON (ApexNativeWriter)
          or a 7-line text record: schema, nonce, login, balance, equity,
          currency, count.

        The nonce pairing between bars and identity files is validated.
        """
        term = self._terminals.get(connection_id)
        if term is None or not (term.is_running or term.external_evidence_only):
            return None
        identity_file = os.path.join(term.export_dir,
                                     "ABUTRON_NATIVE_ACCOUNT_V2_*.txt")
        bars_file = os.path.join(term.export_dir,
                                 "ABUTRON_NATIVE_BARS_V2_*.txt")
        if required_nonce is not None:
            # Latest file may belong to the next incomplete timer cycle.
            # Pair the exact nonce from the latest complete quote snapshot.
            if not required_nonce or "/" in required_nonce or "\\" in required_nonce:
                return None
            ident_path = identity_file.replace("*", required_nonce)
            bars_path = bars_file.replace("*", required_nonce)
            if not (os.path.isfile(ident_path) and os.path.isfile(bars_path)):
                return None
        else:
            ident_path = _latest_match(identity_file)
            bars_path = _latest_match(bars_file)
        if not ident_path or not bars_path:
            return None

        # --- Parse identity (JSON or 7-line text) --------------------
        identity = self._parse_identity_file(ident_path)
        if identity is None:
            return None

        # --- Parse bars (JSON or tab-delimited) ---------------------
        bars_raw = self._parse_bars_file(bars_path)
        if bars_raw is None:
            return None

        # --- Nonce pairing: bars nonce must match identity nonce ----
        bars_nonce = bars_raw.get("_nonce", "")
        ident_nonce = str(identity.get("nonce") or "")
        if not ident_nonce:
            return None
        # Pair all formats to the same native export cycle.  JSON bar
        # payloads omit nonce; their filename carries it.
        bars_nonce = bars_nonce or os.path.basename(bars_path).split(
            "ABUTRON_NATIVE_BARS_V2_", 1)[-1].removesuffix(".txt")
        ident_file_nonce = os.path.basename(ident_path).split(
            "ABUTRON_NATIVE_ACCOUNT_V2_", 1)[-1].removesuffix(".txt")
        if not (bars_nonce == ident_nonce == ident_file_nonce):
            return None
        if any(os.path.islink(f) for f in (ident_path, bars_path)):
            return None
        if max(time.time() - os.stat(f).st_mtime for f in
               (ident_path, bars_path)) > 180:
            return None
        identity["_native_file_mtime"] = min(
            os.stat(ident_path).st_mtime, os.stat(bars_path).st_mtime)

        return {
            "identity": identity,
            "bars": bars_raw,
            "identity_file": ident_path,
            "bars_file": bars_path,
        }

    def read_native_quotes(self, connection_id: str) -> Optional[Dict]:
        """Read the authenticated native quote snapshot.

        Returns ``{"quotes": [...], "_file": path}`` from the
        ``ABUTRON_NATIVE_QUOTES_V2_*.txt`` export produced by
        ApexNativeWriter. Quotes are broker-native bid/ask from
        SymbolInfoDouble, bound to the same nonce/login/server/session.

        Returns None if no quote file exists or parsing fails.
        """
        term = self._terminals.get(connection_id)
        if term is None or not (term.is_running or term.external_evidence_only):
            return None
        quotes_file = os.path.join(term.export_dir,
                                   "ABUTRON_NATIVE_QUOTES_V2_*.txt")
        q_path = _latest_match(quotes_file)
        if not q_path:
            return None
        try:
            if os.path.islink(q_path) or os.stat(q_path).st_size > 1000000:
                return None
            if time.time() - os.stat(q_path).st_mtime > 180:
                return None
            with open(q_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("quotes"), list):
                data["_file"] = q_path
                data["_nonce"] = os.path.basename(q_path).split(
                    "ABUTRON_NATIVE_QUOTES_V2_", 1)[-1].removesuffix(".txt")
                data["_native_file_mtime"] = os.stat(q_path).st_mtime
                return data
        except (ValueError, OSError):
            pass
        return None

    def read_native_export_full(self, connection_id: str) -> Optional[Dict]:
        """Read bars + identity + quotes in one call.

        Combines :meth:`read_native_export` and :meth:`read_native_quotes`.
        Quotes may be None (the adapter decides how to handle them).
        """
        quotes = self.read_native_quotes(connection_id)
        # Use the latest *complete* cycle. The writer publishes identity,
        # then bars, then quotes; latest identity may belong to an
        # incomplete newer cycle and must not be mixed with prior quotes.
        export = self.read_native_export(
            connection_id,
            required_nonce=quotes["_nonce"] if quotes is not None else None)
        if export is None:
            return None
        # Refuse a quote file from a different account/export cycle.
        if quotes is not None:
            if quotes.get("_nonce") != export["identity"].get("nonce"):
                return None
            if abs(quotes["_native_file_mtime"] -
                   export["identity"]["_native_file_mtime"]) > 120:
                return None
        export["quotes"] = quotes
        # Order-flow is optional: consume only the authenticated export-cycle
        # file. Missing, malformed, or foreign files never remove valid bars
        # and quotes from the native decision pass.
        term = self._terminals.get(connection_id)
        if term is not None:
            cycle_nonce = str(export["identity"].get("nonce") or "")
            if (cycle_nonce and len(cycle_nonce) <= 128
                    and "/" not in cycle_nonce and chr(92) not in cycle_nonce):
                of_file = os.path.join(
                    term.export_dir, f"ABUTRON_ORDER_FLOW_V2_{cycle_nonce}.txt")
                try:
                    of_stat = os.stat(of_file, follow_symlinks=False)
                    of_ok = (os.path.isfile(of_file)
                             and not os.path.islink(of_file)
                             and 0 < of_stat.st_size <= 1_000_000
                             and 0 <= time.time() - of_stat.st_mtime <= 180)
                except OSError:
                    of_ok = False
                if of_ok:
                    of_data = Mt5TerminalManager._parse_order_flow_file(of_file)
                    if (of_data is not None
                            and of_data.get("connection_id") == connection_id
                            and of_data.get("nonce") == cycle_nonce
                            and of_data.get("source")
                            in ("trade_ticks", "candle_derived")):
                        export["orderflow"] = of_data
        return export

    @staticmethod
    def _parse_order_flow_file(path: str) -> Optional[Dict]:
        """Parse the latest order-flow export file.

        Format (ApexNativeWriter): JSON
        ``{"connection_id":"...", "source":"candle_derived",
        "trades":[{"symbol":..., "price":..., "size":...,
        "side":"buy"|"sell", "time":"..."}]}``.

        ``source`` records the provenance of the activity: the MQL5 EA
        derives direction from close vs open and size from tick volume
        because MT5 does not expose broker trade prints to EAs. This is
        an estimate, NOT genuine aggressor trades. Returns
        ``{"trades": [...], "source": ..., "connection_id": ...}`` or
        None if unparseable.
        """
        if not os.path.isfile(path) or os.path.islink(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or "trades" not in data:
                return None
            trades = data["trades"]
            if not isinstance(trades, list):
                return None
            validated = []
            for t in trades:
                if not isinstance(t, dict):
                    continue
                try:
                    price = float(t.get("price", 0.0))
                    size = float(t.get("size", 0.0))
                    side = str(t.get("side", "")).lower()
                except (ValueError, TypeError):
                    continue
                if side not in ("buy", "sell") or price <= 0 or size <= 0:
                    continue
                validated.append({
                    "symbol": str(t.get("symbol", "")),
                    "price": price,
                    "size": size,
                    "side": side,
                    "time": str(t.get("time", "")),
                })
            result = {"trades": validated}
            src = data.get("source")
            if src is not None:
                result["source"] = str(src)
            cid = data.get("connection_id")
            if cid is not None:
                result["connection_id"] = str(cid)
            nonce = data.get("nonce")
            if nonce is not None:
                result["nonce"] = str(nonce)
            return result
        except (ValueError, OSError, TypeError):
            return None

    @staticmethod
    def _parse_identity_file(path: str) -> Optional[Dict]:
        """Parse identity from JSON or 7-line text format."""
        # Try JSON first (ApexNativeWriter format).
        try:
            data = json.loads(open(path).read())
            if isinstance(data, dict) and "connection_id" in data:
                # MQL5 TimeToString is broker-server wall time, not UTC.
                # For read-only shadow freshness use the local native file's
                # observed mtime; do not mislabel server time as UTC.
                hb = data.get("worker_health_ts", "")
                if isinstance(hb, str) and "." in hb and "T" not in hb:
                    data["_broker_wall_clock"] = hb
                    data["worker_health_ts"] = _mtime_iso(path)
                return data
        except (ValueError, OSError):
            pass
        # Line-based text format (Deriv MT5 writer).
        try:
            lines = [l.strip() for l in open(path).readlines() if l.strip()]
        except OSError:
            return None
        if not lines or len(lines) < 7:
            return None
        # Line 1 is schema tag; expect ABUTRON_NATIVE_ACCOUNT_V2 or similar.
        if "ACCOUNT" not in lines[0].upper():
            return None
        fields = {
            "schema": lines[0],
            "nonce": lines[1],
            "login": lines[2],
            "balance": lines[3],
            "equity": lines[4],
            "currency": lines[5],
            "count": lines[6],
        }
        # Convert numeric fields.
        try:
            fields["login"] = int(fields["login"])
            fields["balance"] = float(fields["balance"])
            fields["equity"] = float(fields["equity"])
        except (ValueError, TypeError):
            return None
        # Legacy Deriv nonce is an opaque 32-char token, NOT a clock.
        # File mtime is an observation timestamp, not a broker signature.
        # The external terminal binding supplies CID/broker/server.
        fields["connection_id"] = ""
        fields["worker_health_ts"] = _mtime_iso(path)
        fields["_legacy_text_unattested"] = True
        fields["broker"] = "native"
        fields["server"] = ""
        fields["challenge"] = ""
        return fields

    @staticmethod
    def _parse_bars_file(path: str) -> Optional[Dict]:
        """Parse bars from JSON or tab-delimited text format.

        Tab-delimited: line 1 schema tag, line 2 nonce, then 7-field rows:
        ``symbol<TAB>timeframe<TAB>bar_open_epoch<TAB>open<TAB>high<TAB>low<TAB>close``
        """
        # Try JSON first (ApexNativeWriter format).
        try:
            data = json.loads(open(path).read())
            if isinstance(data, dict) and "bars" in data:
                return data
        except (ValueError, OSError):
            pass
        # Tab-delimited text format (Deriv MT5 writer).
        try:
            lines = [l.rstrip("\n") for l in open(path).readlines()]
        except OSError:
            return None
        # Strip trailing empty lines.
        lines = [l for l in lines if l.strip()]
        if len(lines) < 3:
            return None
        if "BARS" not in lines[0].upper():
            return None
        nonce = lines[1].strip()
        bars = []
        for line in lines[2:]:
            parts = line.split("\t")
            if len(parts) < 7:
                continue  # skip malformed rows
            symbol = parts[0].strip()
            timeframe = parts[1].strip()
            try:
                bar_open_epoch = int(float(parts[2]))
                open_p = float(parts[3])
                high = float(parts[4])
                low = float(parts[5])
                close = float(parts[6])
            except (ValueError, TypeError):
                continue  # skip malformed rows
            tf_sec = {"M5": 300}.get(timeframe)
            # The decision adapter uses M5 exclusively. Keep other
            # original native timeframe rows outside this contract.
            if tf_sec is None or bar_open_epoch <= 0:
                continue
            bars.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "open_time": _epoch_to_iso(bar_open_epoch),
                "close_time": _epoch_to_iso(bar_open_epoch + tf_sec),
                "open": open_p,
                "high": high,
                "low": low,
                "close": close,
                "volume": 0.0,
                "bid": 0.0,
                "ask": 0.0,
                "spread": 0.0,
                "tick_size": 0.0,
                "point": 0.0,
                "closed": bar_open_epoch + tf_sec <= time.time(),
            })
        if not bars:
            return None
        return {"bars": bars, "_nonce": nonce}

    def read_native_exposure(self, connection_id: str,
                             since: Optional[float] = None) -> Optional[Dict]:
        """Read a broker-native exposure snapshot (Issue #36/#38).

        Returns the parsed ``ABUTRON_NATIVE_EXPOSURE_V2`` export — open
        positions + equity/balance/currency authenticated by the native
        export nonce. This is the broker-native evidence the worker feeds
        to crm.certify_exposure so exposure certification is derived from
        real broker data, not a manually supplied flag.

        ``since`` (unix timestamp) removes stale-file ambiguity: only
        snapshots whose file mtime is >= ``since`` are considered. This
        prevents a reused Wine prefix from returning an old snapshot as
        if it were fresh (the Issue #38 ``STALE_NATIVE_FILE`` failure).
        When ``since`` is None, the newest file is returned (legacy
        behavior for read-only consumers).

        Rejects symlinks, oversized files, and (on the fresh-run path)
        snapshots whose connection_id/login/server do not match the
        bound terminal.
        """
        term = self._terminals.get(connection_id)
        if term is None or not (term.is_running or term.external_evidence_only):
            return None
        pattern = os.path.join(term.export_dir,
                               "ABUTRON_NATIVE_EXPOSURE_V2_*.txt")
        candidates = sorted(glob.glob(pattern),
                            key=os.path.getmtime, reverse=True)
        if since is not None:
            candidates = [p for p in candidates
                          if os.path.getmtime(p) >= since]
        if not candidates:
            return None
        path = candidates[0]
        try:
            # Reject symlink artifacts and incomplete files; never silently
            # interpret unreadable native evidence as a flat broker account.
            if os.path.islink(path) or os.stat(path).st_size > 4_000_000:
                return None
            with open(path, encoding="utf-8") as fh:
                snapshot = json.load(fh)
        except (ValueError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(snapshot, dict):
            return None
        # Account-binding check (fresh-run path only): when `since` is
        # provided, the snapshot must belong to the bound terminal. This
        # prevents a fresh run from accepting wrong-account/wrong-server
        # data. Legacy read-only consumers (since=None) keep prior
        # behavior; mark_native_authenticated still enforces the binding.
        if since is not None and not self._snapshot_matches_terminal(snapshot, term):
            return None
        snapshot["exposure_file"] = path
        return snapshot

    @staticmethod
    def _snapshot_matches_terminal(snapshot: Dict, term: "Terminal") -> bool:
        """Verify a native snapshot is bound to the correct terminal.

        Checks connection_id, login and server against the bound DEMO
        terminal identity. A snapshot from a different account, server
        or connection must never be accepted as evidence for this
        terminal (fail-closed).
        """
        sid = str(snapshot.get("connection_id", "") or "")
        if sid and sid != term.connection_id:
            return False
        slogin = str(snapshot.get("login", "") or "")
        if slogin and slogin != str(term.login):
            return False
        sserver = str(snapshot.get("server", "") or "")
        if sserver and sserver != term.server:
            return False
        return True

    def health(self, connection_id: str) -> Dict:
        term = self._terminals.get(connection_id)
        if term is None:
            return {"connection_id": connection_id, "status": "UNKNOWN"}
        return {
            "connection_id": connection_id,
            "status": (
                "CONNECTED" if (term.authenticated_at > 0 and
                                time.time() - term.authenticated_at <= 60 and
                                (term.is_running or term.external_evidence_only))
                else "SOURCE_BOUND_UNVERIFIED" if term.external_evidence_only
                else "PROCESS_RUNNING_UNVERIFIED" if term.is_running
                else "DISCONNECTED"
            ),
            "broker": term.broker,
            "login": term.login,
            "uptime_s": round(time.time() - term.started_at, 1),
            "health_ts": term.health_ts,
        }

    def list_terminals(self) -> List[Dict]:
        return [self.health(cid) for cid in self._terminals]


def _latest_match(pattern: str) -> Optional[str]:
    matches = glob.glob(pattern)
    return max(matches, key=os.path.getmtime) if matches else None


def _epoch_to_iso(epoch: int) -> str:
    """Convert a unix epoch (seconds) to an ISO-8601 UTC string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _mtime_iso(path: str) -> str:
    """Trusted host observation timestamp; does not certify broker clock."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(os.stat(path).st_mtime))


# Singleton
terminal_manager = Mt5TerminalManager()
