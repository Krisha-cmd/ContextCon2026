"""Runner step "discover": door 1 (company search) + door 2 (recent jobs and news).

Every candidate carries a Source for the record it came from. Plain code decides which calls
to make and how many; the LLM only pulls company names out of fetched news text, and each
name must really appear in that text (quote_in_text).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.discovery import crustdata
from app.discovery.models import Candidate, Door, Signal
from app.discovery.normalize import get_path, job_date_added, known, normalize_domain
from app.evidence.models import Source, quote_in_text
from app.evidence.store import save_evidence
from app.gateway.vertex import JsonLLM, get_vertex_llm
from app.log import get_logger
from app.spec.models import RunSpec, TriggerType

log = get_logger(__name__)

EXTRACT_ENDPOINT = "vertex.extract"
KEY_HASH_LEN = 12

SaveFn = Callable[..., Source]


@dataclass
class Deps:
    """Everything the doors call out to, so tests can swap in fakes."""

    search_companies: Callable[..., Awaitable[crustdata.Page]] = crustdata.search_companies
    search_jobs: Callable[..., Awaitable[crustdata.Page]] = crustdata.search_jobs
    web_search: Callable[..., Awaitable[crustdata.Page]] = crustdata.web_search
    save: SaveFn = save_evidence
    llm: JsonLLM | None = None
    today: Callable[[], date] = field(default=date.today)

    async def get_llm(self) -> JsonLLM:
        if self.llm is None:
            self.llm = await get_vertex_llm()
        return self.llm


def _api_url(s: Settings, name: str) -> str:
    return s.crustdata.base_url + crustdata.PATHS[name]


async def _save(deps: Deps, raw: Any, url: str, source_name: str, **kw: Any) -> Source:
    # save_evidence uses the sync pool; keep it off the event loop.
    return await asyncio.to_thread(deps.save, raw, url, source_name, **kw)


def _int(value: Any) -> int | None:
    v = known(value)
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _domain(value: Any) -> str | None:
    v = known(value)
    return (normalize_domain(v) or None) if isinstance(v, str) else None


# ---- door 1: company search ---------------------------------------------------------------
def _cond(fieldname: str, op: str, value: Any) -> dict[str, Any]:
    return {"field": fieldname, "type": op, "value": value}


def _any_of(fieldname: str, op: str, values: list[str]) -> dict[str, Any]:
    conds = [_cond(fieldname, op, v) for v in values]
    return conds[0] if len(conds) == 1 else {"op": "or", "conditions": conds}


def company_filters(spec: RunSpec, s: Settings) -> dict[str, Any]:
    """RunSpec -> Crustdata company-search filter: India HQ, industries, states, headcount."""
    f = s.discovery.company_filters
    conds = [_cond(f.country_field, "in", list(f.country_values))]
    if spec.industries:
        conds.append(_any_of(f.industry_field, f.industry_op, spec.industries))
    if spec.geo_states:
        conds.append(_any_of(f.state_field, f.state_op, spec.geo_states))
    if spec.size_min is not None:
        conds.append(_cond(f.headcount_field, "=>", spec.size_min))
    if spec.size_max is not None:
        conds.append(_cond(f.headcount_field, "=<", spec.size_max))
    return {"op": "and", "conditions": conds}


def max_accounts(spec: RunSpec, s: Settings) -> int:
    return spec.max_accounts or s.discovery.default_max_accounts


async def door_search(job_id: Any, spec: RunSpec, deps: Deps, s: Settings) -> list[Candidate]:
    """Page through company search until max_accounts * multiplier candidates or max pages."""
    target = max_accounts(spec, s) * s.discovery.search_candidate_multiplier
    filters = company_filters(spec, s)
    out: list[Candidate] = []
    cursor: str | None = None
    for _ in range(s.discovery.search_max_pages):
        page = await deps.search_companies(job_id, filters, cursor)
        for i, rec in enumerate(page.records):
            out.append(await _from_company(deps, s, rec, page.request_hash, i))
        cursor = page.next_cursor
        if len(out) >= target or not cursor or not page.records:
            break
    return out[:target]


async def _from_company(
    deps: Deps, s: Settings, rec: dict[str, Any], rhash: str, i: int
) -> Candidate:
    src = await _save(
        deps, rec, _api_url(s, "company_search"), "crustdata.company_search",
        field_path=f"companies[{i}]",
    )  # fmt: skip
    cid = _int(
        get_path(rec, "crustdata_company_id") or get_path(rec, "basic_info.crustdata_company_id")
    )
    name = get_path(rec, "basic_info.name") or ""
    domain = _domain(get_path(rec, "basic_info.primary_domain"))
    return Candidate(
        key=f"{Door.search}:{rhash[:KEY_HASH_LEN]}:{i}",
        door=Door.search,
        name=name,
        domain=domain,
        crustdata_id=cid,
        city=get_path(rec, "locations.city"),
        state=get_path(rec, "locations.state"),
        headcount=_int(get_path(rec, "headcount.total")),
        needs_identity=cid is None and domain is None,
        sources=[src],
    )


# ---- door 2a: supply-chain job posts ------------------------------------------------------
def jobs_query(s: Settings) -> dict[str, Any]:
    d = s.discovery
    titles = _any_of(d.job_title_field, d.job_title_op, list(d.job_title_keywords))
    country = _cond(d.job_country_field, "=", d.job_country_value)
    return {"op": "and", "conditions": [titles, country]}


async def door_jobs(job_id: Any, deps: Deps, s: Settings) -> list[Candidate]:
    since = deps.today() - timedelta(days=s.windows.trigger_days)
    page = await deps.search_jobs(job_id, jobs_query(s), since)
    out = []
    for i, job in enumerate(page.records):
        cand = await _from_job(deps, job, page.request_hash, i)
        if cand is not None:
            out.append(cand)
    return out


async def _from_job(deps: Deps, job: dict[str, Any], rhash: str, i: int) -> Candidate | None:
    name = get_path(job, "company.basic_info.name")
    if not name:
        return None
    posted = job_date_added(job)
    url = get_path(job, "job_details.url")
    title = get_path(job, "job_details.title") or ""
    src = await _save(
        deps, job, url or "crustdata:job_search", "crustdata.job_search",
        field_path=f"job_listings[{i}]", record_date=posted,
    )  # fmt: skip
    cid = _int(get_path(job, "company.basic_info.crustdata_company_id"))
    domain = _domain(get_path(job, "company.basic_info.primary_domain"))
    signal = Signal(
        type=TriggerType.supply_chain_hiring, date=posted, title=title, url=url, source=src
    )
    return Candidate(
        key=f"{Door.jobs}:{rhash[:KEY_HASH_LEN]}:{i}",
        door=Door.jobs,
        name=name,
        domain=domain,
        crustdata_id=cid,
        city=get_path(job, "company.locations.city"),
        state=get_path(job, "company.locations.state"),
        headcount=_int(get_path(job, "company.headcount.total")),
        industries=list(get_path(job, "company.basic_info.industries") or []),
        needs_identity=cid is None and domain is None,
        signals=[signal],
        sources=[src],
    )


# ---- door 2b: new-warehouse news ----------------------------------------------------------
_DASHES = "\u2014\u2013-"  # em dash, en dash, hyphen
_SNIPPET_DATE = re.compile(rf"^\s*(?P<d>[^{_DASHES}]{{3,30}}?)\s+[{_DASHES}]\s+")
_RELATIVE = re.compile(r"^(\d+)\s+(minute|hour|day|week)s?\s+ago$", re.IGNORECASE)
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d")
_RELATIVE_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7}


def snippet_date(snippet: str, today: date) -> date | None:
    """Google-style leading date: 'Nov 4, 2026 — ...' or '3 days ago — ...'. Else None."""
    m = _SNIPPET_DATE.match(snippet or "")
    if not m:
        return None
    text = m.group("d").strip()
    rel = _RELATIVE.match(text)
    if rel:
        return today - timedelta(days=int(rel.group(1)) * _RELATIVE_DAYS[rel.group(2).lower()])
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class NewsMention(BaseModel):
    result: int
    company: str
    domain: str | None = None


class NewsMentions(BaseModel):
    mentions: list[NewsMention]


EXTRACT_PROMPT = """Below are numbered web search results (title and snippet).
List each company that a result says is opening, building or planning a new warehouse,
fulfilment centre or distribution centre in India.

Rules:
- Use only the text below. Do not use outside knowledge and do not invent companies.
- "company" must be copied exactly as it is written in that result.
- "domain" only if a company website domain is written in that result; otherwise null.
  Never use the result's own URL or a news site as the domain.
- If no result names such a company, return {{"mentions": []}}.

Results:
{results}"""


def news_queries(spec: RunSpec, s: Settings) -> list[str]:
    industries = spec.industries or [""]
    tpl = s.discovery.news_query_template
    queries = [" ".join(tpl.format(industry=ind).split()) for ind in industries]
    return list(dict.fromkeys(queries))[: s.discovery.news_max_queries]


def _result_text(r: dict[str, Any]) -> str:
    return f"{r.get('title') or ''}\n{r.get('snippet') or ''}"


def grounded_mentions(raw: str, results: list[dict[str, Any]]) -> list[NewsMention]:
    """Parse the LLM reply; keep only mentions whose name (and domain) are in that result."""
    try:
        parsed = NewsMentions.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        log.warning("news extract unparseable", extra={"error": str(exc)})
        return []
    keep = []
    for m in parsed.mentions:
        if not 0 <= m.result < len(results):
            continue
        text = _result_text(results[m.result])
        if not quote_in_text(m.company, text):
            log.warning("news mention not in text", extra={"company": m.company})
            continue
        domain = m.domain if m.domain and quote_in_text(m.domain, text) else None
        keep.append(m.model_copy(update={"domain": domain}))
    return keep


async def door_news(job_id: Any, spec: RunSpec, deps: Deps, s: Settings) -> list[Candidate]:
    out: list[Candidate] = []
    for query in news_queries(spec, s):
        out.extend(await _news_for_query(job_id, query, deps, s))
    return out


async def _news_for_query(job_id: Any, query: str, deps: Deps, s: Settings) -> list[Candidate]:
    today = deps.today()
    since = today - timedelta(days=s.windows.trigger_days)
    page = await deps.web_search(job_id, query)
    # Keep undated results (date checked later); drop ones dated outside the window.
    dated = [
        (i, r, snippet_date(r.get("snippet") or "", today)) for i, r in enumerate(page.records)
    ]
    in_window = [(i, r, d) for i, r, d in dated if d is None or since <= d <= today]
    if not in_window:
        return []
    results = [r for _, r, _ in in_window]
    listing = "\n".join(f"[{n}] {_result_text(r)}" for n, r in enumerate(results))
    llm = await deps.get_llm()
    raw = await llm.generate_json(
        EXTRACT_PROMPT.format(results=listing), NewsMentions.model_json_schema(),
        job_id=job_id, endpoint=EXTRACT_ENDPOINT,
    )  # fmt: skip
    out = []
    for j, m in enumerate(grounded_mentions(raw, results)):
        i, rec, d = in_window[m.result]
        out.append(await _from_news(deps, page.request_hash, i, j, rec, d, m))
    return out


async def _from_news(
    deps: Deps, rhash: str, i: int, j: int, rec: dict[str, Any], d: date | None, m: NewsMention
) -> Candidate:
    url = rec.get("resolved_url") or rec.get("url") or "crustdata:web_search"
    title = rec.get("title") or ""
    src = await _save(
        deps, rec, url, "crustdata.web_search", quote=m.company, record_date=d,
    )  # fmt: skip
    signal = Signal(type=TriggerType.new_warehouse, date=d, title=title, url=url, source=src)
    domain = _domain(m.domain)
    return Candidate(
        key=f"{Door.news}:{rhash[:KEY_HASH_LEN]}:{i}:{j}",
        door=Door.news,
        name=m.company,
        domain=domain,
        needs_identity=domain is None,
        signals=[signal],
        sources=[src],
    )


# ---- step ---------------------------------------------------------------------------------
async def discover(
    job_id: Any, spec: RunSpec, deps: Deps | None = None, settings: Settings | None = None
) -> list[Candidate]:
    """Run both doors. Door 2 adds companies door 1's filters can miss."""
    deps = deps or Deps()
    s = settings or get_settings()
    found = await door_search(job_id, spec, deps, s)
    found += await door_jobs(job_id, deps, s)
    found += await door_news(job_id, spec, deps, s)
    log.info(
        "discovered",
        extra={
            "candidates": len(found),
            "by_door": {d: sum(c.door == d for c in found) for d in Door},
        },
    )
    return found
