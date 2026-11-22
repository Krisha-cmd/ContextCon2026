"""Runner steps "discover", "dedup" and "pre_rank" (job-level).

discover -> candidates (both doors), dedup -> clusters, pre_rank -> one account per
shortlisted cluster. Creating those accounts is what fans the job out to the per-account
steps (identity, registry, news, ...). Each step reads the previous one's stored output.
"""

from __future__ import annotations

from typing import Any

from app import db
from app.config import Settings
from app.discovery.dedup import dedup
from app.discovery.doors import Deps, discover
from app.discovery.models import Candidate, Cluster
from app.discovery.pre_rank import Ranked, pre_rank
from app.revenue.common import run_date, run_spec
from app.runner.context import StepContext

_OUTPUT = (
    "SELECT output FROM steps WHERE job_id = %s AND step = %s AND account_id IS NULL "
    "AND status = 'done'"
)
_SPENT = "SELECT coalesce(sum(inr), 0)::float8 AS inr FROM cost_ledger WHERE job_id = %s"


def prior_output(job_id: Any, step: str) -> dict[str, Any]:
    with db.tx() as conn:
        row = conn.execute(_OUTPUT, (job_id, step)).fetchone()
    if row is None or row["output"] is None:
        raise RuntimeError(f"step {step!r} has no stored output for job {job_id}")
    return row["output"]


def default_deps() -> Deps:
    return Deps()


async def run_discover(ctx: StepContext) -> dict[str, Any]:
    found = await discover(ctx.job_id, run_spec(ctx.job), default_deps(), ctx.config)
    return {"candidates": [c.model_dump(mode="json") for c in found]}


async def run_dedup(ctx: StepContext) -> dict[str, Any]:
    raw = prior_output(ctx.job_id, "discover")["candidates"]
    clusters = dedup([Candidate.model_validate(c) for c in raw], ctx.config)
    return {"clusters": [c.model_dump(mode="json") for c in clusters]}


def budget_credits(job: dict[str, Any], s: Settings) -> float:
    """Credits left for this job: (its INR limit - INR already spent) / INR per credit."""
    limit = (job.get("limits") or {}).get("max_inr_per_job", s.limits.max_inr_per_job)
    with db.tx() as conn:
        spent = conn.execute(_SPENT, (job["id"],)).fetchone()["inr"]
    return max(0.0, (float(limit) - spent) / s.limits.credit_inr)


def account_data(c: Cluster, r: Ranked) -> dict[str, Any]:
    """What the per-account steps read from accounts.data."""
    return {
        "name": c.name,
        "domain": c.domain,
        "crustdata_id": c.crustdata_id,
        "crustdata_ids": c.crustdata_ids,
        "state": c.first("state"),
        "city": c.first("city"),
        "headcount": c.first("headcount"),
        "industries": sorted({i for m in c.members for i in m.industries}),
        "needs_identity": c.needs_identity,
        "signals": [s.model_dump(mode="json") for s in c.signals],
        "sources": [s.model_dump(mode="json") for m in c.members for s in m.sources],
        "cluster_id": c.id,
        "members": len(c.members),
        "pre_rank_score": r.score,
        "pre_rank": r.model_dump(mode="json"),
    }


async def run_pre_rank(ctx: StepContext) -> dict[str, Any]:
    raw = prior_output(ctx.job_id, "dedup")["clusters"]
    clusters = {c["id"]: Cluster.model_validate(c) for c in raw}
    result = pre_rank(
        list(clusters.values()),
        run_spec(ctx.job),
        today=run_date(ctx.job),
        budget_credits=budget_credits(ctx.job, ctx.config),
        settings=ctx.config,
    )
    for r in result.shortlist:
        c = clusters[r.cluster_id]
        ctx.add_account(c.name, c.domain, data=account_data(c, r))
    return {**result.model_dump(mode="json"), "accounts_created": len(result.shortlist)}
