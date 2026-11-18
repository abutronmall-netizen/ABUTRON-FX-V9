"""Hashing, signing and verification utilities.

Used for: source/binary pins (EA SHA256), candidate identity, receipt
integrity and activation revisions. Evidence artifacts are root-owned on
the Ubuntu host; here we only provide the primitives.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional, Tuple


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hmac_sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def hmac_verify(secret: str, message: str, signature: str) -> bool:
    return hmac.compare_digest(hmac_sign(secret, message), signature)


def pin_binary(path: str) -> str:
    """SHA256 pin of a compiled artifact (EA EX5 / worker binary)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_activation_revision(connection_id: str, ea_sha256: str,
                             env: str) -> str:
    """Deterministic activation revision binding account + binary + env."""
    return hmac_sign(os.getenv("ABUTRON_ACTIVATION_SECRET", "apex-activation"),
                     f"{connection_id}|{ea_sha256}|{env}")


def make_nonce(prefix: str = "ABUTRON_NATIVE") -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def verify_nonce(nonce: str, prefix: str = "ABUTRON_NATIVE") -> bool:
    return nonce.startswith(prefix + "_") and len(nonce) == len(prefix) + 1 + 32


def parse_native_nonce(nonce: str) -> Optional[Tuple[str, int]]:
    """Parse an EA-emitted native nonce ``ABUTRON_NATIVE_<login>_<ts>``.

    Returns ``(login, timestamp)`` or ``None`` if the format is invalid.
    The EA's Nonce() emits this login+timestamp form; the timestamp is
    checked for freshness by the caller (replay protection).
    """
    parts = nonce.split("_")
    # ABUTRON_NATIVE_<login>_<ts> -> [ABUTRON, NATIVE, login, ts]
    if len(parts) != 4 or parts[0] != "ABUTRON" or parts[1] != "NATIVE":
        return None
    login, ts_str = parts[2], parts[3]
    try:
        return login, int(ts_str)
    except ValueError:
        return None


def build_exposure_canonical(connection_id: str, login: str, equity: float,
                             nonce: str, positions: list) -> str:
    """Build the canonical message the EA HMAC-signs for an exposure snapshot.

    Must match the MQL5 canonical exactly:
    ``connection_id|login|equity(2dp)|nonce|position_count|positions_canonical``
    where positions_canonical is positions sorted by ticket, each
    ``ticket:symbol:direction:volume(2dp):sl(5dp)``, joined by ``;``.
    """
    equity_str = f"{float(equity):.2f}"
    sorted_pos = sorted(positions, key=lambda p: int(p.get("ticket", 0)))
    parts = []
    for p in sorted_pos:
        parts.append(f"{int(p.get('ticket', 0))}:{p.get('symbol', '')}:"
                     f"{p.get('direction', '')}:"
                     f"{float(p.get('volume', 0)):.2f}:"
                     f"{float(p.get('sl', 0)):.5f}")
    positions_canonical = ";".join(parts)
    return (f"{connection_id}|{login}|{equity_str}|{nonce}|"
            f"{len(positions)}|{positions_canonical}")


def activation_secret() -> str:
    """The shared HMAC secret used to authenticate EA/worker messages."""
    return os.getenv("ABUTRON_ACTIVATION_SECRET", "apex-activation")


def verify_file_pin(path: str, expected_sha256: Optional[str]) -> Tuple[bool, str]:
    """Verify compiled binary against expected pin; returns (ok, actual)."""
    if not os.path.exists(path):
        return False, "MISSING"
    actual = pin_binary(path)
    if expected_sha256 is None:
        return False, actual
    return hmac.compare_digest(actual, expected_sha256), actual
