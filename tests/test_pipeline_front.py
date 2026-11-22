"""The front of the pipeline through the real runner: parse -> discover -> dedup -> pre_rank
must create accounts, and the per-account steps must then run for each one. (A job used to
stop after pre_rank because these four steps were placeholders and made no accounts.)
Crustdata is replaced by its recorded fixture; nothing reaches the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.config import Settings
from app.discovery import steps as dsteps
from app.discovery.crustdata import Page
from app.discovery.doors import Deps
from app.runner.context import StepContext
from app.runner.jobs import create_job
from app.runner.registry import Registry
from app.spec.step import request_of, run_parse
from tests.discovery_helpers import FakeSave
from tests.test_runner import q, run_job, settings  # noqa: F401  (settings is a fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "crustdata" / "company_search.json"
RECORDS = json.loads(FIXTURE.read_text())["response"]["companies"]


def fake_deps() -> Deps:
    async def search(job_id, filters, cursor=None):
        return Page(RECORDS, None, "f" * 64)

    async def nothing(*_a, **_k):
        return Page([], None, "0" * 64)

    return Deps(search_companies=search, search_jobs=nothing, web_search=nothing, save=FakeSave())


def registry(seen: list[dict[str, Any]]) -> Registry:
    reg = Registry()
    reg.step("parse", per_account=False, order=10)(run_parse)
    reg.step("discover", per_account=False, order=20)(dsteps.run_discover)
    reg.step("dedup", per_account=False, order=30)(dsteps.run_dedup)
    reg.step("pre_rank", per_account=False, order=40)(dsteps.run_pre_rank)

    async def identity_probe(ctx: StepContext) -> dict[str, Any]:
        seen.append(ctx.account)
        return {"ok": True}

    reg.step("identity", per_account=True, order=50)(identity_probe)
    return reg


def test_request_of() -> None:
    assert request_of({"request": "FMCG in Gujarat", "client_id": "c"}) == "FMCG in Gujarat"
    assert request_of({"states": ["Goa"], "client_id": "c"}) == {"states": ["Goa"]}


def test_front_of_pipeline_creates_accounts_and_fans_out(
    settings: Settings,  # noqa: F811  (pytest fixture imported above)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dsteps, "default_deps", fake_deps)
    seen: list[dict[str, Any]] = []
    reg = registry(seen)
    with db.tx() as conn:  # kickoff JSON: parsed without the LLM
        job = create_job(
            conn, settings, reg, {"states": ["Tamil Nadu"], "max_accounts": 2}, "test-client"
        )
    run_job(settings, reg, job)

    [row] = q("SELECT status, spec FROM jobs WHERE id = %s", job)
    assert row["status"] == "done"
    assert row["spec"]["run_spec"]["geo_states"] == ["Tamil Nadu"]
    pre = q("SELECT output FROM steps WHERE job_id = %s AND step = 'pre_rank'", job)[0]["output"]
    accounts = q("SELECT id, name, data FROM accounts WHERE job_id = %s", job)
    assert pre["accounts_created"] == len(accounts) > 0
    # Door 1 stops at max_accounts * 3 candidates; pre_rank keeps at most pre_rank_top.
    assert len(accounts) <= min(
        2 * settings.discovery.search_candidate_multiplier, settings.shortlist.pre_rank_top
    )
    # The per-account step ran once for every account (this is what used to never happen).
    assert sorted(str(a["id"]) for a in seen) == sorted(str(a["id"]) for a in accounts)
    data = accounts[0]["data"]
    assert data["crustdata_ids"] and data["pre_rank_score"] is not None
    assert data["sources"][0]["evidence_id"]
    # The top-ranked account matches the requested state.
    best = max(accounts, key=lambda a: a["data"]["pre_rank_score"])
    assert "state_match" in best["data"]["pre_rank"]["reasons"]
