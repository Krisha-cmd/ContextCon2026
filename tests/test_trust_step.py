"""Runner step "registry": active, cancelled, struck off, conflict, provider down, and the
PAN key. Registry answers come from tests/fixtures/registry; evidence writes are faked."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.evidence.models import FactStatus
from app.identity import crosswalk as cw
from app.runner.context import DROPPED, HELD, StepContext
from app.trust.registry import FixtureRegistry, SplitRegistry, UnavailableRegistry
from app.trust.step import NO_IDS, Check, RegistryDeps, classify, run_registry
from tests.discovery_helpers import FakeSave

S = load_settings(DEFAULT_CONFIG_PATH)
FIXTURES = Path(__file__).parent / "fixtures" / "registry"
TODAY = date(2026, 11, 22)
ACME_GSTIN, ACME_CIN = "27AAPCA1234B1Z5", "U15400MH2015PTC000004"
ZETA_GSTIN = "29AABCZ5678K1ZQ"  # GST Cancelled
KAPPA_GSTIN, KAPPA_CIN = "07AAFCK4321M1Z2", "U51909DL2014PTC000555"  # GST Active, MCA Strike Off
OMEGA_CIN = "U63090MH2010PTC000888"  # MCA Strike Off
DELTA_CIN = "U52100KA2018PTC000999"  # MCA Dormant


def _fact(value: str) -> dict[str, Any]:
    return {"value": value, "status": "verified", "missing": None, "stale": False,
            "alternatives": [], "sources": [{"evidence_id": f"ev_site_{value}"}]}  # fmt: skip


def _ctx(gstin: str | None = None, cin: str | None = None,
         entity_id: str | None = "cin:X") -> StepContext:  # fmt: skip
    data: dict[str, Any] = {}
    if gstin:
        data["gstin"] = _fact(gstin)
    if cin:
        data["cin"] = _fact(cin)
    row = {"id": "acc-1", "name": "X", "entity_id": entity_id, "data": data}
    return StepContext(job={"id": "job-1"}, account=row, config=S, gateway=None, db=None)


def _deps(registry: Any = None, pan_owner: Any = None) -> RegistryDeps:
    return RegistryDeps(
        registry=registry or FixtureRegistry(FIXTURES, S.dedup.name_suffixes),
        save=FakeSave(),
        pan_owner=pan_owner or (lambda pan: None),
        today=lambda: TODAY,
    )


def _patch(ctx: StepContext) -> dict[str, Any]:
    w = next(w for w in ctx.writes if w.sql.startswith("UPDATE accounts"))
    return w.params[0].obj


async def test_active_on_both_registries_passes() -> None:
    ctx, deps = _ctx(ACME_GSTIN, ACME_CIN), _deps()
    out = await run_registry(ctx, deps)
    assert out["outcome"] == "pass" and ctx.dispositions == []
    fact = _patch(ctx)["status_active"]
    assert fact["status"] == "verified" and fact["value"] is True
    gst_src, mca_src = fact["sources"]
    assert gst_src["field_path"] == "gst.status" and gst_src["source_name"] == "registry.gst"
    assert gst_src["record_date"] == "2026-10-01"  # the registry's last_updated
    assert mca_src["field_path"] == "mca.status"
    assert mca_src["record_date"] == TODAY.isoformat()  # MCA gives none: retrieval date
    assert len(ctx.audit.entries) == len(ctx.writes)  # every write has an audit row
    json.dumps(out)


async def test_gst_active_without_last_updated_uses_retrieval_date() -> None:
    ctx = _ctx(KAPPA_GSTIN)  # no CIN proven: MCA not checked
    out = await run_registry(ctx, _deps())
    assert out["outcome"] == "pass"
    assert _patch(ctx)["status_active"]["sources"][0]["record_date"] == TODAY.isoformat()
    assert "no CIN proven by identity" in out["flags"]


async def test_gst_cancelled_is_dropped() -> None:
    ctx = _ctx(ZETA_GSTIN)
    await run_registry(ctx, _deps())
    [d] = ctx.dispositions
    assert d.status == DROPPED and "Cancelled" in d.reason and ZETA_GSTIN in d.reason
    fact = _patch(ctx)["status_active"]
    assert fact["status"] == "verified" and fact["value"] is False
    assert d.evidence_ids == (fact["sources"][0]["evidence_id"],)


async def test_struck_off_at_mca_is_dropped() -> None:
    ctx = _ctx(cin=OMEGA_CIN)
    await run_registry(ctx, _deps())
    [d] = ctx.dispositions
    assert d.status == DROPPED and "Strike Off" in d.reason
    assert _patch(ctx)["status_active"]["value"] is False


async def test_gst_active_but_mca_struck_off_is_a_held_conflict() -> None:
    ctx = _ctx(KAPPA_GSTIN, KAPPA_CIN)
    out = await run_registry(ctx, _deps())
    [d] = ctx.dispositions
    assert d.status == HELD and "registries disagree" in d.reason
    fact = _patch(ctx)["status_active"]
    assert fact["status"] == FactStatus.conflict.value and fact["value"] is None
    assert sorted(v for v, _ in fact["alternatives"]) == [False, True]
    assert out["gst"]["check"] == "active" and out["mca"]["check"] == "dead"


async def test_provider_down_is_cannot_verify_and_held() -> None:
    ctx = _ctx(ACME_GSTIN, ACME_CIN)
    await run_registry(ctx, _deps(UnavailableRegistry("HTTP 502 after 3 attempts")))
    [d] = ctx.dispositions
    assert d.status == HELD
    fact = _patch(ctx)["status_active"]
    assert fact["status"] == "cannot_verify" and fact["value"] is None
    assert fact["missing"].startswith("registry unavailable")
    assert "HTTP 502" in fact["missing"]


async def test_gst_active_with_mca_down_passes_with_a_flag() -> None:
    fixtures = FixtureRegistry(FIXTURES, S.dedup.name_suffixes)
    reg = SplitRegistry(gst=fixtures, mca=UnavailableRegistry("no MCA provider"))
    out = await run_registry(_ctx(ACME_GSTIN, ACME_CIN), _deps(reg))
    assert out["outcome"] == "pass"
    assert any("MCA registry unavailable: no MCA provider" in f for f in out["flags"])


async def test_unclear_status_is_held() -> None:
    ctx = _ctx(cin=DELTA_CIN)
    out = await run_registry(ctx, _deps())
    assert ctx.dispositions[0].status == HELD and "Dormant" in out["reason"]
    assert _patch(ctx)["status_active"]["status"] == "cannot_verify"


async def test_proved_id_missing_from_registry_is_held() -> None:
    ctx = _ctx(cin="U15400MH2015PTC999999")
    out = await run_registry(ctx, _deps())
    assert ctx.dispositions[0].status == HELD and "not on the MCA register" in out["reason"]


async def test_no_proven_ids_is_held() -> None:
    ctx = _ctx()
    out = await run_registry(ctx, _deps())
    assert out["reason"] == NO_IDS and ctx.dispositions[0].status == HELD


async def test_unverified_identity_facts_are_ignored() -> None:
    ctx = _ctx()
    ctx.account["data"]["gstin"] = {"value": None, "status": "cannot_verify", "missing": "x"}
    out = await run_registry(ctx, _deps())
    assert out["reason"] == NO_IDS


async def test_pan_key_from_gstin() -> None:
    ctx = _ctx(ACME_GSTIN, entity_id="pan:AAPCA1234B")
    out = await run_registry(ctx, _deps())
    assert out["pan"] == "AAPCA1234B"
    [w] = [w for w in ctx.writes if "entity_keys" in w.sql]
    gst_ev = out["gst"]["evidence_id"]
    assert w.params == ("pan:AAPCA1234B", cw.KEY_PAN, "AAPCA1234B", gst_ev, "trust:registry")


async def test_pan_key_derived_even_when_registry_down() -> None:
    ctx = _ctx(ACME_GSTIN)
    await run_registry(ctx, _deps(UnavailableRegistry()))
    [w] = [w for w in ctx.writes if "entity_keys" in w.sql]
    assert w.params[3] == f"ev_site_{ACME_GSTIN}"  # falls back to identity's GSTIN evidence


async def test_pan_already_stored_for_same_entity_is_not_rewritten() -> None:
    ctx = _ctx(ACME_GSTIN, entity_id="pan:AAPCA1234B")
    await run_registry(ctx, _deps(pan_owner=lambda pan: "pan:AAPCA1234B"))
    assert not any("entity_keys" in w.sql for w in ctx.writes)


async def test_pan_owned_by_another_entity_holds() -> None:
    ctx = _ctx(ACME_GSTIN, ACME_CIN, entity_id="cin:" + ACME_CIN)
    out = await run_registry(ctx, _deps(pan_owner=lambda pan: "cin:OTHER"))
    assert out["outcome"] == "held" and "another entity" in out["reason"]
    assert not any("entity_keys" in w.sql for w in ctx.writes)


@pytest.mark.parametrize(
    ("status", "expected"),
    [("Active", Check.active), ("  ACTIVE ", Check.active), ("Cancelled", Check.dead),
     ("Suspended", Check.unclear), ("Cancelled suo moto", Check.unclear), (None, Check.unclear),
     ("Inactive", Check.unclear)],
)  # fmt: skip
def test_classify_is_exact(status: str | None, expected: Check) -> None:
    reg = S.registry
    assert classify(status, reg.gst_active_statuses, reg.gst_drop_statuses) is expected


def test_registry_step_is_registered() -> None:
    from app.runner.pipeline import IMPLEMENTED

    assert IMPLEMENTED["registry"].__name__ == "run_registry"
