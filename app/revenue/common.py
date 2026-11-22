"""What the revenue steps share: the RunSpec, the run date, and the enrich cache.

accounts.data fields these steps read (written by discovery, identity and trust):
  crustdata_ids | crustdata_id, city, state, headcount, industries, signals (door-2 Signal
  JSON), pre_rank_score (or pre_rank.score), sources (discovery Source JSON),
  status_active and adverse_news (Facts from trust).
The enrich step stores each company record in entity_cache under "crustdata:<id>" as
{"record", "evidence_id", "url", "retrieved_at"} and points the account at it with
data.enrich = {"entity_key", "evidence_id", "request_hash", "confidence", "from_cache"}.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError

from app import db
from app.evidence.models import FactStatus, Source
from app.log import get_logger
from app.spec.models import RunSpec

log = get_logger(__name__)

ENRICH_DATA_TYPE = "company"
ENRICH_SOURCE = "crustdata.company_enrich"
_GET_CACHE = (
    "SELECT payload FROM entity_cache WHERE entity_id = %s AND data_type = %s "
    "AND expires_at > now()"
)
PUT_CACHE = (
    "INSERT INTO entity_cache (entity_id, data_type, payload, expires_at) "
    "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) "
    "ON CONFLICT (entity_id, data_type) DO UPDATE "
    "SET payload = EXCLUDED.payload, expires_at = EXCLUDED.expires_at"
)
UPDATE_DATA = "UPDATE accounts SET data = data || %s WHERE id = %s"


def run_spec(job: dict[str, Any]) -> RunSpec:
    """The job's RunSpec; an unparsed or invalid spec means no preferences (all defaults)."""
    spec = job.get("spec") or {}
    try:
        return RunSpec.model_validate(spec.get("run_spec", spec))
    except ValidationError:
        log.warning("job spec is not a RunSpec; using defaults")
        return RunSpec()


def run_date(job: dict[str, Any]) -> date:
    created = job.get("created_at")
    if isinstance(created, datetime):
        return created.astimezone(UTC).date()
    return datetime.now(UTC).date()


def crustdata_ids(data: dict[str, Any]) -> list[int]:
    ids = data.get("crustdata_ids") or ([data["crustdata_id"]] if data.get("crustdata_id") else [])
    return [int(i) for i in ids]


def entity_key(crustdata_id: int) -> str:
    return f"crustdata:{crustdata_id}"


def read_cache(key: str) -> dict[str, Any] | None:
    with db.tx() as conn:
        row = conn.execute(_GET_CACHE, (key, ENRICH_DATA_TYPE)).fetchone()
    payload = row["payload"] if row else None
    return payload if isinstance(payload, dict) and "record" in payload else None


def cached_enrich(data: dict[str, Any]) -> dict[str, Any] | None:
    """The account's enrich payload ({record, evidence_id, url, retrieved_at}) if cached."""
    ref = data.get("enrich") or {}
    keys = [ref["entity_key"]] if ref.get("entity_key") else []
    keys += [entity_key(i) for i in crustdata_ids(data)]
    for key in dict.fromkeys(keys):
        payload = read_cache(key)
        if payload is not None:
            return payload
    return None


def source_from(payload: dict[str, Any], path: str, record_date: date | None = None) -> Source:
    """A Source for one field of a cached enrich record (its evidence row was saved once)."""
    return Source(
        url=payload["url"],
        source_name=ENRICH_SOURCE,
        field_path=path,
        record_date=record_date,
        retrieved_at=payload["retrieved_at"],
        evidence_id=payload["evidence_id"],
    )


def fact_value(data: dict[str, Any], key: str) -> Any | None:
    """Value of a verified Fact stored on the account, else None."""
    fact = data.get(key)
    if isinstance(fact, dict) and fact.get("status") == FactStatus.verified.value:
        return fact.get("value")
    return None
