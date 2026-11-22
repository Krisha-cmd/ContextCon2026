"""Postgres access: one connection pool, a tx() helper, and schema application."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from app.config import ROOT, Settings, get_settings

SCHEMA_PATH = ROOT / "schema.sql"

_pool: ConnectionPool | None = None
_apool: AsyncConnectionPool | None = None


def _pool_kwargs(s: Settings) -> dict:
    return {"row_factory": dict_row, "connect_timeout": s.db.connect_timeout_s}


async def open_async_pool(settings: Settings | None = None) -> AsyncConnectionPool:
    """Open the process-wide async pool (idempotent). Used by app/gateway."""
    global _apool
    if _apool is None:
        s = settings or get_settings()
        pool = AsyncConnectionPool(
            database_url(s),
            min_size=s.db.pool_min,
            max_size=s.db.pool_max,
            kwargs=_pool_kwargs(s),
            open=False,
        )
        await pool.open(wait=True)
        _apool = pool
    return _apool


async def close_async_pool() -> None:
    global _apool
    if _apool is not None:
        await _apool.close()
        _apool = None


def database_url(settings: Settings) -> str:
    """DATABASE_URL, or a conninfo built from DB_USER/DB_PASSWORD/DB_NAME/DB_HOST (Cloud SQL
    unix socket on Cloud Run)."""
    sec = settings.secrets
    if sec.database_url is not None:
        return sec.database_url.get_secret_value()
    if sec.db_user and sec.db_password and sec.db_name and sec.db_host:
        return conninfo.make_conninfo(
            user=sec.db_user,
            password=sec.db_password.get_secret_value(),
            dbname=sec.db_name,
            host=sec.db_host,
        )
    raise RuntimeError("DATABASE_URL (or DB_USER/DB_PASSWORD/DB_NAME/DB_HOST) is not set")


def open_pool(settings: Settings | None = None) -> ConnectionPool:
    """Open the process-wide pool (idempotent)."""
    global _pool
    if _pool is None:
        s = settings or get_settings()
        _pool = ConnectionPool(
            database_url(s),
            min_size=s.db.pool_min,
            max_size=s.db.pool_max,
            kwargs=_pool_kwargs(s),
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """Borrow a pooled connection inside one transaction; commit on success, roll back on error."""
    with open_pool().connection() as conn, conn.transaction():
        yield conn


def apply_schema(conn: psycopg.Connection, lock_key: int, path: Path = SCHEMA_PATH) -> None:
    """Apply schema.sql in one transaction, serialised by an advisory lock (api + worker race)."""
    sql = path.read_text(encoding="utf-8")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        conn.execute(sql)


def init_db(settings: Settings | None = None) -> None:
    """Startup hook: open the pool and make sure the schema exists."""
    s = settings or get_settings()
    with open_pool(s).connection() as conn:
        apply_schema(conn, s.db.schema_lock_key)


def ping() -> bool:
    with tx() as conn:
        row = conn.execute("SELECT 1 AS ok").fetchone()
    return bool(row and row["ok"] == 1)
