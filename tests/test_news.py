"""Runner step "news". Search, fetch, evidence and the LLM are faked: no network, no credits."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.discovery.crustdata import CrustdataError, FetchResult, Page
from app.evidence.models import quote_in_text
from app.gateway import GatewayError
from app.runner.context import HELD, StepContext
from app.trust.article import extract
from app.trust.news import NO_ADVERSE_NEWS, Names, NewsDeps, find_hit, run_news
from tests.discovery_helpers import FakeSave

S = load_settings(DEFAULT_CONFIG_PATH)
LEGAL = "ACME FOODS PRIVATE LIMITED"
QUERY = ('"ACME FOODS" (fraud OR insolvency OR NCLT OR raid OR "wilful defaulter" OR scam '
         'OR arrested OR penalty)')  # fmt: skip
FILLER = "The company also announced quarterly results and a new product line. " * 8


def _page(body: str, published: str | None = "2026-11-02") -> str:
    ld = (f'<script type="application/ld+json">{{"datePublished": "{published}"}}</script>'
          if published else "")  # fmt: skip
    return f"<html><head>{ld}</head><body><article><p>{body}</p></article></body></html>"


def _account(city: str | None = "Chennai", legal: str | None = LEGAL) -> dict[str, Any]:
    data: dict[str, Any] = {"city": city}
    if legal:
        data["legal_name"] = {"value": legal, "status": "verified", "sources": []}
    return {"id": "acc-1", "name": "Acme Foods", "data": data}


def _result(url: str, title: str = "t", snippet: str = "s") -> dict[str, Any]:
    return {"url": url, "title": title, "snippet": snippet, "resolved_url": None}


class FakeWeb:
    def __init__(self, results: list[dict[str, Any]], pages: dict[str, str],
                 search_error: Exception | None = None) -> None:  # fmt: skip
        self.results, self.pages, self.search_error = results, pages, search_error
        self.queries: list[str] = []
        self.fetched: list[str] = []

    async def search(self, job_id: Any, query: str) -> Page:
        self.queries.append(query)
        if self.search_error:
            raise self.search_error
        return Page(self.results, None, "s" * 64)

    async def fetch(self, job_id: Any, url: str) -> FetchResult:
        self.fetched.append(url)
        content = self.pages.get(url)
        rec = {"url": url, "content": content, "success": content is not None}
        return FetchResult(rec, "f" * 64, url)


class FakeLLM:
    def __init__(self, answer: dict[str, str]) -> None:
        self.answer, self.prompts = answer, []

    async def generate_json(self, prompt: str, schema: Any, *, job_id: Any, endpoint: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.answer)


def _deps(web: FakeWeb, enrich: dict | None = None, llm: FakeLLM | None = None) -> NewsDeps:
    async def get_llm() -> FakeLLM:
        assert llm is not None, "LLM called with use_llm off"
        return llm

    return NewsDeps(web_search=web.search, web_fetch=web.fetch, save=FakeSave(),
                    cached_enrich=lambda account: enrich, get_llm=get_llm)  # fmt: skip


def _settings(**news: Any) -> Any:
    return S.model_copy(update={"news": S.news.model_copy(update=news)})


async def _run(web: FakeWeb, account: dict | None = None, settings: Any = S,
               **kw: Any) -> tuple[StepContext, dict[str, Any]]:  # fmt: skip
    ctx = StepContext(job={"id": "job-1"}, account=account or _account(), config=settings,
                      gateway=None, db=None)  # fmt: skip
    return ctx, await run_news(ctx, _deps(web, **kw))


def _fact(ctx: StepContext) -> dict[str, Any]:
    return (
        next(w for w in ctx.writes if w.sql.startswith("UPDATE accounts"))
        .params[0]
        .obj["adverse_news"]
    )


# ---- issue found --------------------------------------------------------------------------
async def test_legal_name_near_risk_word_is_held_with_spanning_quote() -> None:
    body = f"{FILLER} Police said Acme Foods Pvt Ltd is accused of a GST fraud worth Rs 40 crore."
    web = FakeWeb([_result("https://news.example/a")], {"https://news.example/a": _page(body)})
    ctx, _ = await _run(web)
    fact = _fact(ctx)
    assert fact["status"] == "verified"
    [item] = fact["value"]
    assert item["quote"] == "Acme Foods Pvt Ltd is accused of a GST fraud"
    assert item["category"] == "fraud" and item["via"] == "legal_name"
    assert item["record_date"] == "2026-11-02" and item["date_from"] == "json-ld"
    [src] = fact["sources"]
    assert src["quote"] == item["quote"] and src["record_date"] == "2026-11-02"
    art = extract(_page(body), S.news.date_meta_names, S.news.min_article_chars)
    assert quote_in_text(item["quote"], art.text)
    [d] = ctx.dispositions
    assert d.status == HELD and "a person" in d.reason
    assert web.queries == [QUERY]


async def test_page_without_date_keeps_item_with_unknown_record_date() -> None:
    body = "NCLT admits insolvency plea against Acme Foods Private Limited."
    web = FakeWeb([_result("https://n.example/x")], {"https://n.example/x": _page(body, None)})
    ctx, _ = await _run(web)
    fact = _fact(ctx)
    [item] = fact["value"]
    assert item["record_date"] is None and item["category"] == "insolvency"
    assert fact["sources"][0]["record_date"] is None


async def test_trade_name_plus_city_counts() -> None:
    body = "In Chennai, Acme Foods was fined a penalty by the FSSAI for mislabelling."
    web = FakeWeb([_result("https://n.example/c")], {"https://n.example/c": _page(body)})
    ctx, _ = await _run(web, _account(legal=None))
    [item] = _fact(ctx)["value"]
    assert item["via"] == "trade_name+city" and item["keyword"] == "penalty"


# ---- not attached -------------------------------------------------------------------------
async def test_namesake_in_another_city_is_not_attached() -> None:
    body = "In Pune, a local shop called Acme Foods was raided over a cheating scam."
    web = FakeWeb([_result("https://n.example/p")], {"https://n.example/p": _page(body)})
    ctx, _ = await _run(web, _account(city="Chennai", legal=None))
    fact = _fact(ctx)
    assert fact["value"] == NO_ADVERSE_NEWS and ctx.dispositions == []


async def test_generic_logistics_scam_without_the_name_is_ignored() -> None:
    body = ("Chennai police bust a logistics scam: fake courier firms cheated 200 people. "
            "Experts warn about fraud in the logistics sector.")  # fmt: skip
    web = FakeWeb([_result("https://n.example/g", "Logistics scam busted", body)],
                  {"https://n.example/g": _page(body)})  # fmt: skip
    ctx, _ = await _run(web)
    assert _fact(ctx)["value"] == NO_ADVERSE_NEWS and ctx.dispositions == []


async def test_risk_word_too_far_from_the_name_is_ignored() -> None:
    gap = "Lorem ipsum dolor. " * 20
    body = f"Acme Foods Private Limited opened a plant. {gap} Fraud elsewhere."
    assert len(gap) > S.news.proximity_chars
    web = FakeWeb([_result("https://n.example/f")], {"https://n.example/f": _page(body)})
    ctx, _ = await _run(web)
    assert _fact(ctx)["value"] == NO_ADVERSE_NEWS


def test_keyword_needs_a_word_boundary() -> None:
    names = Names(LEGAL, "ACME FOODS", "Acme Foods", None)
    assert find_hit("Acme Foods Private Limited was afraid of nothing.", names, S) is None
    hit = find_hit("Acme Foods Private Limited raided by ED.", names, S)
    assert hit is not None and hit.keyword == "raid"
    hit = find_hit("Acme Foods Private Limited, a wilful  defaulter, said", names, S)
    assert hit is not None and hit.category == "insolvency"


# ---- wording and sources ------------------------------------------------------------------
async def test_nothing_found_wording_is_exact_and_sourced_by_the_search() -> None:
    page = _page("Acme Foods Private Limited wins award.")
    web = FakeWeb([_result("https://n.example/ok")], {"https://n.example/ok": page})
    ctx, out = await _run(web)
    fact = _fact(ctx)
    assert fact["status"] == "verified" and fact["value"] == "no adverse news found"
    [src] = fact["sources"]
    assert src["field_path"] == QUERY and src["source_name"] == "crustdata.web_search"
    assert "clean" not in json.dumps(fact).lower() and "clean" not in json.dumps(out).lower()
    assert ctx.dispositions == []
    assert len(ctx.audit.entries) == len(ctx.writes)


async def test_search_failed_is_cannot_verify_and_held() -> None:
    web = FakeWeb([], {}, search_error=CrustdataError("web_search", 503, {"error": "down"}))
    ctx, _ = await _run(web)
    fact = _fact(ctx)
    assert fact["status"] == "cannot_verify" and fact["value"] is None
    assert fact["missing"].startswith("news search failed:")
    assert ctx.dispositions[0].status == HELD


async def test_no_name_is_cannot_verify_without_searching() -> None:
    web = FakeWeb([], {})
    account = {"id": "a", "name": "", "data": {}}
    ctx, _ = await _run(web, account)
    assert _fact(ctx)["missing"] == "no company name to search news for" and web.queries == []


# ---- candidates and the fetch cap ---------------------------------------------------------
async def test_fetch_cap_and_unread_results_checked_on_snippet() -> None:
    results = [_result(f"https://n.example/{i}") for i in range(7)]
    results.append(_result("https://www.google.com/goto?url=CAESopaque", "Acme Foods Private "
                           "Limited directors arrested", "ED arrests directors"))  # fmt: skip
    pages = {f"https://n.example/{i}": _page("Nothing to see.") for i in range(7)}
    web = FakeWeb(results, pages)
    ctx, out = await _run(web)
    assert len(web.fetched) == S.news.max_fetch == out["pages_fetched"]
    [item] = _fact(ctx)["value"]
    assert item["page_read"] is False and item["keyword"] == "arrested"
    assert _fact(ctx)["sources"][0]["field_path"] == "results[7]"


async def test_google_redirect_is_resolved_before_fetching() -> None:
    target = "https://n.example/real"
    web = FakeWeb([_result(f"https://www.google.com/url?q={target}&sa=U")],
                  {target: _page("Acme Foods Private Limited wins award.")})  # fmt: skip
    await _run(web)
    assert web.fetched == [target]


async def test_cached_enrich_news_is_fetched_too() -> None:
    enrich = {"news": [{"article_url": "https://n.example/e", "article_title": "Acme raid",
                        "article_publish_date": "2026-11-01"}]}  # fmt: skip
    body = "Income tax raid at Acme Foods Private Limited offices."
    web = FakeWeb([], {"https://n.example/e": _page(body)})
    ctx, _ = await _run(web, enrich=enrich)
    assert web.fetched == ["https://n.example/e"]
    [item] = _fact(ctx)["value"]
    assert item["found_in"] == "enrich"


async def test_fetch_failure_falls_back_to_snippet() -> None:
    async def broken(job_id: Any, url: str) -> FetchResult:
        raise GatewayError("timeout")

    web = FakeWeb([_result("https://n.example/z", "Acme Foods Private Limited fraud probe")], {})
    deps = _deps(web)
    deps.web_fetch = broken
    ctx = StepContext(job={"id": "j"}, account=_account(), config=S, gateway=None, db=None)
    out = await run_news(ctx, deps)
    assert out["findings"] == 1 and any("not fetched" in n for n in out["notes"])


# ---- LLM v2 -------------------------------------------------------------------------------
LLM_ON = _settings(use_llm=True)
NO_KEYWORD = "Acme Foods Private Limited's promoters siphoned customer deposits, the ED said."


async def test_llm_grounded_label_adds_a_finding() -> None:
    llm = FakeLLM({"label": "fraud", "quote": "Acme Foods Private Limited's promoters siphoned "
                   "customer deposits"})  # fmt: skip
    web = FakeWeb([_result("https://n.example/l")], {"https://n.example/l": _page(NO_KEYWORD)})
    ctx, _ = await _run(web, settings=LLM_ON, llm=llm)
    [item] = _fact(ctx)["value"]
    assert item["via"] == "llm" and item["category"] == "fraud"
    assert len(llm.prompts) == 1


async def test_llm_label_with_invented_quote_is_rejected() -> None:
    llm = FakeLLM({"label": "fraud", "quote": "Acme Foods admitted to fraud"})
    web = FakeWeb([_result("https://n.example/l")], {"https://n.example/l": _page(NO_KEYWORD)})
    ctx, out = await _run(web, settings=LLM_ON, llm=llm)
    assert _fact(ctx)["value"] == NO_ADVERSE_NEWS
    assert any("quote not in the article" in n for n in out["notes"])


async def test_llm_cannot_clear_a_lexicon_hit() -> None:
    llm = FakeLLM({"label": "not_about_company", "quote": "Acme Foods Private Limited"})
    body = "Acme Foods Private Limited faces an NCLT insolvency case."
    web = FakeWeb([_result("https://n.example/n")], {"https://n.example/n": _page(body)})
    ctx, _ = await _run(web, settings=LLM_ON, llm=llm)
    [item] = _fact(ctx)["value"]
    assert item["llm_label"] == "not_about_company" and ctx.dispositions[0].status == HELD


async def test_llm_not_called_for_pages_that_do_not_name_the_company() -> None:
    llm = FakeLLM({"label": "fraud", "quote": "x"})
    web = FakeWeb([_result("https://n.example/o")], {"https://n.example/o": _page("Other news.")})
    await _run(web, settings=LLM_ON, llm=llm)
    assert llm.prompts == []


@pytest.mark.parametrize("use_llm", [False])
async def test_llm_off_by_default(use_llm: bool) -> None:
    assert S.news.use_llm is use_llm and S.news.max_fetch == 5
