"""Runner step "registry" (per account): is the legal entity identity proved still alive?

Inputs are the GSTIN / CIN facts the identity step verified; nothing else is looked up.
Each side (GST, MCA) is checked on its own, then the two are combined:
- a dead status on one side and an active one on the other -> conflict, held;
- otherwise a dead status (Cancelled at GST, Strike Off at MCA) -> dropped;
- an unclear status or a proved ID the registry no longer knows -> held;
- an active status -> status_active verified True;
- no registry answer at all -> cannot_verify("registry unavailable: ..."), held.
The PAN (GSTIN characters 3-12) is added to entity_keys. All writes commit with the step.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

from app import db
from app.config import Settings
from app.discovery.dedup import pan_from_gstin
from app.evidence.models import Fact, FactStatus, Source, cannot_verify, conflict, verified
from app.evidence.store import save_evidence
from app.identity import crosswalk as cw
from app.log import get_logger
from app.runner.context import StepContext
from app.trust.registry import (
    InvalidCin,
    InvalidGstin,
    RegistryClient,
    RegistryRecord,
    RegistryUnavailable,
    make_registry,
)

log = get_logger(__name__)

SaveFn = Callable[..., Source]
RESOLVED_BY = "trust:registry"
SOURCE_GST, SOURCE_MCA = "registry.gst", "registry.mca"
FIELD_GST, FIELD_MCA = "gst.status", "mca.status"
NO_IDS = "no GSTIN or CIN proven by identity"
KEY_COLUMNS = ("entity_id", "key_type", "key_value", "evidence_id", "resolved_by")

_UPDATE_ACCOUNT = "UPDATE accounts SET data = data || %s WHERE id = %s"
_PAN_OWNER = "SELECT entity_id FROM entity_keys WHERE key_type = %s AND key_value = %s"


class Check(StrEnum):
    active = "active"
    dead = "dead"
    unclear = "unclear"  # a status we don't classify (Suspended, Dormant, ...) or none
    not_found = "not_found"  # identity proved this ID, the registry doesn't know it now
    unavailable = "unavailable"
    not_checked = "not_checked"  # identity proved no such ID


class Outcome(StrEnum):
    passed = "pass"
    dropped = "dropped"
    held = "held"


@dataclass(frozen=True)
class SideCheck:
    kind: str  # "gst" | "mca"
    id: str | None
    check: Check
    reason: str
    status_text: str | None = None
    source: Source | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "check": self.check.value,
            "status": self.status_text,
            "reason": self.reason,
            "evidence_id": self.source.evidence_id if self.source else None,
        }


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    status_active: Fact[Any]
    flags: list[str] = field(default_factory=list)


def db_pan_owner(pan: str) -> str | None:
    with db.tx() as conn:
        row = conn.execute(_PAN_OWNER, (cw.KEY_PAN, pan)).fetchone()
    return row["entity_id"] if row else None


def _today() -> date:
    return datetime.now(UTC).date()


@dataclass
class RegistryDeps:
    registry: RegistryClient | None = None  # None -> make_registry(ctx.config)
    save: SaveFn = save_evidence
    pan_owner: Callable[[str], str | None] = db_pan_owner
    today: Callable[[], date] = _today


# ---- inputs from identity -----------------------------------------------------------------
def proven_value(fact: Any) -> str | None:
    """The value of a verified Fact stored by identity (as JSON), else None."""
    if not isinstance(fact, dict) or fact.get("status") != FactStatus.verified.value:
        return None
    value = fact.get("value")
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def first_evidence_id(fact: Any) -> str | None:
    sources = fact.get("sources") if isinstance(fact, dict) else None
    return next((s.get("evidence_id") for s in sources or [] if s.get("evidence_id")), None)


# ---- one side -----------------------------------------------------------------------------
def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def classify(status: str | None, active: tuple[str, ...], dead: tuple[str, ...]) -> Check:
    s = _norm(status)
    if s and s in {_norm(a) for a in active}:
        return Check.active
    if s and s in {_norm(d) for d in dead}:
        return Check.dead
    return Check.unclear


async def _cite(
    deps: RegistryDeps, rec: RegistryRecord, name: str, path: str, record_date: date
) -> Source:
    return await asyncio.to_thread(
        deps.save, rec.raw, rec.url, name, field_path=path, record_date=record_date
    )


def _status_reason(kind: str, rid: str, status: str | None, check: Check) -> str:
    where = "the GST registry" if kind == "gst" else "MCA"
    if status is None:
        return f"{where} gave no status for {rid}"
    if check in (Check.active, Check.dead):
        return f"{rid} is {status} in {where}"
    return f"{rid} has status {status!r} in {where}, which needs a person"


async def check_gst(job_id: Any, gstin: str | None, deps: RegistryDeps, s: Settings) -> SideCheck:
    if gstin is None:
        return SideCheck("gst", None, Check.not_checked, "no GSTIN proven by identity")
    reg = deps.registry
    try:
        res = await reg.gst_lookup(job_id, gstin)
    except InvalidGstin:
        return SideCheck("gst", gstin, Check.unclear, f"{gstin} is not a valid GSTIN")
    if isinstance(res, RegistryUnavailable):
        return SideCheck("gst", gstin, Check.unavailable, f"GST registry unavailable: {res.reason}")
    if res is None:
        return SideCheck("gst", gstin, Check.not_found, f"{gstin} is not in the GST registry")
    check = classify(res.status, s.registry.gst_active_statuses, s.registry.gst_drop_statuses)
    src = await _cite(deps, res, SOURCE_GST, FIELD_GST, res.last_updated or deps.today())
    reason = _status_reason("gst", gstin, res.status, check)
    return SideCheck("gst", gstin, check, reason, res.status, src)


async def check_mca(job_id: Any, cin: str | None, deps: RegistryDeps, s: Settings) -> SideCheck:
    if cin is None:
        return SideCheck("mca", None, Check.not_checked, "no CIN proven by identity")
    reg = deps.registry
    try:
        res = await reg.mca_lookup(job_id, cin=cin)
    except InvalidCin:
        return SideCheck("mca", cin, Check.unclear, f"{cin} is not a valid CIN")
    if isinstance(res, RegistryUnavailable):
        return SideCheck("mca", cin, Check.unavailable, f"MCA registry unavailable: {res.reason}")
    rec = next((r for r in res if (r.cin or "").upper() == cin), None)
    if rec is None:
        return SideCheck("mca", cin, Check.not_found, f"{cin} is not on the MCA register")
    check = classify(rec.status, s.registry.mca_active_statuses, s.registry.mca_drop_statuses)
    src = await _cite(deps, rec, SOURCE_MCA, FIELD_MCA, deps.today())
    reason = _status_reason("mca", cin, rec.status, check)
    return SideCheck("mca", cin, check, reason, rec.status, src)


# ---- both sides ---------------------------------------------------------------------------
def decide(gst: SideCheck, mca: SideCheck) -> Decision:
    sides = (gst, mca)
    sources = [x.source for x in sides if x.source]
    active = [x for x in sides if x.check is Check.active]
    dead = [x for x in sides if x.check is Check.dead]
    gaps = [x.reason for x in sides if x.check in (Check.unavailable, Check.not_checked)]
    if active and dead:
        a, d = active[0], dead[0]
        reason = f"registries disagree: {a.reason}, but {d.reason}"
        return Decision(Outcome.held, reason, conflict([(True, a.source), (False, d.source)]))
    if dead:
        reason = "; ".join(x.reason for x in dead)
        return Decision(Outcome.dropped, reason, verified(False, [x.source for x in dead]), gaps)
    unclear = [x for x in sides if x.check in (Check.unclear, Check.not_found)]
    if unclear:
        reason = "; ".join(x.reason for x in unclear)
        return Decision(Outcome.held, reason, cannot_verify(reason, sources), gaps)
    if active:
        reason = "; ".join(x.reason for x in active)
        return Decision(Outcome.passed, reason, verified(True, sources), gaps)
    if any(x.check is Check.unavailable for x in sides):
        down = "; ".join(x.reason for x in sides if x.check is Check.unavailable)
        reason = f"registry unavailable: {down}"
    else:
        reason = NO_IDS
    return Decision(Outcome.held, reason, cannot_verify(reason))


# ---- PAN key ------------------------------------------------------------------------------
def pan_key(
    ctx: StepContext, gstin: str | None, gst: SideCheck, deps: RegistryDeps
) -> tuple[str | None, str | None]:
    """Buffer the PAN entity_keys row. Returns (pan, problem) where problem holds the account."""
    pan = pan_from_gstin(gstin)
    if pan is None:
        return None, None
    data = (ctx.account or {}).get("data") or {}
    entity_id = (ctx.account or {}).get("entity_id") or (data.get("identity") or {}).get(
        "entity_id"
    )
    if not entity_id:
        return pan, None
    owner = deps.pan_owner(pan)
    if owner == entity_id:
        return pan, None
    if owner is not None:
        return pan, f"PAN {pan} is already keyed to another entity ({owner})"
    ev = gst.source.evidence_id if gst.source else first_evidence_id(data.get("gstin"))
    row = (entity_id, cw.KEY_PAN, pan, ev, RESOLVED_BY)
    ctx.write(
        cw.INSERT_KEY,
        row,
        tbl="entity_keys",
        record_id=f"{cw.KEY_PAN}:{pan}",
        before=None,
        after=dict(zip(KEY_COLUMNS, row, strict=True)),
        reason="PAN derived from GSTIN characters 3-12",
        evidence_ids=(ev,) if ev else (),
    )
    return pan, None


# ---- the step -----------------------------------------------------------------------------
async def run_registry(ctx: StepContext, deps: RegistryDeps | None = None) -> dict[str, Any]:
    deps = deps or RegistryDeps()
    if deps.registry is None:
        deps.registry = make_registry(ctx.config)
    account = ctx.account or {}
    data = account.get("data") or {}
    gstin, cin = proven_value(data.get("gstin")), proven_value(data.get("cin"))
    gst = await check_gst(ctx.job_id, gstin, deps, ctx.config)
    mca = await check_mca(ctx.job_id, cin, deps, ctx.config)
    d = decide(gst, mca)
    pan, pan_problem = await asyncio.to_thread(pan_key, ctx, gstin, gst, deps)
    outcome, reason = d.outcome, d.reason
    if pan_problem and outcome is Outcome.passed:
        outcome, reason = Outcome.held, pan_problem
    summary = {
        "outcome": outcome.value,
        "reason": reason,
        "gst": gst.summary(),
        "mca": mca.summary(),
        "pan": pan,
        "flags": [*d.flags, *([pan_problem] if pan_problem else [])],
    }
    patch = {"status_active": d.status_active.model_dump(mode="json"), "registry": summary}
    evs = tuple(sorted({s.evidence_id for s in d.status_active.sources}))
    ctx.write(
        _UPDATE_ACCOUNT,
        (Jsonb(patch), account.get("id")),
        tbl="accounts",
        record_id=account.get("id"),
        before={k: data.get(k) for k in patch},
        after=patch,
        reason=f"registry: {outcome.value} ({reason})",
        evidence_ids=evs,
    )
    if outcome is Outcome.dropped:
        ctx.drop(f"registry: {reason}", evs)
    elif outcome is Outcome.held:
        ctx.hold(f"registry: {reason}", evs)
    log.info("registry", extra={"outcome": outcome.value, "reason": reason})
    return {**summary, "status_active": patch["status_active"]}
