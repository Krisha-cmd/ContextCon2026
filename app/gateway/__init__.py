"""app/gateway: the only door to the outside world.

Use the module-level `call()` / `ledger_summary()`; they share one process-wide Gateway
(async DB pool + httpx client), created lazily on first use.
"""

from __future__ import annotations

from typing import Any

import httpx

from app import db
from app.config import get_settings
from app.gateway.core import BudgetExceeded, Gateway, GatewayError, GatewayResponse

__all__ = [
    "BudgetExceeded",
    "Gateway",
    "GatewayError",
    "GatewayResponse",
    "call",
    "close",
    "get_gateway",
    "ledger_summary",
]

_default: Gateway | None = None


async def get_gateway() -> Gateway:
    global _default
    if _default is None:
        settings = get_settings()
        pool = await db.open_async_pool(settings)
        _default = Gateway(settings, pool, httpx.AsyncClient())
    return _default


async def close() -> None:
    global _default
    if _default is not None:
        await _default.client.aclose()
        _default = None
    await db.close_async_pool()


async def call(
    *,
    job_id: Any,
    endpoint: str,
    method: str,
    url: str,
    json: Any = None,
    params: dict[str, Any] | None = None,
    data_type: str,
    est_credits: float,
    entity_id: str | None = None,
) -> GatewayResponse:
    gw = await get_gateway()
    return await gw.call(
        job_id=job_id, endpoint=endpoint, method=method, url=url, json=json, params=params,
        data_type=data_type, est_credits=est_credits, entity_id=entity_id,
    )  # fmt: skip


async def ledger_summary(job_id: Any) -> dict[str, Any]:
    return await (await get_gateway()).ledger_summary(job_id)
