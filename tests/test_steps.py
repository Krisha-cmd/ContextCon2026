from __future__ import annotations

from datetime import date

import psycopg

from app import db
from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.discovery.crustdata import Page
from app.discovery.doors import Deps
from app.runner.steps import STEPS, StepContext, evidence_ids, save_step_output
from app.spec.models import RunSpec
from tests.discovery_helpers import FakeSave

S = load_settings(DEFAULT_CONFIG_PATH)


def _rec(cid: int, name: str, domain: str) -> dict:
    return {"crustdata_company_id": cid, "headcount": {"total": 100},
            "basic_info": {"name": name, "primary_domain": domain},
            "locations": {"state": "Tamil Nādu", "city": "Chennai"}}  # fmt: skip


async def test_steps_chain_through_json() -> None:
    assert list(STEPS) == ["discover", "dedup", "pre_rank"]
    records = [_rec(583, "Beardsell Ltd.", "beardsell.co.in"),
               _rec(15542175, "BEARDSELL LIMITED", "beardsell.co.in"),
               _rec(9, "Other Co", "other.in")]  # fmt: skip

    async def search(job_id, filters, cursor=None):
        return Page(records, None, "f" * 64)

    async def nothing(*_a, **_k):
        return Page([], None, "0" * 64)

    deps = Deps(search_companies=search, search_jobs=nothing, web_search=nothing,
                save=FakeSave(), today=lambda: date(2026, 11, 22))  # fmt: skip
    ctx = StepContext(job_id="j", spec=RunSpec(geo_states=["Tamil Nadu"]), settings=S, deps=deps,
                      today=date(2026, 11, 22))  # fmt: skip
    outputs: dict = {}
    for name, step in STEPS.items():
        outputs[name] = await step(ctx, outputs)
    assert len(outputs["discover"]["candidates"]) == 3
    assert len(outputs["dedup"]["clusters"]) == 2  # Beardsell twins collapsed
    assert len(outputs["pre_rank"]["shortlist"]) == 2


def test_evidence_ids_walks_nested_output() -> None:
    out = {"a": [{"sources": [{"evidence_id": "ev_2"}, {"evidence_id": "ev_1"}]}],
           "b": {"evidence_id": "ev_2"}}  # fmt: skip
    assert evidence_ids(out) == ["ev_1", "ev_2"]


def test_save_step_output_upserts_and_audits(pg_conn: psycopg.Connection) -> None:
    db.apply_schema(pg_conn, S.db.schema_lock_key)
    with pg_conn.transaction(force_rollback=True):
        job = pg_conn.execute(
            "INSERT INTO jobs (client_id, spec, status, limits) "
            "VALUES ('c', '{}', 'new', '{}') RETURNING id"
        ).fetchone()["id"]
        first = save_step_output(pg_conn, job, "dedup", {"clusters": [], "x": {"evidence_id": "e"}})
        second = save_step_output(pg_conn, job, "dedup", {"clusters": [1]})
        assert first == second
        row = pg_conn.execute("SELECT status, output FROM steps WHERE id = %s", (first,)).fetchone()
        audits = pg_conn.execute(
            "SELECT before, evidence_ids FROM audit_log WHERE tbl = 'steps' AND record_id = %s "
            "ORDER BY id", (str(first),)
        ).fetchall()  # fmt: skip
    assert row == {"status": "done", "output": {"clusters": [1]}}
    assert audits[0]["before"] is None and audits[0]["evidence_ids"] == ["e"]
    assert audits[1]["before"]["output"]["x"] == {"evidence_id": "e"}
