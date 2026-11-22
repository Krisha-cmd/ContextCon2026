"""Registry bridge (MCA side, GST side) and the rule ladder that turns them into a decision.

The two sides are proven separately (the GST registry has no name search and no CIN->PAN),
then joined: every legal name we saw (site, MCA, GST) must agree after normalization.
A wrong match is worse than "cannot verify", so anything unclear is held for a person.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.discovery.dedup import normalize_name, pan_from_gstin
from app.discovery.normalize import normalize_domain
from app.evidence.models import Fact, Source, cannot_verify, conflict, verified
from app.identity import crosswalk as cw
from app.identity.india import cin_state, gst_state, same_state
from app.identity.matcher import features
from app.identity.site import SiteFindings
from app.trust.registry import InvalidGstin, Registry, RegistryError, RegistryRecord

SaveFn = Callable[..., Source]

NO_GSTIN = "no GSTIN published by the company"
SEVERAL = "several registry entities share this name"
NO_ENTITY = "no registry entity has this name"
OTHER_STATE = "the registry entity with this name is registered in another state than the HQ"
STATE_UNKNOWN = "HQ state unknown, so a single registry name match can't be confirmed"
NAMES_DIFFER = "legal names differ between sources"


class Outcome(StrEnum):
    match = "match"
    held = "held"
    cannot_verify = "cannot_verify"
    no_match = "no_match"


@dataclass(frozen=True)
class AccountIn:
    """What identity needs from an accounts row (name/domain columns + data keys)."""

    id: Any
    name: str
    domain: str | None = None
    crustdata_ids: tuple[int, ...] = ()
    state: str | None = None
    city: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AccountIn:
        data = row.get("data") or {}
        ids = data.get("crustdata_ids") or (
            [data["crustdata_id"]] if data.get("crustdata_id") else []
        )
        domain = row.get("domain") or data.get("domain")
        return cls(
            id=row["id"],
            name=row.get("name") or data.get("name") or "",
            domain=(normalize_domain(domain) or None) if domain else None,
            crustdata_ids=tuple(int(i) for i in ids),
            state=data.get("state"),
            city=data.get("city"),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "state": self.state,
            "crustdata_ids": list(self.crustdata_ids),
        }


@dataclass
class Side:
    """One registry side (MCA or GST): proven record + its Source, or what is missing."""

    proven: RegistryRecord | None = None
    source: Source | None = None
    missing: str | None = None
    red_flag: str | None = None  # an ID on the site the registry doesn't know: hold
    no_entity: bool = False  # name search found no entity with this name
    candidates: list[RegistryRecord] = field(default_factory=list)
    extra: list[tuple[RegistryRecord, Source]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class IdentityResult:
    outcome: Outcome
    reason: str
    legal_name: Fact[Any]
    gstin: Fact[Any]
    cin: Fact[Any]
    entity_id: str | None = None
    keys: list[tuple[str, str, str | None]] = field(default_factory=list)  # type, value, ev
    flags: list[str] = field(default_factory=list)
    label: dict[str, Any] | None = None

    def evidence_ids(self) -> tuple[str, ...]:
        ids = {s.evidence_id for f in (self.legal_name, self.gstin, self.cin) for s in f.sources}
        return tuple(sorted(ids))


async def _save(save: SaveFn, rec: RegistryRecord, source_name: str) -> Source:
    return await asyncio.to_thread(save, rec.raw, rec.url, source_name, field_path=rec.field_path)


def _norm(name: str | None, suffixes: tuple[str, ...]) -> str:
    return normalize_name(name or "", suffixes)


def _record_state(rec: RegistryRecord) -> str | None:
    if rec.state:
        return rec.state
    if rec.kind == "mca":
        return cin_state(rec.id)
    return gst_state(rec.id)


# ---- MCA side -----------------------------------------------------------------------------
async def mca_side(
    job_id: Any,
    acct: AccountIn,
    site: SiteFindings,
    reg: Registry,
    save: SaveFn,
    suffixes: tuple[str, ...],
) -> Side:
    if site.cin:
        return await _mca_by_cin(job_id, site.cin.value, reg, save)
    name = site.legal_name.value if site.legal_name else acct.name
    return await _mca_by_name(job_id, name, acct, reg, save, suffixes)


async def _mca_by_cin(job_id: Any, cin: str, reg: Registry, save: SaveFn) -> Side:
    try:
        rec = await reg.mca_lookup(job_id, cin)
    except RegistryError as exc:
        return Side(missing=f"CIN {cin} found on the site but not confirmed: {exc}")
    if rec is None:
        return Side(red_flag=f"CIN {cin} on the site is not on the MCA register")
    return Side(proven=rec, source=await _save(save, rec, "registry.mca"))


async def _mca_by_name(
    job_id: Any, name: str, acct: AccountIn, reg: Registry, save: SaveFn, suffixes: tuple[str, ...]
) -> Side:
    try:
        cands = await reg.company_search(job_id, name)
    except RegistryError as exc:
        return Side(missing=f"legal name {name!r} not confirmed: {exc}")
    pick, missing = choose_by_name(name, acct.state, cands, suffixes)
    if pick is None:
        return Side(missing=missing, no_entity=missing == NO_ENTITY, candidates=cands)
    return Side(proven=pick, source=await _save(save, pick, "registry.mca"), candidates=cands)


def choose_by_name(
    name: str, hq_state: str | None, cands: list[RegistryRecord], suffixes: tuple[str, ...]
) -> tuple[RegistryRecord | None, str | None]:
    """Exactly one candidate with an equal normalized name in the HQ state -> that one."""
    want = _norm(name, suffixes)
    equal = [c for c in cands if want and _norm(c.legal_name, suffixes) == want]
    if not equal:
        return None, NO_ENTITY
    if hq_state is None or not hq_state.strip():
        return None, SEVERAL if len(equal) > 1 else STATE_UNKNOWN
    in_state = [c for c in equal if same_state(_record_state(c), hq_state)]
    if len(in_state) == 1:
        return in_state[0], None
    return None, SEVERAL if len(in_state) > 1 else OTHER_STATE


# ---- GST side -----------------------------------------------------------------------------
async def gst_side(job_id: Any, site: SiteFindings, reg: Registry, save: SaveFn) -> Side:
    if not site.gstin:
        return Side(missing=NO_GSTIN)
    gstin = site.gstin.value
    try:
        rec = await reg.gst_lookup(job_id, gstin)
    except InvalidGstin:
        return Side(red_flag=f"GSTIN {gstin} on the site is not a valid GSTIN")
    except RegistryError as exc:
        return Side(missing=f"GSTIN {gstin} found on the site but not confirmed: {exc}")
    if rec is None:
        return Side(red_flag=f"GSTIN {gstin} on the site is not registered for GST")
    side = Side(proven=rec, source=await _save(save, rec, "registry.gst"))
    await _other_states(job_id, gstin, reg, save, side)
    return side


async def _other_states(job_id: Any, gstin: str, reg: Registry, save: SaveFn, side: Side) -> None:
    pan = pan_from_gstin(gstin)
    try:
        others = await reg.gstins_by_pan(job_id, pan) if pan else []
    except RegistryError as exc:
        side.notes.append(f"registrations in other states not checked: {exc}")
        return
    for rec in others:
        if rec.id != gstin:
            side.extra.append((rec, await _save(save, rec, "registry.gst")))


# ---- the ladder ---------------------------------------------------------------------------
def _names(site: SiteFindings, mca: Side, gst: Side) -> list[tuple[str, Source]]:
    out = []
    if site.legal_name:
        out.append((site.legal_name.value, site.legal_name.source))
    for side in (mca, gst):
        if side.proven and side.proven.legal_name and side.source:
            out.append((side.proven.legal_name, side.source))
    return out


def _distinct_names(names: list[tuple[str, Source]], suffixes: tuple[str, ...]) -> list:
    seen: dict[str, tuple[str, Source]] = {}
    for value, src in names:
        seen.setdefault(_norm(value, suffixes), (value, src))
    return list(seen.values())


def decide(
    acct: AccountIn, site: SiteFindings, mca: Side, gst: Side, suffixes: tuple[str, ...]
) -> IdentityResult:
    distinct = _distinct_names(_names(site, mca, gst), suffixes)
    cin_fact, gstin_fact = _cin_fact(site, mca), _gstin_fact(site, gst)
    if len(distinct) > 1:
        return _unresolved(
            Outcome.held,
            NAMES_DIFFER,
            conflict(distinct),
            cin_fact,
            gstin_fact,
            acct,
            site,
            mca,
            gst,
        )
    red = mca.red_flag or gst.red_flag
    if red:
        return _unresolved(
            Outcome.held, red, _name_unproven(red, site), cin_fact, gstin_fact, acct, site, mca, gst
        )
    if mca.proven or gst.proven:
        return _matched(acct, site, mca, gst, cin_fact, gstin_fact)
    if mca.no_entity and gst.missing == NO_GSTIN:
        return IdentityResult(
            Outcome.no_match, NO_ENTITY, _name_unproven(NO_ENTITY, site), gstin_fact, cin_fact
        )
    reason = mca.missing or gst.missing or "legal entity not proven"
    return _unresolved(
        Outcome.cannot_verify,
        reason,
        _name_unproven(reason, site),
        cin_fact,
        gstin_fact,
        acct,
        site,
        mca,
        gst,
    )


def _name_unproven(reason: str, site: SiteFindings) -> Fact[Any]:
    srcs = [site.legal_name.source] if site.legal_name else []
    return cannot_verify(f"legal name not proven by a registry: {reason}", srcs)


def _cin_fact(site: SiteFindings, mca: Side) -> Fact[Any]:
    site_src = [site.cin.source] if site.cin else []
    if mca.proven and mca.source:
        return verified(mca.proven.id, [*site_src, mca.source])
    return cannot_verify(mca.red_flag or mca.missing or "no CIN found", site_src)


def _gstin_fact(site: SiteFindings, gst: Side) -> Fact[Any]:
    site_src = [site.gstin.source] if site.gstin else []
    if gst.proven and gst.source:
        return verified(gst.proven.id, [*site_src, gst.source])
    return cannot_verify(gst.red_flag or gst.missing or NO_GSTIN, site_src)


def _matched(
    acct: AccountIn,
    site: SiteFindings,
    mca: Side,
    gst: Side,
    cin_fact: Fact[Any],
    gstin_fact: Fact[Any],
) -> IdentityResult:
    rec = mca.proven or gst.proven
    srcs = [s for s in (site.legal_name and site.legal_name.source, mca.source, gst.source) if s]
    name = (mca.proven and mca.proven.legal_name) or (gst.proven and gst.proven.legal_name)
    legal = verified(name, srcs) if name else cannot_verify("registry record has no legal name")
    flags = [*mca.notes, *gst.notes]
    if not mca.proven and mca.missing:
        flags.insert(0, f"MCA: {mca.missing}")
    if not gst.proven and gst.missing:
        flags.insert(0, f"GST: {gst.missing}")
    via = "CIN" if site.cin and mca.proven else "name" if mca.proven else "GSTIN"
    reason = {
        "CIN": "CIN on the site confirmed at MCA",
        "GSTIN": "GSTIN on the site confirmed in the GST registry",
        "name": "one registry entity with this legal name in the HQ state",
    }[via]
    entity_id = f"cin:{mca.proven.id}" if mca.proven else f"pan:{pan_from_gstin(rec.id)}"
    return IdentityResult(
        Outcome.match,
        reason,
        legal,
        gstin_fact,
        cin_fact,
        entity_id,
        _keys(acct, site, mca, gst, name),
        flags,
    )


def _keys(
    acct: AccountIn, site: SiteFindings, mca: Side, gst: Side, name: str | None
) -> list[tuple[str, str, str | None]]:
    link = next((h.source for h in site.hits()), None) or mca.source or gst.source
    link_ev = link.evidence_id if link else None
    keys: list[tuple[str, str, str | None]] = []
    if acct.domain:
        keys.append((cw.KEY_DOMAIN, acct.domain, link_ev))
    keys += [(cw.KEY_CRUSTDATA, str(i), link_ev) for i in acct.crustdata_ids]
    if mca.proven and mca.source:
        keys.append((cw.KEY_CIN, mca.proven.id, mca.source.evidence_id))
    if gst.proven and gst.source:
        pan = pan_from_gstin(gst.proven.id)
        keys.append((cw.KEY_GSTIN, gst.proven.id, gst.source.evidence_id))
        if pan:
            keys.append((cw.KEY_PAN, pan, gst.source.evidence_id))
        keys += [(cw.KEY_GSTIN, r.id, s.evidence_id) for r, s in gst.extra]
    name_src = mca.source or gst.source
    if name and name_src:
        keys.append((cw.KEY_LEGAL_NAME, name, name_src.evidence_id))
    return keys


def _unresolved(
    outcome: Outcome,
    reason: str,
    legal: Fact[Any],
    cin_fact: Fact[Any],
    gstin_fact: Fact[Any],
    acct: AccountIn,
    site: SiteFindings,
    mca: Side,
    gst: Side,
) -> IdentityResult:
    return IdentityResult(
        outcome, reason, legal, gstin_fact, cin_fact, label=label(acct, site, mca, gst, reason)
    )


def _rec_summary(rec: RegistryRecord, acct: AccountIn) -> dict[str, Any]:
    right = {
        "kind": rec.kind,
        "id": rec.id,
        "name": rec.legal_name,
        "status": rec.status,
        "state": _record_state(rec),
    }
    left = {"name": acct.name, "state": acct.state}
    if rec.kind == "mca":
        right["cin"] = rec.id
    else:
        right["gstin"] = rec.id
    return {**right, "features": features(left, right)}


def label(acct: AccountIn, site: SiteFindings, mca: Side, gst: Side, reason: str) -> dict:
    """A match_labels candidate row for a person to resolve later."""
    recs = [*mca.candidates, *(r for r in (mca.proven, gst.proven) if r)]
    seen: dict[str, RegistryRecord] = {r.id: r for r in recs}
    return {
        "left": acct.summary(),
        "right": {
            "reason": reason,
            "candidates": [_rec_summary(r, acct) for r in seen.values()],
            "site": {
                k: getattr(site, k).value
                for k in ("legal_name", "cin", "gstin")
                if getattr(site, k)
            },
            "notes": site.notes + mca.notes + gst.notes,
        },
    }


# ---- crosswalk hit ------------------------------------------------------------------------
def from_crosswalk(acct: AccountIn, hit: cw.CrosswalkHit) -> IdentityResult:
    if hit.ambiguous:
        reason = f"crosswalk keys point at several entities: {hit.entity_ids}"
        unknown = cannot_verify(reason)
        return IdentityResult(
            Outcome.held,
            reason,
            unknown,
            unknown,
            unknown,
            label={
                "left": acct.summary(),
                "right": {"reason": reason, "entity_ids": hit.entity_ids},
            },
        )

    def fact(key_type: str) -> Fact[Any]:
        k = hit.first(key_type)
        if k is None:
            return cannot_verify(f"no {key_type} stored for this entity")
        if k.source is None:
            return cannot_verify(f"stored {key_type} has no evidence")
        return verified(k.key_value, k.source)

    entity_id = hit.entity_ids[0]
    name = fact(cw.KEY_LEGAL_NAME)
    ev = next((s.evidence_id for s in name.sources), None)
    keys = [(cw.KEY_DOMAIN, acct.domain, ev)] if acct.domain else []
    keys += [(cw.KEY_CRUSTDATA, str(i), ev) for i in acct.crustdata_ids]
    return IdentityResult(
        Outcome.match,
        "reused an earlier proof from the crosswalk",
        name,
        fact(cw.KEY_GSTIN),
        fact(cw.KEY_CIN),
        entity_id,
        keys,
    )
