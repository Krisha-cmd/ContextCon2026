"""app/revenue: enrich, trigger, buyer, score, and the title matcher. Crustdata, the cache,
evidence and the LLM are faked: no network, no database, no credits."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.discovery.crustdata import CrustdataError, EnrichedCompany, EnrichResult, Page
from app.revenue.buyer import BuyerDeps, months_before, run_buyer
from app.revenue.enrich import EnrichDeps, run_enrich
from app.revenue.score import run_score, score_account
from app.revenue.titles import match, normalize
from app.revenue.trigger import TriggerDeps, job_type, news_type, run_trigger
from app.runner.context import DROPPED, HELD, StepContext
from app.spec.models import RunSpec, TriggerType
from tests.discovery_helpers import FakeSave, src

S = load_settings(DEFAULT_CONFIG_PATH)
B = S.buyer
RUN = date(2026, 11, 22)
JOB = {"id": "job-1", "created_at": datetime(2026, 11, 22, 6, tzinfo=UTC), "spec": {}}
TARGETS = list(B.default_titles)
SEARCH_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "crustdata" / "person_search.json").read_text("utf-8")
)


def _job(**spec: Any) -> dict[str, Any]:
    return {**JOB, "spec": RunSpec(**spec).model_dump(mode="json")}


def _ctx(data: dict[str, Any] | None = None, job: dict | None = None, **row: Any) -> StepContext:
    account = {"id": "acc-1", "name": "Acme Foods", "domain": "acme.in", "data": data or {}, **row}
    return StepContext(job=job or JOB, account=account, config=S, gateway=None, db=None)


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record": record,
        "evidence_id": "ev_enrich",
        "url": "https://api.crustdata.com/x",
        "retrieved_at": "2026-11-21T00:00:00+00:00",
    }


def _patch(ctx: StepContext) -> dict[str, Any]:
    return next(w for w in ctx.writes if w.sql.startswith("UPDATE accounts")).params[0].obj


def _signal(
    days_ago: int,
    title: str = "Warehouse Manager",
    kind: TriggerType = TriggerType.supply_chain_hiring,
) -> dict[str, Any]:
    d = RUN - timedelta(days=days_ago)
    return {
        "type": kind.value,
        "date": d.isoformat(),
        "title": title,
        "url": "https://jobs.example/1",
        "source": src(record_date=d).model_dump(mode="json"),
    }


# ---- title matcher ------------------------------------------------------------------------
def test_normalize_expands_abbreviations() -> None:
    assert normalize("Sr. VP - SCM & Ops", B) == "senior vice president supply chain and operations"


@pytest.mark.parametrize(
    ("title", "target", "confidence"),
    [
        ("Vice President - Logistics", "VP Logistics", B.confidence.same_rank_or_above),
        ("Chief Operating Officer", "COO", B.confidence.same_rank_or_above),
        ("Head - Warehousing & Fulfilment", "Head of Warehousing", B.confidence.same_rank_or_above),
        (
            "Sr. Director - Supply Chain Solutions",
            "Head of Supply Chain",
            B.confidence.one_rank_below,
        ),
        ("Director of Operations", "Director Operations", B.confidence.same_rank_or_above),
    ],
)
def test_title_matches(title: str, target: str, confidence: float) -> None:
    m = match(title, TARGETS, B)
    assert m is not None and m.target == target and m.confidence == confidence


@pytest.mark.parametrize(
    "title",
    [
        "Senior Director Talent Acquisition",
        "CSO",
        "Logistics Manager",
        "Supply Chain Analyst",
        "",
        None,
    ],
)
def test_title_non_matches(title: str | None) -> None:
    assert match(title, TARGETS, B) is None


def test_vice_president_is_not_president() -> None:
    m = match("Senior Vice President Supply Chain", TARGETS, B)
    assert m is not None and m.rank == B.seniority["vice president"]


# ---- enrich -------------------------------------------------------------------------------
def _accounts(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"a{i}",
            "name": f"Co {i}",
            "domain": f"co{i}.in",
            "entity_id": None,
            "data": {"crustdata_id": 100 + i, "pre_rank_score": float(i)},
        }
        for i in range(n)
    ]


class FakeEnrich:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    async def __call__(self, job_id: Any, domains: list[str], known: dict) -> EnrichResult:
        self.calls.append((domains, known))
        recs = {
            d: EnrichedCompany({"crustdata_company_id": known[d], "news": []}, "h" * 64, 0.9)
            for d in domains
            if d != "co9.in"
        }
        return EnrichResult(records=recs, unresolved={"co9.in": "ambiguous_twins"})


async def test_enrich_top_by_pre_rank_uses_cache_and_drops_the_rest() -> None:
    accounts = _accounts(S.shortlist.enrich_top + 2)  # pre_rank_score = index
    fake = FakeEnrich()
    cached = {"crustdata:108": _payload({"crustdata_company_id": 108})}
    deps = EnrichDeps(
        active_accounts=lambda job_id: accounts,
        read_cache=cached.get,
        enrich_companies=fake,
        save=FakeSave(),
    )
    ctx = StepContext(job=JOB, account=None, config=S, gateway=None, db=None)
    out = await run_enrich(ctx, deps)
    dropped = {d.account_id for d in ctx.dispositions}
    assert dropped == {"a0", "a1"} and all(d.status == DROPPED for d in ctx.dispositions)
    assert out["from_cache"] == ["a8"]
    [(domains, known)] = fake.calls
    assert "co8.in" not in domains and "co0.in" not in domains and len(domains) == 7
    assert known["co9.in"] == 109
    assert out["unresolved"] == {"a9": "ambiguous_twins"}
    cache_writes = [w for w in ctx.writes if "entity_cache" in w.sql]
    assert len(cache_writes) == 6 and cache_writes[0].params[1] == "company"
    assert len(ctx.audit.entries) == len(ctx.writes)


# ---- trigger ------------------------------------------------------------------------------
def test_type_rules() -> None:
    assert (
        job_type("Assistant Manager - Warehouse Operations", S) is TriggerType.supply_chain_hiring
    )
    assert job_type("Fulfilment Associate", S) is TriggerType.supply_chain_hiring
    assert job_type("SCM Lead", S) is TriggerType.supply_chain_hiring
    assert job_type("Inside Sales Manager", S) is None
    assert news_type("Acme opens new warehouse in Pune", S) is TriggerType.new_warehouse
    assert news_type("Acme raises Series B", S) is TriggerType.funding
    assert news_type("Acme wins award", S) is None


async def test_forty_day_old_trigger_drops_the_account() -> None:
    ctx = _ctx({"signals": [_signal(40)]})
    await run_trigger(ctx, TriggerDeps(cached_enrich=lambda data: None))
    [d] = ctx.dispositions
    assert d.status == DROPPED and d.reason == "no dated trigger in the last 30 days"
    fact = _patch(ctx)["trigger"]
    assert fact["status"] == "cannot_verify" and fact["missing"] == d.reason


async def test_recent_enrich_news_wins_and_strength_decays() -> None:
    record = {
        "news": [
            {
                "article_title": "Acme opens new warehouse in Pune",
                "article_url": "https://n/1",
                "article_publish_date": (RUN - timedelta(days=6)).isoformat(),
            },
            {
                "article_title": "Acme raises Series B",
                "article_url": "https://n/2",
                "article_publish_date": (RUN - timedelta(days=2)).isoformat(),
            },
        ],
        "hiring": {"recent_titles_csv": "Warehouse Supervisor, Sales Manager"},
    }
    ctx = _ctx({"signals": [_signal(20)]})
    out = await run_trigger(ctx, TriggerDeps(cached_enrich=lambda data: _payload(record)))
    value = _patch(ctx)["trigger"]["value"]
    assert value["type"] == "new_warehouse" and value["age_days"] == 6
    assert value["strength"] == pytest.approx(math.exp(-6 / S.trigger.decay_days), abs=1e-4)
    src_ = _patch(ctx)["trigger"]["sources"][0]
    assert src_["field_path"] == "news[0]" and src_["record_date"] == "2026-11-16"
    assert out["undated"] == 1  # the hiring title has no date: never a trigger
    assert ctx.dispositions == []


async def test_undated_only_is_dropped_and_spec_types_narrow() -> None:
    record = {"hiring": {"recent_titles_csv": "Warehouse Supervisor"}}
    ctx = _ctx({})
    await run_trigger(ctx, TriggerDeps(cached_enrich=lambda data: _payload(record)))
    assert ctx.dispositions[0].status == DROPPED
    ctx = _ctx({"signals": [_signal(3)]}, job=_job(trigger_types=["funding"]))
    await run_trigger(ctx, TriggerDeps(cached_enrich=lambda data: None))
    assert ctx.dispositions[0].status == DROPPED


async def test_future_dated_signal_does_not_count() -> None:
    ctx = _ctx({"signals": [_signal(-3)]})
    await run_trigger(ctx, TriggerDeps(cached_enrich=lambda data: None))
    assert ctx.dispositions[0].status == DROPPED


class FakeLLM:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer

    async def generate_json(self, prompt: str, schema: Any, *, job_id: Any, endpoint: str) -> str:
        return json.dumps(self.answer)


@pytest.mark.parametrize(
    ("quote", "counts"), [("Pune logistics hub", True), ("new warehouse", False)]
)
async def test_llm_news_label_needs_a_real_quote(quote: str, counts: bool) -> None:
    record = {
        "news": [
            {
                "article_title": "Acme picks Pune logistics hub",
                "article_url": "u",
                "article_publish_date": (RUN - timedelta(days=1)).isoformat(),
            }
        ]
    }
    settings = S.model_copy(update={"trigger": S.trigger.model_copy(update={"use_llm": True})})
    llm = FakeLLM({"type": "expansion", "quote": quote})

    async def get_llm() -> FakeLLM:
        return llm

    ctx = _ctx({})
    ctx.config = settings
    await run_trigger(
        ctx, TriggerDeps(cached_enrich=lambda data: _payload(record), get_llm=get_llm)
    )
    assert (ctx.dispositions == []) is counts


# ---- buyer --------------------------------------------------------------------------------
def _profile(title: str, updated: str = "2026-11-01") -> dict[str, Any]:
    return {
        "basic_profile": {"name": "A Person", "current_title": title, "last_updated": updated},
        "social_handles": {"professional_network_identifier": {"profile_url": "https://p/1"}},
        "contact": {"has_business_email": True},
        "experience": {
            "employment_details": {
                "current": [{"name": "Acme Foods", "employer_name": "Acme Foods", "title": title}]
            }
        },
    }


def _buyer_deps(profiles: list[dict] | Exception, enrich: dict | None = None) -> BuyerDeps:
    async def search(job_id: Any, company_id: int, titles: list[str]) -> Page:
        if isinstance(profiles, Exception):
            raise profiles
        return Page(profiles, None, "p" * 64)

    payload = _payload(enrich) if enrich is not None else None
    return BuyerDeps(search_people=search, cached_enrich=lambda data: payload, save=FakeSave())


async def test_buyer_from_person_search_keeps_only_business_fields() -> None:
    ctx = _ctx({"crustdata_id": 5798})
    await run_buyer(ctx, _buyer_deps(SEARCH_FIXTURE["response"]["profiles"]))
    fact = _patch(ctx)["buyer"]
    assert fact["status"] == "verified" and ctx.dispositions == []
    value = fact["value"]
    assert value["title"] == "Sr. Director - Supply Chain Solutions"
    assert value["found_in"] == "person_search" and value["company"] == "Delhivery"
    assert set(value) == {
        "name",
        "title",
        "company",
        "profile_url",
        "matched_target",
        "confidence",
        "seniority_rank",
        "latest_date",
        "stale",
        "found_in",
    }
    assert not re.search(r"email|phone", json.dumps(fact), re.IGNORECASE)
    assert fact["sources"][0]["source_name"] == "crustdata.person_search"


async def test_buyer_fallback_reaches_decision_makers() -> None:
    enrich = {
        "people": {
            "decision_makers": [
                _profile("Senior Director Talent Acquisition"),
                _profile("Head of Warehousing"),
            ],
            "cxos": [_profile("CFO")],
        }
    }
    ctx = _ctx({"crustdata_id": 1})
    out = await run_buyer(ctx, _buyer_deps([_profile("Sales Director")], enrich))
    fact = _patch(ctx)["buyer"]
    assert fact["value"]["title"] == "Head of Warehousing"
    assert fact["value"]["found_in"] == "enrich.people"
    assert fact["sources"][0]["field_path"] == "people.decision_makers[1]"
    assert fact["sources"][0]["evidence_id"] == "ev_enrich"
    assert out["tried"][0] == "person search: 1 profile(s)"


async def test_buyer_search_failure_also_falls_back() -> None:
    enrich = {"people": {"cxos": [_profile("COO")]}}
    ctx = _ctx({"crustdata_id": 1})
    out = await run_buyer(ctx, _buyer_deps(CrustdataError("person_search", 500, {}), enrich))
    assert _patch(ctx)["buyer"]["value"]["matched_target"] == "COO"
    assert out["tried"][0] == "person search failed: CrustdataError"


async def test_no_buyer_is_cannot_verify_and_held() -> None:
    ctx = _ctx({"crustdata_id": 1})
    await run_buyer(ctx, _buyer_deps([], {"people": {"decision_makers": [_profile("CFO")]}}))
    fact = _patch(ctx)["buyer"]
    assert fact["status"] == "cannot_verify" and ctx.dispositions[0].status == HELD
    assert "decision_makers/cxos" in fact["missing"]


async def test_stale_buyer_is_flagged() -> None:
    ctx = _ctx({"crustdata_id": 1})
    await run_buyer(ctx, _buyer_deps([_profile("VP Logistics", updated="2026-01-15")]))
    fact = _patch(ctx)["buyer"]
    assert fact["stale"] is True and fact["value"]["stale"] is True
    assert months_before(RUN, 6) == date(2026, 5, 22)


# ---- score --------------------------------------------------------------------------------
def _scored_data(stale: bool = False) -> dict[str, Any]:
    def fact(value: Any, url: str, name: str, **kw: Any) -> dict[str, Any]:
        return {
            "status": "verified",
            "value": value,
            "sources": [{"url": url, "source_name": name, "evidence_id": "e"}],
            **kw,
        }

    return {
        "industries": ["Consumer Goods"],
        "headcount": 400,
        "state": "Maharashtra",
        "sources": [
            {
                "url": "https://api.crustdata.com/company/search",
                "source_name": "crustdata.company_search",
            }
        ],
        "trigger": fact(
            {
                "type": "new_warehouse",
                "title": "Acme opens new warehouse in Pune",
                "age_days": 6,
                "strength": 0.6514,
            },
            "https://news.example/a",
            "x",
        ),
        "buyer": fact(
            {
                "title": "Head of Warehousing",
                "confidence": 0.9,
                "stale": stale,
                "latest_date": "2026-01-01",
            },
            "https://p/1",
            "crustdata.person_search",
        ),
        "status_active": fact(True, "u", "registry.gst"),
        "adverse_news": fact("no adverse news found", "u", "crustdata.web_search"),
    }


def _total(reasons: list[str]) -> int:
    return sum(int(re.match(r"^([+-]\d+) ", r).group(1)) for r in reasons)


@pytest.mark.parametrize("stale", [False, True])
async def test_score_reasons_add_up_to_the_score(stale: bool) -> None:
    job = _job(
        industries=["consumer goods"], size_min=100, size_max=1000, geo_states=["Maharashtra"]
    )
    ctx = _ctx(_scored_data(stale), job=job)
    out = await run_score(ctx)
    assert out["score"] == _total(out["score_reasons"])
    assert out["score_reasons"][0] == (
        "+20 new warehouse: Acme opens new warehouse in Pune, "
        "6 days ago (source: https://news.example/a)"
    )
    assert _patch(ctx) == out
    expected = 20 + 10 + 10 + 10 + 18 + 10 + 10 - (10 if stale else 0)
    assert out["score"] == expected


def test_unknowns_earn_nothing_and_still_add_up() -> None:
    total, reasons = score_account({}, RunSpec(industries=["pharma"]), S)
    assert total == 0 == _total(reasons)
    assert "+0 no verified trigger" in reasons and "+0 no verified buyer" in reasons


def test_steps_registered_in_pipeline_order() -> None:
    from app.runner.pipeline import IMPLEMENTED, PIPELINE

    for name in ("enrich", "trigger", "buyer", "score"):
        assert IMPLEMENTED[name].__name__ == f"run_{name}"
    assert dict(PIPELINE)["enrich"] is False and dict(PIPELINE)["score"] is True
