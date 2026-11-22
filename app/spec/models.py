"""Shared run/account contracts: RunSpec (what the client asked for) and Account (what we found)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import Limits, Settings
from app.evidence.models import Fact, cannot_verify

# Only spend/time caps may be overridden, and only downwards (see RunSpec.effective_limits).
# Conversion rates such as credit_inr are not caps and stay config-only.
_OVERRIDABLE_PREFIX = "max_"
NOT_CHECKED = "not checked yet"


class TriggerType(StrEnum):
    new_warehouse = "new_warehouse"
    expansion = "expansion"
    funding = "funding"
    leadership_hire = "leadership_hire"
    supply_chain_hiring = "supply_chain_hiring"
    new_market = "new_market"


class Decision(StrEnum):
    proceed = "proceed"
    held = "held"
    dropped = "dropped"


def overridable_limits() -> set[str]:
    return {name for name in Limits.model_fields if name.startswith(_OVERRIDABLE_PREFIX)}


def _normalize_domain(domain: str) -> str:
    d = domain.strip().lower().removeprefix("https://").removeprefix("http://")
    return d.removeprefix("www.").split("/", 1)[0]


class RunSpec(BaseModel):
    """A parsed client request. The LLM may fill it; plain code decides what it costs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    industries: list[str] = Field(default_factory=list)
    geo_states: list[str] = Field(default_factory=list)
    size_min: int | None = Field(default=None, ge=0)
    size_max: int | None = Field(default=None, ge=0)
    trigger_types: list[TriggerType] = Field(default_factory=list)
    buyer_titles: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    max_accounts: int | None = Field(default=None, gt=0)
    limits: dict[str, float] = Field(default_factory=dict)
    free_text_notes: str | None = None

    @field_validator("exclude_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return sorted({_normalize_domain(d) for d in v if d.strip()})

    @field_validator("limits")
    @classmethod
    def _limits(cls, v: dict[str, float]) -> dict[str, float]:
        allowed = overridable_limits()
        bad = set(v) - allowed
        if bad:
            raise ValueError(f"unknown or non-overridable limits {sorted(bad)}; allowed {allowed}")
        if any(x <= 0 for x in v.values()):
            raise ValueError("limit overrides must be positive")
        return v

    @model_validator(mode="after")
    def _size_range(self) -> Self:
        sizes_set = self.size_min is not None and self.size_max is not None
        if sizes_set and self.size_min > self.size_max:
            raise ValueError("size_min must be <= size_max")
        return self

    def effective_limits(self, settings: Settings) -> Limits:
        """Config limits with this spec's overrides applied. An override can only tighten a
        cap, never raise it, so a parsed request can't grant itself more budget."""
        base = settings.limits.model_dump()
        merged = {k: min(base[k], v) for k, v in self.limits.items()}
        return Limits(**{**base, **merged})


def _unchecked() -> Fact[Any]:
    return cannot_verify(NOT_CHECKED)


class Account(BaseModel):
    """One candidate company. Every claim about it is a Fact; unchecked facts default to
    cannot_verify so nothing is ever implicitly 'true'."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID
    name: str
    domain: str | None = None
    crustdata_id: str | None = None
    legal_name: Fact[str] = Field(default_factory=_unchecked)
    gstin: Fact[str] = Field(default_factory=_unchecked)
    cin: Fact[str] = Field(default_factory=_unchecked)
    status_active: Fact[bool] = Field(default_factory=_unchecked)
    adverse_news: Fact[str] = Field(default_factory=_unchecked)
    trigger: Fact[dict[str, Any]] = Field(default_factory=_unchecked)
    buyer: Fact[dict[str, Any]] = Field(default_factory=_unchecked)
    score: float | None = None
    score_reasons: list[str] = Field(default_factory=list)
    decision: Decision | None = None
    decision_reason: str | None = None

    @model_validator(mode="after")
    def _decision_has_reason(self) -> Self:
        if self.decision is not None and not (self.decision_reason or "").strip():
            raise ValueError("a decision needs a decision_reason")
        return self
