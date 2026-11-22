"""Persist evidence rows and hand back the Source that points at them."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app import db
from app.evidence.models import Source
from app.log import current_context

_INSERT_EVIDENCE = """
INSERT INTO evidence (id, url, field_path, quote, record_date, retrieved_at, raw_hash, source_name)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_AUDIT = """
INSERT INTO audit_log (job_id, step, tbl, record_id, before, after, reason, evidence_ids)
VALUES (%s, %s, 'evidence', %s, NULL, %s, %s, %s)
"""


def raw_hash(raw_response: Any) -> str:
    """sha256 of the raw response. bytes/str are hashed as-is; anything else as sorted JSON."""
    if isinstance(raw_response, bytes):
        data = raw_response
    elif isinstance(raw_response, str):
        data = raw_response.encode("utf-8")
    else:
        data = json.dumps(raw_response, sort_keys=True, default=str, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def new_evidence_id() -> str:
    return f"ev_{uuid.uuid4().hex}"


def save_evidence(
    raw_response: Any,
    url: str,
    source_name: str,
    *,
    field_path: str | None = None,
    quote: str | None = None,
    record_date: date | None = None,
    conn: psycopg.Connection | None = None,
) -> Source:
    """Write one evidence row (plus its audit_log row) and return the Source for it.

    Validation happens before any write: a Source without field_path or quote raises.
    job_id and step for the audit row come from the bound log context (app.log.bind).
    Pass `conn` to join an existing transaction; otherwise a pooled one is used.
    """
    source = Source(
        url=url,
        source_name=source_name,
        field_path=field_path,
        quote=quote,
        record_date=record_date,
        retrieved_at=datetime.now(UTC),
        evidence_id=new_evidence_id(),
    )
    digest = raw_hash(raw_response)
    if conn is None:
        with db.tx() as c:
            _write(c, source, digest)
    else:
        with conn.transaction():
            _write(conn, source, digest)
    return source


def _write(conn: psycopg.Connection, source: Source, digest: str) -> None:
    conn.execute(
        _INSERT_EVIDENCE,
        (
            source.evidence_id,
            source.url,
            source.field_path,
            source.quote,
            source.record_date,
            source.retrieved_at,
            digest,
            source.source_name,
        ),
    )
    ctx = current_context()
    after = {**source.model_dump(mode="json"), "raw_hash": digest}
    conn.execute(
        _INSERT_AUDIT,
        (
            ctx.get("job_id"),
            ctx.get("step"),
            source.evidence_id,
            Jsonb(after),
            "evidence saved",
            [source.evidence_id],
        ),
    )
