"""The side effects themselves. Each one checks first whether it already happened, so a
restart after "side effect done, mark-done lost" never repeats it:

- Calendar: the event id is derived from the idem_key; a second insert gets 409 -> done.
- Email: the key is in an X-Idempotency-Key header, the Message-ID and the body footer; the
  sent folder (gmail) or the sink log (sink) is searched before sending.

Outcome.retry means "not known to have happened": the action stays executing and is
re-claimed after the lease, when the check-first path runs again.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx

from app.actions.models import (
    ActionType,
    CalendarPayload,
    EmailPayload,
    calendar_event_id,
    idem_token,
)
from app.config import ROOT, Settings
from app.gateway import GatewayError, GatewayResponse
from app.log import get_logger

log = get_logger(__name__)

IDEM_HEADER = "X-Idempotency-Key"
DATA_TYPE = "actions"  # cache_ttl_hours.actions = 0: never served from cache
CallFn = Callable[..., Awaitable[GatewayResponse]]


@dataclass(frozen=True)
class Outcome:
    status: str  # "done" | "failed" | "retry"
    result: dict[str, Any] | None = None
    error: str | None = None


def done(**result: Any) -> Outcome:
    return Outcome("done", result)


def failed(error: str) -> Outcome:
    return Outcome("failed", None, error)


def retry(error: str) -> Outcome:
    return Outcome("retry", None, error)


def _from_status(resp: GatewayResponse, what: str) -> Outcome | None:
    """Shared handling of non-success statuses. None = caller handles (2xx, or 409 for calendar)."""
    if resp.status == httpx.codes.TOO_MANY_REQUESTS or resp.status >= 500:
        return retry(f"{what}: HTTP {resp.status}")
    if resp.status >= 400:
        return failed(f"{what}: HTTP {resp.status}: {str(resp.body)[:300]}")
    return None


# ---- sink (test mode) ---------------------------------------------------------------------
def _lock_file(fh: Any) -> None:
    """Exclusive lock until the file is closed: flock on POSIX, msvcrt on Windows."""
    try:
        import fcntl
    except ImportError:  # Windows: lock one byte at the start; released when fh closes
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        fh.seek(0, 2)
        return
    fcntl.flock(fh, fcntl.LOCK_EX)


class Sink:
    """Append-only JSONL log standing in for the outside world in test mode."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(x) for x in lines if x.strip()]

    def find(self, kind: str, key: str) -> dict[str, Any] | None:
        return next((e for e in self.entries() if e["kind"] == kind and e["key"] == key), None)

    def append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            _lock_file(fh)
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()


# ---- builders -----------------------------------------------------------------------------
def event_body(key: str, p: CalendarPayload, default_tz: str) -> dict[str, Any]:
    tz = p.timezone or default_tz
    return {
        "id": calendar_event_id(key),
        "summary": p.summary,
        "description": p.description,
        "start": {"dateTime": p.start.isoformat(), "timeZone": tz},
        "end": {"dateTime": p.end.isoformat(), "timeZone": tz},
        "attendees": [{"email": a} for a in p.attendees],  # only the approved list
        "extendedProperties": {"private": {"idem_key": key}},
    }


def footer(key: str) -> str:
    return f"\n\n--\nRef: {key} [{idem_token(key)}]"


def build_email(key: str, p: EmailPayload, to: list[str], domain: str) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["Subject"] = p.subject
    msg[IDEM_HEADER] = key
    msg["Message-ID"] = f"<{idem_token(key)}@{domain}>"
    msg.set_content(p.body + footer(key))
    return msg


def sent_query(key: str, domain: str) -> str:
    return f"in:sent (rfc822msgid:{idem_token(key)}@{domain} OR {idem_token(key)})"


# ---- effects ------------------------------------------------------------------------------
class Effects:
    def __init__(
        self, settings: Settings, call: CallFn | None = None, sink: Sink | None = None
    ) -> None:
        self.s = settings
        self.a = settings.actions
        self.call = call
        sink_path = Path(self.a.sink_path)
        self.sink = sink or Sink(sink_path if sink_path.is_absolute() else ROOT / sink_path)

    async def run(self, action: dict[str, Any]) -> Outcome:
        kind = ActionType(action["type"])
        try:
            if kind is ActionType.calendar:
                return await self.calendar(action)
            return await self.email(action)
        except GatewayError as exc:  # transport failure: unknown whether it happened
            return retry(f"{kind}: {exc}")

    def _call(self) -> CallFn:
        if self.call is None:
            raise RuntimeError("Google mode needs a gateway call function")
        return self.call

    # -- calendar --
    async def calendar(self, action: dict[str, Any]) -> Outcome:
        key = action["idem_key"]
        p = CalendarPayload.model_validate(action["payload"])
        body = event_body(key, p, self.a.default_timezone)
        if self.a.calendar_mode == "sink":
            return self._calendar_sink(key, body)
        resp = await self._call()(
            job_id=action["job_id"],
            endpoint="gcal:events.insert",
            method="POST",
            url=f"{self.s.gcal.base_url}/calendars/{self.a.calendar_id}/events",
            params={"sendUpdates": self.a.calendar_send_updates},
            json=body,
            data_type=DATA_TYPE,
            est_credits=0,
        )
        if resp.status == httpx.codes.CONFLICT:
            return done(event_id=body["id"], status="already_existed")
        bad = _from_status(resp, "calendar insert")
        if bad:
            return bad
        link = resp.body.get("htmlLink") if isinstance(resp.body, dict) else None
        return done(event_id=body["id"], status="created", html_link=link)

    def _calendar_sink(self, key: str, body: dict[str, Any]) -> Outcome:
        if self.sink.find("calendar", key):
            return done(event_id=body["id"], status="already_existed")  # the sink's "409"
        self.sink.append(
            {"kind": "calendar", "key": key, "event": body, "at": datetime.now(UTC).isoformat()}
        )
        return done(event_id=body["id"], status="created")

    # -- email --
    async def email(self, action: dict[str, Any]) -> Outcome:
        key = action["idem_key"]
        p = EmailPayload.model_validate(action["payload"])
        domain = self.a.message_id_domain
        if self.a.email_mode == "sink":
            return self._email_sink(key, p, domain)
        call = self._call()
        check = await call(
            job_id=action["job_id"],
            endpoint="gmail:messages.list",
            method="GET",
            url=f"{self.s.gmail.base_url}/users/me/messages",
            params={"q": sent_query(key, domain), "maxResults": 1},
            data_type=DATA_TYPE,
            est_credits=0,
        )
        if check.status != httpx.codes.OK:  # can't prove it wasn't sent: never send blind
            return retry(f"sent-folder check: HTTP {check.status}")
        found = (check.body or {}).get("messages") or []
        if found:
            return done(message_id=found[0].get("id"), status="already_sent")
        raw = base64.urlsafe_b64encode(build_email(key, p, p.to, domain).as_bytes()).decode()
        resp = await call(
            job_id=action["job_id"],
            endpoint="gmail:messages.send",
            method="POST",
            url=f"{self.s.gmail.base_url}/users/me/messages/send",
            json={"raw": raw},
            data_type=DATA_TYPE,
            est_credits=0,
        )
        bad = _from_status(resp, "gmail send")
        if bad:
            return bad
        return done(message_id=(resp.body or {}).get("id"), status="sent")

    def _email_sink(self, key: str, p: EmailPayload, domain: str) -> Outcome:
        if self.sink.find("email", key):
            return done(status="already_sent")
        msg = build_email(key, p, [self.a.sink_address], domain)
        self.sink.append(
            {
                "kind": "email",
                "key": key,
                "intended_to": p.to,
                "delivered_to": self.a.sink_address,
                "subject": p.subject,
                "headers": dict(msg.items()),
                "body": msg.get_content(),
                "at": datetime.now(UTC).isoformat(),
            }
        )
        return done(status="sent", delivered_to=self.a.sink_address)
