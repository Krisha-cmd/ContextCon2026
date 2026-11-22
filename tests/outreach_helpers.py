"""Write a drafted email the way the draft step does (outbox action + drafts row)."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.outreach.draft import INSERT_ACTION, INSERT_DRAFT, action_key, draft_text

SUBJECT = "Your new Pune warehouse"


def queue_draft(c: psycopg.Connection, job: Any, n: Any, body: str,
                account_id: Any = None) -> str:  # fmt: skip
    key = action_key(job, account_id or f"acc-{n}")
    context = {
        "subject": SUBJECT,
        "segment": "consumer goods",
        "embed_text": f"Consumer Goods | new_warehouse | Head of Warehousing {n}",
        "meta": {"buyer_name": "Priya Raman", "company": "Acme Foods"},
        "recipient": {"name": "Priya Raman", "title": "Head of Warehousing",
                      "company": "Acme Foods", "profile_url": "https://p/1"},
        "facts": [{"key": "trigger", "text": "Trigger (new warehouse, 2026-11-16): Acme opens "
                   "new warehouse in Pune", "source": "https://n/1"}],
        "score_reasons": ["+20 new warehouse: Acme opens new warehouse in Pune, 6 days ago "
                          "(source: https://n/1)"],
    }  # fmt: skip
    payload = {"to": [], "subject": SUBJECT, "body": body}
    c.execute(INSERT_ACTION, (key, job, account_id, "email", Jsonb(payload), "pending_approval"))
    c.execute(INSERT_DRAFT, (key, job, account_id, draft_text(SUBJECT, body), Jsonb(context)))
    return key
