"""app.discovery.crustdata against recorded Crustdata responses (tests/fixtures/crustdata).

gateway.call is replaced by a fake that returns the fixture body, so these tests need no
network and no database. The fake records every call for request-shape assertions.
"""

from __future__ import annotations

import copy
import inspect
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app import gateway
from app.config import get_settings
from app.discovery import crustdata as cd
from app.gateway import GatewayResponse

FIX = Path(__file__).parent / "fixtures" / "crustdata"
JOB = "00000000-0000-0000-0000-000000000001"


def fixture(name: str) -> Any:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["response"]


class FakeGateway:
    def __init__(self, *bodies: Any, status: int = 200) -> None:
        self.bodies = list(bodies)
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kw: Any) -> GatewayResponse:
        self.calls.append(kw)
        body = self.bodies[min(len(self.calls), len(self.bodies)) - 1]
        return GatewayResponse(
            self.status, copy.deepcopy(body), False, 0.0, 1, f"h{len(self.calls)}"
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    def install(*bodies: Any, status: int = 200) -> FakeGateway:
        fg = FakeGateway(*bodies, status=status)
        monkeypatch.setattr(gateway, "call", fg)
        return fg

    return install


def _common(call: dict[str, Any], name: str, data_type: str) -> None:
    s = get_settings()
    assert call["endpoint"] == f"crustdata:{name}"
    assert call["url"] == "https://api.crustdata.com" + cd.PATHS[name]
    assert call["method"] == "POST"
    assert call["data_type"] == data_type
    assert call["job_id"] == JOB
    assert s.crustdata.base_url == "https://api.crustdata.com"


# ---- company search ---------------------------------------------------------------------
async def test_search_companies_request_and_page(fake) -> None:
    fg = fake(fixture("company_search"))
    filters = {"field": "locations.country", "type": "in", "value": ["India"]}
    page = await cd.search_companies(JOB, filters)
    call = fg.calls[0]
    _common(call, "company_search", "company")
    s = get_settings().crustdata
    assert call["json"] == {
        "filters": filters,
        "fields": list(s.company_search_fields),
        "sorts": [{"field": "crustdata_company_id", "order": "asc"}],
        "limit": s.page_size.company_search,
    }
    assert call["est_credits"] == pytest.approx(
        s.page_size.company_search * s.est_credits.company_search_per_result
    )
    assert len(page.records) == 10
    assert page.next_cursor and page.total_count == 37524
    assert page.request_hash == "h1"


async def test_search_companies_passes_cursor(fake) -> None:
    fg = fake(fixture("company_search"))
    await cd.search_companies(JOB, {"field": "x", "type": "=", "value": 1}, cursor="abc")
    assert fg.calls[0]["json"]["cursor"] == "abc"


# ---- enrich -----------------------------------------------------------------------------
DOMAINS = ["delhivery.com", "abcindia.com", "allcargo.global", "beardsell.co.in", "bluedart.com"]


async def test_enrich_keeps_twin_matching_search_id(fake, caplog) -> None:
    fake(fixture("company_enrich"))
    with caplog.at_level("WARNING"):
        res = await cd.enrich_companies(JOB, DOMAINS, known_ids={"beardsell.co.in": 583})
    # The API lists BEARDSELL LIMITED (15542175) first; search gave us 583.
    assert res.records["beardsell.co.in"].record["crustdata_company_id"] == 583
    assert res.twins == {"beardsell.co.in": [15542175, 583]}
    assert any(r.getMessage() == "enrich twins" for r in caplog.records)
    assert set(res.records) == set(DOMAINS)
    assert res.records["delhivery.com"].request_hash == "h1"


async def test_enrich_twins_without_known_id_are_unresolved(fake) -> None:
    fake(fixture("company_enrich"))
    res = await cd.enrich_companies(JOB, DOMAINS)
    assert "beardsell.co.in" not in res.records
    assert res.unresolved["beardsell.co.in"].startswith("ambiguous_twins")
    assert res.records["delhivery.com"].record["crustdata_company_id"] == 5798


async def test_enrich_known_id_mismatch_is_unresolved(fake) -> None:
    fake(fixture("company_enrich"))
    res = await cd.enrich_companies(JOB, ["delhivery.com"], known_ids={"delhivery.com": 1})
    assert res.records == {}
    assert res.unresolved["delhivery.com"].startswith("known_id_1_not_in_matches")


async def test_enrich_batches_and_normalizes_domains(fake) -> None:
    body = fixture("company_enrich")
    fg = fake(body, [])
    extra = ["https://www.Extra1.com/", "extra2.com"]
    s = get_settings().crustdata
    res = await cd.enrich_companies(JOB, DOMAINS + extra + ["delhivery.com"])  # dup dropped
    assert len(fg.calls) == 2
    first, second = (c["json"] for c in fg.calls)
    assert first["domains"] == DOMAINS
    assert second["domains"] == ["extra1.com", "extra2.com"]
    assert first["fields"] == list(s.enrich_fields)
    assert fg.calls[0]["est_credits"] == pytest.approx(5 * s.est_credits.company_enrich_per_domain)
    assert res.unresolved["extra1.com"] == "no_match"
    _common(fg.calls[0], "company_enrich", "company")


# ---- identify ---------------------------------------------------------------------------
async def test_identify_by_domain_is_exact(fake) -> None:
    fg = fake(fixture("company_identify"))
    res = await cd.identify_company(JOB, domain="https://www.BlueDart.com/")
    _common(fg.calls[0], "company_identify", "company")
    assert fg.calls[0]["json"] == {"domains": ["bluedart.com"], "exact_match": True}
    assert res.matches[0]["company_data"]["crustdata_company_id"] == 736


async def test_identify_by_linkedin(fake) -> None:
    fg = fake(fixture("company_identify_empty"))
    url = "https://www.linkedin.com/company/delhivery"
    res = await cd.identify_company(JOB, linkedin_url=url)
    assert fg.calls[0]["json"] == {"professional_network_profile_urls": [url], "exact_match": True}
    assert res.matches == []


async def test_identify_requires_exactly_one_key_and_has_no_name_option(fake) -> None:
    fake(fixture("company_identify"))
    with pytest.raises(ValueError):
        await cd.identify_company(JOB)
    with pytest.raises(ValueError):
        await cd.identify_company(JOB, domain="a.com", linkedin_url="https://linkedin.com/x")
    assert "name" not in inspect.signature(cd.identify_company).parameters


# ---- people & jobs ----------------------------------------------------------------------
async def test_search_people_filters_by_company_id_and_titles(fake) -> None:
    fg = fake(fixture("person_search"))
    page = await cd.search_people(JOB, 5798, ["head supply chain", "head logistics"])
    call = fg.calls[0]
    _common(call, "person_search", "people")
    conds = call["json"]["filters"]["conditions"]
    assert conds[0] == {"field": cd.PERSON_CURRENT_COMPANY_ID, "type": "=", "value": 5798}
    assert [c["value"] for c in conds[1]["conditions"]] == ["head supply chain", "head logistics"]
    assert call["json"]["limit"] == get_settings().crustdata.page_size.person_search
    assert page.records and page.records[0]["basic_profile"]["name"] == "REDACTED"
    with pytest.raises(ValueError):
        await cd.search_people(JOB, 5798, [])


async def test_search_jobs_adds_date_filter_and_drops_old(fake) -> None:
    fg = fake(fixture("job_search"))
    query = {"field": "company.basic_info.primary_domain", "type": "=", "value": "delhivery.com"}
    page = await cd.search_jobs(JOB, query, posted_after=date(2026, 11, 15))
    call = fg.calls[0]
    _common(call, "job_search", "news")
    assert call["json"]["filters"]["conditions"] == [
        query,
        {"field": "metadata.date_added", "type": "=>", "value": "2026-11-15"},
    ]
    # Fixture has jobs added 2026-11-17 (x3) and 2026-11-13 (x2): the older two are dropped.
    dates = [j["metadata"]["date_added"][:10] for j in page.records]
    assert dates == ["2026-11-17"] * 3


# ---- web --------------------------------------------------------------------------------
async def test_web_search_marks_opaque_google_redirects(fake) -> None:
    fg = fake(fixture("web_search"))
    page = await cd.web_search(JOB, "Delhivery GST")
    call = fg.calls[0]
    _common(call, "web_search", "news")
    assert call["json"] == {"query": "Delhivery GST", "location": "IN", "sources": ["web"]}
    for r in page.records:
        if "google.com/goto" in r["url"]:
            assert r["resolved_url"] is None
        else:
            assert r["resolved_url"] == r["url"]


async def test_web_fetch_resolves_google_url_before_fetching(fake) -> None:
    body = fixture("web_fetch")
    target = body[0]["url"]
    fg = fake(body)
    res = await cd.web_fetch(JOB, f"https://www.google.com/url?q={target}&sa=U")
    _common(fg.calls[0], "web_fetch", "news")
    assert fg.calls[0]["json"] == {"urls": [target]}
    assert res.url == target
    assert res.record is not None and res.record["title"]


async def test_web_fetch_refuses_opaque_redirect_without_spending(fake) -> None:
    fg = fake(fixture("web_fetch"))
    goto = next(r["url"] for r in fixture("web_search")["results"] if "goto" in r["url"])
    with pytest.raises(cd.UnresolvableURLError):
        await cd.web_fetch(JOB, goto)
    assert fg.calls == []


async def test_http_error_raises(fake) -> None:
    fake({"error": {"type": "invalid_request"}}, status=400)
    with pytest.raises(cd.CrustdataError) as exc:
        await cd.search_companies(JOB, {"field": "nope", "type": "=", "value": 1})
    assert exc.value.status == 400
