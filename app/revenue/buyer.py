"""Runner step "buyer" (per account): the person to write to.

Chain: Crustdata person search at the company (titles from the RunSpec, else
buyer.default_titles) -> the cached enrich record's people.decision_makers and people.cxos,
filtered by the same title matcher -> cannot_verify, account held.
A buyer is stale when the profile's latest date is older than windows.stale_buyer_months
(or unknown). Only name, title, company, profile URL and the source are kept: no email and no
phone (see README, "Ethics"). The raw profile is hashed into the evidence row, not stored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb

from app.config import Settings
from app.discovery import crustdata
from app.discovery.crustdata import CrustdataError
from app.discovery.normalize import get_path, known, parse_date
from app.evidence.models import Fact, Source, cannot_verify, verified
from app.evidence.store import save_evidence
from app.gateway import GatewayError
from app.log import get_logger
from app.revenue.common import (
    UPDATE_DATA,
    cached_enrich,
    crustdata_ids,
    run_date,
    run_spec,
    source_from,
)
from app.revenue.titles import TitleMatch, match
from app.runner.context import StepContext

log = get_logger(__name__)

SEARCH, ENRICH = "person_search", "enrich.people"
ENRICH_GROUPS = ("decision_makers", "cxos")
SOURCE_SEARCH = "crustdata.person_search"
PROFILE_URL = "social_handles.professional_network_identifier.profile_url"
MONTHS_PER_YEAR = 12


@dataclass(frozen=True)
class Person:
    origin: str
    path: str
    name: str | None
    title: str | None
    company: str | None
    profile_url: str | None
    latest: date | None
    raw: dict[str, Any]


def _latest(*values: Any) -> date | None:
    dates = [d for d in (parse_date(v) for v in values) if d is not None]
    return max(dates, default=None)


def from_search(profiles: list[dict[str, Any]]) -> list[Person]:
    out = []
    for i, p in enumerate(profiles):
        current = (get_path(p, "experience.employment_details.current") or [{}])[0] or {}
        out.append(Person(
            SEARCH, f"profiles[{i}]", known(get_path(p, "basic_profile.name")),
            known(get_path(p, "basic_profile.current_title")) or known(current.get("title")),
            known(current.get("name")), known(get_path(p, PROFILE_URL)),
            _latest(get_path(p, "metadata.updated_at"), get_path(p, "basic_profile.last_updated")),
            p,
        ))  # fmt: skip
    return out


def from_enrich(record: dict[str, Any] | None) -> list[Person]:
    out = []
    people = (record or {}).get("people") or {}
    for group in ENRICH_GROUPS:
        for i, p in enumerate(people.get(group) or []):
            if not isinstance(p, dict):
                continue
            current = (get_path(p, "experience.employment_details.current") or [{}])[0] or {}
            out.append(Person(
                ENRICH, f"people.{group}[{i}]", known(get_path(p, "basic_profile.name")),
                known(get_path(p, "basic_profile.current_title")),
                known(current.get("employer_name")), known(get_path(p, PROFILE_URL)),
                _latest(get_path(p, "basic_profile.last_updated")), p,
            ))  # fmt: skip
    return out


def pick(people: list[Person], targets: list[str], s: Settings) -> tuple[Person, TitleMatch] | None:
    best: tuple[Person, TitleMatch] | None = None
    for p in people:
        m = match(p.title, targets, s.buyer)
        if m is None:
            continue
        key = (m.confidence, m.rank, p.latest or date.min)
        if best is None or key > (best[1].confidence, best[1].rank, best[0].latest or date.min):
            best = (p, m)
    return best


def months_before(d: date, months: int) -> date:
    total = d.year * MONTHS_PER_YEAR + d.month - 1 - months
    year, month = divmod(total, MONTHS_PER_YEAR)
    day = min(d.day, 28)  # every month has a 28th; a few days of slack is fine for staleness
    return date(year, month + 1, day)


def is_stale(latest: date | None, today: date, s: Settings) -> bool:
    return latest is None or latest < months_before(today, s.windows.stale_buyer_months)


@dataclass
class BuyerDeps:
    search_people: Callable[..., Awaitable[crustdata.Page]] = crustdata.search_people
    cached_enrich: Callable[[dict[str, Any]], dict[str, Any] | None] = cached_enrich
    save: Callable[..., Source] = save_evidence


async def _source(
    p: Person, payload: dict[str, Any] | None, deps: BuyerDeps, s: Settings
) -> Source:
    if p.origin == ENRICH and payload is not None:
        return source_from(payload, p.path, p.latest)
    url = p.profile_url or s.crustdata.base_url + crustdata.PATHS["person_search"]
    return await asyncio.to_thread(deps.save, p.raw, url, SOURCE_SEARCH, field_path=p.path,
                                   record_date=p.latest)  # fmt: skip


def buyer_value(p: Person, m: TitleMatch, stale: bool) -> dict[str, Any]:
    return {"name": p.name, "title": p.title, "company": p.company, "profile_url": p.profile_url,
            "matched_target": m.target, "confidence": m.confidence, "seniority_rank": m.rank,
            "latest_date": p.latest.isoformat() if p.latest else None, "stale": stale,
            "found_in": p.origin}  # fmt: skip


async def find(ctx: StepContext, deps: BuyerDeps) -> tuple[Fact[Any], list[str]]:
    s, account = ctx.config, ctx.account or {}
    data = account.get("data") or {}
    targets = list(run_spec(ctx.job).buyer_titles or s.buyer.default_titles)
    tried: list[str] = []
    found: tuple[Person, TitleMatch] | None = None
    payload = None
    ids = crustdata_ids(data)
    if ids:
        try:
            page = await deps.search_people(ctx.job_id, ids[0], targets)
            found = pick(from_search(page.records), targets, s)
            tried.append(f"person search: {len(page.records)} profile(s)")
        except (CrustdataError, GatewayError) as exc:
            tried.append(f"person search failed: {type(exc).__name__}")
    else:
        tried.append("person search skipped: no crustdata id")
    if found is None:
        payload = await asyncio.to_thread(deps.cached_enrich, data)
        people = from_enrich(payload["record"] if payload else None)
        found = pick(people, targets, s)
        tried.append(f"enrich decision_makers/cxos: {len(people)} profile(s)" if payload
                     else "enrich people: no cached enrich record")  # fmt: skip
    if found is None:
        why = "no buyer matching the target titles; tried " + "; ".join(tried)
        return cannot_verify(why), tried
    p, m = found
    stale = is_stale(p.latest, run_date(ctx.job), s)
    src = await _source(p, payload, deps, s)
    return verified(buyer_value(p, m, stale), src, stale=stale), tried


async def run_buyer(ctx: StepContext, deps: BuyerDeps | None = None) -> dict[str, Any]:
    fact, tried = await find(ctx, deps or BuyerDeps())
    account = ctx.account or {}
    data = account.get("data") or {}
    patch = {"buyer": fact.model_dump(mode="json"), "buyer_check": {"tried": tried}}
    evs = tuple(sorted({x.evidence_id for x in fact.sources}))
    ctx.write(UPDATE_DATA, (Jsonb(patch), account.get("id")), tbl="accounts",
              record_id=account.get("id"), before={k: data.get(k) for k in patch}, after=patch,
              reason=f"buyer: {fact.status.value}", evidence_ids=evs)  # fmt: skip
    if fact.value is None:
        ctx.hold(f"buyer: {fact.missing}", evs)
    log.info("buyer", extra={"status": fact.status.value, "tried": tried})
    return {"buyer": patch["buyer"], "tried": tried}
