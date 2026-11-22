"""Turn a request (text or kickoff JSON) into a job and start it.

Local (api.start_mode = local): insert the job and its first step; the worker picks it up.
Cloud (cloud_run): the same insert, then run the Cloud Run Job through the gateway with
RUN_JOB_ID=<job id>, so one execution works that job until it is done.
"""

from __future__ import annotations

import json
from typing import Any

from app import db, gateway
from app.config import Settings
from app.runner.jobs import create_job
from app.runner.registry import Registry
from app.runner.worker import RUN_JOB_ENV

ENDPOINT_RUN = "cloudrun:run"


class RequestInvalid(ValueError):  # noqa: N818 - shown to the requester as-is
    pass


def spec_from(text: str | None, kickoff: str | bytes | dict | None, max_kb: int) -> dict:
    """The job spec: {"request": text} for free text, the kickoff JSON object as given."""
    if kickoff:
        if isinstance(kickoff, bytes | str):
            if len(kickoff) > max_kb * 1024:
                raise RequestInvalid(f"kickoff JSON is larger than {max_kb} KB")
            try:
                kickoff = json.loads(kickoff)
            except ValueError as exc:
                raise RequestInvalid(f"kickoff file is not valid JSON: {exc}") from exc
        if not isinstance(kickoff, dict) or not kickoff:
            raise RequestInvalid("kickoff JSON must be a non-empty object")
        return kickoff  # the parse step maps known kickoff keys and parses the rest
    if text and text.strip():
        return {"request": text.strip()}
    raise RequestInvalid("type a request or upload a kickoff JSON file")


def form_limits(budget_inr: float | None, max_minutes: float | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if budget_inr:
        out["max_inr_per_job"] = float(budget_inr)
    if max_minutes:
        out["max_minutes_per_job"] = float(max_minutes)
    return out


def insert_job(settings: Settings, registry: Registry, spec: dict, limits: dict) -> Any:
    client_id = str(spec.get("client_id") or settings.api.default_client_id)
    with db.tx() as conn:
        return create_job(conn, settings, registry, spec, client_id, limits)


def run_request(settings: Settings, job_id: Any) -> dict[str, Any]:
    """The Cloud Run Jobs :run call for this job (projects.locations.jobs.run)."""
    project = settings.secrets.gcp_project
    if not project:
        raise RuntimeError("GCP_PROJECT is not set (needed for api.start_mode = cloud_run)")
    c = settings.cloudrun
    url = f"{c.base_url}/projects/{project}/locations/{c.region}/jobs/{c.job}:run"
    body = {"overrides": {"containerOverrides": [
        {"env": [{"name": RUN_JOB_ENV, "value": str(job_id)}]}]}}  # fmt: skip
    return {"url": url, "json": body}


async def trigger_cloud_run(settings: Settings, job_id: Any) -> int:
    req = run_request(settings, job_id)
    resp = await gateway.call(job_id=job_id, endpoint=ENDPOINT_RUN, method="POST", url=req["url"],
                              json=req["json"], data_type="control", est_credits=0)  # fmt: skip
    return resp.status


async def start(settings: Settings, registry: Registry, spec: dict, limits: dict) -> Any:
    job_id = insert_job(settings, registry, spec, limits)
    if settings.api.start_mode == "cloud_run":
        await trigger_cloud_run(settings, job_id)
    return job_id
