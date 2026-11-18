"""Gateway configuration.

Deliberately independent of ``abutron.config`` so the gateway can be
embedded (or tested) without the engine's filesystem side effects. Every
setting is env-overridable; secrets have development defaults that the
service loudly refuses to treat as production-grade.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").strip().lower() in (
        "1", "true", "yes", "on")


def _data_root() -> str:
    return os.getenv("ABUTRON_DATA_ROOT", os.path.abspath("data"))


@dataclass(frozen=True)
class GatewaySettings:
    """Immutable runtime configuration for one gateway process."""

    db_path: str
    host: str = "0.0.0.0"
    port: int = 19744

    # --- Secrets -------------------------------------------------------
    # api_key_pepper salts the stored key hash. bridge_secret signs the
    # per-account token an MT5 bridge EA presents. Both must be rotated
    # (and set from a secret manager) in production.
    api_key_pepper: str = "dev-pepper-change-me"
    bridge_secret: str = "dev-bridge-secret-change-me"

    # --- Command plane -------------------------------------------------
    # How long a REST trade call waits for the account bridge to answer
    # before returning 202 + a command id to poll.
    command_timeout_s: float = 8.0
    bridge_poll_max_s: int = 30
    bridge_stale_after_s: int = 45

    # --- Auth / abuse --------------------------------------------------
    default_rate_limit_per_min: int = 600
    trade_rate_limit_per_min: int = 120
    max_accounts_per_tenant: int = 50

    # --- Webhooks ------------------------------------------------------
    webhooks_enabled: bool = True
    webhook_worker: bool = True
    webhook_interval_s: float = 2.0
    webhook_timeout_s: float = 5.0
    webhook_max_attempts: int = 5

    # --- Behaviour -----------------------------------------------------
    allow_simulated_accounts: bool = True
    default_market_data_mode: str = "simulated"
    guard_automation: bool = True
    # Mirror engine-managed accounts read-only into the API. Safe by design
    # (no second writer), so it is on by default; disable if the deployment
    # should only expose accounts explicitly created through the gateway.
    adopt_engine_accounts: bool = True
    engine_tenant_id: str = "tnt_platform"
    engine_tenant_name: str = "Platform (read-only)"
    landing_page: bool = True
    demo_enabled: bool = True
    demo_api_key: str = "abk_demo_0123456789abcdef0123456789abcdef"
    demo_balance: float = 10_000.0
    # When set, POST /v1/admin/tenants accepts X-Admin-Secret to mint a
    # tenant + key. Empty (the default) disables the endpoint entirely.
    admin_secret: str = ""
    cors_origins: Tuple[str, ...] = ("*",)

    # --- Transport defaults -------------------------------------------
    default_symbols: Tuple[str, ...] = (
        "EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US500")
    symbol_suffix_hints: Tuple[str, ...] = (".r", ".pro", "m")

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        data_root = _data_root()
        symbols = tuple(s.strip() for s in
                        _env("ABUTRON_GATEWAY_DEFAULT_SYMBOLS",
                             "EURUSD,GBPUSD,USDJPY,XAUUSD,US500").split(",")
                        if s.strip())
        suffixes = tuple(s.strip() for s in
                         _env("ABUTRON_GATEWAY_SYMBOL_SUFFIXES",
                              ".r,.pro,m").split(",") if s.strip())
        return cls(
            db_path=_env("ABUTRON_GATEWAY_DB",
                         os.path.join(data_root, "gateway.db")),
            host=_env("ABUTRON_GATEWAY_HOST", "0.0.0.0"),
            port=int(_env("ABUTRON_GATEWAY_PORT", "19744")),
            api_key_pepper=_env("ABUTRON_GATEWAY_PEPPER",
                                "dev-pepper-change-me"),
            bridge_secret=_env("ABUTRON_GATEWAY_BRIDGE_SECRET",
                               "dev-bridge-secret-change-me"),
            command_timeout_s=float(_env("ABUTRON_GATEWAY_COMMAND_TIMEOUT",
                                         "8")),
            bridge_poll_max_s=int(_env("ABUTRON_GATEWAY_POLL_MAX", "30")),
            bridge_stale_after_s=int(_env("ABUTRON_GATEWAY_BRIDGE_STALE",
                                          "45")),
            default_rate_limit_per_min=int(_env(
                "ABUTRON_GATEWAY_RATE_LIMIT", "600")),
            trade_rate_limit_per_min=int(_env(
                "ABUTRON_GATEWAY_TRADE_RATE_LIMIT", "120")),
            max_accounts_per_tenant=int(_env(
                "ABUTRON_GATEWAY_MAX_ACCOUNTS", "50")),
            webhooks_enabled=_bool("ABUTRON_GATEWAY_WEBHOOKS", True),
            webhook_worker=_bool("ABUTRON_GATEWAY_WEBHOOK_WORKER", True),
            webhook_interval_s=float(_env(
                "ABUTRON_GATEWAY_WEBHOOK_INTERVAL", "2")),
            webhook_timeout_s=float(_env(
                "ABUTRON_GATEWAY_WEBHOOK_TIMEOUT", "5")),
            webhook_max_attempts=int(_env(
                "ABUTRON_GATEWAY_WEBHOOK_ATTEMPTS", "5")),
            allow_simulated_accounts=_bool(
                "ABUTRON_GATEWAY_ALLOW_SIMULATED", True),
            default_market_data_mode=_env(
                "ABUTRON_GATEWAY_DEFAULT_MODE", "simulated").strip().lower(),
            guard_automation=_bool("ABUTRON_GATEWAY_GUARD_AUTOMATION", True),
            adopt_engine_accounts=_bool(
                "ABUTRON_GATEWAY_ADOPT_AUTOMATION", True),
            engine_tenant_id=_env("ABUTRON_GATEWAY_ENGINE_TENANT",
                                  "tnt_platform"),
            engine_tenant_name=_env("ABUTRON_GATEWAY_ENGINE_TENANT_NAME",
                                    "Platform (read-only)"),
            landing_page=_bool("ABUTRON_GATEWAY_LANDING", True),
            demo_enabled=_bool("ABUTRON_GATEWAY_DEMO", True),
            demo_api_key=_env(
                "ABUTRON_GATEWAY_DEMO_KEY",
                "abk_demo_0123456789abcdef0123456789abcdef"),
            demo_balance=float(_env("ABUTRON_GATEWAY_DEMO_BALANCE",
                                    "10000")),
            admin_secret=_env("ABUTRON_GATEWAY_ADMIN_SECRET", ""),
            cors_origins=tuple(
                o.strip() for o in _env("ABUTRON_GATEWAY_CORS_ORIGINS", "*")
                .split(",") if o.strip()) or ("*",),
            default_symbols=symbols or ("EURUSD", "GBPUSD", "XAUUSD"),
            symbol_suffix_hints=suffixes or (".r",),
        )

    # ------------------------------------------------------------------
    def is_insecure(self) -> bool:
        return (self.api_key_pepper == "dev-pepper-change-me"
                or self.bridge_secret == "dev-bridge-secret-change-me")
