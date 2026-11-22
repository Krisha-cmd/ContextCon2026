"""Gemini on Vertex AI, behind the gateway: budget guard before the call, cost_ledger after.

Cost is estimated from the response's token counts and the prices in config `vertex`.
The LLM only parses/extracts/classifies/drafts; callers decide whether to call it at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from google import genai
from google.genai import types

from app.config import Settings, Vertex
from app.gateway import get_gateway, store
from app.gateway.core import Gateway, GatewayError
from app.log import get_logger

log = get_logger(__name__)

TOKENS_PER_M = 1_000_000
MS_PER_S = 1000


class JsonLLM(Protocol):
    """What callers depend on. Tests pass a fake; production uses VertexLLM."""

    async def generate_json(
        self, prompt: str, schema: dict[str, Any], *, job_id: Any, endpoint: str
    ) -> str: ...


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


def usage_from(meta: Any) -> Usage:
    """Token counts from usage_metadata. Thinking tokens bill as output. Missing -> 0."""
    if meta is None:
        return Usage(0, 0)
    out = (meta.candidates_token_count or 0) + (getattr(meta, "thoughts_token_count", 0) or 0)
    return Usage(meta.prompt_token_count or 0, out)


def estimate_inr(cfg: Vertex, usage: Usage) -> float:
    usd = (
        usage.input_tokens * cfg.usd_per_m_input_tokens
        + usage.output_tokens * cfg.usd_per_m_output_tokens
    ) / TOKENS_PER_M
    return usd * cfg.inr_per_usd


def make_client(settings: Settings) -> genai.Client:
    """Vertex client using Application Default Credentials and GCP_PROJECT from env."""
    project = settings.secrets.gcp_project
    if not project:
        raise GatewayError("GCP_PROJECT is not set")
    return genai.Client(
        vertexai=True,
        project=project,
        location=settings.vertex.location,
        http_options=types.HttpOptions(timeout=int(settings.vertex.timeout_s * MS_PER_S)),
    )


def generation_config(cfg: Vertex, schema: dict[str, Any]) -> types.GenerateContentConfig:
    thinking = (
        None
        if cfg.thinking_budget is None
        else types.ThinkingConfig(thinking_budget=cfg.thinking_budget)
    )
    return types.GenerateContentConfig(
        temperature=cfg.temperature,
        response_mime_type="application/json",
        response_json_schema=schema,
        thinking_config=thinking,
    )


class VertexLLM:
    def __init__(self, settings: Settings, gateway: Gateway, client: Any = None) -> None:
        self.cfg = settings.vertex
        self.gateway = gateway
        self.client = client if client is not None else make_client(settings)

    async def generate_json(
        self, prompt: str, schema: dict[str, Any], *, job_id: Any, endpoint: str
    ) -> str:
        await self.gateway.guard_budget_inr(job_id, self.cfg.est_inr_per_call)
        t0 = time.perf_counter()
        try:
            resp = await self.client.aio.models.generate_content(
                model=self.cfg.model, contents=prompt, config=generation_config(self.cfg, schema)
            )
        except Exception as exc:
            raise GatewayError(f"vertex {endpoint}: {exc!r}") from exc
        ms = round((time.perf_counter() - t0) * MS_PER_S)
        usage = usage_from(resp.usage_metadata)
        inr = estimate_inr(self.cfg, usage)
        async with self.gateway.pool.connection() as conn:
            await store.write_ledger(
                conn, job_id=job_id, endpoint=endpoint, credits=0, inr=inr, ms=ms,
                cache_hit=False,
            )  # fmt: skip
        log.info(
            "vertex call",
            extra={"endpoint": endpoint, "model": self.cfg.model, "ms": ms, "inr": inr,
                   "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
        )  # fmt: skip
        if not resp.text:
            raise GatewayError(f"vertex {endpoint}: empty response")
        return resp.text


_default: VertexLLM | None = None


async def get_vertex_llm() -> VertexLLM:
    global _default
    if _default is None:
        gw = await get_gateway()
        _default = VertexLLM(gw.settings, gw)
    return _default
