"""Feishu (Lark) custom-bot notifier (spec §7).

Two things here are counter-intuitive and load-bearing:

1. Signature (spec §7.2): the HMAC-SHA256 *key* is ``f"{timestamp}\\n{secret}"``
   and the signed message is the EMPTY string. ``hmac.new(...)`` is never
   ``.update()``-d. Copying a generic HMAC template (which signs the body) fails.

2. Response check (spec §7.4): a message rejected by the bot's security policy
   can still return HTTP 200. Success is defined solely by ``code == 0`` in the
   JSON body. Checking the status code alone makes alerting fail silently.

Cards are coloured by ``header.template`` — red = threshold breach, grey =
heartbeat failure (spec §7.3) — so a change in the world reads differently from
a fault in this system.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import httpx

from ..collectors.base import make_client
from ..pipeline.alerts import Alert


def gen_sign(secret: str, timestamp: int) -> str:
    """Spec §7.2. key = '{timestamp}\\n{secret}', message = '' (never updated)."""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _card(alert: Alert) -> dict:
    title = "阈值告警" if alert.kind == "threshold" else "采集心跳失败"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": alert.template,
            "title": {"tag": "plain_text", "content": f"{title} · {alert.series_id}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": alert.message}},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"severity={alert.severity} · rule={alert.rule_id}"}
                ],
            },
        ],
    }


class FeishuNotifier:
    def __init__(self, webhook_url: str, secret: str | None = None):
        if not webhook_url:
            raise ValueError("Feishu webhook_url is required")
        self.webhook_url = webhook_url
        self.secret = secret

    async def send(self, alert: Alert, client: httpx.AsyncClient | None = None) -> None:
        """Send one alert card. Raises RuntimeError if body code != 0 (spec §7.4)."""
        payload: dict = {"msg_type": "interactive", "card": _card(alert)}
        if self.secret:
            ts = int(time.time())  # must be within 1h validity (spec §7.2)
            payload["timestamp"] = str(ts)
            payload["sign"] = gen_sign(self.secret, ts)

        owns = client is None
        client = client or make_client()
        try:
            resp = await client.post(self.webhook_url, json=payload)
            resp.raise_for_status()
            body = resp.json()
            if body.get("code", -1) != 0:
                raise RuntimeError(
                    f"Feishu rejected message (HTTP {resp.status_code}): "
                    f"code={body.get('code')} msg={body.get('msg')!r}"
                )
        finally:
            if owns:
                await client.aclose()

    async def send_all(self, alerts: list[Alert]) -> list[tuple[Alert, Exception | None]]:
        """Send every alert; collect per-alert outcome without aborting the batch."""
        results: list[tuple[Alert, Exception | None]] = []
        async with make_client() as client:
            for alert in alerts:
                try:
                    await self.send(alert, client=client)
                    results.append((alert, None))
                except Exception as exc:  # noqa: BLE001 — report, don't crash the run
                    results.append((alert, exc))
        return results
