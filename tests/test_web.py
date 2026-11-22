"""The web UI / API through TestClient against the compose Postgres. The LLM and embedder are
faked; rows each test creates are deleted afterwards."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.api import main
from app.config import get_settings
from app.outreach.learning import LearningDeps
from tests.outreach_helpers import SUBJECT, queue_draft
from tests.test_outreach import GOOD, FakeEmbedder, FakeLLM

TOKEN = "test-token"
PRICED = GOOD["body"] + " Our rates start at Rs 18 per order."


@pytest.fixture
def raw(db_url: str) -> Iterator[psycopg.Connection]:
    try:
        conn = psycopg.connect(db_url, connect_timeout=3, row_factory=dict_row, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"postgres not reachable at {db_url}: {exc}")
    with conn:
        yield conn


@pytest.fixture
def client(raw: psycopg.Connection, db_url: str,
           monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:  # fmt: skip
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.delenv("REGISTRY_PROVIDER", raising=False)
    get_settings.cache_clear()

    async def llm() -> FakeLLM:
        return FakeLLM({"rules": []})

    async def embedder() -> FakeEmbedder:
        return FakeEmbedder()

    monkeypatch.setattr(main, "learning_deps", lambda: LearningDeps(llm, embedder))
    with TestClient(main.app, headers={"x-api-token": TOKEN}) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def made(raw: psycopg.Connection) -> Iterator[list[Any]]:
    """Job ids created by a test; their rows are removed afterwards."""
    jobs: list[Any] = []
    yield jobs
    for job in jobs:
        keys = [r["idem_key"] for r in raw.execute(
            "SELECT idem_key FROM actions WHERE job_id = %s", (job,)).fetchall()]  # fmt: skip
        raw.execute("DELETE FROM examples WHERE action_key = ANY(%s)", (keys,))
        raw.execute("DELETE FROM approvals WHERE action_key = ANY(%s)", (keys,))
        for tbl in ("dead_letter", "drafts", "actions", "steps", "accounts", "cost_ledger"):
            col = "step_id IN (SELECT id FROM steps WHERE job_id = %s)" if tbl == "dead_letter" \
                else "job_id = %s"  # fmt: skip
            raw.execute(f"DELETE FROM {tbl} WHERE {col}", (job,))
        raw.execute("DELETE FROM jobs WHERE id = %s", (job,))


def _job(raw: psycopg.Connection, made: list[Any]) -> Any:
    job = raw.execute(
        "INSERT INTO jobs (client_id, spec, status, limits) VALUES ('t', '{}', 'done', %s) "
        "RETURNING id", (Jsonb({"max_inr_per_job": 500}),)).fetchone()["id"]  # fmt: skip
    made.append(job)
    return job


def _src(url: str, **kw: Any) -> dict[str, Any]:
    return {"url": url, "source_name": "crustdata.web_search", "field_path": "results[0]",
            "quote": None, "record_date": "2026-11-16", "retrieved_at": "2026-11-21T10:00:00Z",
            "evidence_id": "ev_t1", **kw}  # fmt: skip


def _account(raw: psycopg.Connection, job: Any, name: str, status: str = "active",
             reason: str | None = None, data: dict | None = None) -> Any:  # fmt: skip
    return raw.execute(
        "INSERT INTO accounts (job_id, name, domain, data, status, status_reason, status_step) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (job, name, f"{name.lower().split()[0]}.in", Jsonb(data or {}), status, reason,
         "news" if status != "active" else None),
    ).fetchone()["id"]  # fmt: skip


def _decide(client: TestClient, key: str, **form: str) -> Any:
    return client.post(f"/approvals/{key}", data=form, headers={"HX-Request": "true"})


# ---- auth and job creation ----------------------------------------------------------------
def test_shared_token_is_required(client: TestClient) -> None:
    bare = TestClient(main.app)
    assert bare.get("/approvals").status_code == 401
    assert bare.get("/health").status_code == 200
    r = bare.get(f"/approvals?token={TOKEN}")
    assert r.status_code == 200 and r.cookies.get("revagent_token") == TOKEN


def test_create_job_from_text_inserts_the_first_step(client: TestClient, raw: psycopg.Connection,
                                                     made: list[Any]) -> None:  # fmt: skip
    r = client.post("/jobs", json={"text": "Find D2C brands in Pune", "budget_inr": 100})
    assert r.status_code == 201
    job_id = r.json()["job_id"]
    made.append(job_id)
    job = raw.execute("SELECT spec, limits, status FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert job["spec"] == {"request": "Find D2C brands in Pune"} and job["status"] == "running"
    assert job["limits"]["max_inr_per_job"] == 100  # tightened, never raised
    [step] = raw.execute("SELECT step, status FROM steps WHERE job_id = %s", (job_id,)).fetchall()
    assert step == {"step": "parse", "status": "pending"}
    page = client.get(f"/jobs/{job_id}/progress")
    assert page.status_code == 200 and "2 · Live progress" in page.text
    partial = client.get(f"/jobs/{job_id}/progress", headers={"HX-Request": "true"})
    assert 'hx-trigger="every 2s"' in partial.text and "<html" not in partial.text


def test_create_job_from_kickoff_upload_and_form_redirect(client: TestClient,
                                                          raw: psycopg.Connection,
                                                          made: list[Any]) -> None:  # fmt: skip
    kickoff = {"sectors": ["D2C"], "states": ["Maharashtra"], "client_id": "shipco"}
    r = client.post("/jobs", files={"kickoff": ("k.json", json.dumps(kickoff), "application/json")},
                    data={"budget_inr": "99999"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and r.headers["location"].endswith("/progress")
    job_id = r.headers["location"].split("/")[2]
    made.append(job_id)
    job = raw.execute("SELECT client_id, spec, limits FROM jobs WHERE id = %s",
                      (job_id,)).fetchone()  # fmt: skip
    assert job["client_id"] == "shipco" and job["spec"] == kickoff
    assert job["limits"]["max_inr_per_job"] == get_settings().limits.max_inr_per_job
    bad = client.post("/jobs", data={"text": "  "})
    assert bad.status_code == 200 and "type a request" in bad.text


# ---- approvals ----------------------------------------------------------------------------
def test_approve_an_edited_draft(client: TestClient, raw: psycopg.Connection,
                                 made: list[Any]) -> None:  # fmt: skip
    job = _job(raw, made)
    acc = _account(raw, job, "Acme Foods")
    key = queue_draft(raw, job, 1, PRICED, account_id=acc)
    queue = client.get("/approvals")
    assert queue.status_code == 200 and SUBJECT in queue.text and "Why this account" in queue.text
    assert "Acme opens new warehouse in Pune" in queue.text  # the facts it relies on
    no_to = _decide(client, key, action="approve", subject=SUBJECT, body=GOOD["body"])
    assert "business email" in no_to.text
    r = _decide(client, key, action="approve", to="priya@acme.example", subject=SUBJECT,
                body=GOOD["body"], by_user="asha")  # fmt: skip
    assert r.status_code == 200 and "Approved" in r.text and "with your edits" in r.text
    action = raw.execute("SELECT state, payload FROM actions WHERE idem_key = %s",
                         (key,)).fetchone()  # fmt: skip
    assert action["state"] == "approved"
    assert action["payload"] == {"to": ["priya@acme.example"], "subject": SUBJECT,
                                 "body": GOOD["body"]}  # fmt: skip
    appr = raw.execute("SELECT decision, by_user, payload_hash FROM approvals "
                       "WHERE action_key = %s", (key,)).fetchone()  # fmt: skip
    assert appr["decision"] == "approved" and appr["by_user"] == "asha" and appr["payload_hash"]
    ex = raw.execute("SELECT decision, diff FROM examples WHERE action_key = %s",
                     (key,)).fetchone()  # fmt: skip
    assert ex["decision"] == "approved" and "Rs 18 per order" in ex["diff"]
    again = client.post(f"/approvals/{key}", json={"action": "approve", "to": "a@b.example"})
    assert again.status_code == 409
    assert SUBJECT not in client.get("/approvals").text  # no longer pending


def test_reject_with_a_reason_stores_feedback(client: TestClient, raw: psycopg.Connection,
                                              made: list[Any]) -> None:  # fmt: skip
    job = _job(raw, made)
    acc = _account(raw, job, "Zeta Retail")
    key = queue_draft(raw, job, 2, PRICED, account_id=acc)
    no_reason = client.post(f"/approvals/{key}", json={"action": "reject"})
    assert no_reason.status_code == 422 and "reason" in no_reason.json()["detail"]
    r = client.post(f"/approvals/{key}", json={"action": "reject", "reason": "wrong buyer",
                                               "by_user": "asha"})  # fmt: skip
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    assert r.json()["feedback"]["recorded"] is True
    action = raw.execute("SELECT state FROM actions WHERE idem_key = %s", (key,)).fetchone()
    assert action["state"] == "rejected"
    ex = raw.execute("SELECT decision, reason, final FROM examples WHERE action_key = %s",
                     (key,)).fetchone()  # fmt: skip
    assert ex == {"decision": "rejected", "reason": "wrong buyer", "final": None}
    appr = raw.execute("SELECT reason FROM approvals WHERE action_key = %s", (key,)).fetchone()
    assert appr["reason"] == "wrong buyer"


# ---- screens 3, 4, 6, 7 -------------------------------------------------------------------
def _facts() -> dict[str, Any]:
    trigger = {"type": "new_warehouse", "title": "Acme opens new warehouse in Pune",
               "record_date": "2026-11-16", "age_days": 6, "strength": 0.65}  # fmt: skip
    return {
        "score": 48, "score_reasons": ["+20 new warehouse: Acme opens new warehouse in Pune, "
                                       "6 days ago (source: https://n/1)", "+0 no verified buyer",
                                       "+10 registered and active (source: registry.gst)"],
        "trigger": {"status": "verified", "value": trigger,
                    "sources": [_src("https://n/1", quote="opens new warehouse in Pune")]},
        "buyer": {"status": "cannot_verify", "value": None, "sources": [],
                  "missing": "no buyer matching the target titles"},
        "status_active": {"status": "verified", "value": True,
                          "sources": [_src("https://www.gstinapi.in/v1/gstin/27AAPCA1234B1Z5",
                                           source_name="registry.gst", field_path="gst.status")]},
    }  # fmt: skip


def test_account_table_and_evidence_drawer(client: TestClient, raw: psycopg.Connection,
                                           made: list[Any]) -> None:  # fmt: skip
    job = _job(raw, made)
    held = _account(raw, job, "Acme Foods", "held", "buyer: no buyer matching the target titles",
                    _facts())  # fmt: skip
    _account(raw, job, "Omega Cargo", "dropped", "registry: CIN is Strike Off in MCA")
    page = client.get(f"/jobs/{job}")
    assert page.status_code == 200
    assert 'class="d-held"' in page.text and "⚠ held" in page.text
    assert "buyer: no buyer matching the target titles" in page.text
    assert "registry: active" in page.text and "news: not checked" in page.text
    data = client.get(f"/jobs/{job}", headers={"accept": "application/json"}).json()
    assert {a["name"]: a["decision"] for a in data["accounts"]} == {
        "Acme Foods": "held", "Omega Cargo": "dropped"}  # fmt: skip
    assert "ledger" in data and data["ledger"]["calls"] == 0
    drawer = client.get(f"/jobs/{job}/accounts/{held}", headers={"HX-Request": "true"})
    assert drawer.status_code == 200 and 'class="drawer on"' in drawer.text
    assert 'href="https://n/1"' in drawer.text and "opens new warehouse in Pune" in drawer.text
    assert (
        "field <code>gst.status</code>" in drawer.text and "record date 2026-11-16" in drawer.text
    )
    assert "Cannot verify: no buyer matching the target titles" in drawer.text
    assert "Adverse news" in drawer.text  # never checked: shown as a gap, not hidden
    full = client.get(f"/jobs/{job}/accounts/{held}", headers={"accept": "application/json"})
    body = full.json()
    assert body["facts"]["buyer"]["missing"] == "no buyer matching the target titles"
    assert "Adverse news" in body["unchecked"]
    assert client.get(f"/jobs/{job}/accounts/{held}").status_code == 200  # full page
    assert client.get(f"/jobs/{job}/accounts/not-a-uuid").status_code == 404


def test_outcome_output_log_and_preferences(client: TestClient, raw: psycopg.Connection,
                                            made: list[Any]) -> None:  # fmt: skip
    job = _job(raw, made)
    _account(raw, job, "Acme Foods", "active", None, _facts())
    raw.execute("INSERT INTO cost_ledger (job_id, endpoint, credits, inr, ms, cache_hit) "
                "VALUES (%s, 'crustdata:web_search', 1, 25, 900, false), "
                "(%s, 'gstinapi:gstin', 0, 0, 3, true)", (job, job))  # fmt: skip
    raw.execute("INSERT INTO audit_log (job_id, step, tbl, record_id, reason) VALUES "
                "(%s, 'news', 'accounts', 'x', 'news: verified'), "
                "(%s, 'trigger', 'accounts', 'x', 'trigger: verified')", (job, job))  # fmt: skip
    out = client.get(f"/jobs/{job}/output").json()
    [acct] = out["accounts"]
    assert acct["decision"] == "proceed" and acct["trigger"]["status"] == "verified"
    assert client.get(f"/jobs/{job}/outcome").status_code == 200
    log = client.get(f"/jobs/{job}/log?step=news", headers={"HX-Request": "true"})
    assert "news: verified" in log.text and "trigger: verified" not in log.text
    assert "crustdata:web_search" in log.text and "₹25.00" in log.text
    assert client.get(f"/jobs/{job}/log?endpoint=gstinapi").text.count("crustdata:web_search") \
        <= 1  # fmt: skip
    prefs = client.get("/preferences")
    assert prefs.status_code == 200 and "Learned rules" in prefs.text
    assert isinstance(client.get("/preferences?format=json").json(), list)
    assert client.get("/").status_code == 200
