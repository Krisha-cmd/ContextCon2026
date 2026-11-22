"""app/outreach: drafting and the learning loop. The LLM and embedder are faked; the learning
tests use the compose Postgres (pgvector) inside a rolled-back transaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from app import db
from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.outreach import learning, store
from app.outreach.draft import (
    INSERT_ACTION,
    INSERT_APPROVAL,
    DraftDeps,
    action_key,
    draft_text,
    run_draft,
)
from app.outreach.learning import LearningDeps, ProposedRule, validate
from app.runner.context import HELD, StepContext

S = load_settings(DEFAULT_CONFIG_PATH)
NO_AUTO_REFLECT = S.model_copy(
    update={"outreach": S.outreach.model_copy(update={"reflect_every": 10_000})}
)
DIMS = S.vertex.embedding_dims


# ---- fakes --------------------------------------------------------------------------------
class FakeLLM:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers, self.prompts = list(answers), []

    async def generate_json(self, prompt: str, schema: Any, *, job_id: Any, endpoint: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.answers.pop(0) if len(self.answers) > 1 else self.answers[0])


class FakeEmbedder:
    async def embed(self, text: str, *, job_id: Any, endpoint: str) -> list[float]:
        seed = hashlib.sha256(text.encode()).digest()
        return [((seed[i % len(seed)] / 255.0) - 0.5) for i in range(DIMS)]


def _src(url: str) -> dict[str, Any]:
    return {
        "url": url,
        "source_name": "x",
        "field_path": "f",
        "evidence_id": f"ev_{url[-1]}",
        "retrieved_at": "2026-11-21T00:00:00+00:00",
    }


def _account() -> dict[str, Any]:
    trigger = {
        "type": "new_warehouse",
        "title": "Acme opens new warehouse in Pune",
        "record_date": "2026-11-16",
        "age_days": 6,
        "strength": 0.65,
    }
    buyer = {
        "name": "Priya Raman",
        "title": "Head of Warehousing",
        "company": "Acme Foods",
        "profile_url": "https://p/1",
        "confidence": 0.9,
        "stale": False,
    }
    return {
        "id": "acc-1",
        "name": "Acme Foods",
        "domain": "acme.in",
        "data": {
            "industries": ["Consumer Goods"],
            "trigger": {"status": "verified", "value": trigger, "sources": [_src("https://n/1")]},
            "buyer": {"status": "verified", "value": buyer, "sources": [_src("https://p/1")]},
            "score_reasons": [
                "+20 new warehouse: Acme opens new warehouse in Pune, 6 days ago "
                "(source: https://n/1)",
                "+0 no verified buyer",
            ],
        },
    }


GOOD = {
    "subject": "Your new Pune warehouse",
    "body": "Hi Priya, I saw that Acme opens a new warehouse in Pune. We run warehousing "
    "and fulfilment across India and could help you ramp up. Open to a short call?",
    "trigger_sentence": "I saw that Acme opens a new warehouse in Pune.",
    "facts_used": ["trigger", "buyer", "company"],
}


def _draft_deps(
    llm: FakeLLM, rules: list[dict] | None = None, examples: list[dict] | None = None
) -> DraftDeps:
    async def get_llm() -> FakeLLM:
        return llm

    async def get_embedder() -> FakeEmbedder:
        return FakeEmbedder()

    return DraftDeps(
        get_llm=get_llm,
        get_embedder=get_embedder,
        rules=lambda scopes: rules or [],
        examples=lambda emb, k: examples or [],
    )


def _ctx(account: dict | None = None) -> StepContext:
    return StepContext(
        job={"id": "job-1", "spec": {}},
        account=account or _account(),
        config=S,
        gateway=None,
        db=None,
    )


# ---- draft step ---------------------------------------------------------------------------
async def test_draft_writes_pending_action_and_approval() -> None:
    ctx = _ctx()
    out = await run_draft(ctx, _draft_deps(FakeLLM(GOOD)))
    key = hashlib.sha256(b"job-1:acc-1:send_email").hexdigest()
    assert out["drafted"] and out["action_key"] == key == action_key("job-1", "acc-1")
    action, approval = ctx.writes
    assert action.sql == INSERT_ACTION and approval.sql == INSERT_APPROVAL
    payload = action.params[4].obj
    assert action.params[3] == "send_email" and action.params[5] == "pending"
    assert payload["to"] == {
        "name": "Priya Raman",
        "title": "Head of Warehousing",
        "company": "Acme Foods",
        "profile_url": "https://p/1",
    }
    assert payload["embed_text"] == "Consumer Goods | new_warehouse | Head of Warehousing"
    assert approval.params[1] == "pending"
    assert approval.params[2] == draft_text(GOOD["subject"], GOOD["body"])
    assert len(ctx.audit.entries) == len(ctx.writes) and ctx.dispositions == []


async def test_drafts_include_active_rules_in_the_prompt() -> None:
    rules = [
        {"id": 7, "version": 2, "scope": "global", "rule": "Remove pricing from first emails"},
        {
            "id": 9,
            "version": 1,
            "scope": "approver:client-approver",
            "rule": "Keep it under 60 words",
        },
    ]
    llm = FakeLLM(GOOD)
    out = await run_draft(_ctx(), _draft_deps(llm, rules=rules))
    assert "- Remove pricing from first emails" in llm.prompts[0]
    assert "- Keep it under 60 words" in llm.prompts[0]
    assert "Body under 60 words" in llm.prompts[0]  # a rule's word limit replaces the default
    assert out["rules"] == [{"id": 7, "version": 2}, {"id": 9, "version": 1}]


async def test_examples_are_in_the_prompt_but_their_names_may_not_leak() -> None:
    example = {
        "id": 3,
        "draft": "d",
        "final": "Hi Rahul, congrats on the Nagpur hub.",
        "meta": {"buyer_name": "Rahul Mehta", "company": "Zeta Retail"},
    }
    leaky = {**GOOD, "body": GOOD["body"] + " Rahul at Zeta Retail liked it."}
    llm = FakeLLM(leaky, GOOD)
    out = await run_draft(_ctx(), _draft_deps(llm, examples=[example]))
    assert "Hi Rahul, congrats on the Nagpur hub." in llm.prompts[0]
    assert "names from other prospects' emails" in llm.prompts[1]  # the repair prompt says why
    assert out["drafted"] and out["example_ids"] == [3]


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ({"body": GOOD["body"] + " We save 40% on costs."}, "numbers not in the facts"),
        ({"trigger_sentence": "We noticed your expansion."}, "trigger_sentence"),
        ({"body": GOOD["body"] + " Call me on +91 98765 43210."}, "phone number"),
        ({"facts_used": ["trigger", "funding"]}, "not in the fact list"),
        ({"body": "word " * 151 + GOOD["trigger_sentence"]}, "words; the limit is 150"),
    ],
)
async def test_failed_checks_retry_then_hold(bad: dict[str, Any], why: str) -> None:
    ctx = _ctx()
    llm = FakeLLM({**GOOD, **bad})
    out = await run_draft(ctx, _draft_deps(llm))
    assert not out["drafted"] and len(llm.prompts) == S.outreach.draft_attempts
    [d] = ctx.dispositions
    assert d.status == HELD and why in d.reason and ctx.writes == []


async def test_no_verified_buyer_holds_without_calling_the_llm() -> None:
    account = _account()
    account["data"]["buyer"] = {"status": "cannot_verify", "value": None, "missing": "x"}
    llm = FakeLLM(GOOD)
    ctx = _ctx(account)
    await run_draft(ctx, _draft_deps(llm))
    assert llm.prompts == [] and ctx.dispositions[0].status == HELD


# ---- rule validation (no DB) --------------------------------------------------------------
DIFFS = {
    1: {"approver": "asha", "segment": "consumer goods"},
    2: {"approver": "asha", "segment": "pharma"},
}
NAMES = {"Priya Raman", "Priya", "Raman", "Acme Foods", "Acme"}


def test_rule_with_a_persons_name_is_rejected() -> None:
    p = ProposedRule(topic="greeting", rule="Address Priya by first name", support=[1, 2])
    scope, why = validate(p, DIFFS, NAMES, S)
    assert scope is None and "names a prospect" in why and "Priya" in why


def test_rule_needs_two_supporting_diffs_and_no_personal_data() -> None:
    one = ProposedRule(topic="pricing", rule="Remove pricing", support=[1, 99])
    assert validate(one, DIFFS, NAMES, S) == (None, "support 1 < 2 diffs")
    mail = ProposedRule(topic="cta", rule="Ask them to mail sales@client.in", support=[1, 2])
    assert "email address" in validate(mail, DIFFS, NAMES, S)[1]


def test_rule_scope_follows_its_support() -> None:
    p = ProposedRule(topic="t", rule="Be brief", scope="approver", support=[1, 2])
    assert validate(p, DIFFS, NAMES, S) == ("approver:asha", None)
    p = ProposedRule(topic="t", rule="Be brief", scope="segment", support=[1, 2])
    assert validate(p, DIFFS, NAMES, S) == ("global", None)  # two segments: not segment-only


# ---- learning loop (Postgres) -------------------------------------------------------------
@pytest.fixture
def conn(pg_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    db.apply_schema(pg_conn, S.db.schema_lock_key)
    with pg_conn.transaction(force_rollback=True):
        yield pg_conn


def _job(c: psycopg.Connection) -> Any:
    return c.execute(
        "INSERT INTO jobs (client_id, spec, status, limits) "
        "VALUES ('c', '{}', 'running', '{}') RETURNING id"
    ).fetchone()["id"]


def _pending(c: psycopg.Connection, job: Any, n: int, body: str = GOOD["body"]) -> str:
    """A drafted action + pending approval, as the draft step writes them."""
    from psycopg.types.json import Jsonb

    key = action_key(job, f"acc-{n}")
    payload = {
        "subject": "Your new Pune warehouse",
        "body": body,
        "segment": "consumer goods",
        "embed_text": f"Consumer Goods | new_warehouse | Head of Warehousing {n}",
        "meta": {"buyer_name": "Priya Raman", "company": "Acme Foods"},
    }
    c.execute(INSERT_ACTION, (key, job, None, "send_email", Jsonb(payload), "pending"))
    c.execute(
        INSERT_APPROVAL,
        (key, "pending", draft_text(payload["subject"], body), "client-approver", key),
    )
    return key


def _deps(llm: FakeLLM | None = None) -> LearningDeps:
    async def get_llm() -> FakeLLM:
        assert llm is not None
        return llm

    async def get_embedder() -> FakeEmbedder:
        return FakeEmbedder()

    return LearningDeps(get_llm=get_llm, get_embedder=get_embedder)


PRICED = GOOD["body"] + " Our rates start at Rs 18 per order."


def _edited(subject: str = "Your new Pune warehouse") -> str:
    return draft_text(subject, GOOD["body"])  # the person removed the pricing sentence


async def test_approving_an_edited_draft_creates_an_example(conn: psycopg.Connection) -> None:
    key = _pending(conn, _job(conn), 1, PRICED)
    out = await learning.record_feedback(
        key,
        "approved",
        _edited(),
        "no pricing yet",
        by_user="asha",
        conn=conn,
        deps=_deps(),
        settings=NO_AUTO_REFLECT,
    )
    assert out["recorded"] and out["edited"]
    ex = conn.execute("SELECT * FROM examples WHERE id = %s", (out["example_id"],)).fetchone()
    assert ex["decision"] == "approved" and ex["final"] == _edited()
    assert "-" in ex["diff"] and "Rs 18 per order" in ex["diff"]
    assert ex["embedding"] is not None and ex["approver"] == "asha"
    state = conn.execute("SELECT state FROM actions WHERE idem_key = %s", (key,)).fetchone()
    assert state["state"] == "approved"
    appr = conn.execute(
        "SELECT decision, final_text FROM approvals WHERE action_key = %s", (key,)
    ).fetchone()
    assert appr == {"decision": "approved", "final_text": _edited()}
    audits = conn.execute(
        "SELECT tbl FROM audit_log WHERE step = 'learning' AND record_id IN (%s, %s, %s)",
        (key, str(ex["id"]), "x"),
    ).fetchall()
    assert {a["tbl"] for a in audits} >= {"actions", "examples"}
    again = await learning.record_feedback(
        key,
        "approved",
        None,
        None,
        by_user="asha",
        conn=conn,
        deps=_deps(),
        settings=NO_AUTO_REFLECT,
    )
    assert again["recorded"] is False  # a decision is recorded once
    # The next draft can retrieve it: nearest approved example by pgvector cosine.
    emb = await FakeEmbedder().embed(
        "Consumer Goods | new_warehouse | Head of Warehousing 1", job_id=None, endpoint="t"
    )
    [near] = store.nearest_examples(conn, emb, 3, "approved")
    assert near["id"] == out["example_id"] and near["distance"] == pytest.approx(0, abs=1e-6)


async def _two_edits(conn: psycopg.Connection) -> list[int]:
    job = _job(conn)
    ids = []
    for n in (1, 2):
        key = _pending(conn, job, n, PRICED)
        out = await learning.record_feedback(
            key,
            "approved",
            _edited(),
            "no pricing",
            by_user="asha",
            conn=conn,
            deps=_deps(),
            settings=NO_AUTO_REFLECT,
        )
        ids.append(out["example_id"])
    return ids


async def test_two_similar_edits_create_a_rule(conn: psycopg.Connection) -> None:
    ids = await _two_edits(conn)
    llm = FakeLLM(
        {
            "rules": [
                {
                    "topic": "pricing_first_email",
                    "rule": "Remove pricing from first emails",
                    "support": ids,
                }
            ]
        }
    )
    out = await learning.reflect(conn=conn, deps=_deps(llm), settings=S)
    [rule] = out["accepted"]
    assert rule["support"] == 2 and rule["version"] == 1 and rule["scope"] == "global"
    assert f"[{ids[0]}]" in llm.prompts[0] and "Rs 18 per order" in llm.prompts[0]
    [row] = learning.preferences(conn)
    assert row["status"] == "active" and row["source_diffs"] == ids
    # The next draft sees it.
    active = store.active_preferences(conn, ["global"])
    assert [r["rule"] for r in active] == ["Remove pricing from first emails"]


async def test_reflect_rejects_a_rule_naming_a_prospect(conn: psycopg.Connection) -> None:
    ids = await _two_edits(conn)
    llm = FakeLLM(
        {"rules": [{"topic": "greeting", "rule": "Greet Priya Raman warmly", "support": ids}]}
    )
    out = await learning.reflect(conn=conn, deps=_deps(llm), settings=S)
    assert out["accepted"] == [] and "names a prospect" in out["rejected"][0]["reason"]
    assert learning.preferences(conn) == []


async def test_versions_conflicts_rollback_and_disable(conn: psycopg.Connection) -> None:
    ids = await _two_edits(conn)

    async def propose(*rules: dict[str, Any]) -> dict[str, Any]:
        return await learning.reflect(
            conn=conn, deps=_deps(FakeLLM({"rules": list(rules)})), settings=S
        )

    v1 = await propose({"topic": "pricing", "rule": "Remove pricing", "support": ids})
    v1_id = v1["accepted"][0]["id"]
    same = await propose({"topic": "pricing", "rule": "Remove pricing", "support": ids})
    assert same["accepted"] == [] and "no new support" in same["rejected"][0]["reason"]
    v2 = await propose(
        {"topic": "pricing", "rule": "Mention pricing only if asked", "support": ids}
    )
    v2_id = v2["accepted"][0]["id"]
    assert v2["accepted"][0]["version"] == 2
    other = await propose(
        {"topic": "length", "rule": "Keep it under 80 words", "support": ids, "replaces": v2_id}
    )
    status = {r["id"]: r["status"] for r in learning.preferences(conn)}
    assert status[v1_id] == "superseded" and status[v2_id] == "superseded"  # newer rule wins
    length_id = other["accepted"][0]["id"]
    with pytest.raises(learning.FeedbackError, match="no earlier version"):
        learning.rollback_rule(length_id, "asha", conn)
    learning.disable_rule(length_id, "asha", conn)
    again = await propose({"topic": "length", "rule": "Keep it under 70 words", "support": ids})
    assert "disabled by a person" in again["rejected"][0]["reason"]
    v3 = await propose({"topic": "pricing", "rule": "Never quote prices", "support": ids})
    v3_id = v3["accepted"][0]["id"]
    restored = learning.rollback_rule(v3_id, "asha", conn)
    assert restored["id"] == v2_id and restored["status"] == "active"


async def test_reflect_runs_after_every_n_decisions(conn: psycopg.Connection) -> None:
    every2 = S.model_copy(update={"outreach": S.outreach.model_copy(update={"reflect_every": 2})})
    job = _job(conn)
    base = store.count_examples(conn)
    llm = FakeLLM({"rules": []})
    outs = []
    for n in range(2 - base % 2):
        key = _pending(conn, job, n, PRICED)
        outs.append(
            await learning.record_feedback(
                key,
                "approved",
                _edited(),
                None,
                by_user="asha",
                conn=conn,
                deps=_deps(llm),
                settings=every2,
            )
        )
    assert "reflect" in outs[-1]


async def test_metric_batches_mean_normalized_distance(conn: psycopg.Connection) -> None:
    job = _job(conn)
    for n in range(6):
        key = _pending(conn, job, n, PRICED)
        final = draft_text("Your new Pune warehouse", PRICED) if n % 2 else _edited()
        await learning.record_feedback(
            key,
            "approved",
            final,
            None,
            by_user="asha",
            conn=conn,
            deps=_deps(),
            settings=NO_AUTO_REFLECT,
        )
    points = learning.metric(conn, S)
    assert [p["n"] for p in points] == [5, 1] and not points[1]["complete"]
    assert all(0 <= p["mean_distance"] <= 1 for p in points)
    assert points[0]["complete"] and points[0]["mean_distance"] > 0
