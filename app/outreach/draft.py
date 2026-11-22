"""Runner step "draft" (per account): a first email for a person to approve. Nothing is sent.

Inputs: the account's verified trigger and buyer Facts, its score reasons, the client profile
(job kickoff data, else config), active preference rules (global, segment, approver), and the
outreach.examples_k nearest approved examples (pgvector cosine on the embedding of
"industry | trigger type | buyer title").
Gemini writes subject + body from a numbered fact sheet; plain code then checks the draft:
word limit, the trigger cited (its sentence must be in the body), only fact-sheet facts used,
no number that isn't in the facts, no personal data, no other prospect's name. One repair try,
then the account is held. A passing draft becomes an outbox "email" action in
pending_approval (key email:<job>:<account>, so one email per account) plus a `drafts` row
with everything the approver needs to judge it, both written with the step.
actions.payload is exactly what the outbox will send ({to, subject, body}). `to` starts empty:
we never store a personal address, so the approver enters the business address on approval.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from app import db
from app.actions.models import ActionType, State, make_key
from app.config import Settings
from app.evidence.models import quote_in_text
from app.gateway import GatewayError
from app.gateway.vertex import Embedder, JsonLLM, get_vertex_embedder, get_vertex_llm
from app.log import get_logger
from app.outreach import store
from app.outreach.privacy import names_from_meta, names_in, personal_data_in
from app.revenue.common import fact_value, run_spec
from app.runner.context import StepContext

log = get_logger(__name__)

ACTION_TYPE = ActionType.email.value
DRAFT_ENDPOINT, EMBED_ENDPOINT = "vertex.draft", "vertex.embed"
APPROVED = "approved"
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"\S+")

INSERT_ACTION = (
    "INSERT INTO actions (idem_key, job_id, account_id, type, payload, state) "
    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (idem_key) DO NOTHING"
)
INSERT_DRAFT = (
    "INSERT INTO drafts (action_key, job_id, account_id, draft_text, context) "
    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (action_key) DO NOTHING"
)

PROMPT = """You write a short first email from {client_name} to a prospect.
About the sender: {client}

Facts you may use (each has a source). Mention no other fact, name, number or claim:
{facts}

Rules from the approver (follow all of them; later rules win):
{rules}

Approved emails to similar prospects (learn tone and shape only; never reuse their names or
facts):
{examples}

Requirements:
- Cite the trigger (fact "trigger") in plain words, in one sentence of the body.
- Body under {limit} words. No pricing unless a fact gives it. No email addresses or phone
  numbers. Plain text, no placeholders.
Return subject, body, trigger_sentence (that sentence, copied exactly from the body) and
facts_used (the keys of the facts you used).{errors}"""


class DraftOut(BaseModel):
    subject: str
    body: str
    trigger_sentence: str
    facts_used: list[str]


@dataclass(frozen=True)
class FactLine:
    key: str
    text: str
    source: str


def action_key(job_id: Any, account_id: Any) -> str:
    """The outbox key: one email per (job, account)."""
    return make_key(ACTION_TYPE, job_id, account_id)


def draft_text(subject: str, body: str) -> str:
    return f"Subject: {subject}\n\n{body}"


def _first_source(fact: Any) -> str:
    srcs = fact.get("sources") if isinstance(fact, dict) else None
    return (srcs[0].get("url") or srcs[0].get("source_name")) if srcs else "account record"


def fact_sheet(account: dict[str, Any]) -> list[FactLine] | None:
    """Numbered facts the draft may use; None without a verified trigger and buyer."""
    data = account.get("data") or {}
    trigger, buyer = fact_value(data, "trigger"), fact_value(data, "buyer")
    if not isinstance(trigger, dict) or not isinstance(buyer, dict):
        return None
    legal = fact_value(data, "legal_name")
    company = buyer.get("company") or account.get("name")
    lines = [
        FactLine(
            "company",
            f"Prospect company: {legal or company}",
            _first_source(data.get("legal_name") if legal else data.get("buyer")),
        ),
        FactLine(
            "trigger",
            f"Trigger ({str(trigger.get('type')).replace('_', ' ')}, "
            f"{trigger.get('record_date')}): {trigger.get('title')}",
            _first_source(data.get("trigger")),
        ),
        FactLine(
            "buyer",
            f"Recipient: {buyer.get('name')}, {buyer.get('title')} at {company}",
            _first_source(data.get("buyer")),
        ),
    ]
    reasons = [
        r for r in data.get("score_reasons") or [] if "(source:" in r and not r.startswith("+0")
    ]
    for i, r in enumerate(reasons, start=1):
        text = re.sub(r"^[+-]\d+\s+", "", r)
        lines.append(FactLine(f"reason_{i}", text, text.rsplit("(source:", 1)[-1].rstrip(")")))
    return lines


def segment_of(account: dict[str, Any], industries: list[str], s: Settings) -> str:
    have = [i for i in (account.get("data") or {}).get("industries") or [] if i]
    return (have or industries or [s.outreach.default_segment])[0]


def embed_text_for(segment: str, account: dict[str, Any]) -> str:
    data = account.get("data") or {}
    trigger, buyer = fact_value(data, "trigger") or {}, fact_value(data, "buyer") or {}
    return f"{segment} | {trigger.get('type', '')} | {buyer.get('title', '')}"


def word_limit(rules: list[dict[str, Any]], s: Settings) -> int:
    """outreach.max_words, unless an active rule states a limit (the narrowest scope wins)."""
    limit = s.outreach.max_words
    for r in rules:  # broad -> narrow, so the last match is the most specific
        m = re.search(s.outreach.word_limit_regex, r["rule"], re.IGNORECASE)
        if m:
            limit = int(m.group(1))
    return limit


def client_profile(job: dict[str, Any], s: Settings) -> dict[str, Any]:
    kickoff = (job.get("spec") or {}).get("client_profile")
    return kickoff if isinstance(kickoff, dict) and kickoff else dict(s.outreach.client_profile)


def own_names(account: dict[str, Any]) -> set[str]:
    data = account.get("data") or {}
    buyer = fact_value(data, "buyer") or {}
    meta = {
        "buyer_name": buyer.get("name"),
        "company": buyer.get("company"),
        "account_name": account.get("name"),
        "legal_name": fact_value(data, "legal_name"),
    }
    return names_from_meta([meta])


def build_prompt(
    sheet: list[FactLine],
    client: dict[str, Any],
    rules: list[dict[str, Any]],
    examples: list[dict[str, Any]],
    limit: int,
    errors: list[str],
) -> str:
    facts = "\n".join(f"[{f.key}] {f.text} (source: {f.source})" for f in sheet)
    rule_lines = "\n".join(f"- {r['rule']}" for r in rules) or "- (none yet)"
    ex = (
        "\n\n".join(
            f"Example {i}:\n{e['final'] or e['draft']}" for i, e in enumerate(examples, start=1)
        )
        or "(none yet)"
    )
    err = (
        ("\n\nYour previous draft failed these checks; fix all of them:\n- " + "\n- ".join(errors))
        if errors
        else ""
    )
    return PROMPT.format(
        client_name=client.get("name", "the sender"),
        client=json.dumps(client, ensure_ascii=False),
        facts=facts,
        rules=rule_lines,
        examples=ex,
        limit=limit,
        errors=err,
    )


def check(
    d: DraftOut, sheet: list[FactLine], client: dict[str, Any], limit: int, forbidden: set[str]
) -> list[str]:
    errors = []
    if not d.subject.strip() or not d.body.strip():
        errors.append("subject and body must not be empty")
    words = len(_WORD.findall(d.body))
    if words > limit:
        errors.append(f"body has {words} words; the limit is {limit}")
    if not d.trigger_sentence.strip() or not quote_in_text(d.trigger_sentence, d.body):
        errors.append("trigger_sentence must be copied exactly from the body")
    keys = {f.key for f in sheet}
    if unknown := sorted(set(d.facts_used) - keys):
        errors.append(f"facts_used has keys not in the fact list: {unknown}")
    if "trigger" not in d.facts_used:
        errors.append("the trigger must be cited")
    allowed = " ".join(f.text for f in sheet) + " " + json.dumps(client, ensure_ascii=False)
    text = f"{d.subject}\n{d.body}"
    if stray := sorted({n for n in _NUMBER.findall(text) if n not in allowed}):
        errors.append(f"numbers not in the facts: {stray}")
    errors += [f"remove {p}" for p in personal_data_in(text)]
    if leaked := names_in(text, forbidden):
        errors.append(f"names from other prospects' emails: {leaked}")
    return errors


# ---- deps ---------------------------------------------------------------------------------
def db_rules(scopes: list[str]) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return store.active_preferences(conn, scopes)


def db_examples(embedding: list[float], k: int) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return store.nearest_examples(conn, embedding, k, APPROVED)


async def _llm() -> JsonLLM:
    return await get_vertex_llm()


async def _embedder() -> Embedder:
    return await get_vertex_embedder()


@dataclass
class DraftDeps:
    get_llm: Callable[[], Awaitable[JsonLLM]] = _llm
    get_embedder: Callable[[], Awaitable[Embedder]] = _embedder
    rules: Callable[[list[str]], list[dict[str, Any]]] = db_rules
    examples: Callable[[list[float], int], list[dict[str, Any]]] = db_examples


async def nearest(ctx: StepContext, text: str, deps: DraftDeps, notes: list[str]) -> tuple:
    try:
        emb = await (await deps.get_embedder()).embed(
            text, job_id=ctx.job_id, endpoint=EMBED_ENDPOINT
        )
    except GatewayError as exc:
        notes.append(f"no examples: embedding failed ({type(exc).__name__})")
        return None, []
    k = ctx.config.outreach.examples_k
    return emb, await asyncio.to_thread(deps.examples, emb, k)


async def write_draft(
    ctx: StepContext, prompt_parts: dict[str, Any], forbidden: set[str], deps: DraftDeps
) -> tuple[DraftOut | None, list[str], str]:
    llm, errors, prompt = await deps.get_llm(), [], ""
    for _ in range(ctx.config.outreach.draft_attempts):
        prompt = build_prompt(**prompt_parts, errors=errors)
        raw = await llm.generate_json(
            prompt, DraftOut.model_json_schema(), job_id=ctx.job_id, endpoint=DRAFT_ENDPOINT
        )
        try:
            d = DraftOut.model_validate_json(raw)
        except ValidationError as exc:
            errors = [f"output did not match the schema: {exc.error_count()} error(s)"]
            continue
        errors = check(
            d,
            prompt_parts["sheet"],
            prompt_parts["client"],
            prompt_parts["limit"],
            forbidden,
        )
        if not errors:
            return d, [], prompt
    return None, errors, prompt


async def run_draft(ctx: StepContext, deps: DraftDeps | None = None) -> dict[str, Any]:
    deps, s = deps or DraftDeps(), ctx.config
    account = ctx.account or {}
    sheet = fact_sheet(account)
    if sheet is None:
        ctx.hold("draft: needs a verified trigger and buyer")
        return {"drafted": False, "reason": "no verified trigger and buyer"}
    spec = run_spec(ctx.job)
    segment = segment_of(account, spec.industries, s)
    scopes = [store.GLOBAL, store.scope_segment(segment), store.scope_approver(s.outreach.approver)]
    rules = await asyncio.to_thread(deps.rules, scopes)
    notes: list[str] = []
    embed_text = embed_text_for(segment, account)
    _, examples = await nearest(ctx, embed_text, deps, notes)
    forbidden = names_from_meta(e.get("meta") or {} for e in examples) - own_names(account)
    parts = {
        "sheet": sheet,
        "client": client_profile(ctx.job, s),
        "rules": rules,
        "examples": examples,
        "limit": word_limit(rules, s),
    }
    d, errors, _prompt = await write_draft(ctx, parts, forbidden, deps)
    if d is None:
        ctx.hold(f"draft failed checks: {'; '.join(errors)}")
        return {"drafted": False, "errors": errors, "notes": notes}
    key = action_key(ctx.job_id, ctx.account_id)
    data = account.get("data") or {}
    buyer = fact_value(data, "buyer") or {}
    evs = sorted(
        {
            src["evidence_id"]
            for k in ("trigger", "buyer")
            for src in (data.get(k) or {}).get("sources", [])
            if src.get("evidence_id")
        }
    )
    context = {
        "subject": d.subject,
        "trigger_sentence": d.trigger_sentence,
        "facts_used": d.facts_used,
        "facts": [{"key": f.key, "text": f.text, "source": f.source} for f in sheet],
        "score_reasons": data.get("score_reasons") or [],
        "evidence_ids": evs,
        "recipient": {k: buyer.get(k) for k in ("name", "title", "company", "profile_url")},
        "approver": s.outreach.approver,
        "segment": segment,
        "embed_text": embed_text,
        "rules": [{"id": r["id"], "version": r["version"], "rule": r["rule"]} for r in rules],
        "example_ids": [e["id"] for e in examples],
        "meta": {
            "buyer_name": buyer.get("name"),
            "company": buyer.get("company"),
            "account_name": account.get("name"),
            "legal_name": fact_value(data, "legal_name"),
        },
    }
    outbox_payload = {"to": [], "subject": d.subject, "body": d.body}
    text = draft_text(d.subject, d.body)
    ctx.write(
        INSERT_ACTION,
        (
            key,
            ctx.job_id,
            ctx.account_id,
            ACTION_TYPE,
            Jsonb(outbox_payload),
            State.pending_approval.value,
        ),
        tbl="actions",
        record_id=key,
        before=None,
        after={"type": ACTION_TYPE, "state": State.pending_approval.value},
        reason="draft: proposed, waiting for approval",
        evidence_ids=tuple(evs),
    )
    ctx.write(
        INSERT_DRAFT,
        (key, ctx.job_id, ctx.account_id, text, Jsonb(context)),
        tbl="drafts",
        record_id=key,
        before=None,
        after={"approver": s.outreach.approver, "rules": len(rules), "examples": len(examples)},
        reason="draft: context for the approver",
        evidence_ids=tuple(evs),
    )
    log.info("draft", extra={"rules": len(rules), "examples": len(examples)})
    return {
        "drafted": True,
        "action_key": key,
        "subject": d.subject,
        "rules": [{"id": r["id"], "version": r["version"]} for r in rules],
        "example_ids": context["example_ids"],
        "notes": notes,
    }
