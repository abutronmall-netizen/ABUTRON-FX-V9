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


def verify_file_pin(path: str, expected_sha256: Optional[str]) -> Tuple[bool, str]:
    """Verify compiled binary against expected pin; returns (ok, actual)."""
    if not os.path.exists(path):
        return False, "MISSING"
    actual = pin_binary(path)
    if expected_sha256 is None:
        return False, actual
    return hmac.compare_digest(actual, expected_sha256), actual
