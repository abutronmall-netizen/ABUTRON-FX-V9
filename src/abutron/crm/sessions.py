"""Session management + client authentication (blueprint Identity layer).

Implements secure HttpOnly-cookie sessions for the client portal. A session
token is issued once at login; only its SHA-256 hash is persisted. Every
client API call is scoped to the authenticated user's `owner_id`, so one
customer cannot reach another tenant's accounts (blueprint C15).

This module is intentionally transport-agnostic: it issues/verifies/revokes
tokens and exposes a FastAPI dependency that reads the session cookie.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..config import utc_now
from ..db import portal_db

SESSION_COOKIE = "abutron_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _future(seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= exp
    except ValueError:
        return False


class SessionStore:
    """Issue, verify and revoke portal sessions (hashed tokens at rest)."""

    def issue(self, user: Dict[str, Any]) -> Dict[str, Any]:
        token = secrets.token_urlsafe(32)
        sess = {
            "session_id": f"s_{secrets.token_hex(8)}",
            "user_id": user["user_id"],
            "username": user["username"],
            "role": user["role"],
            "owner_id": user.get("owner_id", ""),
            "token_hash": _hash_token(token),
            "created_at": utc_now(),
            "expires_at": _future(SESSION_TTL_SECONDS),
            "revoked": 0,
        }
        portal_db.create_session(sess)
        return {**sess, "token": token}

    def verify(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        sess = portal_db.get_session_by_token(_hash_token(token))
        if sess is None or sess["revoked"] == 1:
            return None
        if _is_expired(sess.get("expires_at", "")):
            return None
        return sess

    def revoke(self, token: Optional[str]) -> bool:
        if not token:
            return False
        sess = portal_db.get_session_by_token(_hash_token(token))
        if sess is None:
            return False
        return portal_db.revoke_session(sess["session_id"])

    def revoke_all_for_user(self, user_id: str) -> int:
        # Best-effort: revoke all non-revoked sessions for a user.
        count = 0
        for sess in portal_db.list_sessions(user_id):
            if sess["revoked"] == 0:
                portal_db.revoke_session(sess["session_id"])
                count += 1
        return count


session_store = SessionStore()
