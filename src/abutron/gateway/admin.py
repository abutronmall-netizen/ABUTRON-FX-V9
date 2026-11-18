"""Operator CLI for tenant/key bootstrap (no HTTP admin secret needed).

    python -m abutron.gateway.admin create-tenant --name "Acme Ltd"
    python -m abutron.gateway.admin list-tenants
    python -m abutron.gateway.admin create-key --tenant tnt_...

Uses the same database and pepper as the API process, so run it with the
same ``ABUTRON_DATA_ROOT`` / ``ABUTRON_GATEWAY_*`` environment.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from .auth import ApiKeyService
from .settings import GatewaySettings
from .store import GatewayStore


def create_tenant(store: GatewayStore, keys: ApiKeyService, name: str, *,
                  scopes: Optional[List[str]] = None,
                  rate_limit_per_min: Optional[int] = None
                  ) -> Dict[str, Any]:
    tenant_id = store.create_tenant(name)
    raw, key_id = keys.create_key(
        tenant_id, label="operator", scopes=scopes,
        rate_limit_per_min=rate_limit_per_min)
    return {"tenant_id": tenant_id, "key_id": key_id, "api_key": raw,
            "scopes": scopes or ["read", "trade", "manage"]}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="abutron-gateway-admin",
        description="Manage gateway tenants and API keys.")
    parser.add_argument("--db", default=None,
                        help="override the gateway SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-tenant", help="mint a tenant + API key")
    create.add_argument("--name", default="")
    create.add_argument("--scopes", default="read,trade,manage")
    create.add_argument("--rate-limit", type=int, default=None)

    key = sub.add_parser("create-key", help="mint another key for a tenant")
    key.add_argument("--tenant", required=True)
    key.add_argument("--label", default="operator")
    key.add_argument("--scopes", default="read,trade,manage")
    key.add_argument("--rate-limit", type=int, default=None)

    sub.add_parser("list-tenants", help="list tenants and key prefixes")

    args = parser.parse_args(argv)
    settings = GatewaySettings.from_env()
    store = GatewayStore(args.db or settings.db_path)
    store.init()
    keys = ApiKeyService(store, settings)

    if args.command == "create-tenant":
        result = create_tenant(
            store, keys, args.name,
            scopes=[s.strip() for s in args.scopes.split(",") if s.strip()],
            rate_limit_per_min=args.rate_limit)
        print(json.dumps(result, indent=2))
        print("\nStore this key now: it is not recoverable.", file=sys.stderr)
        return 0

    if args.command == "create-key":
        if store.get_tenant(args.tenant) is None:
            print(f"tenant not found: {args.tenant}", file=sys.stderr)
            return 2
        raw, key_id = keys.create_key(
            args.tenant, label=args.label,
            scopes=[s.strip() for s in args.scopes.split(",") if s.strip()],
            rate_limit_per_min=args.rate_limit)
        print(json.dumps({"tenant_id": args.tenant, "key_id": key_id,
                          "api_key": raw}, indent=2))
        print("\nStore this key now: it is not recoverable.", file=sys.stderr)
        return 0

    if args.command == "list-tenants":
        print(json.dumps(store.list_tenants(), indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
