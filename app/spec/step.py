"""Runner step "parse" (job-level): the job's request -> RunSpec, stored in jobs.spec.run_spec,
where every later step reads it (app.revenue.common.run_spec)."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.runner.context import StepContext
from app.spec.parser import parse_request

RUN_SPEC_KEY = "run_spec"
NOT_REQUEST = ("client_id", RUN_SPEC_KEY)  # job.spec keys that are not part of the request
SET_RUN_SPEC = "UPDATE jobs SET spec = spec || %s WHERE id = %s"


def request_of(spec: dict[str, Any]) -> str | dict[str, Any]:
    """{"request": text} -> the text; a kickoff JSON object -> itself (minus job metadata)."""
    rest = {k: v for k, v in spec.items() if k not in NOT_REQUEST}
    if set(rest) == {"request"} and isinstance(rest["request"], str):
        return rest["request"]
    return rest


async def run_parse(ctx: StepContext) -> dict[str, Any]:
    spec = ctx.job.get("spec") or {}
    if spec.get(RUN_SPEC_KEY):  # a re-run after a crash: already parsed and committed
        return {RUN_SPEC_KEY: spec[RUN_SPEC_KEY], "reused": True}
    parsed = (await parse_request(request_of(spec), job_id=ctx.job_id)).model_dump(mode="json")
    patch = {RUN_SPEC_KEY: parsed}
    ctx.write(
        SET_RUN_SPEC,
        (Jsonb(patch), ctx.job_id),
        tbl="jobs",
        record_id=ctx.job_id,
        before={RUN_SPEC_KEY: None},
        after=patch,
        reason="request parsed into a RunSpec",
    )
    return patch
