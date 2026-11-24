"""Operator dispatch override for ABUTRON V9 agentic runtime.

Provides an explicit, logged, scoped override that allows the account
owner to consciously activate order dispatch past the policy gates
(``DEMO_NOT_EFFECTIVE_ON`` and ``LIVE_NOT_AUTHORIZED``).

Design rules:

* **Fail-closed by default.** The override is off unless an explicit,
  validated config file or environment variable is present.
* **Explicit and auditable.** Every override must record who authorized
  it, when, and which connection IDs it covers. Every bypass logs the
  gate that was bypassed and the risk state at the time.
* **Scoped.** Overrides apply to specific connection IDs only — never a
  global kill switch that silently disables all gates everywhere.
* **Non-negotiable invariants are NOT bypassable:**
  - Market data errors (data correctness — cannot trade on bad data)
  - Exposure certification inside ``NativeDispatcher`` (20% ceiling,
    0.15%/trade, R:R >= 1.50 — real risk invariants, not policy gates)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


class OverrideError(Exception):
    """Raised when the override config is invalid or tampered with."""


@dataclass
class OperatorOverride:
    """A validated operator dispatch override.

    Attributes:
        authorized_by: operator identity who set this override.
        authorized_at: unix timestamp when set.
        connections: set of connection IDs this override covers.
        reason: human-readable reason for the override.
        active: whether the override is currently in effect.
    """

    authorized_by: str
    authorized_at: float
    connections: Set[str] = field(default_factory=set)
    reason: str = ""
    active: bool = True

    def covers(self, connection_id: str) -> bool:
        """Return True if this override covers the given connection."""
        return self.active and connection_id in self.connections

    def to_dict(self) -> Dict[str, Any]:
        return {
            "authorized_by": self.authorized_by,
            "authorized_at": self.authorized_at,
            "connections": sorted(self.connections),
            "reason": self.reason,
            "active": self.active,
        }


def load_override(
    config_path: Optional[str] = None,
    *,
    env_var: str = "ABUTRON_OPERATOR_OVERRIDE",
) -> Optional[OperatorOverride]:
    """Load an operator override from a JSON file or environment variable.

    Returns None if no override is configured (fail-closed default).

    The config must contain:
    - ``authorized_by``: non-empty operator identity
    - ``connections``: non-empty list of connection IDs
    - ``reason``: non-empty reason string
    - ``active``: optional boolean (default True)

    Raises ``OverrideError`` if the config is present but invalid.
    """
    # Try environment variable first (JSON string).
    env_value = os.environ.get(env_var, "").strip()
    if env_value:
        return _parse_override(env_value, source=f"env:{env_var}")

    # Try config file.
    path = config_path or os.environ.get(
        "ABUTRON_OPERATOR_OVERRIDE_FILE", "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path) as fh:
                raw = fh.read()
        except OSError as exc:
            raise OverrideError(f"cannot read override file {path}: {exc}")
        return _parse_override(raw, source=f"file:{path}")

    return None


def _parse_override(raw: str, *, source: str) -> OperatorOverride:
    """Parse and validate an override from JSON text."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise OverrideError(
            f"override config invalid JSON ({source}): {exc}")

    if not isinstance(data, dict):
        raise OverrideError(f"override config must be an object ({source})")

    authorized_by = str(data.get("authorized_by", "")).strip()
    if not authorized_by:
        raise OverrideError(
            f"override missing authorized_by ({source})")

    connections_raw = data.get("connections", [])
    if not isinstance(connections_raw, list) or not connections_raw:
        raise OverrideError(
            f"override connections must be a non-empty list ({source})")
    connections = {str(c).strip() for c in connections_raw if str(c).strip()}
    if not connections:
        raise OverrideError(
            f"override connections list is empty after cleaning ({source})")

    reason = str(data.get("reason", "")).strip()
    if not reason:
        raise OverrideError(f"override missing reason ({source})")

    active = bool(data.get("active", True))

    return OperatorOverride(
        authorized_by=authorized_by,
        authorized_at=time.time(),
        connections=connections,
        reason=reason,
        active=active,
    )


def revoke_override(override: OperatorOverride) -> OperatorOverride:
    """Revoke an override (set active to False)."""
    override.active = False
    return override
