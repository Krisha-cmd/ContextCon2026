"""Runner step "identity" (per account): prove which legal entity the account is.

1. Crosswalk (entity_keys) by domain / crustdata_id: a hit reuses the earlier proof, free.
2. Legal-name bridge: the company's own pages -> legal name, CIN, GSTIN (each a quoted Source).
3. Registry bridge: MCA side and GST side proven separately, then joined.
4. Rule ladder -> match / held / cannot_verify / no_match.
5. Facts written to the account; entity_keys on a match; a match_labels row when held or
   cannot_verify; the account held or dropped otherwise. All writes commit with the step.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from app import db
from app.config import Settings
from app.discovery import crustdata
from app.evidence.store import save_evidence
from app.identity import crosswalk as cw
from app.identity.ladder import (
    AccountIn,
    IdentityResult,
    Outcome,
    decide,
    from_crosswalk,
    gst_side,
    mca_side,
)
from app.identity.site import FetchFn, SaveFn, SiteFindings, scan_site
from app.log import get_logger
from app.runner.context import StepContext
from app.trust.registry import RegistryClient, make_registry

log = get_logger(__name__)

FACT_KEYS = ("legal_name", "gstin", "cin")
LABEL_PENDING = "candidate"
RESOLVED_PENDING = "pending"

_UPDATE_ACCOUNT = (
    "UPDATE accounts SET data = data || %s, entity_id = coalesce(%s, entity_id) WHERE id = %s"
)
_INSERT_LABEL = (
    "INSERT INTO match_labels (left_record, right_record, label, resolved_by) "
    "VALUES (%s, %s, %s, %s)"
)


def db_crosswalk(pairs: list[tuple[str, str]]) -> cw.CrosswalkHit | None:
    with db.tx() as conn:
        return cw.lookup(conn, pairs)


@dataclass
class IdentityDeps:
    fetch: FetchFn = crustdata.web_fetch
    registry: RegistryClient = field(default_factory=make_registry)
    save: SaveFn = save_evidence
    crosswalk: Callable[[list[tuple[str, str]]], cw.CrosswalkHit | None] = db_crosswalk


def lookup_pairs(acct: AccountIn) -> list[tuple[str, str]]:
    pairs = [(cw.KEY_DOMAIN, acct.domain)] if acct.domain else []
    return pairs + [(cw.KEY_CRUSTDATA, str(i)) for i in acct.crustdata_ids]


async def identify(acct: AccountIn, job_id: Any, deps: IdentityDeps, s: Settings) -> IdentityResult:
    hit = await asyncio.to_thread(deps.crosswalk, lookup_pairs(acct))
    if hit is not None:
        return from_crosswalk(acct, hit)
    if acct.domain:
        site = await scan_site(job_id, acct.domain, s.identity.site_paths, deps.fetch, deps.save)
    else:
        site = SiteFindings(notes=["no domain, so no company pages to read"])
    suffixes = s.dedup.name_suffixes
    mca = await mca_side(job_id, acct, site, deps.registry, deps.save, suffixes)
    gst = await gst_side(job_id, site, deps.registry, deps.save)
    return decide(acct, site, mca, gst, suffixes)


def apply(ctx: StepContext, acct: AccountIn, r: IdentityResult) -> None:
    """Buffer every write on the context; the runner commits them with the step."""
    data = dict((ctx.account or {}).get("data") or {})
    patch = {k: getattr(r, k).model_dump(mode="json") for k in FACT_KEYS}
    patch["identity"] = {
        "outcome": r.outcome.value,
        "reason": r.reason,
        "flags": r.flags,
        "entity_id": r.entity_id,
    }
    before = {k: data.get(k) for k in patch}
    evs = r.evidence_ids()
    ctx.write(
        _UPDATE_ACCOUNT,
        (Jsonb(patch), r.entity_id, acct.id),
        tbl="accounts",
        record_id=acct.id,
        before=before,
        after=patch,
        reason=f"identity: {r.outcome.value} ({r.reason})",
        evidence_ids=evs,
    )
    for key_type, value, ev in r.keys:
        row = (r.entity_id, key_type, value, ev, cw.RESOLVED_BY)
        ctx.write(
            cw.INSERT_KEY,
            row,
            tbl="entity_keys",
            record_id=f"{key_type}:{value}",
            before=None,
            after=dict(
                zip(
                    ("entity_id", "key_type", "key_value", "evidence_id", "resolved_by"),
                    row,
                    strict=True,
                )
            ),
            reason="identity match",
            evidence_ids=(ev,) if ev else (),
        )
    if r.label is not None:
        ctx.write(
            _INSERT_LABEL,
            (Jsonb(r.label["left"]), Jsonb(r.label["right"]), LABEL_PENDING, RESOLVED_PENDING),
            tbl="match_labels",
            record_id=f"account:{acct.id}",
            before=None,
            after=r.label,
            reason=f"identity {r.outcome.value}: needs a person",
            evidence_ids=evs,
        )
    if r.outcome in (Outcome.held, Outcome.cannot_verify):
        ctx.hold(f"identity {r.outcome.value}: {r.reason}", evs)
    elif r.outcome is Outcome.no_match:
        ctx.drop(f"identity no_match: {r.reason}", evs)


async def run_identity(ctx: StepContext, deps: IdentityDeps | None = None) -> dict[str, Any]:
    acct = AccountIn.from_row(ctx.account or {})
    r = await identify(acct, ctx.job_id, deps or IdentityDeps(), ctx.config)
    apply(ctx, acct, r)
    log.info("identity", extra={"outcome": r.outcome.value, "reason": r.reason})
    return {
        "outcome": r.outcome.value,
        "reason": r.reason,
        "entity_id": r.entity_id,
        "flags": r.flags,
        **{k: getattr(r, k).model_dump(mode="json") for k in FACT_KEYS},
    }
