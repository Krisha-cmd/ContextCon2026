"""Discovery contracts: Candidate (one record from one door), Signal, Cluster (deduped company)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.evidence.models import Source
from app.spec.models import TriggerType


class Door(StrEnum):
    search = "search"  # door 1: company search by RunSpec filters
    jobs = "jobs"  # door 2: recent supply-chain job posts
    news = "news"  # door 2: recent new-warehouse news


class Signal(BaseModel):
    """A possible trigger seen during discovery. date=None means not dated yet: it does not
    count as a trigger until a later step verifies it inside the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: TriggerType
    date: date | None
    title: str
    url: str | None
    source: Source


class Candidate(BaseModel):
    """One company record as one door returned it. Only search fields; nothing verified yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str  # stable within a job: door + request hash + position
    door: Door
    name: str
    domain: str | None = None
    crustdata_id: int | None = None
    city: str | None = None
    state: str | None = None
    headcount: int | None = None
    industries: list[str] = Field(default_factory=list)
    gstin: str | None = None
    needs_identity: bool = False
    signals: list[Signal] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


class Cluster(BaseModel):
    """Candidates that are the same company. `members` keeps every record so all evidence
    survives; the flat fields are read from the best member (see dedup.canonical)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    members: list[Candidate]
    edges: list[str] = Field(default_factory=list)  # edge kinds that joined the members
    held: bool = False
    hold_reason: str | None = None
    possible_duplicate_of: list[str] = Field(default_factory=list)  # other cluster ids

    def first(self, attr: str) -> object | None:
        """First known value of `attr` across members (members are best-first)."""
        for m in self.members:
            value = getattr(m, attr)
            if value is not None:
                return value
        return None

    @property
    def name(self) -> str:
        return self.members[0].name

    @property
    def domain(self) -> str | None:
        return self.first("domain")  # type: ignore[return-value]

    @property
    def crustdata_id(self) -> int | None:
        return self.first("crustdata_id")  # type: ignore[return-value]

    @property
    def crustdata_ids(self) -> list[int]:
        return sorted({m.crustdata_id for m in self.members if m.crustdata_id is not None})

    @property
    def needs_identity(self) -> bool:
        return self.crustdata_id is None and self.domain is None

    @property
    def signals(self) -> list[Signal]:
        return [s for m in self.members for s in m.signals]
