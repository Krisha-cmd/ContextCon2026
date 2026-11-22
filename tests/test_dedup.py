from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.discovery.dedup import (
    EDGE_DOMAIN,
    EDGE_ID,
    EDGE_PAN,
    HOLD_POSSIBLE_DUPLICATE,
    UnionFind,
    dedup,
    fold,
    normalize_name,
    pan_from_gstin,
)
from app.discovery.doors import Deps, door_search
from app.discovery.models import Door
from app.spec.models import RunSpec
from tests.discovery_helpers import FakeSave, cand

S = load_settings(DEFAULT_CONFIG_PATH)
SUFFIXES = S.dedup.name_suffixes
SEARCH_FIXTURE = Path(__file__).parent / "fixtures" / "crustdata" / "company_search.json"


# --- the three hard cases -----------------------------------------------------------------


def test_beardsell_twins_collapse() -> None:
    # Enrich returns two records for beardsell.co.in (API-research): 583 is the one search
    # returns, 15542175 is a thin duplicate. Same domain -> one cluster, both records kept.
    search = cand("Beardsell Ltd.", crustdata_id=583, domain="beardsell.co.in", headcount=96,
                  city="Chennai")  # fmt: skip
    twin = cand("BEARDSELL LIMITED", Door.jobs, crustdata_id=15542175,
                domain="https://www.beardsell.co.in/about", headcount=47)  # fmt: skip
    [cluster] = dedup([twin, search], S)
    assert EDGE_DOMAIN in cluster.edges
    assert not cluster.held
    assert cluster.crustdata_id == 583  # door-1 record leads
    assert cluster.crustdata_ids == [583, 15542175]
    assert cluster.first("headcount") == 96
    assert {m.crustdata_id for m in cluster.members} == {583, 15542175}
    assert len([s for m in cluster.members for s in m.sources]) == 2  # all evidence kept


async def test_beardsell_twin_collapses_with_real_search_record() -> None:
    page_body = json.loads(SEARCH_FIXTURE.read_text(encoding="utf-8"))["response"]

    async def search(job_id, filters, cursor=None):
        from app.discovery.crustdata import Page

        return Page(page_body["companies"], None, "h" * 64)

    found = await door_search("job", RunSpec(max_accounts=10), Deps(search_companies=search,
                              save=FakeSave()), S)  # fmt: skip
    twin = cand("BEARDSELL LIMITED", Door.jobs, crustdata_id=15542175, domain="beardsell.co.in")
    clusters = dedup([*found, twin], S)
    assert len(clusters) == len(found)  # the twin joined an existing cluster
    [beardsell] = [c for c in clusters if 15542175 in c.crustdata_ids]
    assert beardsell.crustdata_ids == [583, 15542175]


def test_sharma_logistics_in_two_cities_stays_separate() -> None:
    mumbai = cand("Sharma Logistics Pvt Ltd", city="Mumbai", state="Maharashtra")
    delhi = cand("Sharma Logistics", city="Delhi", state="Delhi")
    clusters = dedup([mumbai, delhi], S)
    assert len(clusters) == 2
    assert not any(c.held for c in clusters)


def test_name_only_near_duplicate_is_held_not_merged() -> None:
    known = cand("Sharma Logistics Pvt. Ltd.", crustdata_id=101, domain="sharmalogistics.in",
                 city="Mumbai")  # fmt: skip
    name_only = cand("Sharma Logistic Private Limited", Door.news, needs_identity=True)
    a, b = dedup([known, name_only], S)
    assert len(a.members) == len(b.members) == 1  # not merged
    assert a.held and b.held
    assert a.hold_reason == b.hold_reason == HOLD_POSSIBLE_DUPLICATE
    assert a.possible_duplicate_of == [b.id] and b.possible_duplicate_of == [a.id]


# --- edges --------------------------------------------------------------------------------


def test_same_crustdata_id_merges() -> None:
    [c] = dedup([cand("Delhivery", crustdata_id=5798), cand("DELHIVERY LTD", Door.jobs,
                                                           crustdata_id=5798)], S)  # fmt: skip
    assert c.edges == [EDGE_ID]


def test_same_pan_merges_across_states() -> None:
    a = cand("Acme Foods", gstin="27AAPCS9575E1ZR", city="Pune")
    b = cand("Acme Foods South", gstin="29AAPCS9575E1Z5", city="Bengaluru")
    [c] = dedup([a, b], S)
    assert c.edges == [EDGE_PAN]


def test_strong_edge_beats_name_hold() -> None:
    a = cand("Sharma Logistics", domain="sharma.in", city="Mumbai")
    b = cand("Sharma Logistics Ltd", domain="www.sharma.in", city="Mumbai")
    [c] = dedup([a, b], S)
    assert not c.held


def test_transitive_merge_and_stable_ids() -> None:
    a = cand("A", crustdata_id=1)
    b = cand("A2", crustdata_id=1, domain="a.com")
    c = cand("A3", domain="a.com")
    first = dedup([a, b, c], S)
    assert len(first) == 1 and len(first[0].members) == 3
    assert dedup([c, b, a], S)[0].id == first[0].id


# --- normalizers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Beardsell Ltd.", "beardsell"),
        ("BEARDSELL LIMITED", "beardsell"),
        ("ABC India Pvt. Ltd.", "abc"),
        ("Holisol Logistics Private Limited", "holisol logistics"),
        ("Acme LLP", "acme"),
        ("Foo, Inc.", "foo"),
        ("India Ltd", "india"),  # never strips down to nothing
        ("Limited Brands India", "limited brands"),
    ],
)
def test_normalize_name(raw: str, want: str) -> None:
    assert normalize_name(raw, SUFFIXES) == want


def test_pan_from_gstin() -> None:
    assert pan_from_gstin("27AAPCS9575E1ZR") == "AAPCS9575E"
    assert pan_from_gstin(" 27aapcs9575e1zr ") == "AAPCS9575E"
    assert pan_from_gstin("NOT-A-GSTIN") is None
    assert pan_from_gstin(None) is None


def test_fold_strips_diacritics() -> None:
    assert fold("Tamil Nādu") == "tamil nadu"
    assert fold("  Haryāna ") == "haryana"


def test_union_find() -> None:
    uf = UnionFind(4)
    assert uf.union(0, 1) and uf.union(2, 3) and uf.union(1, 3)
    assert not uf.union(0, 2)
    assert len({uf.find(i) for i in range(4)}) == 1
