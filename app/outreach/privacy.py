"""Personal-data checks shared by drafting and reflect(): known names, emails, phones, profiles.

Names come from the records we already hold (buyer name, company and legal name), so the check
is a plain regex against them, never a guess about what "looks like" a name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\w)\+?\d[\d\s-]{8,}\d(?!\w)")
PROFILE_URL = re.compile(r"linkedin\.com/in/", re.IGNORECASE)
MIN_TOKEN = 3  # shorter name parts ("A", "Li") are too ambiguous to block on
NAME_KEYS = ("buyer_name", "company", "account_name", "legal_name")
PERSON_KEYS = ("buyer_name",)


def names_from_meta(metas: Iterable[dict[str, Any]]) -> set[str]:
    """Full names plus, for people, each name part; for companies the first word."""
    out: set[str] = set()
    for meta in metas:
        for key in NAME_KEYS:
            value = (meta or {}).get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            out.add(value.strip())
            parts = value.split()
            picked = parts if key in PERSON_KEYS else parts[:1]
            out.update(p for p in picked if len(p) >= MIN_TOKEN)
    return out


def names_in(text: str, names: Iterable[str]) -> list[str]:
    found = []
    for name in sorted(set(names), key=len, reverse=True):
        words = r"\s+".join(re.escape(w) for w in name.split())
        if re.search(rf"(?<!\w){words}(?!\w)", text, re.IGNORECASE):
            found.append(name)
    return found


def personal_data_in(text: str) -> list[str]:
    hits = []
    if EMAIL.search(text):
        hits.append("an email address")
    if PHONE.search(text):
        hits.append("a phone number")
    if PROFILE_URL.search(text):
        hits.append("a personal profile URL")
    return hits
