"""Runner step "pre_rank": a cheap score from search fields only, then a greedy knapsack.

score: weighted share of industry fit, headcount in range, India state match and a dated
door-2 trigger inside the window. Unknown fields earn nothing: they are not evidence.
Order by score / estimated credits still to spend, keep the top shortlist.pre_rank_top.
Held clusters (possible duplicates) and excluded domains never enter the shortlist.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz

from app.config import Settings, get_settings
from app.discovery.dedup import fold
from app.discovery.models import Cluster, Door
from app.log import get_logger
from app.spec.models import RunSpec

log = get_logger(__name__)

DROP_EXCLUDED = "excluded domain"


class Ranked(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster_id: str
    score: float
    est_credits: float
    value_per_credit: float
    reasons: list[str] = Field(default_factory=list)


class PreRankResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shortlist: list[Ranked]
    rest: list[Ranked]  # ranked but cut by pre_rank_top or the credit budget
    held: dict[str, str]  # cluster_id -> reason
    dropped: dict[str, str]  # cluster_id -> reason


def industry_fit(c: Cluster, spec: RunSpec, s: Settings) -> bool:
    """Door 1 filtered on the spec's industries; door-2 records carry their own industries."""
    if not spec.industries:
        return True
    if any(m.door is Door.search for m in c.members):
        return True
    have = [fold(i) for m in c.members for i in m.industries]
    return any(
        fuzz.token_set_ratio(fold(want), got) >= s.pre_rank.industry_ratio
        for want in spec.industries
        for got in have
    )


def headcount_in_range(c: Cluster, spec: RunSpec) -> bool:
    hc = c.first("headcount")
    if not isinstance(hc, int):
        return False
    lo = spec.size_min if spec.size_min is not None else hc
    hi = spec.size_max if spec.size_max is not None else hc
    return lo <= hc <= hi


def state_match(c: Cluster, spec: RunSpec) -> bool:
    wanted = {fold(st) for st in spec.geo_states}
    states = {fold(m.state) for m in c.members if m.state}
    return bool(states & wanted) if wanted else bool(states)


def trigger_present(c: Cluster, today: date, s: Settings) -> bool:
    since = today - timedelta(days=s.windows.trigger_days)
    return any(sig.date is not None and since <= sig.date <= today for sig in c.signals)


def score(c: Cluster, spec: RunSpec, today: date, s: Settings) -> tuple[float, list[str]]:
    w = s.pre_rank.weights
    checks = [
        ("industry_fit", w.industry_fit, industry_fit(c, spec, s)),
        ("headcount_in_range", w.headcount_in_range, headcount_in_range(c, spec)),
        ("state_match", w.state_match, state_match(c, spec)),
        ("trigger_present", w.trigger_present, trigger_present(c, today, s)),
    ]
    total = sum(weight for _, weight, _ in checks)
    got = sum(weight for _, weight, ok in checks if ok)
    return (got / total if total else 0.0), [name for name, _, ok in checks if ok]


def est_credits_remaining(c: Cluster, s: Settings) -> float:
    """Credits the later steps will likely spend on this account, from config estimates:
    identify (if needed) + enrich + buyer search + one news search and fetch."""
    e = s.crustdata.est_credits
    credits = (
        e.company_enrich_per_domain
        + e.person_search_per_result * s.crustdata.page_size.person_search
        + e.web_search_per_page
        + e.web_fetch_per_url
    )
    if c.needs_identity:
        credits += e.company_identify_per_input
    return credits


def _excluded(c: Cluster, spec: RunSpec) -> bool:
    excluded = set(spec.exclude_domains)
    return any(m.domain in excluded for m in c.members if m.domain)


def pre_rank(
    clusters: Sequence[Cluster],
    spec: RunSpec,
    *,
    today: date | None = None,
    budget_credits: float | None = None,
    settings: Settings | None = None,
) -> PreRankResult:
    s = settings or get_settings()
    today = today or date.today()
    held = {c.id: c.hold_reason or "" for c in clusters if c.held}
    dropped = {c.id: DROP_EXCLUDED for c in clusters if not c.held and _excluded(c, spec)}
    ranked = []
    for c in clusters:
        if c.id in held or c.id in dropped:
            continue
        sc, reasons = score(c, spec, today, s)
        est = est_credits_remaining(c, s)
        ranked.append(Ranked(cluster_id=c.id, score=sc, est_credits=est,
                             value_per_credit=sc / est, reasons=reasons))  # fmt: skip
    ranked.sort(key=lambda r: (-r.value_per_credit, -r.score, r.cluster_id))
    shortlist, rest, spent = [], [], 0.0
    for r in ranked:
        fits = budget_credits is None or spent + r.est_credits <= budget_credits
        if len(shortlist) < s.shortlist.pre_rank_top and fits:
            shortlist.append(r)
            spent += r.est_credits
        else:
            rest.append(r)
    log.info(
        "pre-ranked",
        extra={"shortlist": len(shortlist), "rest": len(rest), "held": len(held),
               "dropped": len(dropped), "est_credits": spent},
    )  # fmt: skip
    return PreRankResult(shortlist=shortlist, rest=rest, held=held, dropped=dropped)
