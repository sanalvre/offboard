"""Discord webhook adapter: one embed per pipeline step, best-effort (a failure is traced, never fatal).
Limits honoured: content 2000, embed title 256, description 4096, 25 fields, 6000 total (Discord developer docs).
The mock records payloads in the trace only."""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from ..config import settings
from ..trace import Tracer


def _clip(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


class DiscordAdapter:
    name = "discord"

    def __init__(self, tracer: Tracer):
        self.tracer = tracer

    def _send(self, payload: dict) -> Any:
        raise NotImplementedError

    def post(self, title: str, description: str = "", fields: Optional[list[tuple[str, str]]] = None, footer: str = "") -> bool:
        embed: dict[str, Any] = {"title": _clip(title, 256), "description": _clip(description, 4096)}
        if fields:
            embed["fields"] = [{"name": _clip(n, 256), "value": _clip(v, 1024), "inline": True} for n, v in fields[:25]]
        if footer:
            embed["footer"] = {"text": _clip(footer, 2048)}
        payload = {"username": "Receipt", "embeds": [embed]}
        t0 = time.perf_counter()
        try:
            resp = self._send(payload)
        except Exception as e:  # noqa: BLE001
            self.tracer.tool_call(f"{self.name}.post", {"payload": payload}, error={"type": type(e).__name__, "message": str(e)},
                                  latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            return False
        self.tracer.tool_call(f"{self.name}.post", {"payload": payload}, response=resp, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        return True


class LiveDiscord(DiscordAdapter):
    def __init__(self, tracer: Tracer):
        super().__init__(tracer)
        self.url = settings.discord_webhook_url
        self.http = httpx.Client(timeout=15)

    def _send(self, payload: dict) -> Any:
        if not self.url:
            raise RuntimeError("DISCORD_WEBHOOK_URL not configured")
        r = self.http.post(self.url, params={"wait": "true"}, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
        j = r.json()
        return {"status": r.status_code, "message_id": j.get("id"), "ratelimit_remaining": r.headers.get("x-ratelimit-remaining")}


class MockDiscord(DiscordAdapter):
    name = "discord.mock"

    def __init__(self, tracer: Tracer):
        super().__init__(tracer)
        self.sent: list[dict] = []

    def _send(self, payload: dict) -> Any:
        self.sent.append(payload)
        return {"status": 200, "message_id": f"mock-{len(self.sent)}"}
