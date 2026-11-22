"""SQL for the gateway: caches, cost ledger, budget. Plain SQL, connection passed in."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


async def get_entity_cache(conn: AsyncConnection, entity_id: str, data_type: str) -> Any | None:
    cur = await conn.execute(
        "SELECT payload FROM entity_cache "
        "WHERE entity_id = %s AND data_type = %s AND expires_at > now()",
        (entity_id, data_type),
    )
    row = await cur.fetchone()
    return None if row is None else row["payload"]


async def put_entity_cache(
    conn: AsyncConnection, entity_id: str, data_type: str, payload: Any, ttl_hours: float
) -> None:
    await conn.execute(
        "INSERT INTO entity_cache (entity_id, data_type, payload, expires_at) "
        "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) "
        "ON CONFLICT (entity_id, data_type) DO UPDATE "
        "SET payload = EXCLUDED.payload, expires_at = EXCLUDED.expires_at",
        (entity_id, data_type, Jsonb(payload), ttl_hours * 3600),
    )


async def get_http_cache(conn: AsyncConnection, request_hash: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT status, response, is_negative FROM http_cache "
        "WHERE request_hash = %s AND expires_at > now()",
        (request_hash,),
    )
    return await cur.fetchone()


async def put_http_cache(
    conn: AsyncConnection,
    *,
    request_hash: str,
    endpoint: str,
    status: int,
    body: Any,
    is_negative: bool,
    ttl_hours: float,
) -> None:
    await conn.execute(
        "INSERT INTO http_cache "
        "(request_hash, endpoint, status, response, is_negative, fetched_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, now(), now() + make_interval(secs => %s)) "
        "ON CONFLICT (request_hash) DO UPDATE SET endpoint = EXCLUDED.endpoint, "
        "status = EXCLUDED.status, response = EXCLUDED.response, "
        "is_negative = EXCLUDED.is_negative, fetched_at = EXCLUDED.fetched_at, "
        "expires_at = EXCLUDED.expires_at",
        (request_hash, endpoint, status, Jsonb(body), is_negative, ttl_hours * 3600),
    )


async def write_ledger(
    conn: AsyncConnection,
    *,
    job_id: Any,
    endpoint: str,
    credits: float,
    inr: float,
    ms: int,
    cache_hit: bool,
) -> None:
    await conn.execute(
        "INSERT INTO cost_ledger (job_id, endpoint, credits, inr, ms, cache_hit) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (job_id, endpoint, credits, inr, ms, cache_hit),
    )


async def job_spent_inr(conn: AsyncConnection, job_id: Any) -> float:
    cur = await conn.execute(
        "SELECT coalesce(sum(inr), 0)::float8 AS inr FROM cost_ledger WHERE job_id = %s",
        (job_id,),
    )
    row = await cur.fetchone()
    return float(row["inr"])


async def job_limit_inr(conn: AsyncConnection, job_id: Any) -> float | None:
    """The job's own max_inr_per_job from jobs.limits, or None if unset."""
    cur = await conn.execute(
        "SELECT (limits ->> 'max_inr_per_job')::float8 AS lim FROM jobs WHERE id = %s",
        (job_id,),
    )
    row = await cur.fetchone()
    return None if row is None or row["lim"] is None else float(row["lim"])


async def ledger_rows(conn: AsyncConnection, job_id: Any) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT endpoint, count(*)::int AS calls, "
        "count(*) FILTER (WHERE cache_hit)::int AS cache_hits, "
        "coalesce(sum(credits), 0)::float8 AS credits, coalesce(sum(inr), 0)::float8 AS inr "
        "FROM cost_ledger WHERE job_id = %s GROUP BY endpoint ORDER BY endpoint",
        (job_id,),
    )
    return await cur.fetchall()
