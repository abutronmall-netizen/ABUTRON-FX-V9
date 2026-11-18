"""API keys, scopes, bridge tokens and in-process rate limiting.

Key format: ``abk_<prefix>_<secret>``.

Only ``sha256(pepper + secret)`` is ever persisted, together with the
non-secret prefix used for O(1) lookup. Comparison is constant time. The
raw key is returned exactly once, at creation.

Bridge tokens are stateless HMACs over ``connection_id | salt`` where the
per-account salt is stored on the account row. Rotating the salt (or
deleting the account) invalidates every previously issued token without
touching a token table.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .errors import GatewayError, unauthorized
from .settings import GatewaySettings
from .store import GatewayStore

KEY_PREFIX = "abk"
SCOPES = ("read", "trade", "manage")


# ---------------------------------------------------------------------------
# Key primitives
# ---------------------------------------------------------------------------
def _hash_secret(secret: str, pepper: str) -> str:
    return hashlib.sha256(f"{pepper}:{secret}".encode("utf-8")).hexdigest()


def split_api_key(raw: str) -> Optional[Tuple[str, str]]:
    if not raw or not raw.startswith(KEY_PREFIX + "_"):
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3:
        return None
    _, prefix, secret = parts
    if not prefix or not secret:
        return None
    return prefix, secret


def generate_api_key(prefix_len: int = 8, secret_len: int = 32) -> str:
    return f"{KEY_PREFIX}_{secrets.token_hex(prefix_len // 2)}_" \
           f"{secrets.token_hex(secret_len // 2)}"


@dataclass
class AuthContext:
    tenant_id: str
    key_id: str
    scopes: List[str] = field(default_factory=list)
    rate_limit_per_min: int = 600

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def require(self, scope: str) -> None:
        if not self.has(scope):
            raise GatewayError(403, "FORBIDDEN_SCOPE",
                               f"API key lacks the '{scope}' scope")


# ---------------------------------------------------------------------------
# API key service
# ---------------------------------------------------------------------------
class ApiKeyService:
    def __init__(self, store: GatewayStore, settings: GatewaySettings):
        self.store = store
        self.settings = settings
        self._touched: Dict[str, float] = {}
        self._touch_lock = threading.Lock()

    # ---- issuance ----
    def create_key(self, tenant_id: str, *, label: str = "",
                   scopes: Optional[List[str]] = None,
                   rate_limit_per_min: Optional[int] = None) -> Tuple[str, str]:
        scopes = scopes or list(SCOPES)
        bad = [s for s in scopes if s not in SCOPES]
        if bad:
            raise GatewayError(400, "INVALID_SCOPE",
                               f"unknown scopes: {', '.join(bad)}")
        raw = generate_api_key()
        prefix, secret = split_api_key(raw)  # type: ignore[misc]
        key_id = f"key_{secrets.token_hex(8)}"
        self.store.create_api_key(
            tenant_id=tenant_id, key_id=key_id, prefix=prefix,
            key_hash=_hash_secret(secret, self.settings.api_key_pepper),
            label=label, scopes=scopes,
            rate_limit_per_min=rate_limit_per_min
            or self.settings.default_rate_limit_per_min)
        return raw, key_id

    def register_key(self, raw: str, tenant_id: str, *, label: str = "",
                     scopes: Optional[List[str]] = None,
                     rate_limit_per_min: Optional[int] = None) -> str:
        """Register an externally-chosen key (demo/bootstrap)."""
        parsed = split_api_key(raw)
        if parsed is None:
            raise GatewayError(400, "INVALID_API_KEY_FORMAT",
                               "expected abk_<prefix>_<secret>")
        prefix, secret = parsed
        key_id = f"key_{secrets.token_hex(8)}"
        self.store.create_api_key(
            tenant_id=tenant_id, key_id=key_id, prefix=prefix,
            key_hash=_hash_secret(secret, self.settings.api_key_pepper),
            label=label, scopes=scopes or list(SCOPES),
            rate_limit_per_min=rate_limit_per_min
            or self.settings.default_rate_limit_per_min)
        return key_id

    # ---- verification ----
    def authenticate(self, raw: str) -> Tuple[AuthContext, Dict]:
        parsed = split_api_key(raw)
        if parsed is None:
            raise unauthorized()
        prefix, secret = parsed
        row = self.store.get_api_key_by_prefix(prefix)
        if row is None or row["revoked_at"]:
            raise unauthorized()
        expected = row["key_hash"]
        actual = _hash_secret(secret, self.settings.api_key_pepper)
        if not hmac.compare_digest(expected, actual):
            raise unauthorized()
        now = time.time()
        with self._touch_lock:
            last = self._touched.get(row["key_id"], 0.0)
            if now - last > 60:
                self._touched[row["key_id"]] = now
                should_touch = True
            else:
                should_touch = False
        if should_touch:
            self.store.touch_api_key(row["key_id"])
        ctx = AuthContext(
            tenant_id=row["tenant_id"], key_id=row["key_id"],
            scopes=row["scopes"],
            rate_limit_per_min=row["rate_limit_per_min"])
        return ctx, row


# ---------------------------------------------------------------------------
# Rate limiter (fixed 60s window, per key)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self):
        self._windows: Dict[str, Deque[float]] = {}
        self._totals: Dict[str, int] = {}
        self._trades: Dict[str, int] = {}
        self._lock = threading.Lock()

    def hit(self, key_id: str, limit: int, *, trade: bool = False
            ) -> Tuple[bool, int, float]:
        now = time.monotonic()
        with self._lock:
            window = self._windows.setdefault(key_id, deque())
            while window and now - window[0] >= 60.0:
                window.popleft()
            allowed = len(window) < limit
            if allowed:
                window.append(now)
            remaining = max(0, limit - len(window))
            reset_in = (60.0 - (now - window[0])) if window else 60.0
            self._totals[key_id] = self._totals.get(key_id, 0) + 1
            if trade and allowed:
                self._trades[key_id] = self._trades.get(key_id, 0) + 1
            return allowed, remaining, max(0.0, reset_in)

    def stats(self, key_id: str, limit: int) -> Dict[str, float]:
        now = time.monotonic()
        with self._lock:
            window = self._windows.get(key_id, deque())
            while window and now - window[0] >= 60.0:
                window.popleft()
            return {
                "requests_this_minute": len(window),
                "requests_total": self._totals.get(key_id, 0),
                "trades_total": self._trades.get(key_id, 0),
                "reset_in_s": round(
                    (60.0 - (now - window[0])) if window else 60.0, 1),
            }


# ---------------------------------------------------------------------------
# Bridge tokens
# ---------------------------------------------------------------------------
def derive_bridge_token(secret: str, connection_id: str, salt: str) -> str:
    digest = hmac.new(secret.encode("utf-8"),
                      f"{connection_id}|{salt}".encode("utf-8"),
                      hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_bridge_token(secret: str, connection_id: str, salt: str,
                        presented: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(
        derive_bridge_token(secret, connection_id, salt), presented)
