"""Token bucket per endpoint group, stored in Postgres so it holds across processes."""

from __future__ import annotations

import asyncio

from psycopg_pool import AsyncConnectionPool

SECONDS_PER_MINUTE = 60.0


def refill_and_take(
    tokens: float, elapsed_s: float, rate_per_s: float, capacity: float
) -> tuple[float, float]:
    """Refill for elapsed time, then try to take one token.

    Returns (tokens_after, wait_s). wait_s == 0 means a token was taken.
    """
    available = min(capacity, tokens + max(0.0, elapsed_s) * rate_per_s)
    if available >= 1.0:
        return available - 1.0, 0.0
    return available, (1.0 - available) / rate_per_s


_ENSURE = """
INSERT INTO rate_buckets (bucket, tokens, updated_at) VALUES (%s, %s, clock_timestamp())
ON CONFLICT (bucket) DO NOTHING
"""
_LOCK = """
SELECT tokens, extract(epoch FROM clock_timestamp() - updated_at)::float8 AS elapsed
FROM rate_buckets WHERE bucket = %s FOR UPDATE
"""
_SAVE = "UPDATE rate_buckets SET tokens = %s, updated_at = clock_timestamp() WHERE bucket = %s"


async def _try_take(pool: AsyncConnectionPool, bucket: str, rate: float, capacity: float) -> float:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(_ENSURE, (bucket, capacity))
        row = await (await conn.execute(_LOCK, (bucket,))).fetchone()
        tokens, wait = refill_and_take(row["tokens"], row["elapsed"], rate, capacity)
        await conn.execute(_SAVE, (tokens, bucket))
    return wait


async def acquire(pool: AsyncConnectionPool, bucket: str, rpm: float, capacity: float) -> float:
    """Block until a token is available. Waits, never fails. Returns seconds waited."""
    rate = rpm / SECONDS_PER_MINUTE
    waited = 0.0
    while (wait := await _try_take(pool, bucket, rate, capacity)) > 0:
        await asyncio.sleep(wait)
        waited += wait
    return waited
