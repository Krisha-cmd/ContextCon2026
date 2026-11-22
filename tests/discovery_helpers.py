"""Shared builders for discovery tests."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime
from typing import Any

from app.discovery.models import Candidate, Door, Signal
from app.evidence.models import Source
from app.spec.models import TriggerType

_n = itertools.count()


def src(**kw: Any) -> Source:
    base = {
        "url": "https://api.crustdata.com/company/search",
        "source_name": "crustdata.company_search",
        "field_path": "companies[0]",
        "retrieved_at": datetime(2026, 11, 20, tzinfo=UTC),
        "evidence_id": f"ev_{next(_n)}",
    }
    return Source(**{**base, **kw})


def cand(name: str, door: Door = Door.search, **kw: Any) -> Candidate:
    key = kw.pop("key", f"{door}:test:{next(_n)}")
    return Candidate(key=key, door=door, name=name, sources=[src()], **kw)


def signal(d: date | None, t: TriggerType = TriggerType.supply_chain_hiring) -> Signal:
    return Signal(type=t, date=d, title="Warehouse Manager", url="https://x/j/1", source=src())


class FakeSave:
    """Stands in for app.evidence.store.save_evidence; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, raw: Any, url: str, source_name: str, **kw: Any) -> Source:
        self.calls.append({"raw": raw, "url": url, "source_name": source_name, **kw})
        locator = {k: kw[k] for k in ("field_path", "quote") if kw.get(k)}
        return src(url=url, source_name=source_name, record_date=kw.get("record_date"),
                   **{"field_path": None, **locator})  # fmt: skip
