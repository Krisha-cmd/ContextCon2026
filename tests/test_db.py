from __future__ import annotations

import psycopg
import pytest

from app import db
from app.config import DEFAULT_CONFIG_PATH, load_settings

EXPECTED_TABLES = {
    "jobs", "steps", "accounts", "entity_keys", "audit_log", "evidence", "http_cache",
    "entity_cache", "cost_ledger", "actions", "approvals", "parse_rules", "preferences",
    "examples", "match_labels", "source_stats", "dead_letter",
}  # fmt: skip


def _lock_key() -> int:
    return load_settings(DEFAULT_CONFIG_PATH).db.schema_lock_key


def test_schema_applies_twice(pg_conn: psycopg.Connection) -> None:
    db.apply_schema(pg_conn, _lock_key())
    db.apply_schema(pg_conn, _lock_key())  # idempotent: must not raise
    rows = pg_conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    assert {r["table_name"] for r in rows} >= EXPECTED_TABLES


def test_vector_extension_enabled(pg_conn: psycopg.Connection) -> None:
    db.apply_schema(pg_conn, _lock_key())
    row = pg_conn.execute("SELECT 1 AS ok FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert row is not None


def test_steps_unique_including_null_account(pg_conn: psycopg.Connection) -> None:
    db.apply_schema(pg_conn, _lock_key())
    with pg_conn.transaction(force_rollback=True):
        job = pg_conn.execute(
            "INSERT INTO jobs (client_id, spec, status, limits) "
            "VALUES ('c', '{}', 'new', '{}') RETURNING id"
        ).fetchone()
        ins = (
            "INSERT INTO steps (job_id, account_id, step, status) VALUES (%s, NULL, 'spec', 'new')"
        )
        pg_conn.execute(ins, (job["id"],))
        with pytest.raises(psycopg.errors.UniqueViolation), pg_conn.transaction():
            pg_conn.execute(ins, (job["id"],))


def test_tx_commits_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, db_url: str, pg_conn: psycopg.Connection
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = load_settings(DEFAULT_CONFIG_PATH)
    db.close_pool()
    db.init_db(settings)
    try:
        assert db.ping()
        key = "test-tx-rollback"
        with pytest.raises(RuntimeError), db.tx() as conn:
            conn.execute(
                "INSERT INTO source_stats (source) VALUES (%s) ON CONFLICT DO NOTHING", (key,)
            )
            raise RuntimeError("boom")
        with db.tx() as conn:
            assert (
                conn.execute("SELECT 1 FROM source_stats WHERE source=%s", (key,)).fetchone()
                is None
            )
    finally:
        db.close_pool()
