from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.discovery.crustdata import Page
from app.discovery.doors import (
    EXTRACT_ENDPOINT,
    Deps,
    company_filters,
    discover,
    door_jobs,
    door_news,
    door_search,
    grounded_mentions,
    news_queries,
    snippet_date,
)
from app.discovery.models import Door
from app.spec.models import RunSpec, TriggerType
from tests.discovery_helpers import FakeSave

S = load_settings(DEFAULT_CONFIG_PATH)
FIX = Path(__file__).parent / "fixtures" / "crustdata"
TODAY = date(2026, 11, 22)
H = "a" * 64


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))["response"]


class FakeSearch:
    """Serves pages of company records; records the cursors it was asked for."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages, self.cursors = pages, []

    async def __call__(self, job_id: Any, filters: dict, cursor: str | None = None) -> Page:
        self.cursors.append(cursor)
        n = len(self.cursors) - 1
        more = f"cur{n + 1}" if n + 1 < len(self.pages) else None
        return Page(self.pages[n], more, f"{n}" * 64)


class FakeLLM:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply, self.calls = json.dumps(reply), []

    async def generate_json(self, prompt, schema, *, job_id, endpoint) -> str:
        self.calls.append({"prompt": prompt, "endpoint": endpoint})
        return self.reply


def _deps(**kw: Any) -> Deps:
    return Deps(save=kw.pop("save", FakeSave()), today=lambda: TODAY, **kw)


# --- door 1 -------------------------------------------------------------------------------


def test_company_filters_from_spec() -> None:
    spec = RunSpec(industries=["FMCG", "Retail"], geo_states=["Gujarat"], size_min=50,
                   size_max=500)  # fmt: skip
    conds = company_filters(spec, S)["conditions"]
    f = S.discovery.company_filters
    assert conds[0] == {"field": f.country_field, "type": "in", "value": ["India"]}
    assert conds[1]["op"] == "or" and len(conds[1]["conditions"]) == 2
    assert conds[2] == {"field": f.state_field, "type": f.state_op, "value": "Gujarat"}
    assert {"field": f.headcount_field, "type": "=>", "value": 50} in conds
    assert {"field": f.headcount_field, "type": "=<", "value": 500} in conds


def test_company_filters_minimal_spec_is_india_only() -> None:
    assert len(company_filters(RunSpec(), S)["conditions"]) == 1


async def test_door_search_reads_records_with_evidence() -> None:
    save = FakeSave()
    recs = _fixture("company_search.json")["companies"]
    out = await door_search("j", RunSpec(max_accounts=10), _deps(
        search_companies=FakeSearch([recs]), save=save), S)  # fmt: skip
    beardsell = next(c for c in out if c.crustdata_id == 583)
    assert beardsell.domain == "beardsell.co.in" and beardsell.headcount == 96
    assert beardsell.state == "Tamil Nādu"
    assert len(save.calls) == len(out) == len(recs)
    assert beardsell.sources[0].field_path == "companies[2]"


async def test_door_search_stops_at_three_times_max_accounts() -> None:
    recs = _fixture("company_search.json")["companies"]  # 10 per page
    search = FakeSearch([recs, recs, recs, recs])
    out = await door_search("j", RunSpec(max_accounts=4), _deps(search_companies=search), S)
    assert len(out) == 12  # 4 * 3
    assert search.cursors == [None, "cur1"]


async def test_door_search_stops_after_max_pages() -> None:
    recs = _fixture("company_search.json")["companies"]
    search = FakeSearch([recs[:1]] * 5)
    out = await door_search("j", RunSpec(max_accounts=100), _deps(search_companies=search), S)
    assert len(search.cursors) == S.discovery.search_max_pages
    assert len(out) == S.discovery.search_max_pages


# --- door 2: jobs -------------------------------------------------------------------------


async def test_door_jobs_tags_supply_chain_hiring_with_date() -> None:
    listings = _fixture("job_search.json")["job_listings"]
    seen: dict[str, Any] = {}

    async def search_jobs(job_id, query, posted_after):
        seen.update(query=query, posted_after=posted_after)
        return Page(listings, None, H)

    out = await door_jobs("j", _deps(search_jobs=search_jobs), S)
    assert seen["posted_after"] == date(2026, 10, 23)  # 30-day window
    keywords = [c["value"] for c in seen["query"]["conditions"][0]["conditions"]]
    assert keywords == list(S.discovery.job_title_keywords)
    first = out[0]
    assert first.door is Door.jobs and first.crustdata_id == 5798
    assert first.domain == "delhivery.com" and not first.needs_identity
    [sig] = first.signals
    assert sig.type is TriggerType.supply_chain_hiring and sig.date is not None
    assert sig.source.record_date == sig.date


# --- door 2: news -------------------------------------------------------------------------


def test_snippet_date() -> None:
    assert snippet_date("Nov 4, 2026 — Acme opens", TODAY) == date(2026, 11, 4)
    assert snippet_date("3 days ago — Acme opens", TODAY) == date(2026, 11, 19)
    assert snippet_date("4 अग॰ 2026 — Hindi month", TODAY) is None
    assert snippet_date("Acme opens a warehouse", TODAY) is None


def test_news_queries_per_industry_capped() -> None:
    spec = RunSpec(industries=["FMCG", "pharma", "retail", "toys"])
    qs = news_queries(spec, S)
    assert qs[0] == "FMCG new warehouse India"
    assert len(qs) == S.discovery.news_max_queries
    assert news_queries(RunSpec(), S) == ["new warehouse India"]


RESULTS = [
    {"title": "Acme Foods opens 2 lakh sq ft warehouse in Bhiwandi",
     "snippet": "Nov 10, 2026 — Acme Foods (acmefoods.in) said on Monday...",
     "url": "https://www.google.com/goto?url=CAES", "resolved_url": None},
    {"title": "Old news: Zeta Retail warehouse", "snippet": "Jan 2, 2025 — Zeta Retail ...",
     "url": "https://news.example/zeta", "resolved_url": "https://news.example/zeta"},
    {"title": "Warehousing demand rises", "snippet": "Beta Pharma plans a new DC in Hyderabad",
     "url": "https://news.example/beta", "resolved_url": "https://news.example/beta"},
]  # fmt: skip


def test_grounded_mentions_reject_invented_names_and_domains() -> None:
    raw = json.dumps({"mentions": [
        {"result": 0, "company": "Acme Foods", "domain": "acmefoods.in"},
        {"result": 0, "company": "Reliance Retail", "domain": None},   # not in text
        {"result": 1, "company": "Beta Pharma", "domain": "betapharma.com"},  # domain invented
        {"result": 9, "company": "Ghost", "domain": None},             # bad index
    ]})  # fmt: skip
    results = [RESULTS[0], RESULTS[2]]
    got = grounded_mentions(raw, results)
    assert [(m.company, m.domain) for m in got] == [
        ("Acme Foods", "acmefoods.in"), ("Beta Pharma", None)]  # fmt: skip
    assert grounded_mentions("not json", results) == []


async def test_door_news_drops_out_of_window_and_tags_name_only() -> None:
    async def web_search(job_id, query):
        return Page(RESULTS, None, H)

    # After the out-of-window result is dropped, the LLM sees [0]=Acme, [1]=Beta.
    llm = FakeLLM({"mentions": [{"result": 0, "company": "Acme Foods", "domain": "acmefoods.in"},
                                {"result": 1, "company": "Beta Pharma"}]})  # fmt: skip
    out = await door_news(
        "j", RunSpec(industries=["FMCG"]), _deps(web_search=web_search, llm=llm), S
    )
    assert llm.calls[0]["endpoint"] == EXTRACT_ENDPOINT
    assert "Zeta" not in llm.calls[0]["prompt"]
    acme, beta = out
    assert acme.domain == "acmefoods.in" and not acme.needs_identity
    assert acme.signals[0].date == date(2026, 11, 10)
    assert acme.signals[0].type is TriggerType.new_warehouse
    assert acme.sources[0].quote == "Acme Foods"
    assert beta.domain is None and beta.needs_identity
    assert beta.signals[0].date is None  # undated: kept, but not yet a trigger


# --- whole step ---------------------------------------------------------------------------


async def test_discover_runs_all_doors() -> None:
    recs = _fixture("company_search.json")["companies"]

    async def search_jobs(job_id, query, posted_after):
        return Page(_fixture("job_search.json")["job_listings"][:2], None, H)

    async def web_search(job_id, query):
        return Page(RESULTS[:1], None, H)

    deps = _deps(search_companies=FakeSearch([recs]), search_jobs=search_jobs,
                 web_search=web_search,
                 llm=FakeLLM({"mentions": [{"result": 0, "company": "Acme Foods"}]}))  # fmt: skip
    out = await discover("j", RunSpec(industries=["FMCG"], max_accounts=10), deps, S)
    assert {c.door for c in out} == {Door.search, Door.jobs, Door.news}
    assert len({c.key for c in out}) == len(out)  # keys are unique
