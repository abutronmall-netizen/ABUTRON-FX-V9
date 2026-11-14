"""Wine-based MT5 terminal management on Ubuntu.

Each broker account gets an isolated Wine prefix + terminal profile. The
worker runs `wine Terminal64.exe` headless, uses the MQL5 Files directory
for native exports, and the terminal's Files are read back by the reader.

This module manages: prefix init, terminal launch/stop, file transfer to
the MQL5/Files drop zone, and health heartbeats. It does NOT place orders
— the native EA inside the terminal is the only order authority.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import (MT5_EXE, MT5_EXPORT_DIR, MT5_ROOT, WINE_PREFIX)
from ..crypto import make_nonce


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
               server: str, *, headless: bool = True) -> Terminal:
        if connection_id in self._terminals and self._terminals[connection_id].is_running:
            raise Mt5Error("terminal already running")
        prefix = self.ensure_prefix(connection_id)
        export_dir = os.path.join(prefix, "drive_c", "Program Files",
                                  "MetaTrader 5", "MQL5", "Files")
        os.makedirs(export_dir, exist_ok=True)
        term = Terminal(connection_id, broker, login, server, prefix, export_dir)
        self._terminals[connection_id] = term

        # Stage the EA + export config into MQL5/Files before launch
        self._stage_worker_files(connection_id, export_dir)

        env = self._wine_env(prefix)
        cmd = ["wine", MT5_EXE]
        if headless:
            cmd += ["/headless", "/nosplash"]
        term.proc = subprocess.Popen(cmd, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        term.started_at = time.time()
        term.health_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return term

    def _stage_worker_files(self, connection_id: str, export_dir: str) -> None:
        """Copy the per-account export EA / config into MQL5/Files.

        The EA itself is root-owned and pinned; only the thin per-account
        config (connection id, nonce, broker server) is staged here.
        """
        src_ea = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "..", "mql5",
            "ApexOrderFlowExport.mq5")
        src_ea = os.path.normpath(src_ea)
        if os.path.exists(src_ea):
            shutil.copy2(src_ea, os.path.join(export_dir,
                                              "ApexOrderFlowExport.mq5"))
        cfg = {"connection_id": connection_id, "nonce": make_nonce(),
               "broker": None, "login": 0}
        with open(os.path.join(export_dir, "apex_export_config.json"),
                  "w") as fh:
            json.dump(cfg, fh, indent=2)

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

    def restart(self, connection_id: str) -> Terminal:
        self.stop(connection_id)
        term = self._terminals[connection_id]
        return self.launch(connection_id, term.broker, term.login,
                           term.server)

    # ------------------------------------------------------------------
    # Native export read-back
    # ------------------------------------------------------------------
    def get(self, connection_id: str) -> Optional["Terminal"]:
        """Return the live terminal for a connection, or None."""
        return self._terminals.get(connection_id)

    def read_native_export(self, connection_id: str) -> Optional[Dict]:
        """Read the latest native export (bars + identity) from MQL5/Files."""
        term = self._terminals.get(connection_id)
        if term is None or not term.is_running:
            return None
        identity_file = os.path.join(term.export_dir,
                                     "ABUTRON_NATIVE_ACCOUNT_V2_*.txt")
        bars_file = os.path.join(term.export_dir,
                                 "ABUTRON_NATIVE_BARS_V2_*.txt")
        ident = _latest_match(identity_file)
        bars = _latest_match(bars_file)
        if not ident or not bars:
            return None
        try:
            identity = json.loads(open(ident).read())
            bars_raw = json.loads(open(bars).read())
        except (ValueError, OSError):
            return None
        return {
            "identity": identity,
            "bars": bars_raw,
            "identity_file": ident,
            "bars_file": bars,
        }

    def health(self, connection_id: str) -> Dict:
        term = self._terminals.get(connection_id)
        if term is None:
            return {"connection_id": connection_id, "status": "UNKNOWN"}
        return {
            "connection_id": connection_id,
            "status": "CONNECTED" if term.is_running else "DISCONNECTED",
            "broker": term.broker,
            "login": term.login,
            "uptime_s": round(time.time() - term.started_at, 1),
            "health_ts": term.health_ts,
        }

    def list_terminals(self) -> List[Dict]:
        return [self.health(cid) for cid in self._terminals]


def _latest_match(pattern: str) -> Optional[str]:
    import glob
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


# Singleton
terminal_manager = Mt5TerminalManager()
