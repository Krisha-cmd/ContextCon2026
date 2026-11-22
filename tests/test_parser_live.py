"""Live Gemini call. Skipped by default; run with `uv run pytest -m live`.
Needs GCP_PROJECT, Application Default Credentials and the compose postgres (for cost_ledger)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from app import db, gateway
from app.config import DEFAULT_CONFIG_PATH, get_settings, load_settings
from app.gateway import vertex
from app.spec.models import TriggerType
from app.spec.parser import ENDPOINT, parse_request

SAMPLE = Path(__file__).parent / "fixtures" / "requests" / "01_d2c_warehouse_funding.json"


@pytest.mark.live
async def test_live_gemini_parses_sample(
    monkeypatch: pytest.MonkeyPatch, db_url: str, pg_conn: psycopg.Connection
) -> None:
    if not os.environ.get("GCP_PROJECT"):
        pytest.skip("GCP_PROJECT not set")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr(vertex, "_default", None)  # fresh client bound to this test's gateway
    get_settings.cache_clear()
    db.apply_schema(pg_conn, load_settings(DEFAULT_CONFIG_PATH).db.schema_lock_key)
    started = datetime.now(UTC)
    try:
        spec = await parse_request(json.loads(SAMPLE.read_text(encoding="utf-8"))["input"])
    finally:
        await gateway.close()
        get_settings.cache_clear()
    assert {TriggerType.new_warehouse, TriggerType.funding} <= set(spec.trigger_types)
    assert {"Maharashtra", "Karnataka"} <= set(spec.geo_states)
    assert spec.exclude_domains == ["myntra.com"]
    assert (spec.size_min, spec.size_max) == (50, 500)
    row = pg_conn.execute(
        "SELECT inr FROM cost_ledger WHERE endpoint = %s AND at >= %s", (ENDPOINT, started)
    ).fetchone()
    assert row is not None and row["inr"] > 0
