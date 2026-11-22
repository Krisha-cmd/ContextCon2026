"""audit_log writes. Steps buffer entries via Auditor; the runner flushes them in the same
transaction that marks the step done, so a re-run after a crash never duplicates them."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

_INSERT = (
    "INSERT INTO audit_log (job_id, step, tbl, record_id, before, after, reason, evidence_ids) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)


@dataclass(frozen=True)
class AuditEntry:
    tbl: str
    record_id: str
    before: Any
    after: Any
    reason: str
    evidence_ids: tuple[str, ...] = ()


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value, dumps=_dumps)


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


def write(conn: Connection, job_id: Any, step: str | None, entry: AuditEntry) -> None:
    conn.execute(
        _INSERT,
        (
            job_id,
            step,
            entry.tbl,
            str(entry.record_id),
            _json(entry.before),
            _json(entry.after),
            entry.reason,
            list(entry.evidence_ids),
        ),
    )


@dataclass
class Auditor:
    """Buffers audit rows for one step run: call it like audit(tbl, record_id, ...)."""

    entries: list[AuditEntry] = field(default_factory=list)

    def __call__(
        self,
        tbl: str,
        record_id: Any,
        before: Any,
        after: Any,
        reason: str,
        evidence_ids: Sequence[str] = (),
    ) -> None:
        if not reason:
            raise ValueError("audit entries need a reason")
        self.entries.append(
            AuditEntry(tbl, str(record_id), before, after, reason, tuple(evidence_ids))
        )

    def flush(self, conn: Connection, job_id: Any, step: str | None) -> int:
        for entry in self.entries:
            write(conn, job_id, step, entry)
        n = len(self.entries)
        self.entries.clear()
        return n


def audit(
    conn: Connection,
    job_id: Any,
    step: str | None,
    tbl: str,
    record_id: Any,
    before: Any,
    after: Any,
    reason: str,
    evidence_ids: Sequence[str] = (),
) -> None:
    """Write one audit row now (inside the caller's transaction)."""
    write(conn, job_id, step, AuditEntry(tbl, str(record_id), before, after, reason,
                                         tuple(evidence_ids)))  # fmt: skip
