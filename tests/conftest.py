from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

# Compose maps postgres to host port 5433 so it doesn't clash with a local install.
DEFAULT_TEST_DB = "postgresql://revagent:revagent@localhost:5433/revagent"


@pytest.fixture
def db_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_TEST_DB)


@pytest.fixture
def pg_conn(db_url: str) -> Iterator[psycopg.Connection]:
    try:
        conn = psycopg.connect(db_url, connect_timeout=3, row_factory=dict_row, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"postgres not reachable at {db_url}: {exc}")
    with conn:
        yield conn
