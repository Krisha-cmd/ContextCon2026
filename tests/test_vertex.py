from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.gateway.core import GatewayError
from app.gateway.vertex import Usage, VertexLLM, estimate_inr, generation_config, usage_from

SETTINGS = load_settings(DEFAULT_CONFIG_PATH)
CFG = SETTINGS.vertex


class FakeConn:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    async def execute(self, sql: str, params: tuple) -> None:
        self.sink.append((sql, params))


class FakePool:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def connection(self):
        yield FakeConn(self.executed)


class FakeGateway:
    def __init__(self) -> None:
        self.pool = FakePool()
        self.guarded: list[tuple[Any, float]] = []

    async def guard_budget_inr(self, job_id: Any, est_inr: float) -> None:
        self.guarded.append((job_id, est_inr))


class FakeModels:
    def __init__(self, text: str | None, usage: Any) -> None:
        self.resp = SimpleNamespace(text=text, usage_metadata=usage)
        self.kwargs: dict[str, Any] = {}

    async def generate_content(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.resp


def _client(text: str | None, usage: Any) -> Any:
    return SimpleNamespace(aio=SimpleNamespace(models=FakeModels(text, usage)))


def _usage(prompt: int, out: int, thoughts: int | None = None) -> Any:
    return SimpleNamespace(
        prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts
    )


def test_usage_counts_thinking_as_output_and_missing_as_zero() -> None:
    assert usage_from(_usage(100, 20, 5)) == Usage(100, 25)
    assert usage_from(_usage(None, None)) == Usage(0, 0)
    assert usage_from(None) == Usage(0, 0)


def test_estimate_inr_uses_config_prices() -> None:
    usage = Usage(1_000_000, 1_000_000)
    expected = (CFG.usd_per_m_input_tokens + CFG.usd_per_m_output_tokens) * CFG.inr_per_usd
    assert estimate_inr(CFG, usage) == pytest.approx(expected)


def test_generation_config_is_deterministic_json_with_schema() -> None:
    schema = {"type": "object"}
    gc = generation_config(CFG, schema)
    assert gc.temperature == CFG.temperature == 0
    assert gc.response_mime_type == "application/json"
    assert gc.response_json_schema == schema


async def test_call_guards_budget_then_logs_cost_to_ledger() -> None:
    gw = FakeGateway()
    client = _client('{"geo_states": []}', _usage(1000, 100))
    llm = VertexLLM(SETTINGS, gw, client=client)
    out = await llm.generate_json("p", {"type": "object"}, job_id="j1", endpoint="vertex.parse")
    assert out == '{"geo_states": []}'
    assert gw.guarded == [("j1", CFG.est_inr_per_call)]
    assert client.aio.models.kwargs["model"] == CFG.model
    [(sql, params)] = gw.pool.executed
    assert "cost_ledger" in sql
    job_id, endpoint, credits, inr, _ms, cache_hit = params
    assert (job_id, endpoint, credits, cache_hit) == ("j1", "vertex.parse", 0, False)
    assert inr == pytest.approx(estimate_inr(CFG, Usage(1000, 100)))


async def test_empty_response_is_an_error_but_still_costed() -> None:
    gw = FakeGateway()
    llm = VertexLLM(SETTINGS, gw, client=_client(None, _usage(10, 0)))
    with pytest.raises(GatewayError, match="empty"):
        await llm.generate_json("p", {}, job_id=None, endpoint="vertex.parse")
    assert len(gw.pool.executed) == 1
