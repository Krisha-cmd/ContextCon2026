"""The self-improving loop: every decision becomes an example; repeated edits become rules.

record_feedback(): called by the API when a person decides an approval (the API moves the
action through app.actions.outbox in the same request). Stores draft, final, a unified diff,
the embedding (Vertex text-embedding) and the decision in `examples`. The next draft retrieves these
examples, so every decision changes later drafts. After every outreach.reflect_every
decisions it runs reflect().
reflect(): Gemini reads recent diffs and proposes rules. Plain code keeps a rule only if at
least outreach.min_support of those diffs support it and it names no prospect and carries no
personal data. Rules are versioned per (topic, scope): a new version supersedes the old one, and
a rule that contradicts another (`replaces`) supersedes it too: the newer rule wins. A rule a
person disabled is never re-enabled by reflect().
metric(): mean normalized Levenshtein distance between draft and final per batch of decisions.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import Connection
from pydantic import BaseModel, ValidationError
from rapidfuzz.distance import Levenshtein

from app import db
from app.config import Settings, get_settings
from app.gateway import GatewayError
from app.gateway.vertex import Embedder, JsonLLM, get_vertex_embedder, get_vertex_llm
from app.log import get_logger
from app.outreach import store
from app.outreach.draft import EMBED_ENDPOINT
from app.outreach.privacy import names_from_meta, names_in, personal_data_in
from app.runner.audit import audit

log = get_logger(__name__)

STEP = "learning"
REFLECT_ENDPOINT = "vertex.reflect"
REFLECT_BY = "reflect"


class FeedbackError(ValueError):
    """The decision can't be recorded (unknown action, bad decision value)."""


# ---- plumbing -----------------------------------------------------------------------------
@contextmanager
def _tx(conn: Connection | None) -> Iterator[Connection]:
    if conn is None:
        with db.tx() as c:
            yield c
    else:
        with conn.transaction():
            yield conn


async def _llm() -> JsonLLM:
    return await get_vertex_llm()


async def _embedder() -> Embedder:
    return await get_vertex_embedder()


@dataclass
class LearningDeps:
    get_llm: Callable[[], Awaitable[JsonLLM]] = _llm
    get_embedder: Callable[[], Awaitable[Embedder]] = _embedder


def unified_diff(draft: str, final: str | None) -> str:
    if final is None or final == draft:
        return ""
    lines = difflib.unified_diff(
        draft.splitlines(), final.splitlines(), "draft", "final", lineterm=""
    )
    return "\n".join(lines)


# ---- record_feedback ----------------------------------------------------------------------
async def record_feedback(
    action_key: str,
    decision: str,
    final_text: str | None,
    reason: str | None,
    *,
    by_user: str,
    conn: Connection | None = None,
    deps: LearningDeps | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Store one decision as an example (draft, final, diff, embedding, decision).

    The action itself is moved to approved / rejected by app.actions.outbox (the API does both
    for one request); this function only learns from it. Recorded once per action.
    """
    s, deps = settings or get_settings(), deps or LearningDeps()
    if decision not in s.outreach.decisions:
        raise FeedbackError(f"decision must be one of {list(s.outreach.decisions)}")
    draft = await asyncio.to_thread(_read_draft, conn, action_key)
    ctx = draft["context"] or {}
    try:
        embedding = await (await deps.get_embedder()).embed(
            ctx.get("embed_text", ""), job_id=draft["job_id"], endpoint=EMBED_ENDPOINT
        )
    except GatewayError as exc:  # the example is still kept; it just can't be retrieved
        log.warning("feedback embedding failed", extra={"error": type(exc).__name__})
        embedding = None
    out = await asyncio.to_thread(
        _record, conn, draft, decision, final_text, reason, by_user, embedding
    )
    if out["recorded"] and out["count"] % s.outreach.reflect_every == 0:
        out["reflect"] = await reflect(conn=conn, deps=deps, settings=s)
    return out


def _read_draft(conn: Connection | None, key: str) -> dict[str, Any]:
    with _tx(conn) as c:
        row = store.get_draft(c, key)
    if row is None:
        raise FeedbackError(f"no draft for action {key}")
    return row


def _record(
    conn: Connection | None,
    draft: dict[str, Any],
    decision: str,
    final_text: str | None,
    reason: str | None,
    by_user: str,
    embedding: list[float] | None,
) -> dict[str, Any]:
    ctx, text = draft["context"] or {}, draft["draft_text"]
    final = (final_text or text) if decision == "approved" else final_text
    diff = unified_diff(text, final)
    with _tx(conn) as c:
        ex = store.insert_example(
            c,
            action_key=draft["action_key"],
            job_id=draft["job_id"],
            draft=text,
            final=final,
            diff=diff,
            embedding=embedding,
            decision=decision,
            approver=by_user,
            segment=ctx.get("segment"),
            embed_text=ctx.get("embed_text", ""),
            reason=reason,
            meta=ctx.get("meta") or {},
        )
        if ex is None:
            return {"recorded": False, "reason": "decision already recorded", "count": 0}
        after = {"decision": decision, "edited": bool(diff), "embedded": embedding is not None}
        audit(c, draft["job_id"], STEP, "examples", ex["id"], None, after,
              f"{decision} by {by_user} stored as an example")  # fmt: skip
        count = store.count_examples(c)
    log.info("feedback", extra={"decision": decision, "edited": bool(diff)})
    return {"recorded": True, "example_id": ex["id"], "edited": bool(diff), "count": count}


# ---- reflect ------------------------------------------------------------------------------
class ProposedRule(BaseModel):
    topic: str  # short key for what the rule is about, e.g. "pricing_in_first_email"
    rule: str
    scope: Literal["global", "approver", "segment"] = "global"
    support: list[int]  # example ids whose diffs show this rule
    replaces: int | None = None  # id of an existing rule this one contradicts


class Proposals(BaseModel):
    rules: list[ProposedRule]


REFLECT_PROMPT = """You review how a person edited AI-drafted sales emails before approving them.
Propose short, general writing rules that would have made the drafts need fewer edits
(e.g. "Remove pricing from first emails"). Each rule must be supported by at least {min_support}
of the diffs below: list their ids in `support`. Never put a person's or company's name, an
email address or a phone number in a rule. `topic` is a short snake_case key for what the rule
is about. If a rule contradicts one of the current rules, put that rule's id in `replaces`.
scope: "approver" if only this approver would want it, "segment" if it is about one industry,
else "global".

Current rules:
{current}

Diffs (unified diff, draft -> final):
{diffs}"""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")[:64] or "rule"


def _same_rule(a: str, b: str) -> bool:
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def validate(
    p: ProposedRule, diffs: dict[int, dict[str, Any]], names: set[str], s: Settings
) -> tuple[str | None, str | None]:
    """(scope, None) if the rule is kept, else (None, why it was rejected)."""
    support = sorted(set(p.support) & set(diffs))
    if len(support) < s.outreach.min_support:
        return None, f"support {len(support)} < {s.outreach.min_support} diffs"
    if not p.rule.strip():
        return None, "empty rule"
    if leaked := names_in(p.rule, names):
        return None, f"names a prospect: {leaked}"
    if pd := personal_data_in(p.rule):
        return None, f"contains {', '.join(pd)}"
    rows = [diffs[i] for i in support]
    if p.scope == "approver" and len({r["approver"] for r in rows}) == 1:
        return store.scope_approver(rows[0]["approver"]), None
    if p.scope == "segment" and len({r["segment"] for r in rows}) == 1 and rows[0]["segment"]:
        return store.scope_segment(rows[0]["segment"]), None
    return store.GLOBAL, None


async def reflect(
    *,
    conn: Connection | None = None,
    deps: LearningDeps | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    s, deps = settings or get_settings(), deps or LearningDeps()
    diffs, current = await asyncio.to_thread(_reflect_inputs, conn, s)
    if len(diffs) < s.outreach.min_support:
        return {"accepted": [], "rejected": [], "skipped": "not enough edited drafts yet"}
    prompt = REFLECT_PROMPT.format(
        min_support=s.outreach.min_support,
        current="\n".join(f"[{r['id']}] ({r['scope']}) {r['rule']}" for r in current) or "(none)",
        diffs="\n\n".join(
            f"[{d['id']}] approver={d['approver']} segment={d['segment']} "
            f"reason={d['reason'] or '-'}\n{d['diff']}"
            for d in diffs
        ),
    )
    llm = await deps.get_llm()
    try:
        raw = await llm.generate_json(
            prompt, Proposals.model_json_schema(), job_id=None, endpoint=REFLECT_ENDPOINT
        )
        proposals = Proposals.model_validate_json(raw).rules
    except ValidationError as exc:
        return {"accepted": [], "rejected": [], "skipped": f"bad LLM output: {exc.error_count()}"}
    by_id = {d["id"]: d for d in diffs}
    names = names_from_meta(d.get("meta") or {} for d in diffs)
    return await asyncio.to_thread(_apply, conn, proposals, by_id, names, s)


def _reflect_inputs(conn: Connection | None, s: Settings) -> tuple[list, list]:
    with _tx(conn) as c:
        diffs = store.recent_diffs(c, s.outreach.reflect_window)
        current = c.execute(
            "SELECT id, rule, scope, rule_key FROM preferences WHERE status = %s ORDER BY id",
            (store.ACTIVE,),
        ).fetchall()
    return diffs, current


def _apply(
    conn: Connection | None,
    proposals: list[ProposedRule],
    diffs: dict[int, dict],
    names: set[str],
    s: Settings,
) -> dict[str, Any]:
    accepted, rejected = [], []
    with _tx(conn) as c:
        for p in proposals:
            scope, why = validate(p, diffs, names, s)
            if scope is None:
                rejected.append({"rule": p.rule, "reason": why})
                log.info("rule rejected", extra={"reason": why})
                continue
            row, why = _upsert(c, p, scope, sorted(set(p.support) & set(diffs)))
            if row is None:
                rejected.append({"rule": p.rule, "reason": why})
                continue
            accepted.append(
                {
                    "id": row["id"],
                    "rule": row["rule"],
                    "scope": scope,
                    "version": row["version"],
                    "support": row["support"],
                }
            )
            if p.replaces is not None:
                _supersede(c, p.replaces, row["id"])
    return {"accepted": accepted, "rejected": rejected}


def _upsert(
    c: Connection, p: ProposedRule, scope: str, support: list[int]
) -> tuple[dict[str, Any] | None, str | None]:
    key = slug(p.topic)
    latest = store.latest_version(c, key, scope)
    if latest is not None and latest["status"] == store.DISABLED:
        return None, "this rule was disabled by a person"
    if latest is not None and latest["status"] == store.ACTIVE:
        if _same_rule(latest["rule"], p.rule):
            support = sorted(set(support) | set(latest["source_diffs"] or []))
            if support == sorted(latest["source_diffs"] or []):
                return None, "no new support for an existing rule"
        store.set_preference_status(c, latest["id"], store.SUPERSEDED, REFLECT_BY)
        audit(
            c,
            None,
            STEP,
            "preferences",
            latest["id"],
            {"status": store.ACTIVE},
            {"status": store.SUPERSEDED},
            "newer version of this rule",
        )
    version = (latest["version"] + 1) if latest else 1
    row = store.insert_preference(
        c,
        rule_key=key,
        rule=p.rule.strip(),
        scope=scope,
        version=version,
        support=len(support),
        source_diffs=support,
        changed_by=REFLECT_BY,
    )
    audit(
        c,
        None,
        STEP,
        "preferences",
        row["id"],
        None,
        {"rule": row["rule"], "scope": scope, "version": version, "support": len(support)},
        f"rule proposed by reflect from diffs {support}",
    )
    return row, None


def _supersede(c: Connection, old_id: int, new_id: int) -> None:
    old = store.get_preference(c, old_id, lock=True)
    if old is None or old["status"] != store.ACTIVE or old["id"] == new_id:
        return
    store.set_preference_status(c, old_id, store.SUPERSEDED, REFLECT_BY)
    audit(
        c,
        None,
        STEP,
        "preferences",
        old_id,
        {"status": store.ACTIVE},
        {"status": store.SUPERSEDED, "superseded_by": new_id},
        f"conflicts with newer rule {new_id}: the newer rule wins",
    )


# ---- a person's controls ------------------------------------------------------------------
def disable_rule(pref_id: int, by_user: str, conn: Connection | None = None) -> dict[str, Any]:
    with _tx(conn) as c:
        row = store.get_preference(c, pref_id, lock=True)
        if row is None or row["status"] != store.ACTIVE:
            raise FeedbackError(f"rule {pref_id} is not active")
        out = store.set_preference_status(c, pref_id, store.DISABLED, by_user)
        audit(
            c,
            None,
            STEP,
            "preferences",
            pref_id,
            {"status": store.ACTIVE},
            {"status": store.DISABLED},
            f"disabled by {by_user}",
        )
    return out


def enable_rule(pref_id: int, by_user: str, conn: Connection | None = None) -> dict[str, Any]:
    """Re-activate a disabled rule, unless a newer version of it is active."""
    with _tx(conn) as c:
        row = store.get_preference(c, pref_id, lock=True)
        if row is None or row["status"] != store.DISABLED:
            raise FeedbackError(f"rule {pref_id} is not disabled")
        latest = store.latest_version(c, row["rule_key"], row["scope"])
        if latest is not None and latest["id"] != pref_id and latest["status"] == store.ACTIVE:
            raise FeedbackError(f"a newer version ({latest['id']}) of this rule is active")
        out = store.set_preference_status(c, pref_id, store.ACTIVE, by_user)
        audit(c, None, STEP, "preferences", pref_id, {"status": store.DISABLED},
              {"status": store.ACTIVE}, f"enabled by {by_user}")  # fmt: skip
    return out


def rollback_rule(pref_id: int, by_user: str, conn: Connection | None = None) -> dict[str, Any]:
    """Retire this version and re-activate the version before it."""
    with _tx(conn) as c:
        row = store.get_preference(c, pref_id, lock=True)
        if row is None or row["status"] != store.ACTIVE:
            raise FeedbackError(f"rule {pref_id} is not active")
        prev = store.previous_version(c, row)
        if prev is None:
            raise FeedbackError(f"rule {pref_id} has no earlier version; disable it instead")
        store.set_preference_status(c, pref_id, store.ROLLED_BACK, by_user)
        out = store.set_preference_status(c, prev["id"], store.ACTIVE, by_user)
        audit(
            c,
            None,
            STEP,
            "preferences",
            pref_id,
            {"status": store.ACTIVE},
            {"status": store.ROLLED_BACK},
            f"rolled back by {by_user}",
        )
        audit(
            c,
            None,
            STEP,
            "preferences",
            prev["id"],
            {"status": prev["status"]},
            {"status": store.ACTIVE},
            f"restored by rollback of {pref_id}",
        )
    return out


def preferences(conn: Connection | None = None) -> list[dict[str, Any]]:
    with _tx(conn) as c:
        return store.list_preferences(c)


# ---- metric -------------------------------------------------------------------------------
def metric(conn: Connection | None = None, settings: Settings | None = None) -> list[dict]:
    """One point per batch of outreach.metric_batch decisions: mean normalized edit distance
    between draft and final (0 = approved as drafted). The last batch may be partial."""
    s = settings or get_settings()
    with _tx(conn) as c:
        pairs = store.decided_pairs(c)
    size, out = s.outreach.metric_batch, []
    for start in range(0, len(pairs), size):
        batch = pairs[start : start + size]
        dists = [Levenshtein.normalized_distance(p["draft"], p["final"]) for p in batch]
        out.append(
            {
                "batch": start // size + 1,
                "n": len(batch),
                "mean_distance": round(sum(dists) / len(dists), 4),
                "complete": len(batch) == size,
            }
        )
    return out
