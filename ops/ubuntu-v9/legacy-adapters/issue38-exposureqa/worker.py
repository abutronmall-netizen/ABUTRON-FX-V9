#!/usr/bin/python3
"""Issue38: bounded, isolated Deriv DEMO exposure evidence probe.

This service reuses systemd-encrypted DEMO credentials, NOT production MT5
terminal/profile state. The pinned EA has no order submission functions.
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

STATE_LABEL = "abutron-issue38-deriv6207-exposureqa"
CONNECTION_ID = "issue38-deriv-demo-6207"
EXPECTED_BROKER = "Deriv"
EXPECTED_SERVER = "Deriv-Demo"
EXPECTED_ACCOUNT_SUFFIX = "6207"
EXPECTED_ID = __import__("os").environ["ABUTRON_CRM_ACCOUNT_ID_6207"]
EXPECTED_EX5_SHA = "7540e51c752df990edc669fa6fd004e70d8eef6ad88e3ab22b00df6a3a469e21"
SOURCE_EX5 = Path("/opt/abutron-issue38-exposureqa/ApexNativeExposureProbe.ex5")
BASE = Path("/opt/abutron/client-mt5-template-v3-usd-50-100-canary-20261103")
WINE = "/opt/abutron/apex-wine-source/bin/wine"
SYMBOL = "Volatility 10 Index.0"
POLL_SECONDS = 3
MAX_SESSION_SECONDS = 150


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
        "native_order_functions_compiled": False,
        "automated_trading_configured": False,
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
        raise RuntimeError("pinned NO-ORDER EA source missing")
    if sha_bytes(source.read_bytes()) != EXPECTED_EX5_SHA:
        raise RuntimeError("NO-ORDER EX5 SHA-256 mismatch")
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
        staged = experts / "ApexNativeExposureProbe.ex5"
        if staged.is_symlink() or not staged.is_file() or            sha_bytes(staged.read_bytes()) != EXPECTED_EX5_SHA:
            raise RuntimeError("reused QA EA SHA mismatched")
        allowed = {"ApexNativeExposureProbe.ex5"}
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
        shutil.copy2(SOURCE_EX5, experts / "ApexNativeExposureProbe.ex5")
        if sha_bytes((experts / "ApexNativeExposureProbe.ex5").read_bytes()) != EXPECTED_EX5_SHA:
            raise RuntimeError("copied QA EA changed checksum")
        key = secrets.token_hex(32)
        write_private(files / "ABUTRON_ACTIVATION_SECRET.txt", key + "\r\n")
    # MT5's start-up ExpertParameters must reside in MQL5/Presets.
    preset = (
        "InpConnectionId=" + CONNECTION_ID + "\r\n"
        "InpBrokerServer=Deriv-Demo\r\n"
        "InpActivationSeed=0\r\n"
        "InpActivationSecret=\r\n"
        "InpExposureOnly=true\r\n"
        "InpManagedCharts=0\r\n"
        "InpExportPeriod=5\r\n"
    )
    for dirname in ("Presets", "presets"):
        preset_dir = terminal / "MQL5" / dirname
        preset_path = preset_dir / "Issue38ExposureProbe.set"
        if reuse:
            if preset_path.is_symlink() or not preset_path.is_file() or                preset_path.read_bytes() != preset.encode("utf-8"):
                raise RuntimeError("QA preset mismatch or not exposure-only")
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
        "Enabled=1\r\nAllowLiveTrading=0\r\nAllowDllImport=0\r\n"
        "Account=1\r\nProfile=1\r\n"
        "[StartUp]\r\n"
        "Expert=ABUTRON\\ApexNativeExposureProbe\r\n"
        "ExpertParameters=Issue38ExposureProbe.set\r\n"
        "Symbol=" + SYMBOL + "\r\n"
        "Period=M5\r\nShutdownTerminal=0\r\n"
    )
    auth = runtime / "qa-native-login.ini"
    write_private(auth, config)
    document["password"] = ""
    return terminal, files, auth, key


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


def fresh_native_candidates(files: Path, run_start_wall: float):
    """Filter old or malicious native files; signature validated separately."""
    candidates = []
    for p in files.glob("ABUTRON_NATIVE_EXPOSURE_V2_ABUTRON_NATIVE_*.txt"):
        if p.is_symlink() or not p.is_file():
            continue
        st = p.stat()
        if (0 < st.st_size <= 1_000_000 and
            run_start_wall - 1 <= st.st_mtime <= time.time() + 5):
            candidates.append(p)
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def reset_qa_chart_capacity(state: Path, terminal: Path):
    """Preserve original QA charts once; keep later no-order QA profiles bounded.

    Called before Wine startup, only from the already-account-bound isolated
    DynamicUser QA state. Never touches any production terminal/profile.
    """
    profiles = terminal / "MQL5" / "Profiles" / "Charts"
    if profiles.is_symlink():
        raise RuntimeError("isolated QA chart folder symlink")
    profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
    active = profiles / "Default"
    first = state / "issue38-original-pre-fix-qa-charts"
    previous = state / "issue38-previous-qa-session-charts"
    if active.is_symlink() or first.is_symlink() or previous.is_symlink():
        raise RuntimeError("isolated QA chart archive symlink")
    if not active.exists():
        active.mkdir(mode=0o700)
        return
    if not active.is_dir():
        raise RuntimeError("isolated QA chart profile not directory")
    items = list(active.iterdir())
    if len(items) > 150:
        raise RuntimeError("unbounded isolated QA chart profile")
    for item in items:
        if (item.is_symlink() or not item.is_file() or
            (item.name != "order.wnd" and
             re.fullmatch(r"chart[0-9]{2,3}\.chr", item.name) is None) or
            item.stat().st_size > 2_000_000):
            raise RuntimeError("unknown or untrusted QA chart profile artifact")
    if not items:
        return
    if not first.exists():
        # Original pre-fix 99-chart profile archived intact and never pruned.
        active.rename(first)
    else:
        if not first.is_dir():
            raise RuntimeError("original QA chart backup invalid")
        if previous.exists():
            if not previous.is_dir():
                raise RuntimeError("previous QA chart backup invalid")
            past = list(previous.iterdir())
            if (len(past) > 3 or any(
                x.is_symlink() or not x.is_file() or
                (x.name != "order.wnd" and
                 re.fullmatch(r"chart[0-9]{2,3}\.chr", x.name) is None)
                for x in past)):
                raise RuntimeError("untrusted QA previous-session archive")
            shutil.rmtree(previous)
        active.rename(previous)
    active.mkdir(mode=0o700)


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
            timeout=MAX_SESSION_SECONDS + 40,
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
    # A reused QA profile accumulated 99 charts; MT5 logged
    # "open charts limit reached" and never attached the NO-ORDER expert.
    reset_qa_chart_capacity(state, terminal)
    login = str(document["login"])
    record_health(health, "NO_ORDER_PROBE_INSTALLED", sha256=EXPECTED_EX5_SHA)
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
    # Establish the start fence BEFORE Wine or the EA can publish a receipt.
    run_start_wall = time.time()
    try:
        child = subprocess.Popen(
            cmd, env=env, cwd=str(state), stdin=subprocess.DEVNULL,
            stdout=logfd, stderr=subprocess.STDOUT,
        )
    finally:
        os.close(logfd)
    started = time.monotonic()
    last_status = None
    try:
        while time.monotonic() - started < MAX_SESSION_SECONDS:
            # Wine/MT5 may exit its initial launcher after a platform/MQL5
            # bootstrap update while the terminal worker re-spawns in cgroup.
            # Do not mistake that first clean exit for failed broker login.
            # Evidence is authoritative; timeout remains strictly bounded.
            if child.poll() not in (None, 0):
                record_health(health, "MT5_LAUNCH_NONZERO_EXIT",
                              launcher_exit_code=child.poll())
                return 5
            # Never re-accept an old exposure receipt from a reused profile.
            # A fresh native producer must write during THIS DEMO invocation;
            # full account/server/nonce/HMAC validation remains in check_snapshot.
            candidates = fresh_native_candidates(files, run_start_wall)
            if candidates:
                outcome, evidence = check_snapshot(candidates[0], login, secret)
                if outcome != last_status:
                    record_health(
                        health, outcome,
                        native_probe_sha256=EXPECTED_EX5_SHA,
                        **(evidence or {}),
                    )
                    last_status = outcome
                if evidence is not None:
                    auth.unlink(missing_ok=True)
                    return 0
            time.sleep(POLL_SECONDS)
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
