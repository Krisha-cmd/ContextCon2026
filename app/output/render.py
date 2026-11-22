"""Map internal models to the client output format.

Which fields appear is driven by config.yaml `output.fields`, so adding a field is a config
change. Fact fields are always emitted whole (value, status, sources, missing, stale,
alternatives) so no fact leaves the system without its evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg

from app import db
from app.config import get_settings
from app.spec.models import Account

_SELECT_JOB = "SELECT id, client_id, status, created_at FROM jobs WHERE id = %s"
_SELECT_ACCOUNTS = (
    "SELECT id, name, domain, entity_id, data FROM accounts WHERE job_id = %s ORDER BY id"
)


def output_fields(configured: Sequence[str]) -> list[str]:
    """Validate configured field names against Account. Empty config means all fields."""
    known = list(Account.model_fields)
    if not configured:
        return known
    unknown = [f for f in configured if f not in Account.model_fields]
    if unknown:
        raise ValueError(f"output.fields has unknown fields {unknown}; known: {known}")
    return list(configured)


def render_account(account: Account, fields: Sequence[str]) -> dict[str, Any]:
    dumped = account.model_dump(mode="json")
    # TODO(kickoff output format): rename/reshape fields here to the client's format once it is
    # announced at kickoff. Keep the mapping data-driven (config) rather than per-field code.
    return {f: dumped[f] for f in fields}


def render_accounts(accounts: Sequence[Account], configured: Sequence[str]) -> list[dict]:
    fields = output_fields(configured)
    return [render_account(a, fields) for a in accounts]


def account_from_row(row: dict[str, Any]) -> Account:
    """accounts.data holds the Account fact fields; identity columns live on the row itself."""
    return Account.model_validate(
        {
            **(row["data"] or {}),
            "id": row["id"],
            "name": row["name"],
            "domain": row["domain"],
            "crustdata_id": row["entity_id"],
        }
    )


def load_job(conn: psycopg.Connection, job_id: UUID | str) -> tuple[dict, list[Account]]:
    job = conn.execute(_SELECT_JOB, (job_id,)).fetchone()
    if job is None:
        raise LookupError(f"job {job_id} not found")
    rows = conn.execute(_SELECT_ACCOUNTS, (job_id,)).fetchall()
    return job, [account_from_row(r) for r in rows]


def render_job(job_id: UUID | str, conn: psycopg.Connection | None = None) -> dict[str, Any]:
    configured = get_settings().output.fields
    if conn is None:
        with db.tx() as c:
            job, accounts = load_job(c, job_id)
    else:
        job, accounts = load_job(conn, job_id)
    # TODO(kickoff output format): envelope shape (keys, job metadata) is provisional.
    return {
        "job_id": str(job["id"]),
        "status": job["status"],
        "accounts": render_accounts(accounts, configured),
    }
