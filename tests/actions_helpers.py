"""Fakes for outbox tests: a Google Calendar + Gmail stand-in behind the gateway call shape."""

from __future__ import annotations

import asyncio
import base64
from email import message_from_bytes, policy
from typing import Any

from app.gateway import GatewayError, GatewayResponse


class FakeGoogle:
    """Calendar: insert with a client id -> 200, again -> 409. Gmail: list by query, send.
    `fail` maps an endpoint to a status (or "transport") to force errors."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.events: dict[str, dict] = {}
        self.sent: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.fail: dict[str, Any] = {}
        self.delay_s = delay_s

    def _resp(self, status: int, body: Any) -> GatewayResponse:
        return GatewayResponse(status, body, False, 0.0, 1, "h" * 64)

    async def __call__(self, **kw: Any) -> GatewayResponse:
        self.calls.append(kw)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        ep = kw["endpoint"]
        forced = self.fail.get(ep)
        if forced == "transport":
            raise GatewayError(f"{ep}: connection reset")
        if forced:
            return self._resp(forced, {"error": "forced"})
        if ep == "gcal:events.insert":
            body = kw["json"]
            if body["id"] in self.events:
                return self._resp(409, {"error": {"code": 409, "message": "duplicate"}})
            self.events[body["id"]] = body
            return self._resp(200, {"id": body["id"], "htmlLink": f"https://cal/{body['id']}"})
        if ep == "gmail:messages.list":
            token = kw["params"]["q"].split(" OR ")[-1].rstrip(")")
            hits = [{"id": f"m{i}"} for i, m in enumerate(self.sent) if token in m.as_string()]
            return self._resp(200, {"messages": hits[:1], "resultSizeEstimate": len(hits)})
        if ep == "gmail:messages.send":
            msg = message_from_bytes(
                base64.urlsafe_b64decode(kw["json"]["raw"]), policy=policy.default
            )
            self.sent.append(msg)
            return self._resp(200, {"id": f"m{len(self.sent) - 1}"})
        raise AssertionError(f"unexpected endpoint {ep}")

    def posts(self, endpoint: str) -> int:
        return sum(c["endpoint"] == endpoint for c in self.calls)
