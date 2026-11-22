from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.discovery.dedup import normalize_name
from app.evidence.models import FactStatus, Source
from app.identity import crosswalk as cw
from app.identity.ladder import (
    NAMES_DIFFER,
    NO_ENTITY,
    NO_GSTIN,
    OTHER_STATE,
    SEVERAL,
    STATE_UNKNOWN,
    AccountIn,
    Outcome,
)
from app.identity.matcher import features, score
from app.identity.step import IdentityDeps, identify, run_identity
from app.runner.context import DROPPED, HELD, StepContext
from tests.discovery_helpers import FakeSave
from tests.identity_helpers import FakeFetch, FakeRegistry, gst, mca

S = load_settings(DEFAULT_CONFIG_PATH)
SUFFIXES = S.dedup.name_suffixes
MAMAEARTH = json.loads(
    (Path(__file__).parent / "fixtures" / "identity" / "mamaearth.json").read_text(encoding="utf-8")
)


def _deps(fetch: FakeFetch | None = None, registry: FakeRegistry | None = None,
          crosswalk: Any = None) -> IdentityDeps:  # fmt: skip
    return IdentityDeps(
        fetch=fetch or FakeFetch({}),
        registry=registry or FakeRegistry(),
        save=FakeSave(),
        crosswalk=crosswalk or (lambda pairs: None),
    )


def _mca_fixture() -> FakeRegistry:
    def rec(d: dict) -> Any:
        return mca(d["cin"], d["company_name"], d["state"], d["status"])

    return FakeRegistry(
        mca_by_cin={k: rec(v) for k, v in MAMAEARTH["mca"].items()},
        search={k: [rec(v) for v in vs] for k, vs in MAMAEARTH["company_search"].items()},
    )


def _acct(name: str, domain: str | None = None, **data: Any) -> AccountIn:
    return AccountIn.from_row({"id": "acc-1", "name": name, "domain": domain, "data": data})


# --- the three hard cases -----------------------------------------------------------------


async def test_mamaearth_resolves_to_honasa_consumer_limited() -> None:
    fetch, reg = FakeFetch(MAMAEARTH["pages"]), _mca_fixture()
    acct = AccountIn.from_row(MAMAEARTH["account"])
    r = await identify(acct, "job", _deps(fetch, reg), S)

    assert r.outcome is Outcome.match
    assert r.legal_name.status is FactStatus.verified
    assert normalize_name(r.legal_name.value, SUFFIXES) == "honasa consumer"
    site_src, registry_src = r.legal_name.sources
    assert site_src.quote == "© 2026 Honasa Consumer Limited"
    assert registry_src.source_name == "registry.mca"
    assert r.cin.value == "L74999DL2016PLC000001" and len(r.cin.sources) == 2
    # No GSTIN on the site: GST side is cannot_verify, but an MCA-proven entity proceeds.
    assert r.gstin.status is FactStatus.cannot_verify and r.gstin.missing == NO_GSTIN
    assert any(NO_GSTIN in f for f in r.flags)
    # Fetching stopped at the first page with a legal name / CIN.
    assert fetch.calls == ["https://mamaearth.in", "https://mamaearth.in/privacy"]
    assert ("mca_lookup", "L74999DL2016PLC000001") in reg.calls
    assert r.entity_id == "cin:L74999DL2016PLC000001"
    kinds = {(k, v) for k, v, _ in r.keys}
    assert {(cw.KEY_DOMAIN, "mamaearth.in"), (cw.KEY_CRUSTDATA, "5844"),
            (cw.KEY_CIN, "L74999DL2016PLC000001")} <= kinds  # fmt: skip


async def test_mamaearth_by_name_needs_the_hq_state_to_agree() -> None:
    # Only the copyright line on the site: the MCA side goes through company search.
    pages = {"https://mamaearth.in": "© 2026 Honasa Consumer Limited. All rights reserved."}
    row = {**MAMAEARTH["account"]}
    # Crustdata HQ is Haryana (Gurugram); the registered office is in Delhi -> held, not dropped.
    r = await identify(AccountIn.from_row(row), "job", _deps(FakeFetch(pages), _mca_fixture()), S)
    assert r.outcome is Outcome.cannot_verify and r.reason == OTHER_STATE
    row["data"] = {**row["data"], "state": "Delhi"}
    r = await identify(AccountIn.from_row(row), "job", _deps(FakeFetch(pages), _mca_fixture()), S)
    assert r.outcome is Outcome.match
    assert r.cin.value == "L74999DL2016PLC000001"


SHARMA = [
    mca("U63090MH2011PTC000001", "SHARMA LOGISTICS PRIVATE LIMITED", "Maharashtra"),
    mca("U63090DL2014PLC000002", "SHARMA LOGISTICS LIMITED", None),  # state from CIN: Delhi
]


@pytest.mark.parametrize(
    ("hq_state", "outcome", "reason", "cin"),
    [
        (None, Outcome.cannot_verify, SEVERAL, None),
        ("Maharashtra", Outcome.match, None, "U63090MH2011PTC000001"),
        ("Delhi", Outcome.match, None, "U63090DL2014PLC000002"),
        ("Gujarat", Outcome.cannot_verify, OTHER_STATE, None),
    ],
)
async def test_two_sharma_logistics_need_state_to_disambiguate(
    hq_state: str | None, outcome: Outcome, reason: str | None, cin: str | None
) -> None:
    reg = FakeRegistry(search={"Sharma Logistics Pvt Ltd": SHARMA})
    acct = _acct("Sharma Logistics Pvt Ltd", state=hq_state)
    r = await identify(acct, "job", _deps(registry=reg), S)
    assert r.outcome is outcome
    if reason:
        assert r.reason == reason
        assert r.label is not None and len(r.label["right"]["candidates"]) == 2
    else:
        assert r.cin.value == cin


async def test_made_up_firm_is_no_match() -> None:
    unrelated = [mca("U51909KA2019PTC000003", "ZYX TRADERS PRIVATE LIMITED", "Karnataka")]
    reg = FakeRegistry(search={"Zyxqw Freight Pvt Ltd": unrelated})
    fetch = FakeFetch({})  # the site serves nothing
    r = await identify(_acct("Zyxqw Freight Pvt Ltd", "zyxqw.example", state="Karnataka"),
                       "job", _deps(fetch, reg), S)  # fmt: skip
    assert r.outcome is Outcome.no_match and r.reason == NO_ENTITY
    assert r.legal_name.status is FactStatus.cannot_verify
    assert len(fetch.calls) == len(S.identity.site_paths)  # tried every page, found nothing


# --- other ladder rungs -------------------------------------------------------------------

ACME_GSTIN = "27AAPCA1234B1Z5"
ACME_PAGE = ("© 2026 Acme Foods Private Limited. CIN U15400MH2015PTC000004. "
             f"GSTIN: {ACME_GSTIN}")  # fmt: skip


async def test_gst_and_mca_names_must_agree() -> None:
    reg = FakeRegistry(
        mca_by_cin={"U15400MH2015PTC000004": mca("U15400MH2015PTC000004",
                                                 "ACME BEVERAGES PRIVATE LIMITED", "Maharashtra")},
        gst_by_gstin={ACME_GSTIN: gst(ACME_GSTIN, "ACME FOODS PRIVATE LIMITED")},
    )  # fmt: skip
    r = await identify(_acct("Acme", "acme.in"), "job",
                       _deps(FakeFetch({"https://acme.in": ACME_PAGE}), reg), S)  # fmt: skip
    assert r.outcome is Outcome.held and r.reason == NAMES_DIFFER
    assert r.legal_name.status is FactStatus.conflict
    values = sorted(v for v, _ in r.legal_name.alternatives)
    assert values == ["ACME BEVERAGES PRIVATE LIMITED", "Acme Foods Private Limited"]
    assert r.label is not None and r.keys == []


async def test_gst_only_match_when_mca_lookups_are_unsupported() -> None:
    # gstinapi.in today: GST lookup works; MCA and GSTIN-by-PAN don't exist.
    reg = FakeRegistry(gst_by_gstin={ACME_GSTIN: gst(ACME_GSTIN, "ACME FOODS PRIVATE LIMITED")},
                       unsupported={"mca_lookup"})  # fmt: skip
    r = await identify(_acct("Acme", "acme.in", state="Maharashtra"), "job",
                       _deps(FakeFetch({"https://acme.in": ACME_PAGE}), reg), S)  # fmt: skip
    assert r.outcome is Outcome.match
    assert r.gstin.value == ACME_GSTIN and len(r.gstin.sources) == 2
    assert r.cin.status is FactStatus.cannot_verify
    assert "U15400MH2015PTC000004" in r.cin.missing and "no mca_lookup" in r.cin.missing
    assert r.cin.sources[0].quote == "U15400MH2015PTC000004"  # the site hit is kept
    assert any("other states not checked" in f for f in r.flags)
    assert r.entity_id == "pan:AAPCA1234B"
    assert (cw.KEY_PAN, "AAPCA1234B") in {(k, v) for k, v, _ in r.keys}


async def test_name_only_with_unsupported_search_cannot_verify() -> None:
    reg = FakeRegistry(unsupported={"mca_lookup"})
    r = await identify(_acct("Sharma Logistics", state="Maharashtra"), "job", _deps(registry=reg),
                       S)  # fmt: skip
    assert r.outcome is Outcome.cannot_verify
    assert "no mca_lookup" in r.reason


async def test_single_name_match_with_unknown_hq_state_cannot_verify() -> None:
    reg = FakeRegistry(search={"Sharma Logistics Pvt Ltd": SHARMA[:1]})
    r = await identify(_acct("Sharma Logistics Pvt Ltd"), "job", _deps(registry=reg), S)
    assert r.outcome is Outcome.cannot_verify and r.reason == STATE_UNKNOWN


async def test_unregistered_gstin_on_site_is_held() -> None:
    reg = FakeRegistry(unsupported={"mca_lookup"})
    page = f"Contact us. GSTIN {ACME_GSTIN}. © 2026 Acme Foods Private Limited"
    r = await identify(_acct("Acme", "acme.in"), "job",
                       _deps(FakeFetch({"https://acme.in": page}), reg), S)  # fmt: skip
    assert r.outcome is Outcome.held and "not registered" in r.reason


# --- crosswalk ----------------------------------------------------------------------------


def _stored_src(ev: str) -> Source:
    return Source(url="mca://x", source_name="registry.mca", field_path="data",
                  retrieved_at=datetime(2026, 11, 1, tzinfo=UTC), evidence_id=ev)  # fmt: skip


async def test_crosswalk_hit_reuses_proof_at_no_cost() -> None:
    hit = cw.CrosswalkHit(["cin:L1"], [
        cw.StoredKey(cw.KEY_CIN, "L74999DL2016PLC000001", _stored_src("ev_cin")),
        cw.StoredKey(cw.KEY_LEGAL_NAME, "HONASA CONSUMER LIMITED", _stored_src("ev_name")),
    ])  # fmt: skip
    fetch, reg = FakeFetch(MAMAEARTH["pages"]), _mca_fixture()
    seen: list = []
    deps = _deps(fetch, reg, crosswalk=lambda pairs: seen.append(pairs) or hit)
    r = await identify(AccountIn.from_row(MAMAEARTH["account"]), "job", deps, S)
    assert r.outcome is Outcome.match and r.entity_id == "cin:L1"
    assert fetch.calls == [] and reg.calls == []  # free
    assert (cw.KEY_DOMAIN, "mamaearth.in") in seen[0]
    assert r.legal_name.sources[0].evidence_id == "ev_name"
    assert r.gstin.status is FactStatus.cannot_verify


async def test_crosswalk_pointing_at_two_entities_is_held() -> None:
    hit = cw.CrosswalkHit(["cin:A", "cin:B"])
    r = await identify(_acct("X", "x.in"), "job", _deps(crosswalk=lambda p: hit), S)
    assert r.outcome is Outcome.held and r.label is not None


# --- the runner step ----------------------------------------------------------------------


def _ctx(row: dict[str, Any]) -> StepContext:
    return StepContext(job={"id": "job-1"}, account=row, config=S, gateway=None, db=None)


async def test_step_buffers_facts_keys_and_audit_on_match() -> None:
    ctx = _ctx(MAMAEARTH["account"])
    out = await run_identity(ctx, _deps(FakeFetch(MAMAEARTH["pages"]), _mca_fixture()))
    assert out["outcome"] == "match" and ctx.dispositions == []
    sqls = [w.sql for w in ctx.writes]
    assert sqls[0].startswith("UPDATE accounts")
    assert sum("entity_keys" in s for s in sqls) >= 3
    assert not any("match_labels" in s for s in sqls)
    assert len(ctx.audit.entries) == len(ctx.writes)  # every write has an audit row
    patch = ctx.writes[0].params[0].obj
    assert patch["legal_name"]["status"] == "verified"
    json.dumps(out)  # the step output is JSON


async def test_step_holds_and_labels_when_unresolved() -> None:
    reg = FakeRegistry(search={"Sharma Logistics Pvt Ltd": SHARMA})
    ctx = _ctx({"id": "a2", "name": "Sharma Logistics Pvt Ltd", "domain": None, "data": {}})
    await run_identity(ctx, _deps(registry=reg))
    [d] = ctx.dispositions
    assert d.status == HELD and SEVERAL in d.reason
    assert any("match_labels" in w.sql for w in ctx.writes)
    assert not any("entity_keys" in w.sql for w in ctx.writes)


async def test_step_drops_no_match() -> None:
    ctx = _ctx({"id": "a3", "name": "Zyxqw Freight", "domain": None, "data": {"state": "Goa"}})
    await run_identity(ctx, _deps())
    [d] = ctx.dispositions
    assert d.status == DROPPED


# --- T07b hook ----------------------------------------------------------------------------


def test_matcher_hook() -> None:
    f = features({"name": "Honasa Consumer Ltd", "state": "Delhi"},
                 {"name": "HONASA CONSUMER LIMITED", "state": "Delhi", "cin": "L1"})  # fmt: skip
    assert f["name_equal"] is True and f["state_equal"] is True and f["cin_equal"] is None
    assert score(f) is None
