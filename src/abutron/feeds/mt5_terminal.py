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
                raise Mt5Error("QA backup hash mismatch")
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
        ident = f"{time.time_ns()}.{secrets.token_hex(8)}"
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
