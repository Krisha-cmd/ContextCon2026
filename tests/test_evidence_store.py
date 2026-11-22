from __future__ import annotations

import psycopg
import pytest
from pydantic import ValidationError

from app import db
from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.evidence.store import raw_hash, save_evidence
from app.log import bind


def test_raw_hash_is_stable_for_dicts() -> None:
    assert raw_hash({"a": 1, "b": 2}) == raw_hash({"b": 2, "a": 1})
    assert raw_hash("x") == raw_hash(b"x")


def test_save_evidence_writes_row_and_audit(pg_conn: psycopg.Connection) -> None:
    db.apply_schema(pg_conn, load_settings(DEFAULT_CONFIG_PATH).db.schema_lock_key)
    raw = {"company": {"legal_name": "Acme Pvt Ltd"}}
    with pg_conn.transaction(force_rollback=True), bind(step="identity"):
        src = save_evidence(
            raw, "https://api.example/x", "crustdata",
            field_path="company.legal_name", conn=pg_conn,
        )  # fmt: skip
        row = pg_conn.execute("SELECT * FROM evidence WHERE id = %s", (src.evidence_id,)).fetchone()
        audit = pg_conn.execute(
            "SELECT * FROM audit_log WHERE tbl = 'evidence' AND record_id = %s",
            (src.evidence_id,),
        ).fetchone()
    assert row["raw_hash"] == raw_hash(raw)
    assert row["field_path"] == "company.legal_name"
    assert row["source_name"] == "crustdata"
    assert audit["step"] == "identity"
    assert audit["evidence_ids"] == [src.evidence_id]


def test_save_evidence_rejects_missing_locator_before_writing() -> None:
    # No conn and no DB needed: validation fails before any write.
    with pytest.raises(ValidationError, match="field_path or quote"):
        save_evidence({}, "https://api.example/x", "crustdata")
