"""Normalisation for API data: unknown values, field paths, field-level dates, domains/URLs.

Rule: 0, 0.0, "", [], {} and None from an API mean "unknown", never a fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def is_unknown(value: Any) -> bool:
    """True for None, "", empty containers and numeric zero. Booleans are real values."""
    if value is None or isinstance(value, bool):
        return value is None
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def known(value: Any) -> Any | None:
    return None if is_unknown(value) else value


def field_path(root: str, *parts: str | int) -> str:
    """field_path("enrich", "news", 2, "title") -> "enrich.news[2].title"."""
    out = root
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


def get_path(obj: Any, path: str) -> Any | None:
    """Read "news[2].title" from a record. Missing or unknown values return None."""
    cur = obj
    for name, index in _PATH_TOKEN.findall(path):
        if index:
            i = int(index)
            if not isinstance(cur, list) or i >= len(cur):
                return None
            cur = cur[i]
        elif isinstance(cur, dict):
            cur = cur.get(name)
        else:
            return None
    return known(cur)


def parse_date(value: Any) -> date | None:
    """Accept "2026-11-17", "2026-11-17T10:49:30", "2024-12-01 00:00:00", or epoch seconds."""
    if is_unknown(value):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


@dataclass(frozen=True)
class DatedValue:
    """A value with the date the API says it applies to, and where it came from."""

    path: str
    record_date: date
    value: Any


def news_items(record: dict[str, Any]) -> list[DatedValue]:
    """Enrich `news[]` entries that have a publish date; value is the article dict."""
    out = []
    for i, item in enumerate(record.get("news") or []):
        d = parse_date(item.get("article_publish_date")) if isinstance(item, dict) else None
        if d is not None:
            out.append(DatedValue(f"news[{i}]", d, item))
    return out


def job_date_added(job: dict[str, Any]) -> date | None:
    return parse_date(get_path(job, "metadata.date_added"))


def latest_headcount(record: dict[str, Any]) -> DatedValue | None:
    """Most recent headcount.timeseries point with a known employee_count."""
    best: DatedValue | None = None
    for i, point in enumerate(get_path(record, "headcount.timeseries") or []):
        d = parse_date(point.get("date")) if isinstance(point, dict) else None
        count = known(point.get("employee_count")) if isinstance(point, dict) else None
        if d is not None and count is not None and (best is None or d > best.record_date):
            best = DatedValue(f"headcount.timeseries[{i}].employee_count", d, count)
    return best


def normalize_domain(value: str) -> str:
    """'https://www.Delhivery.com/about' -> 'delhivery.com'."""
    text = value.strip().lower()
    host = urlsplit(text if "//" in text else f"//{text}").hostname or ""
    return host.removeprefix("www.")


GOOGLE_HOSTS = ("google.com", "www.google.com")
GOOGLE_TARGET_PARAMS = ("q", "url")


def resolve_google_redirect(url: str) -> str | None:
    """Return the real target of a Google redirect URL.

    - Non-Google URLs come back unchanged.
    - google.com/url?q=<http...> (or ?url=<http...>) -> the target URL.
    - Opaque redirects such as google.com/goto?url=CAES... carry no readable target -> None.
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in GOOGLE_HOSTS:
        return url
    query = parse_qs(parts.query)
    for key in GOOGLE_TARGET_PARAMS:
        for candidate in query.get(key, []):
            if candidate.startswith(("http://", "https://")):
                return candidate
    return None
