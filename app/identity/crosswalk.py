"""entity_keys crosswalk: domain / crustdata_id / GSTIN / CIN / PAN / legal_name -> entity_id.

A hit reuses an earlier proof at no cost: the stored keys come back with the evidence rows
that proved them, so the facts keep real Sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from app.evidence.models import Source

RESOLVED_BY = "identity:rule_ladder"
KEY_DOMAIN, KEY_CRUSTDATA, KEY_GSTIN, KEY_CIN, KEY_PAN, KEY_LEGAL_NAME = (
    "domain",
    "crustdata_id",
    "gstin",
    "cin",
    "pan",
    "legal_name",
)
LOOKUP_TYPES = (KEY_DOMAIN, KEY_CRUSTDATA, KEY_GSTIN, KEY_CIN)

_FIND = """
SELECT DISTINCT entity_id FROM entity_keys
WHERE (key_type, key_value) IN (SELECT * FROM unnest(%s::text[], %s::text[]))
"""
_KEYS = """
SELECT k.key_type, k.key_value, e.id, e.url, e.field_path, e.quote, e.record_date,
       e.retrieved_at, e.source_name
FROM entity_keys k LEFT JOIN evidence e ON e.id = k.evidence_id
WHERE k.entity_id = %s ORDER BY k.key_type, k.key_value
"""
INSERT_KEY = """
INSERT INTO entity_keys (entity_id, key_type, key_value, evidence_id, resolved_by)
VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key_type, key_value) DO NOTHING
"""


@dataclass(frozen=True)
class StoredKey:
    key_type: str
    key_value: str
    source: Source | None


@dataclass
class CrosswalkHit:
    entity_ids: list[str]
    keys: list[StoredKey] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return len(self.entity_ids) > 1

    def first(self, key_type: str) -> StoredKey | None:
        return next((k for k in self.keys if k.key_type == key_type), None)


def _source(row: dict) -> Source | None:
    if row["id"] is None or not (row["field_path"] or row["quote"]):
        return None
    rd = row["record_date"]
    return Source(
        url=row["url"] or "",
        source_name=row["source_name"],
        field_path=row["field_path"],
        quote=row["quote"],
        record_date=rd.date() if rd else None,
        retrieved_at=row["retrieved_at"],
        evidence_id=row["id"],
    )


def lookup(conn: psycopg.Connection, pairs: list[tuple[str, str]]) -> CrosswalkHit | None:
    """Entity ids any of the (key_type, key_value) pairs point at, with that entity's keys."""
    if not pairs:
        return None
    types, values = [t for t, _ in pairs], [v for _, v in pairs]
    ids = sorted(r["entity_id"] for r in conn.execute(_FIND, (types, values)).fetchall())
    if not ids:
        return None
    hit = CrosswalkHit(ids)
    if not hit.ambiguous:
        rows = conn.execute(_KEYS, (ids[0],)).fetchall()
        hit.keys = [StoredKey(r["key_type"], r["key_value"], _source(r)) for r in rows]
    return hit
