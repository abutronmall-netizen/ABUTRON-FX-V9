"""Uniform error type + HTTP mapping for the gateway API."""
from __future__ import annotations

from typing import Any, Dict, Optional


class GatewayError(Exception):
    """A client-visible failure with a stable machine code."""

    def __init__(self, status_code: int, code: str, message: str,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(what: str = "resource") -> GatewayError:
    # 404 (not 403) for cross-tenant lookups: never confirm that another
    # tenant's account id exists.
    return GatewayError(404, "NOT_FOUND", f"{what} not found")


def unauthorized(message: str = "missing or invalid API key") -> GatewayError:
    return GatewayError(401, "UNAUTHORIZED", message)


def forbidden_scope(scope: str) -> GatewayError:
    return GatewayError(403, "FORBIDDEN_SCOPE",
                        f"API key lacks the '{scope}' scope")


def conflict(code: str, message: str,
             details: Optional[Dict[str, Any]] = None) -> GatewayError:
    return GatewayError(409, code, message, details)


def bad_request(code: str, message: str,
                details: Optional[Dict[str, Any]] = None) -> GatewayError:
    return GatewayError(400, code, message, details)
