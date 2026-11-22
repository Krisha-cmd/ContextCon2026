"""StepContext: what a step function receives. Side effects are buffered, not written."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from app.config import Settings
from app.runner.audit import Auditor

ACTIVE, DROPPED, HELD = "active", "dropped", "held"


@dataclass(frozen=True)
class NewAccount:
    name: str | None
    domain: str | None
    entity_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Disposition:
    account_id: Any
    status: str  # DROPPED | HELD
    reason: str
    evidence_ids: tuple[str, ...] = ()


@dataclass
class StepContext:
    """job/account are DB rows (dicts). `db` and `gateway` are for reads and outside calls;
    every write a step wants goes through the buffers below and is committed with the step."""

    job: dict[str, Any]
    account: dict[str, Any] | None
    config: Settings
    gateway: ModuleType
    db: ModuleType
    audit: Auditor = field(default_factory=Auditor)
    new_accounts: list[NewAccount] = field(default_factory=list)
    dispositions: list[Disposition] = field(default_factory=list)

    @property
    def job_id(self) -> Any:
        return self.job["id"]

    @property
    def account_id(self) -> Any | None:
        return None if self.account is None else self.account["id"]

    def add_account(self, name: str | None, domain: str | None, entity_id: str | None = None,
                    data: dict[str, Any] | None = None) -> None:  # fmt: skip
        self.new_accounts.append(NewAccount(name, domain, entity_id, data or {}))

    def set_account_status(
        self, account_id: Any, status: str, reason: str, evidence_ids: tuple[str, ...] = ()
    ) -> None:
        if status not in (DROPPED, HELD):
            raise ValueError(f"status must be {DROPPED!r} or {HELD!r}")
        if not reason:
            raise ValueError("a drop/hold needs a reason")
        self.dispositions.append(Disposition(account_id, status, reason, evidence_ids))

    def drop(self, reason: str, evidence_ids: tuple[str, ...] = ()) -> None:
        """Drop the current account: later steps skip it."""
        self.set_account_status(self._require_account(), DROPPED, reason, evidence_ids)

    def hold(self, reason: str, evidence_ids: tuple[str, ...] = ()) -> None:
        """Hold the current account for a person: later steps skip it."""
        self.set_account_status(self._require_account(), HELD, reason, evidence_ids)

    def _require_account(self) -> Any:
        if self.account is None:
            raise ValueError("drop()/hold() need a per-account step; use set_account_status")
        return self.account["id"]
