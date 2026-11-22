"""Registry adapters (gstinapi.in, generic provider, fixtures, unavailable) and the choice
between them. The gateway call is faked: these tests never reach a registry or spend a credit."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import DEFAULT_CONFIG_PATH, Settings, load_settings
from app.gateway import Gateway, GatewayError, GatewayResponse
from app.trust import registry as reg_mod
from app.trust.registry import (
    STATUS_MESSAGES,
    FixtureRegistry,
    GstinApiRegistry,
    InvalidCin,
    InvalidGstin,
    ProviderRegistry,
    RegistryUnavailable,
    SplitRegistry,
    UnavailableRegistry,
    gst_lookup_response,
    make_registry,
    mca_lookup_response,
    normalize_gstin,
)

S = load_settings(DEFAULT_CONFIG_PATH)
FIXTURES = Path(__file__).parent / "fixtures" / "registry"
DOC_EXAMPLE = json.loads((FIXTURES / "gstin_documented_example.json").read_text("utf-8"))
GSTIN = "33AAACC1206D1ZN"


def _settings(**secrets: Any) -> Settings:
    base = {"gstin_api_key": None, "registry_api_key": None, "registry_base_url": None,
            "registry_provider": None}  # fmt: skip
    return S.model_copy(update={"secrets": S.secrets.model_copy(update={**base, **secrets})})


class FakeGatewayCall:
    def __init__(self, status: int, body: Any, exc: Exception | None = None) -> None:
        self.status, self.body, self.exc, self.calls = status, body, exc, []

    async def __call__(self, **kw: Any) -> GatewayResponse:
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return GatewayResponse(self.status, self.body, False, 1.0, 5, "h" * 64)


def _patch(monkeypatch: pytest.MonkeyPatch, status: int, body: Any = None,
           exc: Exception | None = None) -> FakeGatewayCall:  # fmt: skip
    fake = FakeGatewayCall(status, body, exc)
    monkeypatch.setattr(reg_mod.gateway, "call", fake)
    return fake


# ---- gstinapi.in --------------------------------------------------------------------------
async def test_gstinapi_success_maps_record(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch(monkeypatch, 200, DOC_EXAMPLE)
    rec = await GstinApiRegistry(S).gst_lookup("job", GSTIN.lower())
    assert rec.gstin == GSTIN and rec.legal_name == "CENTRAL WAREHOUSING CORPORATION"
    assert rec.status == "Active" and rec.registration_date == date(2017, 7, 1)
    assert rec.last_updated is None  # the documented example has none: unknown, not invented
    [call] = fake.calls
    assert call["url"] == f"https://www.gstinapi.in/v1/gstin/{GSTIN}?include=profile"
    assert call["endpoint"] == "gstinapi:gstin" and call["data_type"] == "registry"


async def test_gstinapi_sandbox_response_has_last_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Captured once from gstinapi.in with the free test GSTIN (include=profile)."""
    _patch(monkeypatch, 200, json.loads((FIXTURES / "gstin_test_sample.json").read_text("utf-8")))
    rec = await GstinApiRegistry(S).gst_lookup("job", "00AAAAA0000A1ZT")
    assert rec.legal_name == "GSTINAPI TEST PRIVATE LIMITED"
    assert rec.last_updated == date(2020, 1, 1)
    assert rec.state is None  # address_details.state is null on this record


async def test_invalid_gstin_never_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch(monkeypatch, 200, DOC_EXAMPLE)
    with pytest.raises(InvalidGstin):
        await GstinApiRegistry(S).gst_lookup("job", "33AAACC1206D1Z")
    assert fake.calls == []
    assert normalize_gstin(" 00aaaaa0000a1zt ") == "00AAAAA0000A1ZT"


async def test_gstinapi_404_is_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, 404, {"success": False, "error": "GSTIN not found"})
    assert await GstinApiRegistry(S).gst_lookup("job", GSTIN) is None


@pytest.mark.parametrize("status", [401, 402, 403, 429, 502, 500])
async def test_gstinapi_failures_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # The live API answered 401 with {"error": ...} and no "success" field: handle both shapes.
    _patch(monkeypatch, status, {"error": "Invalid API key"})
    res = await GstinApiRegistry(S).gst_lookup("job", GSTIN)
    assert isinstance(res, RegistryUnavailable) and res.status == status
    if status in STATUS_MESSAGES:
        assert STATUS_MESSAGES[status] in res.reason


async def test_gstinapi_transport_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, 0, exc=GatewayError("timed out after 3 attempts"))
    res = await GstinApiRegistry(S).gst_lookup("job", GSTIN)
    assert isinstance(res, RegistryUnavailable) and "timed out" in res.reason


async def test_gstinapi_has_no_search_or_mca() -> None:
    r = GstinApiRegistry(S)
    assert isinstance(await r.gst_search("j", "X Ltd"), RegistryUnavailable)
    assert isinstance(await r.mca_lookup("j", cin="U15400MH2015PTC000004"), RegistryUnavailable)


# ---- generic provider ---------------------------------------------------------------------
def test_provider_mapping_functions() -> None:
    gst = gst_lookup_response(
        {"data": {"gstin": GSTIN, "legal_name": "X LTD", "status": "Active",
                  "last_updated": "2026-11-01", "state": "Tamil Nadu"}}, "u")  # fmt: skip
    assert gst.last_updated == date(2026, 11, 1) and gst.state == "Tamil Nadu"
    assert gst_lookup_response({"data": None}, "u") is None
    [mca] = mca_lookup_response(
        {"data": {"cin": "U15400MH2015PTC000004", "company_name": "", "status": "Active"}}, "u"
    )
    assert mca.name is None  # "" is unknown
    assert mca.field_path == "data[0]"


async def test_provider_calls_through_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"data": [{"cin": "U15400MH2015PTC000004", "company_name": "ACME FOODS PRIVATE LIMITED",
                      "status": "Active", "date_of_incorporation": "2015-04-01"}]}  # fmt: skip
    fake = _patch(monkeypatch, 200, body)
    reg = ProviderRegistry("https://reg.example/", S)
    [rec] = await reg.mca_lookup("job", cin="u15400mh2015ptc000004")
    assert rec.incorporation_date == date(2015, 4, 1)
    [call] = fake.calls
    assert call["endpoint"] == "registryapi:mca_lookup" and call["data_type"] == "registry"
    assert call["url"] == "https://reg.example/mca/companies"
    assert call["params"] == {"cin": "U15400MH2015PTC000004"}


async def test_provider_gst_lookup_path_and_404(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch(monkeypatch, 404, {"error": "not found"})
    assert await ProviderRegistry("https://reg.example", S).gst_lookup("job", GSTIN) is None
    assert fake.calls[0]["url"] == f"https://reg.example/gst/{GSTIN}"


@pytest.mark.parametrize("status", [401, 402, 500, 502])
async def test_provider_down_is_unavailable(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _patch(monkeypatch, status, {"error": "boom"})
    reg = ProviderRegistry("https://reg.example", S)
    for res in (await reg.gst_lookup("j", GSTIN), await reg.gst_search("j", "X"),
                await reg.mca_lookup("j", name="X")):  # fmt: skip
        assert isinstance(res, RegistryUnavailable) and res.status == status


async def test_mca_lookup_needs_exactly_one_key() -> None:
    reg = ProviderRegistry("https://reg.example", S)
    with pytest.raises(ValueError, match="exactly one"):
        await reg.mca_lookup("j")
    with pytest.raises(InvalidCin):
        await reg.mca_lookup("j", cin="NOT-A-CIN")


def test_gateway_group_settings() -> None:
    gw = Gateway(_settings(gstin_api_key=SecretStr("k"), registry_api_key=SecretStr("r")),
                 pool=None, client=None)  # fmt: skip
    assert gw.group_max_attempts("gstinapi") == 3
    assert gw.group_retryable("gstinapi", 429) and gw.group_retryable("gstinapi", 502)
    assert not any(gw.group_retryable("gstinapi", s) for s in (400, 401, 402, 403, 404, 500))
    assert gw.group_retryable("crustdata", 500)  # other groups keep the default rule
    assert gw.to_inr(2, "gstinapi") == 2 * S.gstinapi.credit_inr
    assert gw.to_inr(2, "crustdata") == 2 * S.limits.credit_inr
    assert gw.group_headers("gstinapi") == {"x-api-key": "k"}
    assert gw.group_headers("registryapi") == {"x-api-key": "r"}
    assert gw.group_rpm("registryapi") == S.registryapi.rpm
    assert gw.to_inr(2, "registryapi") == 2 * S.registryapi.credit_inr
    with pytest.raises(GatewayError, match="REGISTRY_API_KEY"):
        Gateway(_settings(), pool=None, client=None).group_headers("registryapi")


# ---- fixtures, unavailable, choice --------------------------------------------------------
async def test_fixture_registry() -> None:
    reg = FixtureRegistry(FIXTURES, S.dedup.name_suffixes)
    acme = await reg.gst_lookup("j", "27aapca1234b1z5")
    assert acme.status == "Active" and acme.url.startswith("fixture://registry/")
    assert await reg.gst_lookup("j", "27AAPCA9999B1Z5") is None
    assert [r.gstin for r in await reg.gst_search("j", "Acme Foods Pvt Ltd")] == [acme.gstin]
    assert await reg.gst_search("j", "Acme Foods Pvt Ltd", state="Goa") == []
    [kappa] = await reg.mca_lookup("j", name="Kappa Traders Private Limited")
    assert kappa.status == "Strike Off" and kappa.registered_state == "Delhi"
    assert await reg.mca_lookup("j", cin="U15400MH2015PTC999999") == []


async def test_unavailable_registry() -> None:
    reg = UnavailableRegistry("down for maintenance")
    for res in (await reg.gst_lookup("j", GSTIN), await reg.gst_search("j", "X"),
                await reg.mca_lookup("j", cin="U15400MH2015PTC000004")):  # fmt: skip
        assert res == RegistryUnavailable("down for maintenance")


def test_make_registry_choice() -> None:
    # Defaults: gstinapi for GST, the generic provider for MCA. No keys -> both unavailable.
    reg = make_registry(_settings())
    assert isinstance(reg, SplitRegistry)
    assert isinstance(reg.gst, UnavailableRegistry) and "GSTIN_API_KEY" in reg.gst.reason
    assert isinstance(reg.mca, UnavailableRegistry) and "REGISTRY_BASE_URL" in reg.mca.reason
    reg = make_registry(_settings(gstin_api_key=SecretStr("k"), registry_api_key=SecretStr("r"),
                                  registry_base_url="https://reg.example"))  # fmt: skip
    assert isinstance(reg.gst, GstinApiRegistry) and isinstance(reg.mca, ProviderRegistry)
    assert isinstance(make_registry(_settings(registry_provider="fixture")), FixtureRegistry)
    with pytest.raises(ValueError, match="unknown registry provider"):
        make_registry(_settings(registry_provider="nope"))


def test_registry_provider_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGISTRY_PROVIDER", "unavailable")
    assert isinstance(make_registry(load_settings(DEFAULT_CONFIG_PATH)), UnavailableRegistry)


@pytest.mark.live
async def test_live_free_test_gstin(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uses gstinapi.in's free sample GSTIN only (never spends a credit)."""
    if not os.environ.get("GSTIN_API_KEY"):
        pytest.skip("GSTIN_API_KEY not set")
    from app import gateway
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()
    try:
        rec = await GstinApiRegistry().gst_lookup(None, "00AAAAA0000A1ZT")
    finally:
        await gateway.close()
        get_settings.cache_clear()
    assert rec is not None and rec.gstin == "00AAAAA0000A1ZT"
