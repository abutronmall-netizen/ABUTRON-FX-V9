"""Signed outbound webhooks with persisted retries.

Every delivery is persisted before the first attempt, so a gateway crash
does not lose events. Retries back off and stop after
``webhook_max_attempts``. Receivers verify:

    X-Abutron-Signature: t=<unix>,v1=<hex hmac_sha256(secret, f"{t}.{body}")>

The secret is shown once when the webhook is created.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

import httpx

from .settings import GatewaySettings
from .store import GatewayStore, utc_now

BACKOFF_SECONDS = (2, 10, 30, 120, 600)


def sign_payload(secret: str, timestamp: int, body: str) -> str:
    message = f"{timestamp}.{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message,
                    hashlib.sha256).hexdigest()


class WebhookDispatcher:
    def __init__(self, store: GatewayStore, settings: GatewaySettings):
        self.store = store
        self.settings = settings
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    def enqueue(self, event_type: str, tenant_id: str,
                data: Dict[str, Any]) -> int:
        if not self.settings.webhooks_enabled:
            return 0
        hooks = self.store.webhooks_for_event(tenant_id, event_type)
        if not hooks:
            return 0
        envelope = {
            "id": f"evt_{int(time.time() * 1000)}",
            "type": event_type, "created_at": utc_now(), "data": data,
        }
        count = 0
        for hook in hooks:
            self.store.create_delivery(
                webhook_id=hook["webhook_id"], tenant_id=tenant_id,
                event_type=event_type, payload=envelope,
                next_attempt_at=time.time())
            count += 1
        return count

    # ------------------------------------------------------------------
    async def run_once(self, client: Optional[httpx.AsyncClient] = None
                       ) -> int:
        deliveries = self.store.due_deliveries(limit=20)
        if not deliveries:
            return 0
        owned = client is None
        http = client or httpx.AsyncClient(
            timeout=self.settings.webhook_timeout_s)
        sent = 0
        try:
            for delivery in deliveries:
                hook = self.store.get_webhook(delivery["tenant_id"],
                                              delivery["webhook_id"])
                if hook is None or not hook["active"]:
                    self.store.finish_delivery(
                        delivery["delivery_id"], status="CANCELLED",
                        attempts=delivery["attempts"],
                        last_error="webhook deleted")
                    continue
                body = json.dumps(delivery["payload"], sort_keys=True,
                                  separators=(",", ":"))
                ts = int(time.time())
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "ABUTRON-MT5-Gateway/0.1",
                    "X-Abutron-Event": delivery["event_type"],
                    "X-Abutron-Delivery": delivery["delivery_id"],
                    "X-Abutron-Signature":
                        f"t={ts},v1={sign_payload(hook['secret'], ts, body)}",
                }
                attempts = delivery["attempts"] + 1
                try:
                    response = await http.post(hook["url"], content=body,
                                               headers=headers)
                    ok = 200 <= response.status_code < 300
                    error = "" if ok else f"HTTP {response.status_code}"
                except Exception as exc:  # network/timeout
                    ok = False
                    error = f"{type(exc).__name__}: {exc}"[:400]
                if ok:
                    self.store.finish_delivery(
                        delivery["delivery_id"], status="DELIVERED",
                        attempts=attempts)
                    sent += 1
                elif attempts >= self.settings.webhook_max_attempts:
                    self.store.finish_delivery(
                        delivery["delivery_id"], status="FAILED",
                        attempts=attempts, last_error=error)
                else:
                    delay = BACKOFF_SECONDS[min(attempts - 1,
                                                len(BACKOFF_SECONDS) - 1)]
                    self.store.finish_delivery(
                        delivery["delivery_id"], status="PENDING",
                        attempts=attempts,
                        next_attempt_at=time.time() + delay,
                        last_error=error)
        finally:
            if owned:
                await http.aclose()
        return sent

    async def loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.webhook_interval_s)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()
