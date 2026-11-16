"""Optimized runtime rules that are deliberately *not* neural weights.

Sniper TP/SL geometry, entry filters and the Chandelier trailing multiple are
execution rules. They are optimized on held-out paper trades and loaded from
``ABUTRON_RUNTIME_RULES``; the neural brain learns signal features only. This
separation is intentional: equity, broker lot constraints and risk caps must
remain deterministic and auditable.
"""
from __future__ import annotations

import json
import os
import tempfile
from functools import lru_cache
from typing import Any, Dict, Optional

from ..config import utc_now

RUNTIME_RULES_ENV = "ABUTRON_RUNTIME_RULES"
RULE_VERSION = "runtime_rules_v1"
DEFAULT_TRAIL_K = 3.0


def configured_path(explicit: Optional[str] = None) -> str:
    return os.path.abspath(str(explicit or os.getenv(RUNTIME_RULES_ENV, "") or ""))


@lru_cache(maxsize=16)
def _read_rules(path: str, mtime_ns: int) -> Dict[str, Any]:
    del mtime_ns  # cache key only
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
        return dict(data["symbols"])
    return dict(data) if isinstance(data, dict) else {}


def load_runtime_rules(path: Optional[str] = None) -> Dict[str, Any]:
    p = configured_path(path)
    mtime = os.stat(p).st_mtime_ns if p and os.path.isfile(p) else 0
    return dict(_read_rules(p, mtime))


def invalidate_cache() -> None:
    _read_rules.cache_clear()


def runtime_rule_for(symbol: str, path: Optional[str] = None) -> Dict[str, Any]:
    rule = load_runtime_rules(path).get(symbol, {})
    return dict(rule) if isinstance(rule, dict) else {}


def trail_k_for(symbol: str, *, default: float = DEFAULT_TRAIL_K,
                path: Optional[str] = None) -> float:
    try:
        return float(runtime_rule_for(symbol, path).get("trail_k", default))
    except (TypeError, ValueError):
        return float(default)


def write_runtime_rules(path: str, symbols: Dict[str, Dict[str, Any]],
                        *, metadata: Optional[Dict[str, Any]] = None) -> str:
    """Atomically write a versioned runtime-rules file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    payload = {
        "version": RULE_VERSION,
        "generated_at": utc_now(),
        "metadata": metadata or {},
        "symbols": symbols,
    }
    fd, tmp = tempfile.mkstemp(prefix=".runtime_rules.", suffix=".json",
                               dir=os.path.dirname(os.path.abspath(path)) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    invalidate_cache()
    return path
