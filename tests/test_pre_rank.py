from __future__ import annotations

from datetime import date

import pytest

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.discovery.dedup import dedup
from app.discovery.models import Door
from app.discovery.pre_rank import DROP_EXCLUDED, est_credits_remaining, pre_rank, score
from app.spec.models import RunSpec
from tests.discovery_helpers import cand, signal

S = load_settings(DEFAULT_CONFIG_PATH)
TODAY = date(2026, 11, 22)
SPEC = RunSpec(industries=["FMCG"], geo_states=["Tamil Nadu"], size_min=50, size_max=500)


def _one(c):
    [cluster] = dedup([c], S)
    return cluster


def test_full_score_needs_all_four_signals() -> None:
    c = _one(cand("A", crustdata_id=1, state="Tamil Nādu", headcount=96,
                  signals=[signal(date(2026, 11, 10))]))  # fmt: skip
    sc, reasons = score(c, SPEC, TODAY, S)
    assert sc == 1.0
    assert reasons == ["industry_fit", "headcount_in_range", "state_match", "trigger_present"]


def test_unknowns_earn_nothing() -> None:
    c = _one(cand("B", Door.news, needs_identity=True, signals=[signal(None)]))
    sc, reasons = score(c, SPEC, TODAY, S)
    assert sc == 0.0 and reasons == []


def test_old_trigger_does_not_count() -> None:
    c = _one(cand("C", crustdata_id=3, signals=[signal(date(2026, 9, 1))]))
    assert "trigger_present" not in score(c, SPEC, TODAY, S)[1]


def test_job_industry_fuzzy_fit() -> None:
    c = _one(cand("D", Door.jobs, crustdata_id=4, industries=["FMCG & Consumer Goods"]))
    assert "industry_fit" in score(c, SPEC, TODAY, S)[1]
    other = _one(cand("E", Door.jobs, crustdata_id=5, industries=["Software Development"]))
    assert "industry_fit" not in score(other, SPEC, TODAY, S)[1]


def test_identity_costs_extra() -> None:
    known = _one(cand("F", crustdata_id=6))
    unknown = _one(cand("G", Door.news, needs_identity=True))
    extra = est_credits_remaining(unknown, S) - est_credits_remaining(known, S)
    assert extra == pytest.approx(S.crustdata.est_credits.company_identify_per_input)


def test_ranks_by_value_per_credit_and_keeps_top_n() -> None:
    n = S.shortlist.pre_rank_top
    strong = [cand(f"S{i}", crustdata_id=100 + i, state="Tamil Nadu", headcount=100)
              for i in range(n)]  # fmt: skip
    # Same score as the strong ones would be lower per credit if identity is needed:
    weak = cand("W", Door.jobs, industries=["FMCG"], state="Tamil Nadu", headcount=100,
                needs_identity=True)  # fmt: skip
    clusters = dedup([weak, *strong], S)
    result = pre_rank(clusters, SPEC, today=TODAY, settings=S)
    assert len(result.shortlist) == n
    weak_id = next(c.id for c in clusters if c.name == "W")
    assert [r.cluster_id for r in result.rest] == [weak_id]
    vpc = [r.value_per_credit for r in result.shortlist]
    assert vpc == sorted(vpc, reverse=True)


def test_budget_caps_the_shortlist() -> None:
    clusters = dedup([cand(f"X{i}", crustdata_id=i + 1) for i in range(3)], S)
    per = est_credits_remaining(clusters[0], S)
    result = pre_rank(clusters, SPEC, today=TODAY, budget_credits=per * 2, settings=S)
    assert len(result.shortlist) == 2 and len(result.rest) == 1


def test_held_and_excluded_never_shortlisted() -> None:
    spec = RunSpec(exclude_domains=["skipme.in"])
    a = cand("Sharma Logistics", crustdata_id=1, city="Mumbai")
    b = cand("Sharma Logistics Pvt Ltd", Door.news, needs_identity=True)
    c = cand("Skip Me", crustdata_id=2, domain="skipme.in")
    clusters = dedup([a, b, c], S)
    result = pre_rank(clusters, spec, today=TODAY, settings=S)
    assert result.shortlist == [] and result.rest == []
    assert len(result.held) == 2
    assert list(result.dropped.values()) == [DROP_EXCLUDED]
