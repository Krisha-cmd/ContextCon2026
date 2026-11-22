"""Runner step "trigger" (per account): the strongest dated reason to call now.

Candidates: enrich news (article title + publish date), enrich hiring titles (Crustdata gives
them without dates, so they can never count), and door-2 signals found during discovery.
Type: the job-title regex first (supply_chain_hiring), then news-title rules, then, if
trigger.use_llm, a Gemini label whose quote must be in the title.
Only signals with an explicit record_date within windows.trigger_days of the run date count
(the RunSpec's trigger_types, if any, narrow them). None left -> dropped.
strength = type_weights[type] * exp(-age_days / decay_days).
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.discovery.normalize import known, parse_date
from app.evidence.models import Fact, Source, cannot_verify, quote_in_text, verified
from app.gateway import GatewayError
from app.gateway.vertex import JsonLLM, get_vertex_llm
from app.log import get_logger
from app.revenue.common import UPDATE_DATA, cached_enrich, run_date, run_spec, source_from
from app.runner.context import StepContext
from app.spec.models import TriggerType

log = get_logger(__name__)

LLM_ENDPOINT = "vertex.trigger_classify"
NEWS, HIRING, DOOR = "enrich.news", "enrich.hiring", "discovery"
TITLES_SEP = ", "


class TriggerLabel(BaseModel):
    type: TriggerType | None  # None: not a trigger
    quote: str


LLM_PROMPT = """Label this news headline about the company {name} for a 3PL sales team.
Types: {types}. Use null if it is none of them.
Return the type and the words of the headline that show it, copied exactly.

Headline: {title}"""


@dataclass(frozen=True)
class Candidate:
    origin: str
    title: str
    url: str | None
    record_date: date | None
    source: Source
    type: TriggerType | None = None


def job_type(title: str, s: Settings) -> TriggerType | None:
    hit = re.search(s.trigger.job_title_regex, title, re.IGNORECASE)
    return TriggerType.supply_chain_hiring if hit else None


def news_type(title: str, s: Settings) -> TriggerType | None:
    for name, pattern in s.trigger.news_title_regex.items():
        if re.search(pattern, title, re.IGNORECASE):
            return TriggerType(name)
    return None


# ---- candidates ---------------------------------------------------------------------------
def from_enrich(payload: dict[str, Any] | None, s: Settings) -> list[Candidate]:
    if not payload:
        return []
    record = payload.get("record") or {}
    out = []
    for i, item in enumerate(record.get("news") or []):
        title = known(item.get("article_title")) if isinstance(item, dict) else None
        if not title:
            continue
        d = parse_date(item.get("article_publish_date"))
        src = source_from(payload, f"news[{i}]", d)
        out.append(Candidate(NEWS, title, known(item.get("article_url")), d, src,
                             news_type(title, s)))  # fmt: skip
    csv = known((record.get("hiring") or {}).get("recent_titles_csv"))
    for title in csv.split(TITLES_SEP) if isinstance(csv, str) else []:
        if (t := job_type(title, s)) is not None:
            src = source_from(payload, "hiring.recent_titles_csv")
            out.append(Candidate(HIRING, title.strip(), None, None, src, t))
    return out


def from_doors(signals: list[dict[str, Any]], s: Settings) -> list[Candidate]:
    out = []
    for sig in signals:
        try:
            src = Source.model_validate(sig["source"])
            declared = TriggerType(sig["type"])
        except (KeyError, ValueError, ValidationError):
            continue
        title = sig.get("title") or ""
        kind = job_type(title, s) if declared is TriggerType.supply_chain_hiring else declared
        d = parse_date(sig.get("date"))
        out.append(Candidate(DOOR, title, sig.get("url"), d, src, kind))
    return out


async def _default_llm() -> JsonLLM:
    return await get_vertex_llm()


@dataclass
class TriggerDeps:
    cached_enrich: Callable[[dict[str, Any]], dict[str, Any] | None] = cached_enrich
    get_llm: Callable[[], Awaitable[JsonLLM]] = _default_llm


async def llm_type(c: Candidate, name: str, job_id: Any, deps: TriggerDeps) -> TriggerType | None:
    prompt = LLM_PROMPT.format(name=name, title=c.title,
                               types=", ".join(t.value for t in TriggerType))  # fmt: skip
    try:
        llm = await deps.get_llm()
        raw = await llm.generate_json(prompt, TriggerLabel.model_json_schema(), job_id=job_id,
                                      endpoint=LLM_ENDPOINT)  # fmt: skip
        label = TriggerLabel.model_validate_json(raw)
    except (GatewayError, ValidationError) as exc:
        log.warning("trigger label skipped", extra={"error": type(exc).__name__})
        return None
    if label.type is None or not quote_in_text(label.quote, c.title):
        return None
    return label.type


# ---- scoring ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Scored:
    c: Candidate
    age_days: int
    strength: float


def in_window(c: Candidate, today: date, s: Settings) -> int | None:
    """Age in days if the candidate counts (dated, not in the future, inside the window)."""
    if c.record_date is None or c.type is None:
        return None
    age = (today - c.record_date).days
    return age if 0 <= age <= s.windows.trigger_days else None


def strength(kind: TriggerType, age_days: int, s: Settings) -> float:
    return s.trigger.type_weights.get(kind.value, 0.0) * math.exp(-age_days / s.trigger.decay_days)


def best(cands: list[Candidate], today: date, s: Settings,
         wanted: list[TriggerType]) -> tuple[Scored | None, list[Scored]]:  # fmt: skip
    scored = []
    for c in cands:
        age = in_window(c, today, s)
        if age is None or (wanted and c.type not in wanted):
            continue
        scored.append(Scored(c, age, strength(c.type, age, s)))
    scored.sort(key=lambda x: (-x.strength, x.age_days, x.c.title))
    return (scored[0] if scored else None), scored


def fact_for(top: Scored | None, s: Settings) -> Fact[Any]:
    if top is None:
        return cannot_verify(f"no dated trigger in the last {s.windows.trigger_days} days")
    c = top.c
    value = {"type": c.type.value, "title": c.title, "url": c.url,
             "record_date": c.record_date.isoformat(), "age_days": top.age_days,
             "strength": round(top.strength, 4), "origin": c.origin}  # fmt: skip
    return verified(value, c.source)


# ---- the step -----------------------------------------------------------------------------
async def run_trigger(ctx: StepContext, deps: TriggerDeps | None = None) -> dict[str, Any]:
    deps, s = deps or TriggerDeps(), ctx.config
    account = ctx.account or {}
    data = account.get("data") or {}
    payload = await asyncio.to_thread(deps.cached_enrich, data)
    cands = from_enrich(payload, s) + from_doors(data.get("signals") or [], s)
    if s.trigger.use_llm:
        name = account.get("name") or ""
        cands = [
            replace(c, type=await llm_type(c, name, ctx.job_id, deps))
            if c.type is None and c.origin == NEWS
            else c
            for c in cands
        ]
    today = run_date(ctx.job)
    top, counted = best(cands, today, s, run_spec(ctx.job).trigger_types)
    fact = fact_for(top, s)
    summary = {
        "run_date": today.isoformat(),
        "candidates": len(cands),
        "undated": sum(1 for c in cands if c.record_date is None),
        "untyped": sum(1 for c in cands if c.type is None),
        "in_window": len(counted),
    }
    patch = {"trigger": fact.model_dump(mode="json"), "trigger_check": summary}
    evs = tuple(sorted({x.evidence_id for x in fact.sources}))
    ctx.write(UPDATE_DATA, (Jsonb(patch), account.get("id")), tbl="accounts",
              record_id=account.get("id"), before={k: data.get(k) for k in patch}, after=patch,
              reason=f"trigger: {fact.status.value}", evidence_ids=evs)  # fmt: skip
    if top is None:
        ctx.drop(fact.missing or "no trigger", evs)
    log.info("trigger", extra={"found": top is not None, **summary})
    return {**summary, "trigger": patch["trigger"]}
