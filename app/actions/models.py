"""Outbox states, action payloads and idempotency helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class State(StrEnum):
    pending_approval = "pending_approval"
    approved = "approved"
    executing = "executing"
    done = "done"
    failed = "failed"
    rejected = "rejected"


TERMINAL = frozenset({State.done, State.failed, State.rejected})


class ActionType(StrEnum):
    calendar = "calendar"
    email = "email"


_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
CAL_ID_MIN, CAL_ID_MAX = 5, 1024  # Google Calendar client-provided event id length bounds
IDEM_TOKEN_LEN = 24


def _emails(values: list[str]) -> list[str]:
    out = [v.strip() for v in values]
    bad = [v for v in out if not _EMAIL.match(v)]
    if bad:
        raise ValueError(f"not email addresses: {bad}")
    if not out:
        raise ValueError("at least one recipient is required")
    return out


class CalendarPayload(BaseModel):
    """Exactly what the approver saw. Attendees come only from here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1)
    description: str = ""
    start: AwareDatetime
    end: AwareDatetime
    timezone: str | None = None
    attendees: list[str]

    _check_attendees = field_validator("attendees")(lambda cls, v: _emails(v))

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class EmailPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    to: list[str]
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)

    _check_to = field_validator("to")(lambda cls, v: _emails(v))


PAYLOADS: dict[ActionType, type[BaseModel]] = {
    ActionType.calendar: CalendarPayload,
    ActionType.email: EmailPayload,
}


def validate_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return PAYLOADS[ActionType(kind)].model_validate(payload).model_dump(mode="json")


def make_key(kind: str, job_id: Any, account_id: Any) -> str:
    """One meeting / one email per (job, account): the same proposal can't be sent twice."""
    return f"{ActionType(kind).value}:{job_id}:{account_id}"


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def calendar_event_id(idem_key: str) -> str:
    """base32hex of the key, lowercase, no padding: only a-v and 0-9, as Calendar requires."""
    out = base64.b32hexencode(idem_key.encode()).decode().rstrip("=").lower()
    if not CAL_ID_MIN <= len(out) <= CAL_ID_MAX:
        raise ValueError(f"calendar event id length {len(out)} outside {CAL_ID_MIN}-{CAL_ID_MAX}")
    return out


def idem_token(idem_key: str) -> str:
    """A short search-friendly token for the key (Gmail search splits on ':' and '-')."""
    return "idem" + hashlib.sha256(idem_key.encode()).hexdigest()[:IDEM_TOKEN_LEN]
