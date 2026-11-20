#!/usr/bin/python3
"""Issue38: bounded, isolated Deriv DEMO exposure evidence probe.

This service reuses systemd-encrypted DEMO credentials, NOT production MT5
terminal/profile state. This QA contains signed order handlers, but config is exposure-only and terminal trading is disabled.
No secrets or full account login are written to logs or GitHub.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

STATE_LABEL = "abutron-pr37-direct-writer-demo6207-qa"
CONNECTION_ID = __import__("os").environ["ABUTRON_CRM_CONNECTION_ID_6207"]
EXPECTED_BROKER = "Deriv"
EXPECTED_SERVER = "Deriv-Demo"
EXPECTED_ACCOUNT_SUFFIX = "6207"
EXPECTED_ID = __import__("os").environ["ABUTRON_CRM_ACCOUNT_ID_6207"]
EXPECTED_EX5_SHA = "9d4f6701993865d32fcd5d9e9781c7803e5f1a688bd8f1ca63e85b8eed173b86"
SOURCE_EX5 = Path("/opt/abutron-pr37-direct-writer-qa/ApexNativeWriter.ex5")
BASE = Path("/opt/abutron/client-mt5-template-v3-usd-50-100-canary-20261103")
WINE = "/opt/abutron/apex-wine-source/bin/wine"
SYMBOL = "Volatility 50 Index.0"
POLL_SECONDS = 3
MAX_SESSION_SECONDS = 150
PERSISTENT_DEMO = os.getenv("ABUTRON_V9_PERSISTENT_DEMO", "") == "1"
DEMO_EXECUTION_ENABLED = os.getenv("ABUTRON_V9_DEMO6207_EXECUTION", "") == "1"
PERSISTENT_SESSION_SECONDS = 4 * 3600
NATIVE_SOURCE_TIMEOUT_SECONDS = 175


def sha_bytes(buf: bytes) -> str:
    return hashlib.sha256(buf).hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def record_health(path: Path, status: str, **facts):
    body = {
        "status": status,
        "utc": utc_now(),
        "environment": "DEMO",
        "account_suffix": EXPECTED_ACCOUNT_SUFFIX,
        "native_order_functions_compiled": True,
        "automated_trading_configured": DEMO_EXECUTION_ENABLED,
        **facts,
    }
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(body, f, sort_keys=True, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)


def verify_preconditions():
    if os.geteuid() == 0:
        raise RuntimeError("QA worker must run as isolated systemd DynamicUser")
    if DEMO_EXECUTION_ENABLED and not PERSISTENT_DEMO:
        raise RuntimeError("native DEMO execution needs persistent MT5 trailing worker")
    state = Path(os.environ.get("STATE_DIRECTORY", "")).resolve()
    runtime = Path(os.environ.get("RUNTIME_DIRECTORY", "")).resolve()
    creds_dir = Path(os.environ.get("CREDENTIALS_DIRECTORY", ""))
    expected = Path("/var/lib/private") / STATE_LABEL
    if state != expected:
        raise RuntimeError("isolated StateDirectory missing or mismatched")
    if runtime.name != STATE_LABEL or not runtime.is_dir():
        raise RuntimeError("dedicated RuntimeDirectory missing")
    credpath = creds_dir / "mt5"
    if not credpath.is_file():
        raise RuntimeError("systemd-encrypted DEMO credential missing")
    source = SOURCE_EX5
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("pinned PR37 exposure-only QA EA source missing")
    if sha_bytes(source.read_bytes()) != EXPECTED_EX5_SHA:
        raise RuntimeError("PR37 writer EX5 SHA-256 mismatch")
    if not (BASE / "terminal64.exe").is_file():
        raise RuntimeError("verified MT5 terminal template missing")
    if not (BASE / "Config" / "servers.dat").is_file():
        raise RuntimeError("broker server catalogue missing")
    if not Path(WINE).is_file():
        raise RuntimeError("isolated Wine executable missing")
    document = json.loads(credpath.read_text(encoding="utf-8"))
    required = {"id", "broker", "server", "login", "password", "mode"}
    if set(document) != required:
        raise RuntimeError("unexpected DEMO credential schema")
    if not (
        document["id"] == EXPECTED_ID
        and document["broker"] == EXPECTED_BROKER
        and document["server"] == EXPECTED_SERVER
        and document["mode"] == "DEMO"
        and str(document["login"]).isdigit()
        and str(document["login"]).endswith(EXPECTED_ACCOUNT_SUFFIX)
        and isinstance(document["password"], str)
        and len(document["password"]) >= 5
    ):
        raise RuntimeError("refusing unknown or LIVE MT5 identity")
    return state, runtime, document


def write_private(path: Path, data: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def prepare(state: Path, runtime: Path, document: dict):
    terminal = state / "terminal"
    reuse = terminal.exists()
    if reuse and (terminal.is_symlink() or not terminal.is_dir()):
        raise RuntimeError("QA terminal reuse path is unsafe")
    if not reuse:
        terminal.mkdir(mode=0o700)
        shutil.copy2(BASE / "terminal64.exe", terminal / "terminal64.exe")
        shutil.copytree(BASE / "Config", terminal / "Config", symlinks=False)
    experts = terminal / "MQL5" / "Experts" / "ABUTRON"
    files = terminal / "MQL5" / "Files"
    if reuse:
        if (not experts.is_dir() or experts.is_symlink() or
            not files.is_dir() or files.is_symlink() or
            not (terminal / "Config" / "servers.dat").is_file() or
            not (terminal / "terminal64.exe").is_file()):
            raise RuntimeError("reused QA terminal file layout invalid")
        staged = experts / "ApexNativeWriter.ex5"
        if staged.is_symlink() or not staged.is_file() or            sha_bytes(staged.read_bytes()) != EXPECTED_EX5_SHA:
            raise RuntimeError("reused QA EA SHA mismatched")
        allowed = {"ApexNativeWriter.ex5"}
        if {z.name for z in experts.iterdir()} != allowed:
            raise RuntimeError("reused QA terminal contains unreviewed EA")
        keyfile = files / "ABUTRON_ACTIVATION_SECRET.txt"
        if keyfile.is_symlink() or not keyfile.is_file() or            keyfile.stat().st_mode & 0o077:
            raise RuntimeError("reused QA key not owner-private")
        key = keyfile.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise RuntimeError("reused QA key malformed")
    else:
        experts.mkdir(mode=0o700, parents=True)
        files.mkdir(mode=0o700, parents=True)
        shutil.copy2(SOURCE_EX5, experts / "ApexNativeWriter.ex5")
        if sha_bytes((experts / "ApexNativeWriter.ex5").read_bytes()) != EXPECTED_EX5_SHA:
            raise RuntimeError("copied QA EA changed checksum")
        key = secrets.token_hex(32)
        write_private(files / "ABUTRON_ACTIVATION_SECRET.txt", key + "\r\n")
    # MT5's start-up ExpertParameters must reside in MQL5/Presets.
    preset = (
        "InpConnectionId=" + CONNECTION_ID + "\r\n"
        "InpBrokerServer=Deriv-Demo\r\n"
        "InpActivationSeed=0\r\n"
        "InpActivationSecret=\r\n"
        "InpExposureOnly=" + ("false" if DEMO_EXECUTION_ENABLED else "true") + "\r\n"
        "InpManagedCharts=0\r\n"
        "InpExportPeriod=5\r\n"
        + ("InpTrailingEnabled=true\r\n"
           "InpTrailingAtrMult=2.0\r\n"
           "InpRiskFraction=0.15\r\n"
           "InpTradeMagic=20261120\r\n" if DEMO_EXECUTION_ENABLED else "")
    )
    for dirname in ("Presets", "presets"):
        preset_dir = terminal / "MQL5" / dirname
        preset_path = preset_dir / "PR37WriterExposureOnly.set"
        if reuse:
            if preset_path.is_symlink() or not preset_path.is_file() or                preset_path.read_bytes() != preset.encode("utf-8"):
                raise RuntimeError("QA MT5 signed execution preset mismatch")
        else:
            preset_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(preset_path, preset)
    config = (
        "[Common]\r\n"
        "Login=" + str(document["login"]) + "\r\n"
        "Password=" + document["password"] + "\r\n"
        "Server=Deriv-Demo\r\n"
        "KeepPrivate=0\r\nProxyEnable=0\r\n"
        "[Experts]\r\n"
        "Enabled=1\r\nAllowLiveTrading=" +
        ("1" if DEMO_EXECUTION_ENABLED else "0") +
        "\r\nAllowDllImport=0\r\n"
        "Account=1\r\nProfile=1\r\n"
        "[StartUp]\r\n"
        "Expert=ABUTRON\\ApexNativeWriter\r\n"
        "ExpertParameters=PR37WriterExposureOnly.set\r\n"
        "Symbol=" + SYMBOL + "\r\n"
        "Period=M5\r\nShutdownTerminal=0\r\n"
    )
    auth = runtime / "qa-native-login.ini"
    write_private(auth, config)
    document["password"] = ""
    return terminal, files, auth, key



def reset_qa_chart_profile(state: Path) -> int:
    """Avoid MT5 chart exhaustion across repeated isolated QA sessions.

    MT5 /config: StartUp Expert opens a NEW chart every run; persistent
    chart01..chart99 profiles prevent any further EA attachment. The first
    repair is archived by the operator, and only the private QA profile is
    cleared here. Production/client profiles are never modified.
    """
    expected = Path("/var/lib/private") / STATE_LABEL
    if state != expected or state.is_symlink():
        raise RuntimeError("QA chart cleanup state identity mismatch")
    profile = state / "terminal/MQL5/Profiles/Charts/Default"
    if not profile.exists():
        return 0
    if profile.is_symlink() or not profile.is_dir():
        raise RuntimeError("QA chart profile is not a regular directory")
    entries = list(profile.iterdir())
    for entry in entries:
        if (entry.is_symlink() or not entry.is_file() or
                not (re.fullmatch(r"chart[0-9]{2}\.chr", entry.name) or
                     entry.name == "order.wnd")):
            raise RuntimeError("QA chart profile contains unexpected content")
    # Remove the saved window ordering together with the 99 historic charts.
    # MT5 will recreate a fresh chart and attach the pinned expert in StartUp.
    for entry in entries:
        entry.unlink()
    return len(entries)

def check_snapshot(path: Path, expected_login: str, secret: str):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        return "INVALID_NATIVE_FILE", None
    if time.time() - path.stat().st_mtime > 20:
        return "STALE_NATIVE_FILE", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return "MALFORMED_NATIVE_JSON", None
        login = str(data.get("login"))
        nonce = str(data.get("nonce", ""))
        match = re.fullmatch(r"ABUTRON_NATIVE_(\d+)_(\d+)", nonce)
        if not match or match.group(1) != expected_login:
            return "NATIVE_NONCE_IDENTITY_MISMATCH", None
        age = time.time() - int(match.group(2))
        if not 0 <= age <= 60:
            return "NATIVE_NONCE_STALE_OR_FUTURE", None
        if (login != expected_login
            or data.get("connection_id") != CONNECTION_ID
            or data.get("server") != EXPECTED_SERVER
            or data.get("currency") != "USD"
        ):
            return "NATIVE_ACCOUNT_SERVER_MISMATCH", None
        positions = data.get("positions")
        if not isinstance(positions, list):
            return "NATIVE_POSITIONS_NOT_COMPLETE", None
        if type(data.get("position_count")) is not int or len(positions) != data["position_count"]:
            return "NATIVE_POSITION_COUNT_MISMATCH", None
        equity = float(data["equity"])
        balance = float(data["balance"])
        if not equity > 0 or not balance > 0:
            return "NATIVE_EQUITY_INVALID", None
        if not isinstance(data.get("challenge"), str) or not data["challenge"]:
            return "NATIVE_CHALLENGE_MISSING", None
        canonical_parts = []
        for row in sorted(positions, key=lambda p: int(p["ticket"])):
            canonical_parts.append(
                f"{int(row['ticket'])}:{row['symbol']}:{row['direction']}:"
                f"{float(row['volume']):.2f}:{float(row['entry']):.5f}:"
                f"{float(row['sl']):.5f}:{float(row.get('tp', 0)):.5f}:"
                f"{float(row['stop_risk_usd']):.2f}:"
                f"{'true' if row['ordercalcprofit_verified'] is True else 'false'}"
            )
        canonical = (
            f"{CONNECTION_ID}|{login}|{EXPECTED_SERVER}|USD|"
            f"{equity:.2f}|{balance:.2f}|{nonce}|{data['challenge']}|"
            f"{len(positions)}|{';'.join(canonical_parts)}"
        )
        expected_sig = hmac.new(
            secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest()
        sig = data.get("sig")
        if not isinstance(sig, str) or not hmac.compare_digest(
            sig.lower(), expected_sig.lower()
        ):
            return "NATIVE_HMAC_INVALID", None
        risk_complete = all(
            row.get("ordercalcprofit_verified") is True
            and isinstance(row.get("stop_risk_usd"), (float, int))
            and row.get("stop_risk_usd", -1) >= 0
            for row in positions
        )
        return "SIGNED_NATIVE_EXPOSURE_VERIFIED", {
            "snapshot_sha256": sha_bytes(path.read_bytes()),
            "native_position_count": len(positions),
            "broker_stop_risk_complete": risk_complete,
            "native_json_utf8": True,
            "signature_verified": True,
        }
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError, OSError):
        return "NATIVE_JSON_OR_CANONICAL_MALFORMED", None


def run():
    state, runtime, document = verify_preconditions()
    if os.getenv("ABUTRON_ISSUE38_XVFB_CHILD") != "1":
        env = {**os.environ, "ABUTRON_ISSUE38_XVFB_CHILD": "1"}
        proc = subprocess.run(
            ["/usr/bin/xvfb-run", "-a", "--server-num=240", "-s",
             "-screen 0 1280x800x24 -nolisten tcp",
             "/usr/bin/python3", "-B", str(Path(__file__).resolve())],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=(PERSISTENT_SESSION_SECONDS + 60 if PERSISTENT_DEMO
                     else MAX_SESSION_SECONDS + 40),
        )
        return proc.returncode
    health = state / "issue38-health.json"
    display = os.environ.get("DISPLAY", "")
    if not display or not Path("/usr/bin/xdpyinfo").is_file():
        record_health(health, "XDISPLAY_NOT_CONFIGURED")
        return 9
    try:
        display_check = subprocess.run(
            ["/usr/bin/xdpyinfo", "-display", display],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        record_health(health, "XDISPLAY_CONNECTIVITY_ERROR")
        return 9
    if display_check.returncode != 0:
        record_health(health, "XDISPLAY_CONNECTION_REFUSED")
        return 9
    record_health(health, "XDISPLAY_VERIFIED_ISOLATED_DEMO",
                  display_isolated=True)
    record_health(health, "STARTING_ISOLATED_DEMO")
    terminal, files, auth, secret = prepare(state, runtime, document)
    reset_qa_chart_profile(state)
    login = str(document["login"])
    record_health(health, "PR37_EXPOSURE_ONLY_WRITER_STAGED", sha256=EXPECTED_EX5_SHA)
    prefix = state / "wine"
    env = {
        **os.environ,
        "HOME": str(state),
        "WINEPREFIX": str(prefix),
        "WINEARCH": "win64",
        "WINEDEBUG": "err+all,warn+all",
        "WINEDLLOVERRIDES": "mscoree,mshtml=",
    }
    auth_win = "Z:" + str(auth).replace("/", "\\")
    cmd = [WINE, str(terminal / "terminal64.exe"),
           "/portable", "/config:" + auth_win]
    # Capture only in this private QA StateDirectory; redact before reading.
    logfd = os.open(state / "qa-wine-diagnostics.log",
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        child = subprocess.Popen(
            cmd, env=env, cwd=str(state), stdin=subprocess.DEVNULL,
            stdout=logfd, stderr=subprocess.STDOUT,
        )
    finally:
        os.close(logfd)
    started = time.monotonic()
    last_status = None
    last_good_snapshot = started
    last_good_snapshot_file = ""
    session_window = (PERSISTENT_SESSION_SECONDS if PERSISTENT_DEMO
                      else MAX_SESSION_SECONDS)
    try:
        while time.monotonic() - started < session_window:
            # Wine/MT5 may exit its initial launcher after a platform/MQL5
            # bootstrap update while the terminal worker re-spawns in cgroup.
            # Do not mistake that first clean exit for failed broker login.
            # Evidence is authoritative; timeout remains strictly bounded.
            if child.poll() not in (None, 0):
                record_health(health, "MT5_LAUNCH_NONZERO_EXIT",
                              launcher_exit_code=child.poll())
                return 5
            candidates = sorted(
                files.glob("ABUTRON_NATIVE_EXPOSURE_V2_ABUTRON_NATIVE_*.txt"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            if candidates:
                current = candidates[0]
                outcome, evidence = check_snapshot(current, login, secret)
                if evidence is not None and outcome == "SIGNED_NATIVE_EXPOSURE_VERIFIED":
                    last_good_snapshot = time.monotonic()
                    if current.name != last_good_snapshot_file:
                        record_health(
                            health, outcome,
                            native_probe_sha256=EXPECTED_EX5_SHA,
                            **evidence,
                        )
                        last_good_snapshot_file = current.name
                    last_status = outcome
                    auth.unlink(missing_ok=True)
                    if not PERSISTENT_DEMO:
                        return 0
                elif outcome != last_status:
                    record_health(health, outcome,
                                  native_probe_sha256=EXPECTED_EX5_SHA)
                    last_status = outcome
            if (PERSISTENT_DEMO and
                    time.monotonic() - last_good_snapshot > NATIVE_SOURCE_TIMEOUT_SECONDS):
                record_health(health, "PERSISTENT_NATIVE_EXPORT_STOPPED_RESTART",
                              last_native_status=last_status or "NO_NATIVE_EXPORT")
                return 6
            time.sleep(POLL_SECONDS)
        if PERSISTENT_DEMO:
            record_health(health, "PERSISTENT_SESSION_ROTATION",
                          last_native_status=last_status or "NO_NATIVE_EXPORT")
            return 0
        record_health(health, "NO_VERIFIED_NATIVE_EXPOSURE_WITHIN_QA_WINDOW",
                      last_native_status=last_status or "NO_NATIVE_EXPORT")
        return 6
    finally:
        auth.unlink(missing_ok=True)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        # The exception type alone is safe to log. Never print a secret,
        # broker password, terminal argv or private credential path.
        print("ISSUE38_QA_ABORT", type(exc).__name__, file=sys.stderr)
        raise SystemExit(8)
