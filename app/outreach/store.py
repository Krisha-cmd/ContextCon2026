"""SQL for app/outreach (plain psycopg, dict rows). Callers own the transaction and the audit."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

ACTIVE, SUPERSEDED, DISABLED, ROLLED_BACK = "active", "superseded", "disabled", "rolled_back"
PENDING = "pending"
GLOBAL = "global"


def scope_approver(approver: str) -> str:
    return f"approver:{approver}"


def scope_segment(segment: str) -> str:
    return f"segment:{segment.strip().casefold()}"


def vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


# ---- preferences --------------------------------------------------------------------------
def active_preferences(conn: Connection, scopes: Sequence[str]) -> list[dict[str, Any]]:
    """Active rules in these scopes, broadest scope first (scopes are given broad -> narrow)."""
    rows = conn.execute(
        "SELECT * FROM preferences WHERE status = %s AND scope = ANY(%s) ORDER BY id",
        (ACTIVE, list(scopes)),
    ).fetchall()
    order = {s: i for i, s in enumerate(scopes)}
    return sorted(rows, key=lambda r: (order[r["scope"]], r["id"]))


def list_preferences(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, rule_key, rule, scope, version, support, source_diffs, status, created_at, "
        "updated_at, changed_by FROM preferences ORDER BY rule_key, scope, version DESC"
    ).fetchall()


def get_preference(conn: Connection, pref_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    sql = "SELECT * FROM preferences WHERE id = %s" + (" FOR UPDATE" if lock else "")
    return conn.execute(sql, (pref_id,)).fetchone()


def latest_version(conn: Connection, rule_key: str, scope: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM preferences WHERE rule_key = %s AND scope = %s "
        "ORDER BY version DESC LIMIT 1 FOR UPDATE",
        (rule_key, scope),
    ).fetchone()


def previous_version(conn: Connection, row: dict[str, Any]) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM preferences WHERE rule_key = %s AND scope = %s AND version < %s "
        "AND status = %s ORDER BY version DESC LIMIT 1 FOR UPDATE",
        (row["rule_key"], row["scope"], row["version"], SUPERSEDED),
    ).fetchone()


def insert_preference(
    conn: Connection,
    *,
    rule_key: str,
    rule: str,
    scope: str,
    version: int,
    support: int,
    source_diffs: list[int],
    changed_by: str,
) -> dict[str, Any]:
    return conn.execute(
        "INSERT INTO preferences (rule_key, rule, scope, version, support, source_diffs, status, "
        "changed_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (rule_key, rule, scope, version, support, Jsonb(source_diffs), ACTIVE, changed_by),
    ).fetchone()


def set_preference_status(
    conn: Connection, pref_id: int, status: str, changed_by: str
) -> dict[str, Any]:
    return conn.execute(
        "UPDATE preferences SET status = %s, changed_by = %s, updated_at = now() WHERE id = %s "
        "RETURNING *",
        (status, changed_by, pref_id),
    ).fetchone()


# ---- examples -----------------------------------------------------------------------------
def nearest_examples(
    conn: Connection, embedding: Sequence[float], k: int, decision: str
) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, draft, final, segment, meta, embedding <=> %s::vector AS distance "
        "FROM examples WHERE decision = %s AND embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::vector, id LIMIT %s",
        (vector_literal(embedding), decision, vector_literal(embedding), k),
    ).fetchall()


def insert_example(
    conn: Connection,
    *,
    action_key: str,
    job_id: Any,
    draft: str,
    final: str | None,
    diff: str,
    embedding: Sequence[float] | None,
    decision: str,
    approver: str,
    segment: str | None,
    embed_text: str,
    reason: str | None,
    meta: dict[str, Any],
) -> dict[str, Any] | None:
    """None when this action already has an example (a decision is recorded once)."""
    return conn.execute(
        "INSERT INTO examples (action_key, job_id, draft, final, diff, embedding, decision, "
        "approver, segment, embed_text, reason, meta) "
        "VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (action_key) DO NOTHING RETURNING id, decision, created_at",
        (
            action_key,
            job_id,
            draft,
            final,
            diff,
            vector_literal(embedding) if embedding else None,
            decision,
            approver,
            segment,
            embed_text,
            reason,
            Jsonb(meta),
        ),
    ).fetchone()


def count_examples(conn: Connection) -> int:
    return conn.execute("SELECT count(*) AS n FROM examples").fetchone()["n"]


def recent_diffs(conn: Connection, limit: int) -> list[dict[str, Any]]:
    """Newest decided examples that were edited (non-empty diff), oldest first."""
    rows = conn.execute(
        "SELECT id, diff, decision, approver, segment, reason, meta FROM examples "
        "WHERE diff <> '' ORDER BY created_at DESC, id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    return list(reversed(rows))


def decided_pairs(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, draft, final, created_at FROM examples WHERE final IS NOT NULL "
        "ORDER BY created_at, id"
    ).fetchall()


# ---- actions / approvals ------------------------------------------------------------------
def action_for_update(conn: Connection, action_key: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM actions WHERE idem_key = %s FOR UPDATE", (action_key,)
    ).fetchone()


def pending_approval(conn: Connection, action_key: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM approvals WHERE action_key = %s AND decision = %s ORDER BY id LIMIT 1 "
        "FOR UPDATE",
        (action_key, PENDING),
    ).fetchone()
