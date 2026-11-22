"""Runner step "enrich" (job-level, so calls batch across accounts).

Only accounts still active after trust are considered, best pre_rank first; the top
shortlist.enrich_top are enriched and the rest dropped (not worth the credits). A record already
in entity_cache costs nothing; the others go to Crustdata in batches of crustdata.batch_enrich.
Each new record gets one evidence row and an entity_cache entry (cache_ttl_hours.company);
the account points at it via data.enrich. All writes commit with the step.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from app import db
from app.discovery import crustdata
from app.discovery.normalize import normalize_domain
from app.evidence.models import Source
from app.evidence.store import save_evidence
from app.log import get_logger
from app.revenue.common import (
    ENRICH_DATA_TYPE,
    ENRICH_SOURCE,
    PUT_CACHE,
    UPDATE_DATA,
    crustdata_ids,
    entity_key,
    read_cache,
)
from app.runner.context import ACTIVE, DROPPED, StepContext

log = get_logger(__name__)

RECORD_PATH = "company_data"
SECONDS_PER_HOUR = 3600
_ACTIVE_ACCOUNTS = (
    "SELECT id, name, domain, entity_id, data FROM accounts "
    "WHERE job_id = %s AND status = %s ORDER BY id"
)


def db_active_accounts(job_id: Any) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return conn.execute(_ACTIVE_ACCOUNTS, (job_id, ACTIVE)).fetchall()


@dataclass
class EnrichDeps:
    active_accounts: Callable[[Any], list[dict[str, Any]]] = db_active_accounts
    read_cache: Callable[[str], dict[str, Any] | None] = read_cache
    enrich_companies: Callable[..., Awaitable[crustdata.EnrichResult]] = crustdata.enrich_companies
    save: Callable[..., Source] = save_evidence


def pre_rank_score(account: dict[str, Any]) -> float:
    data = account.get("data") or {}
    value = data.get("pre_rank_score", (data.get("pre_rank") or {}).get("score"))
    return float(value) if isinstance(value, int | float) else -math.inf


def _set_ref(ctx: StepContext, account: dict[str, Any], ref: dict[str, Any], reason: str) -> None:
    data = account.get("data") or {}
    ctx.write(
        UPDATE_DATA,
        (Jsonb({"enrich": ref}), account["id"]),
        tbl="accounts",
        record_id=account["id"],
        before={"enrich": data.get("enrich")},
        after={"enrich": ref},
        reason=reason,
        evidence_ids=(ref["evidence_id"],) if ref.get("evidence_id") else (),
    )


async def _store(
    ctx: StepContext, deps: EnrichDeps, domain: str, ec: crustdata.EnrichedCompany, url: str
) -> dict[str, Any]:
    """One new record: evidence row now, entity_cache row with the step. Returns the ref."""
    record = ec.record
    src = await asyncio.to_thread(deps.save, record, url, ENRICH_SOURCE, field_path=RECORD_PATH)
    key = entity_key(int(record["crustdata_company_id"]))
    payload = {"record": record, "evidence_id": src.evidence_id, "url": url,
               "retrieved_at": src.retrieved_at.isoformat(), "domain": domain}  # fmt: skip
    ttl_s = ctx.config.cache_ttl_hours.company * SECONDS_PER_HOUR
    summary = {"evidence_id": src.evidence_id, "request_hash": ec.request_hash}
    ctx.write(
        PUT_CACHE,
        (key, ENRICH_DATA_TYPE, Jsonb(payload), ttl_s),
        tbl="entity_cache",
        record_id=f"{key}:{ENRICH_DATA_TYPE}",
        before=None,
        after=summary,
        reason="enrich: company record cached",
        evidence_ids=(src.evidence_id,),
    )
    return {"entity_key": key, "evidence_id": src.evidence_id, "request_hash": ec.request_hash,
            "confidence": ec.confidence, "from_cache": False}  # fmt: skip


def _cached_ref(account: dict[str, Any], deps: EnrichDeps) -> dict[str, Any] | None:
    for cid in crustdata_ids(account.get("data") or {}):
        payload = deps.read_cache(entity_key(cid))
        if payload is not None:
            return {"entity_key": entity_key(cid), "evidence_id": payload.get("evidence_id"),
                    "request_hash": None, "confidence": None, "from_cache": True}  # fmt: skip
    return None


async def run_enrich(ctx: StepContext, deps: EnrichDeps | None = None) -> dict[str, Any]:
    deps = deps or EnrichDeps()
    s = ctx.config
    accounts = sorted(
        await asyncio.to_thread(deps.active_accounts, ctx.job_id),
        key=lambda a: (-pre_rank_score(a), str(a["id"])),
    )
    top, rest = accounts[: s.shortlist.enrich_top], accounts[s.shortlist.enrich_top :]
    for a in rest:
        ctx.set_account_status(
            a["id"], DROPPED, f"enrich: outside the top {s.shortlist.enrich_top} by pre_rank"
        )
    out: dict[str, Any] = {"enriched": [], "from_cache": [], "unresolved": {}, "dropped": len(rest)}
    to_call: dict[str, dict[str, Any]] = {}  # domain -> account
    for a in top:
        ref = await asyncio.to_thread(_cached_ref, a, deps)
        if ref is not None:
            _set_ref(ctx, a, ref, "enrich: record already cached (no credits)")
            out["from_cache"].append(str(a["id"]))
        elif a.get("domain"):
            to_call[a["domain"]] = a
        else:
            out["unresolved"][str(a["id"])] = "no domain to enrich by"
    if to_call:
        known = {d: crustdata_ids(a.get("data") or {})[0] for d, a in to_call.items()
                 if crustdata_ids(a.get("data") or {})}  # fmt: skip
        result = await deps.enrich_companies(ctx.job_id, list(to_call), known)
        url = s.crustdata.base_url + crustdata.PATHS["company_enrich"]
        for domain, a in to_call.items():
            ec = _record_for(result, domain)
            if ec is None:
                reason = result.unresolved.get(normalize_domain(domain)) or "no enrich record"
                out["unresolved"][str(a["id"])] = reason
                continue
            ref = await _store(ctx, deps, domain, ec, url)
            _set_ref(ctx, a, ref, "enrich: company record fetched")
            out["enriched"].append(str(a["id"]))
    counts = {k: len(v) if isinstance(v, list | dict) else v for k, v in out.items()}
    log.info("enrich", extra=counts)
    return out


def _record_for(result: crustdata.EnrichResult, domain: str) -> crustdata.EnrichedCompany | None:
    return result.records.get(normalize_domain(domain))
