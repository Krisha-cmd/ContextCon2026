from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.discovery.normalize import (
    field_path,
    get_path,
    is_unknown,
    job_date_added,
    known,
    latest_headcount,
    news_items,
    normalize_domain,
    parse_date,
    resolve_google_redirect,
)

FIX = Path(__file__).parent / "fixtures" / "crustdata"


def _fixture(name: str):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["response"]


@pytest.mark.parametrize("value", [None, 0, 0.0, "", "  ", [], {}, ()])
def test_unknown_values(value) -> None:
    assert is_unknown(value)
    assert known(value) is None


@pytest.mark.parametrize("value", [1, -2.5, "x", [0], {"a": None}, False, True])
def test_known_values(value) -> None:
    assert not is_unknown(value)
    assert known(value) == value


def test_field_path_and_get_path() -> None:
    rec = {"news": [{"title": "a"}, {"title": ""}, {"title": "c"}], "funding": {"total": 0.0}}
    assert field_path("enrich", "news", 2, "title") == "enrich.news[2].title"
    assert get_path(rec, "news[2].title") == "c"
    assert get_path(rec, "news[1].title") is None  # "" is unknown
    assert get_path(rec, "funding.total") is None  # 0.0 is unknown
    assert get_path(rec, "news[9].title") is None
    assert get_path(rec, "missing.deep[0]") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-17", date(2026, 11, 17)),
        ("2026-11-17T10:49:30", date(2026, 11, 17)),
        ("2024-12-01 00:00:00", date(2024, 12, 1)),
        (1795331191, date(2026, 11, 22)),
        ("", None),
        (None, None),
        (0, None),
        ("not a date", None),
    ],
)
def test_parse_date(raw, expected) -> None:
    assert parse_date(raw) == expected


def test_enrich_field_dates_from_real_response() -> None:
    delhivery = _fixture("company_enrich")[0]["matches"][0]["company_data"]
    news = news_items(delhivery)
    assert news and all(n.path.startswith("news[") for n in news)
    assert all(isinstance(n.record_date, date) for n in news)
    hc = latest_headcount(delhivery)
    assert hc is not None
    assert hc.path.startswith("headcount.timeseries[") and hc.path.endswith(".employee_count")
    assert hc.value > 0


def test_job_date_added_from_real_response() -> None:
    jobs = _fixture("job_search")["job_listings"]
    assert all(job_date_added(j) is not None for j in jobs)
    assert job_date_added({"metadata": {"date_added": None}}) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("delhivery.com", "delhivery.com"),
        ("https://www.Delhivery.com/about", "delhivery.com"),
        ("WWW.bluedart.com", "bluedart.com"),
        ("http://abcindia.com", "abcindia.com"),
    ],
)
def test_normalize_domain(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


def test_resolve_google_redirects() -> None:
    real = "https://techresearchonline.com/news/delhivery-gst-notice/"
    assert resolve_google_redirect(real) == real
    assert resolve_google_redirect(f"https://www.google.com/url?q={real}&sa=U") == real
    assert resolve_google_redirect(f"https://google.com/url?url={real}") == real
    goto = _fixture("web_search")["results"]
    opaque = next(r["url"] for r in goto if "google.com/goto" in r["url"])
    assert resolve_google_redirect(opaque) is None
