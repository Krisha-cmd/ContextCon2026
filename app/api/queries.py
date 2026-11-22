"""Read-only SQL for the screens. Every function takes an open connection (dict rows)."""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from app.actions.models import ActionType, State


def job(conn: Connection, job_id: Any) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, client_id, spec, status, limits, stop_optional, created_at, "
        "extract(epoch FROM CASE WHEN status = 'running' THEN now() ELSE updated_at END "
        "- created_at)::int AS elapsed_s "
        "FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone()


def recent_jobs(conn: Connection, limit: int = 10) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, client_id, status, created_at FROM jobs ORDER BY created_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def step_rows(conn: Connection, job_id: Any) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT step, account_id, status, attempts, last_error FROM steps WHERE job_id = %s "
        "ORDER BY priority, id",
        (job_id,),
    ).fetchall()


def ledger(conn: Connection, job_id: Any) -> dict[str, Any]:
    total = conn.execute(
        "SELECT count(*) AS calls, count(*) FILTER (WHERE cache_hit) AS cache_hits, "
        "coalesce(sum(credits), 0)::float AS credits, coalesce(sum(inr), 0)::float AS inr "
        "FROM cost_ledger WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    by_endpoint = conn.execute(
        "SELECT endpoint, count(*) AS calls, count(*) FILTER (WHERE cache_hit) AS cache_hits, "
        "coalesce(sum(credits), 0)::float AS credits, coalesce(sum(inr), 0)::float AS inr "
        "FROM cost_ledger WHERE job_id = %s GROUP BY endpoint ORDER BY endpoint",
        (job_id,),
    ).fetchall()
    return {**total, "by_endpoint": by_endpoint}


def ledger_rows(conn: Connection, job_id: Any, endpoint: str | None, limit: int) -> list[dict]:
    return conn.execute(
        "SELECT at, endpoint, credits::float, inr::float, ms, cache_hit FROM cost_ledger "
        "WHERE job_id = %s AND (%s::text IS NULL OR endpoint LIKE %s || '%%') "
        "ORDER BY at, id LIMIT %s",
        (job_id, endpoint, endpoint, limit),
    ).fetchall()


def accounts(conn: Connection, job_id: Any) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, name, domain, entity_id, data, status, status_reason, status_step "
        "FROM accounts WHERE job_id = %s ORDER BY name, id",
        (job_id,),
    ).fetchall()


def account(conn: Connection, job_id: Any, account_id: Any) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, job_id, name, domain, entity_id, data, status, status_reason, status_step "
        "FROM accounts WHERE job_id = %s AND id = %s",
        (job_id, account_id),
    ).fetchone()


def account_audit(conn: Connection, job_id: Any, account_id: Any) -> list[dict[str, Any]]:
    """Audit rows about this account, its steps and its actions."""
    return conn.execute(
        "SELECT at, step, tbl, record_id, before, after, reason, evidence_ids FROM audit_log "
        "WHERE job_id = %s AND (record_id = %s::text "
        "  OR record_id IN (SELECT id::text FROM steps WHERE account_id = %s) "
        "  OR record_id IN (SELECT idem_key FROM actions WHERE account_id = %s)) "
        "ORDER BY at, id",
        (job_id, account_id, account_id, account_id),
    ).fetchall()


def job_audit(conn: Connection, job_id: Any, step: str | None, limit: int) -> list[dict]:
    return conn.execute(
        "SELECT at, step, tbl, record_id, reason, evidence_ids FROM audit_log "
        "WHERE job_id = %s AND (%s::text IS NULL OR step = %s) ORDER BY at, id LIMIT %s",
        (job_id, step, step, limit),
    ).fetchall()


def audit_steps(conn: Connection, job_id: Any) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT step FROM audit_log WHERE job_id = %s AND step IS NOT NULL ORDER BY step",
        (job_id,),
    ).fetchall()
    return [r["step"] for r in rows]


def pending_approvals(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT a.idem_key, a.job_id, a.account_id, a.payload, a.created_at, d.draft_text, "
        "d.context, acc.name AS account_name FROM actions a "
        "JOIN drafts d ON d.action_key = a.idem_key "
        "LEFT JOIN accounts acc ON acc.id = a.account_id "
        "WHERE a.state = %s AND a.type = %s ORDER BY a.created_at, a.idem_key",
        (State.pending_approval.value, ActionType.email.value),
    ).fetchall()


def approval_card(conn: Connection, key: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT a.idem_key, a.job_id, a.account_id, a.payload, a.state, d.draft_text, d.context, "
        "acc.name AS account_name FROM actions a JOIN drafts d ON d.action_key = a.idem_key "
        "LEFT JOIN accounts acc ON acc.id = a.account_id WHERE a.idem_key = %s",
        (key,),
    ).fetchone()


def job_actions(conn: Connection, job_id: Any) -> list[dict[str, Any]]:
    """Every action for the job with its latest decision (the outcome screen)."""
    return conn.execute(
        "SELECT a.idem_key, a.type, a.state, a.payload, a.result, a.last_error, a.updated_at, "
        "a.account_id, acc.name AS account_name, ap.decision, ap.by_user, ap.reason "
        "FROM actions a LEFT JOIN accounts acc ON acc.id = a.account_id "
        "LEFT JOIN LATERAL (SELECT decision, by_user, reason FROM approvals p "
        "  WHERE p.action_key = a.idem_key ORDER BY p.id DESC LIMIT 1) ap ON true "
        "WHERE a.job_id = %s ORDER BY acc.name, a.type",
        (job_id,),
    ).fetchall()
