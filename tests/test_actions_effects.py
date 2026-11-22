"""Side effects without a database: event ids, check-before-act, 409 / already-sent, sink."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.actions.effects import IDEM_HEADER, Effects, Sink, event_body, footer
from app.actions.models import (
    CalendarPayload,
    calendar_event_id,
    idem_token,
    make_key,
    validate_payload,
)
from app.config import DEFAULT_CONFIG_PATH, Settings, load_settings
from tests.actions_helpers import FakeGoogle

S = load_settings(DEFAULT_CONFIG_PATH)
KEY = make_key(
    "calendar", "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
)
CAL = {
    "summary": "Intro: Acme x 3PL",
    "description": "15 minutes on your new Bhiwandi warehouse",
    "start": "2026-11-25T10:00:00+05:30",
    "end": "2026-11-25T10:30:00+05:30",
    "attendees": ["head.scm@acmefoods.in"],
}
MAIL = {"to": ["head.scm@acmefoods.in"], "subject": "Your new warehouse", "body": "Hi Priya,"}


def settings(tmp_path: Path, **actions: str) -> Settings:
    a = S.actions.model_copy(update={"sink_path": str(tmp_path / "sink.jsonl"), **actions})
    return S.model_copy(update={"actions": a})


def action(kind: str, payload: dict, key: str | None = None) -> dict:
    return {
        "idem_key": key or make_key(kind, "job-1", "acct-1"),
        "job_id": "job-1",
        "account_id": "acct-1",
        "type": kind,
        "payload": validate_payload(kind, payload),
    }


# --- ids and payloads ---------------------------------------------------------------------


def test_calendar_event_id_is_base32hex_of_the_key() -> None:
    eid = calendar_event_id(KEY)
    assert re.fullmatch(r"[a-v0-9]{5,1024}", eid)
    assert calendar_event_id(KEY) == eid  # deterministic
    padded = eid.upper() + "=" * (-len(eid) % 8)
    assert base64.b32hexdecode(padded).decode() == KEY
    with pytest.raises(ValueError, match="length"):
        calendar_event_id("ab")


def test_payloads_are_validated() -> None:
    with pytest.raises(ValidationError, match="not email"):
        validate_payload("email", {**MAIL, "to": ["not-an-email"]})
    with pytest.raises(ValidationError, match="end must be after start"):
        validate_payload("calendar", {**CAL, "end": CAL["start"]})
    with pytest.raises(ValidationError):
        validate_payload("calendar", {**CAL, "extra_attendees": ["x@y.in"]})


def test_attendees_come_only_from_the_payload() -> None:
    body = event_body(KEY, CalendarPayload.model_validate(CAL), "Asia/Kolkata")
    assert body["attendees"] == [{"email": "head.scm@acmefoods.in"}]
    assert body["id"] == calendar_event_id(KEY)
    assert body["start"]["timeZone"] == "Asia/Kolkata"


# --- calendar (google mode) ---------------------------------------------------------------


async def test_calendar_insert_then_409_is_done(tmp_path: Path) -> None:
    g = FakeGoogle()
    fx = Effects(settings(tmp_path, calendar_mode="google"), call=g)
    a = action("calendar", CAL)
    first, second = await fx.run(a), await fx.run(a)
    assert first.status == "done" and first.result["status"] == "created"
    assert second.status == "done" and second.result["status"] == "already_existed"
    assert len(g.events) == 1 and g.posts("gcal:events.insert") == 2
    call = g.calls[0]
    assert call["params"] == {"sendUpdates": "none"} and call["data_type"] == "actions"


@pytest.mark.parametrize(
    ("forced", "status"),
    [(400, "failed"), (403, "failed"), (503, "retry"), (429, "retry"), ("transport", "retry")],
)
async def test_calendar_errors(tmp_path: Path, forced: object, status: str) -> None:
    g = FakeGoogle()
    g.fail["gcal:events.insert"] = forced
    out = await Effects(settings(tmp_path, calendar_mode="google"), call=g).run(
        action("calendar", CAL)
    )
    assert out.status == status and g.events == {}


# --- email (gmail mode) -------------------------------------------------------------------


async def test_gmail_sends_with_key_in_header_message_id_and_footer(tmp_path: Path) -> None:
    g = FakeGoogle()
    a = action("email", MAIL)
    out = await Effects(settings(tmp_path, email_mode="gmail"), call=g).run(a)
    assert out.status == "done" and out.result["status"] == "sent"
    [msg] = g.sent
    assert msg[IDEM_HEADER] == a["idem_key"]
    assert msg["Message-ID"] == f"<{idem_token(a['idem_key'])}@{S.actions.message_id_domain}>"
    assert msg["To"] == "head.scm@acmefoods.in"
    assert footer(a["idem_key"]).strip() in msg.get_content()
    # checked the sent folder before sending
    assert [c["endpoint"] for c in g.calls] == ["gmail:messages.list", "gmail:messages.send"]


async def test_gmail_restart_finds_it_in_sent_and_does_not_resend(tmp_path: Path) -> None:
    g = FakeGoogle()
    fx = Effects(settings(tmp_path, email_mode="gmail"), call=g)
    a = action("email", MAIL)
    await fx.run(a)  # sent, then "crash" before mark-done
    again = await fx.run(a)
    assert again.status == "done" and again.result["status"] == "already_sent"
    assert len(g.sent) == 1 and g.posts("gmail:messages.send") == 1


async def test_gmail_never_sends_blind_when_the_check_fails(tmp_path: Path) -> None:
    g = FakeGoogle()
    g.fail["gmail:messages.list"] = 500
    out = await Effects(settings(tmp_path, email_mode="gmail"), call=g).run(action("email", MAIL))
    assert out.status == "retry" and g.sent == [] and g.posts("gmail:messages.send") == 0


@pytest.mark.parametrize(
    ("forced", "status"), [(400, "failed"), (500, "retry"), ("transport", "retry")]
)
async def test_gmail_send_errors(tmp_path: Path, forced: object, status: str) -> None:
    g = FakeGoogle()
    g.fail["gmail:messages.send"] = forced
    out = await Effects(settings(tmp_path, email_mode="gmail"), call=g).run(action("email", MAIL))
    assert out.status == status


# --- sink (test mode) ---------------------------------------------------------------------


async def test_email_sink_delivers_once_to_the_sink_address(tmp_path: Path) -> None:
    s = settings(tmp_path)  # repo default: email_mode=sink, calendar_mode=sink
    fx = Effects(s)
    a = action("email", MAIL)
    first, again = await fx.run(a), await fx.run(a)
    assert first.result["status"] == "sent" and again.result["status"] == "already_sent"
    [entry] = Sink(tmp_path / "sink.jsonl").entries()
    assert entry["delivered_to"] == s.actions.sink_address
    assert entry["intended_to"] == MAIL["to"]
    assert entry["headers"][IDEM_HEADER] == a["idem_key"]
    assert a["idem_key"] in entry["body"]


async def test_calendar_sink_once(tmp_path: Path) -> None:
    fx = Effects(settings(tmp_path))
    a = action("calendar", CAL)
    assert (await fx.run(a)).result["status"] == "created"
    assert (await fx.run(a)).result["status"] == "already_existed"
    assert len(Sink(tmp_path / "sink.jsonl").entries()) == 1


def test_repo_defaults_are_safe() -> None:
    assert S.actions.email_mode == "sink" and S.actions.calendar_mode == "sink"
    assert S.gmail.max_attempts == 1 and S.gmail.retry_statuses == ()
    assert S.actions.calendar_send_updates == "none"
    assert S.cache_ttl_hours.actions == 0
