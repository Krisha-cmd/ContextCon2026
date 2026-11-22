"""Gateway integration tests: real Postgres (compose, port 5433), httpx mocked with respx."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from app import db
from app.config import DEFAULT_CONFIG_PATH, Settings, load_settings
from app.gateway import BudgetExceeded, Gateway, GatewayError

BASE = "https://api.crustdata.test"
ENDPOINT = "crustdata:company/identify"


def _settings() -> Settings:
    """Repo config, made fast for tests: high rpm, tiny backoff, a fake key."""
    s = load_settings(DEFAULT_CONFIG_PATH)
    return s.model_copy(
        update={
            "crustdata": s.crustdata.model_copy(update={"rpm": 60_000}),
            "gateway": s.gateway.model_copy(
                update={"backoff_base_s": 0.01, "backoff_jitter_s": 0.01, "backoff_max_s": 0.05}
            ),
            "secrets": s.secrets.model_copy(update={"crustdata_api_key": SecretStr("test-key")}),
        }
    )


@pytest.fixture
async def pool(db_url: str, pg_conn: psycopg.Connection) -> AsyncIterator[AsyncConnectionPool]:
    db.apply_schema(pg_conn, load_settings(DEFAULT_CONFIG_PATH).db.schema_lock_key)
    p = AsyncConnectionPool(db_url, min_size=1, max_size=10, kwargs={"row_factory": dict_row},
                            open=False)  # fmt: skip
    await p.open(wait=True)
    yield p
    await p.close()


@pytest.fixture
async def gw(pool: AsyncConnectionPool) -> AsyncIterator[Gateway]:
    async with httpx.AsyncClient() as client:
        yield Gateway(_settings(), pool, client)


async def _new_job(pool: AsyncConnectionPool, limits: dict | None = None) -> uuid.UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO jobs (client_id, spec, status, limits) "
            "VALUES ('test', '{}', 'new', %s) RETURNING id",
            (Jsonb(limits or {}),),
        )
        return (await cur.fetchone())["id"]


def _url() -> str:
    """Unique URL per test so http_cache rows never collide between tests/runs."""
    return f"{BASE}/company/identify/{uuid.uuid4().hex}"


async def _call(gw: Gateway, job: uuid.UUID, url: str, **kw):
    args = {
        "job_id": job,
        "endpoint": ENDPOINT,
        "method": "POST",
        "url": url,
        "json": {"domains": ["delhivery.com"]},
        "data_type": "company",
        "est_credits": 0.2,
    }
    return await gw.call(**(args | kw))


@respx.mock
async def test_second_identical_call_is_cached_and_free(gw: Gateway, pool) -> None:
    job, url = await _new_job(pool), _url()
    route = respx.post(url).respond(
        200, json=[{"matched_on": "delhivery.com", "matches": [{"id": 5798}]}],
        headers={"X-Credits-Used": "0.2"},
    )  # fmt: skip
    first = await _call(gw, job, url)
    second = await _call(gw, job, url)

    assert route.call_count == 1
    assert (first.from_cache, first.credits) == (False, 0.2)
    assert (second.from_cache, second.credits) == (True, 0.0)
    assert second.body == first.body
    assert second.request_hash == first.request_hash
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer test-key"
    assert sent.headers["x-api-version"] == "2025-11-01"

    summary = await gw.ledger_summary(job)
    assert summary["calls"] == 2
    assert summary["cache_hits"] == 1
    assert summary["credits"] == pytest.approx(0.2)
    assert summary["inr"] == pytest.approx(0.2 * gw.settings.limits.credit_inr)
    assert summary["by_endpoint"][ENDPOINT]["calls"] == 2


@respx.mock
async def test_budget_guard_raises_before_sending(gw: Gateway, pool) -> None:
    limit = gw.settings.limits.credit_inr  # room for exactly 1 credit
    job, url = await _new_job(pool, {"max_inr_per_job": limit}), _url()
    route = respx.post(url).respond(200, json={"matches": [{"id": 1}]})

    await _call(gw, job, url, est_credits=1.0)  # spends exactly the limit (no header -> est)
    with pytest.raises(BudgetExceeded) as exc:
        await _call(gw, job, _url(), est_credits=0.5)
    assert exc.value.limit_inr == limit
    assert route.call_count == 1


@respx.mock
async def test_budget_falls_back_to_config_limit(gw: Gateway, pool) -> None:
    job = await _new_job(pool)  # no limits in the job row
    too_many = gw.settings.limits.max_inr_per_job / gw.settings.limits.credit_inr + 1
    with pytest.raises(BudgetExceeded):
        await _call(gw, job, _url(), est_credits=too_many)


@respx.mock
async def test_429_then_200_retries_once(gw: Gateway, pool) -> None:
    job, url = await _new_job(pool), _url()
    route = respx.post(url).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"type": "rate_limit_error"}}),
            httpx.Response(200, json={"matches": [{"id": 1}]}, headers={"X-Credits-Used": "0.2"}),
        ]
    )
    resp = await _call(gw, job, url)
    assert route.call_count == 2
    assert resp.status == 200
    assert resp.credits == pytest.approx(0.2)


@respx.mock
async def test_4xx_is_not_retried_or_cached(gw: Gateway, pool) -> None:
    job, url = await _new_job(pool), _url()
    route = respx.post(url).respond(400, json={"error": {"type": "invalid_request"}})
    first = await _call(gw, job, url)
    second = await _call(gw, job, url)
    assert (first.status, second.status) == (400, 400)
    assert route.call_count == 2  # one try per call, and the error was not cached
    assert first.credits == 0.0


@respx.mock
async def test_timeouts_exhaust_attempts_then_raise(gw: Gateway, pool) -> None:
    job, url = await _new_job(pool), _url()
    route = respx.post(url).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(GatewayError):
        await _call(gw, job, url)
    assert route.call_count == gw.settings.gateway.max_attempts


@respx.mock
async def test_negative_result_cached_with_short_ttl(gw: Gateway, pool) -> None:
    job, url = await _new_job(pool), _url()
    route = respx.post(url).respond(200, json=[{"matched_on": "nope.example", "matches": []}])
    first = await _call(gw, job, url)
    second = await _call(gw, job, url)
    assert route.call_count == 1
    assert second.from_cache

    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT is_negative, extract(epoch FROM expires_at - fetched_at)::float8 AS ttl_s "
            "FROM http_cache WHERE request_hash = %s", (first.request_hash,),
        )).fetchone()  # fmt: skip
    assert row["is_negative"] is True
    assert row["ttl_s"] == pytest.approx(gw.settings.cache_ttl_hours.negative * 3600, abs=5)


@respx.mock
async def test_entity_cache_short_circuits_other_requests(gw: Gateway, pool) -> None:
    job, entity = await _new_job(pool), f"ent-{uuid.uuid4().hex}"
    url_a, url_b = _url(), _url()
    route_a = respx.post(url_a).respond(200, json={"matches": [{"id": 1}]})
    route_b = respx.post(url_b).respond(200, json={"matches": [{"id": 2}]})
    first = await _call(gw, job, url_a, entity_id=entity)
    second = await _call(gw, job, url_b, entity_id=entity)  # different request, same entity
    assert route_a.call_count == 1
    assert route_b.call_count == 0
    assert second.from_cache and second.body == first.body


@respx.mock
async def test_concurrent_identical_calls_make_one_request(gw: Gateway, pool) -> None:
    job, url = await _new_job(pool), _url()

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json={"matches": [{"id": 1}]}, headers={"X-Credits-Used": "0.2"})

    route = respx.post(url).mock(side_effect=slow)
    a, b = await asyncio.gather(_call(gw, job, url), _call(gw, job, url))
    assert route.call_count == 1
    assert sorted([a.from_cache, b.from_cache]) == [False, True]
    assert (await gw.ledger_summary(job))["credits"] == pytest.approx(0.2)


@respx.mock
async def test_logs_one_line_per_call(gw: Gateway, pool, caplog: pytest.LogCaptureFixture) -> None:
    job, url = await _new_job(pool), _url()
    respx.post(url).respond(200, json={"matches": [{"id": 1}]}, headers={"X-Credits-Used": "0.2"})
    with caplog.at_level("INFO", logger="app.gateway.core"):
        resp = await _call(gw, job, url)
    (rec,) = [r for r in caplog.records if r.name == "app.gateway.core"]
    assert rec.endpoint == ENDPOINT
    assert rec.hash == resp.request_hash[:12]
    assert (rec.from_cache, rec.credits) == (False, 0.2)
    assert "test-key" not in rec.getMessage() + str(rec.__dict__)


async def test_rate_limit_spaces_calls_across_bucket(pool) -> None:
    from app.gateway import ratelimit

    bucket = f"test-{uuid.uuid4().hex}"
    rpm = 600  # 1 token per 0.1 s, capacity 1
    assert await ratelimit.acquire(pool, bucket, rpm, capacity=1) == 0  # first is free
    waited = await ratelimit.acquire(pool, bucket, rpm, capacity=1)
    assert 0.05 < waited < 0.5
