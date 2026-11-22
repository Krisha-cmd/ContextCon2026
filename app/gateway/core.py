"""The gateway: the only code that talks to the outside world.

Order per call: hash -> entity cache -> request cache -> budget guard -> single-flight lock
(re-check cache) -> rate limit -> send with retries -> ledger -> cache write -> log.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.gateway import google_auth, ratelimit, store
from app.gateway.hashing import request_hash
from app.gateway.negative import is_negative
from app.log import bind, get_logger

log = get_logger(__name__)

CREDITS_HEADER = "X-Credits-Used"
GOOGLE_GROUPS = ("gcal", "gmail", "cloudrun")  # OAuth bearer from Application Default Credentials
HASH_PREFIX_LEN = 12
# A call can hold one connection for the single-flight lock and borrow a second for the
# rate-limit bucket, so in-flight calls are capped at pool_max // CONNS_PER_CALL.
CONNS_PER_CALL = 2


class GatewayError(RuntimeError):
    """Transport failure after all retries, or a misconfigured call."""


class BudgetExceeded(RuntimeError):  # noqa: N818 - name fixed by the spec
    def __init__(self, job_id: Any, spent_inr: float, est_inr: float, limit_inr: float) -> None:
        super().__init__(
            f"job {job_id}: spent {spent_inr:.2f} + est {est_inr:.2f} INR > limit {limit_inr:.2f}"
        )
        self.job_id, self.spent_inr, self.est_inr, self.limit_inr = (
            job_id,
            spent_inr,
            est_inr,
            limit_inr,
        )


@dataclass(frozen=True)
class GatewayResponse:
    status: int
    body: Any
    from_cache: bool
    credits: float
    ms: int
    request_hash: str


@dataclass(frozen=True)
class _Sent:
    status: int
    body: Any
    credits: float


def endpoint_group(endpoint: str) -> str:
    group, sep, name = endpoint.partition(":")
    if not sep or not group or not name:
        raise GatewayError(f"endpoint must look like '<group>:<name>', got {endpoint!r}")
    return group


def parse_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def header_credits(resp: httpx.Response) -> float | None:
    raw = resp.headers.get(CREDITS_HEADER)
    try:
        return None if raw is None else float(raw)
    except ValueError:
        return None


def is_retryable_status(status: int) -> bool:
    return status == httpx.codes.TOO_MANY_REQUESTS or status >= httpx.codes.INTERNAL_SERVER_ERROR


def backoff_s(settings: Settings, attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with jitter for retry number `attempt` (1-based), capped."""
    g = settings.gateway
    base = min(g.backoff_max_s, g.backoff_base_s * 2 ** (attempt - 1))
    wait = base + random.uniform(0, g.backoff_jitter_s)
    if retry_after is not None:
        wait = max(wait, retry_after)
    return min(wait, g.backoff_max_s)


def retry_after_s(resp: httpx.Response) -> float | None:
    for name in ("Retry-After", "X-RateLimit-Reset"):
        try:
            return float(resp.headers[name])
        except (KeyError, ValueError):
            continue
    return None


class Gateway:
    def __init__(
        self, settings: Settings, pool: AsyncConnectionPool, client: httpx.AsyncClient
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.client = client
        self._inflight = asyncio.Semaphore(max(1, settings.db.pool_max // CONNS_PER_CALL))

    # ---- config lookups -------------------------------------------------------------
    def ttl_hours(self, data_type: str, negative: bool) -> float:
        key = "negative" if negative else data_type
        if not hasattr(self.settings.cache_ttl_hours, key):
            raise GatewayError(f"no cache_ttl_hours entry for data_type {key!r}")
        return float(getattr(self.settings.cache_ttl_hours, key))

    def endpoint_rpm(self, endpoint: str) -> float:
        """Per-endpoint override from gateway.endpoint_rpm, else the endpoint's group rpm."""
        override = self.settings.gateway.endpoint_rpm.get(endpoint)
        return float(override) if override is not None else self.group_rpm(endpoint_group(endpoint))

    def group_rpm(self, group: str) -> float:
        section = getattr(self.settings, group, None)
        return float(getattr(section, "rpm", self.settings.gateway.default_rpm))

    def group_timeout(self, group: str) -> float:
        section = getattr(self.settings, group, None)
        return float(getattr(section, "timeout_s", self.settings.gateway.default_timeout_s))

    def group_max_attempts(self, group: str) -> int:
        section = getattr(self.settings, group, None)
        return int(getattr(section, "max_attempts", self.settings.gateway.max_attempts))

    def group_retryable(self, group: str, status: int) -> bool:
        """A group may list its own retry_statuses (gstinapi: only 429/502); else the default."""
        statuses = getattr(getattr(self.settings, group, None), "retry_statuses", None)
        return status in statuses if statuses is not None else is_retryable_status(status)

    def group_headers(self, group: str) -> dict[str, str]:
        if group in GOOGLE_GROUPS:
            return {"Authorization": f"Bearer {google_auth.access_token(self.settings)}"}
        if group == "gstinapi":
            key = self.settings.secrets.gstin_api_key
            if key is None:
                raise GatewayError("GSTIN_API_KEY is not set")
            return {"x-api-key": key.get_secret_value()}
        if group == "registryapi":
            key = self.settings.secrets.registry_api_key
            if key is None:
                raise GatewayError("REGISTRY_API_KEY is not set")
            api = self.settings.registryapi
            return {api.auth_header: f"{api.auth_prefix}{key.get_secret_value()}"}
        if group == "crustdata":
            key = self.settings.secrets.crustdata_api_key
            if key is None:
                raise GatewayError("CRUSTDATA_API_KEY is not set")
            return {
                "Authorization": f"Bearer {key.get_secret_value()}",
                "x-api-version": self.settings.crustdata.api_version,
            }
        return {}

    def to_inr(self, credits: float, group: str | None = None) -> float:
        """A group's own credit_inr (gstinapi bills per lookup) else the Crustdata rate."""
        section = getattr(self.settings, group, None) if group else None
        rate = getattr(section, "credit_inr", None)
        return credits * (self.settings.limits.credit_inr if rate is None else rate)

    # ---- public ---------------------------------------------------------------------
    async def call(
        self,
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
        group = endpoint_group(endpoint)
        self.ttl_hours(data_type, negative=False)  # fail fast on unknown data_type
        rhash = request_hash(method, url, params, json)
        t0 = time.perf_counter()
        with bind(job_id=str(job_id)):
            async with self._inflight:
                resp = await self._call(
                    job_id, endpoint, group, method, url, json, params, data_type,
                    est_credits, entity_id, rhash, t0,
                )  # fmt: skip
            log.info(
                "gateway call",
                extra={
                    "endpoint": endpoint,
                    "hash": rhash[:HASH_PREFIX_LEN],
                    "from_cache": resp.from_cache,
                    "credits": resp.credits,
                    "ms": resp.ms,
                    "status": resp.status,
                },
            )
        return resp

    async def ledger_summary(self, job_id: Any) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            rows = await store.ledger_rows(conn, job_id)
        by_endpoint = {
            r["endpoint"]: {k: r[k] for k in ("calls", "cache_hits", "credits", "inr")}
            for r in rows
        }
        return {
            "calls": sum(r["calls"] for r in rows),
            "cache_hits": sum(r["cache_hits"] for r in rows),
            "credits": sum(r["credits"] for r in rows),
            "inr": sum(r["inr"] for r in rows),
            "by_endpoint": by_endpoint,
        }

    # ---- steps ----------------------------------------------------------------------
    async def _call(
        self, job_id, endpoint, group, method, url, json, params, data_type,
        est_credits, entity_id, rhash, t0,
    ) -> GatewayResponse:  # fmt: skip
        async with self.pool.connection() as conn, conn.transaction():
            hit = await self._cached(conn, job_id, endpoint, rhash, data_type, entity_id, t0)
        if hit:
            return hit
        await self._guard_budget(job_id, est_credits, group)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (rhash,))
            hit = await self._cached(conn, job_id, endpoint, rhash, data_type, entity_id, t0)
            if hit:
                return hit
            sent = await self._send(endpoint, group, method, url, json, params, est_credits)
            ms = _elapsed_ms(t0)
            await self._record(conn, job_id, endpoint, rhash, data_type, entity_id, sent, ms)
        return GatewayResponse(sent.status, sent.body, False, sent.credits, ms, rhash)

    async def _cached(
        self, conn: AsyncConnection, job_id, endpoint, rhash, data_type, entity_id, t0
    ) -> GatewayResponse | None:
        status, body = 200, None
        if entity_id is not None:
            body = await store.get_entity_cache(conn, entity_id, data_type)
        if body is None:
            row = await store.get_http_cache(conn, rhash)
            if row is None:
                return None
            status, body = row["status"], row["response"]
        ms = _elapsed_ms(t0)
        await store.write_ledger(
            conn, job_id=job_id, endpoint=endpoint, credits=0, inr=0, ms=ms, cache_hit=True
        )
        return GatewayResponse(status, body, True, 0.0, ms, rhash)

    async def _guard_budget(self, job_id: Any, est_credits: float, group: str) -> None:
        await self.guard_budget_inr(job_id, self.to_inr(est_credits, group))

    async def guard_budget_inr(self, job_id: Any, est_inr: float) -> None:
        """Raise BudgetExceeded if spending est_inr would push the job over its limit.
        Also used by callers that are priced in INR rather than credits (app/gateway/vertex)."""
        async with self.pool.connection() as conn:
            spent = await store.job_spent_inr(conn, job_id)
            limit = await store.job_limit_inr(conn, job_id)
        if limit is None:
            limit = self.settings.limits.max_inr_per_job
        if spent + est_inr > limit:
            raise BudgetExceeded(job_id, spent, est_inr, limit)

    async def _send(self, endpoint, group, method, url, json, params, est_credits) -> _Sent:
        headers = self.group_headers(group)
        timeout = self.group_timeout(group)
        rpm = self.endpoint_rpm(endpoint)
        capacity = self.settings.gateway.bucket_capacity
        max_attempts = self.group_max_attempts(group)
        header_total: float | None = None
        for attempt in range(1, max_attempts + 1):
            # One bucket per endpoint: Crustdata enforces its rate limits per endpoint.
            await ratelimit.acquire(self.pool, endpoint, rpm, capacity)
            try:
                resp = await self.client.request(
                    method, url, json=json, params=params, headers=headers, timeout=timeout
                )
            except httpx.TransportError as exc:  # includes timeouts
                if attempt == max_attempts:
                    raise GatewayError(f"{method} {url}: {exc!r} after {attempt} attempts") from exc
                await asyncio.sleep(backoff_s(self.settings, attempt))
                continue
            used = header_credits(resp)
            if used is not None:
                header_total = (header_total or 0.0) + used
            if self.group_retryable(group, resp.status_code) and attempt < max_attempts:
                await asyncio.sleep(backoff_s(self.settings, attempt, retry_after_s(resp)))
                continue
            break
        credits = _final_credits(header_total, resp.is_success, est_credits)
        return _Sent(resp.status_code, parse_body(resp), credits)

    async def _record(
        self, conn: AsyncConnection, job_id, endpoint, rhash, data_type, entity_id, sent: _Sent,
        ms: int,
    ) -> None:  # fmt: skip
        await store.write_ledger(
            conn, job_id=job_id, endpoint=endpoint, credits=sent.credits,
            inr=self.to_inr(sent.credits, endpoint_group(endpoint)), ms=ms, cache_hit=False,
        )  # fmt: skip
        if not httpx.codes.is_success(sent.status):
            return  # errors are never cached
        negative = is_negative(sent.body)
        await store.put_http_cache(
            conn, request_hash=rhash, endpoint=endpoint, status=sent.status, body=sent.body,
            is_negative=negative, ttl_hours=self.ttl_hours(data_type, negative),
        )  # fmt: skip
        if entity_id is not None and not negative:
            await store.put_entity_cache(
                conn, entity_id, data_type, sent.body, self.ttl_hours(data_type, False)
            )


def _final_credits(header_total: float | None, success: bool, est_credits: float) -> float:
    """Header value when the API reported one; else the estimate for a success; else 0."""
    if header_total is not None:
        return header_total
    return est_credits if success else 0.0


def _elapsed_ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)
