"""Shared evidence contracts: Source, FactStatus, Fact[T]. Everything downstream imports these.

Rules enforced here (see CLAUDE.md):
- a verified fact carries a known value and at least one Source;
- a fact we could not verify carries value=None and a `missing` reason;
- a conflict keeps every alternative value with its own Source and picks none of them;
- 0, 0.0, None and empty values are unknown, never facts.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_WS = re.compile(r"\s+")


class Source(BaseModel):
    """Where one fact came from. Points at an `evidence` row via evidence_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    source_name: str
    field_path: str | None = None
    quote: str | None = None
    record_date: date | None = None
    retrieved_at: AwareDatetime
    evidence_id: str

    @model_validator(mode="after")
    def _needs_locator(self) -> Self:
        if not (self.field_path or self.quote):
            raise ValueError("Source needs field_path or quote")
        return self


class FactStatus(StrEnum):
    verified = "verified"
    conflict = "conflict"
    cannot_verify = "cannot_verify"


def is_unknown(value: Any) -> bool:
    """True for values an API uses to mean 'nothing': None, 0, 0.0, '', whitespace, empty
    containers. Booleans are real answers (status_active=False is a fact), so never unknown."""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value == 0
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple | set | dict):
        return len(value) == 0
    return False


class Fact[T](BaseModel):
    """One claim about an account plus the evidence for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: T | None = None
    status: FactStatus
    sources: list[Source] = Field(default_factory=list)
    missing: str | None = None
    stale: bool = False
    alternatives: list[tuple[Any, Source]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_status(self) -> Self:
        if self.status is FactStatus.verified:
            self._check_verified()
        elif self.status is FactStatus.cannot_verify:
            self._check_cannot_verify()
        else:
            self._check_conflict()
        return self

    def _check_verified(self) -> None:
        if is_unknown(self.value):
            raise ValueError("verified fact needs a known value (0/empty/null count as unknown)")
        if not self.sources:
            raise ValueError("verified fact needs at least one source")
        if self.alternatives:
            raise ValueError("only a conflict fact may carry alternatives")

    def _check_cannot_verify(self) -> None:
        if self.value is not None:
            raise ValueError("cannot_verify fact must have value=None")
        if not (self.missing and self.missing.strip()):
            raise ValueError("cannot_verify fact needs a missing reason")
        if self.alternatives:
            raise ValueError("only a conflict fact may carry alternatives")

    def _check_conflict(self) -> None:
        if self.value is not None:
            raise ValueError("conflict fact must not pick a value; keep them in alternatives")
        if len(self.alternatives) < 2:
            raise ValueError("conflict needs at least 2 alternatives")
        values = [v for v, _ in self.alternatives]
        if all(v == values[0] for v in values[1:]):
            raise ValueError("conflict needs alternatives whose values differ")
        alt_ids = {s.evidence_id for _, s in self.alternatives}
        if len(alt_ids) < 2:
            raise ValueError("conflict needs at least 2 distinct sources")
        if not alt_ids <= {s.evidence_id for s in self.sources}:
            raise ValueError("every alternative's source must be listed in sources")


def verified[T](value: T, source: Source | Iterable[Source], *, stale: bool = False) -> Fact[T]:
    sources = [source] if isinstance(source, Source) else list(source)
    return Fact[T](value=value, status=FactStatus.verified, sources=sources, stale=stale)


def cannot_verify(missing: str, sources: Iterable[Source] = ()) -> Fact[Any]:
    """`sources` may record what was checked and came back empty."""
    return Fact[Any](status=FactStatus.cannot_verify, missing=missing, sources=list(sources))


def conflict(values_with_sources: Iterable[tuple[Any, Source]]) -> Fact[Any]:
    alts = list(values_with_sources)
    sources: list[Source] = []
    for _, src in alts:
        if src.evidence_id not in {s.evidence_id for s in sources}:
            sources.append(src)
    return Fact[Any](status=FactStatus.conflict, sources=sources, alternatives=alts)


def _normalize(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


def quote_in_text(quote: str, text: str) -> bool:
    """True when `quote` really appears in `text`, ignoring case and whitespace differences.
    Use it to reject LLM-extracted quotes that are not in the fetched text. Empty quotes fail."""
    q = _normalize(quote)
    return bool(q) and q in _normalize(text)
