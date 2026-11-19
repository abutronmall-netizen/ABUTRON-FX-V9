"""Wine-based MT5 terminal management on Ubuntu.

Each broker account gets an isolated Wine prefix + terminal profile. The
worker runs `wine Terminal64.exe` headless, uses the MQL5 Files directory
for native exports, and the terminal's Files are read back by the reader.

This module manages: prefix init, terminal launch/stop, file transfer to
the MQL5/Files drop zone, and health heartbeats. It does NOT place orders
— the native EA inside the terminal is the only order authority.
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
               server: str, *, headless: bool = True) -> Terminal:
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
        term.proc = subprocess.Popen(cmd, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        term.started_at = time.time()
        term.health_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return term

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

    def stage_demo_writer_qa(self, *, connection_id: str, broker: str,
                             login: int, server: str, environment: str,
                             data_dir: str, compiled_ex5: str,
                             expected_sha256: str) -> str:
        """Stage, but DO NOT attach, a pinned compiled EA on an existing DEMO.

        Does not modify Files/, chart profiles, terminal settings or
        activation secrets. Refuses unknown/unpinned sources and overwrites.
        """
        if environment.lower() != "demo":
            raise Mt5Error("native writer QA staging is DEMO-only")
        if not connection_id or not broker or not server or int(login) <= 0:
            raise Mt5Error("registered DEMO account identity required")
        files_dir, experts_dir = self._validated_terminal_data_dir(data_dir)
        if not os.path.isfile(compiled_ex5) or os.path.islink(compiled_ex5):
            raise Mt5Error("compiled EA source must be a regular file")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise Mt5Error("expected SHA-256 pin must be 64 hex characters")
        with open(compiled_ex5, "rb") as fh:
            payload = fh.read()
        actual = hashlib.sha256(payload).hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise Mt5Error("native EA pin mismatch")
        if not payload or not compiled_ex5.lower().endswith(".ex5"):
            raise Mt5Error("native QA staging requires a compiled EX5")
        destdir = os.path.join(experts_dir, "ABUTRON-PR37-QA")
        if os.path.islink(destdir):
            raise Mt5Error("native QA expert path cannot be a symlink")
        os.makedirs(destdir, mode=0o750, exist_ok=True)
        # The MT5 session user needs to traverse its own Experts directory.
        st = os.stat(files_dir)
        if os.geteuid() == 0:
            os.chown(destdir, st.st_uid, st.st_gid)
        elif os.geteuid() != st.st_uid:
            raise Mt5Error("stage as the MT5 data owner or root only")
        os.chmod(destdir, 0o750)
        dest = os.path.join(destdir, "ApexNativeWriter.ex5")
        if os.path.lexists(dest):
            if not os.path.isfile(dest) or os.path.islink(dest):
                raise Mt5Error("native QA destination is not a regular file")
            with open(dest, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != actual:
                    raise Mt5Error("different native writer already staged: refuse overwrite")
            return dest
        # Atomic install; no chart/profile/Files changes or order activation.
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=".ApexNativeWriter.", dir=destdir)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            # The EA must be readable by the account-specific MT5 user,
            # not inadvertently left as a root-only binary.
            st = os.stat(files_dir)
            if os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
            elif os.geteuid() != st.st_uid:
                raise Mt5Error("stage as the MT5 data owner or root only")
            os.chmod(tmp, 0o640)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
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
        if term is None or not (term.is_running or term.external_evidence_only):
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

    def read_native_exposure(self, connection_id: str) -> Optional[Dict]:
        """Read the latest broker-native exposure snapshot (Issue #36).

        Returns the parsed ABUTRON_NATIVE_EXPOSURE_V2 export — open
        positions + equity/balance/currency authenticated by the native
        export nonce. This is the broker-native evidence the worker feeds
        to crm.certify_exposure so exposure certification is derived from
        real broker data, not a manually supplied flag.
        """
        term = self._terminals.get(connection_id)
        if term is None or not (term.is_running or term.external_evidence_only):
            return None
        pattern = os.path.join(term.export_dir,
                               "ABUTRON_NATIVE_EXPOSURE_V2_*.txt")
        path = _latest_match(pattern)
        if not path:
            return None
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
        snapshot["exposure_file"] = path
        return snapshot

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


# Singleton
terminal_manager = Mt5TerminalManager()
