"""gstinapi.in adapter and the gateway's per-group settings. The gateway call is faked:
these tests never reach gstinapi.in and never spend a credit."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.gateway import Gateway, GatewayResponse
from app.trust import registry as reg_mod
from app.trust.registry import (
    STATUS_MESSAGES,
    GstinApiRegistry,
    InvalidGstin,
    RegistryError,
    RegistryOutOfCredits,
    RegistryUnsupported,
    normalize_gstin,
)

S = load_settings(DEFAULT_CONFIG_PATH)
DOC_EXAMPLE = json.loads(
    (Path(__file__).parent / "fixtures" / "registry" / "gstin_documented_example.json").read_text()
)
GSTIN = "33AAACC1206D1ZN"


class FakeGatewayCall:
    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body, self.calls = status, body, []

    async def __call__(self, **kw: Any) -> GatewayResponse:
        self.calls.append(kw)
        return GatewayResponse(self.status, self.body, False, 1.0, 5, "h" * 64)


def _patch(monkeypatch: pytest.MonkeyPatch, status: int, body: Any) -> FakeGatewayCall:
    fake = FakeGatewayCall(status, body)
    monkeypatch.setattr(reg_mod.gateway, "call", fake)
    return fake


async def test_success_maps_record_and_uses_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch(monkeypatch, 200, DOC_EXAMPLE)
    rec = await GstinApiRegistry(S).gst_lookup("job", GSTIN.lower())
    assert rec.legal_name == "CENTRAL WAREHOUSING CORPORATION"
    assert rec.status == "Active" and rec.state_code == "33"
    assert rec.extra["business_constitution"] is None  # null stays unknown
    [call] = fake.calls
    assert call["url"] == f"https://www.gstinapi.in/v1/gstin/{GSTIN}?include=profile"
    assert call["endpoint"] == "gstinapi:gstin" and call["data_type"] == "registry"


async def test_invalid_format_never_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch(monkeypatch, 200, DOC_EXAMPLE)
    with pytest.raises(InvalidGstin):
        await GstinApiRegistry(S).gst_lookup("job", "33AAACC1206D1Z")
    assert fake.calls == []
    assert normalize_gstin(" 00aaaaa0000a1zt ") == "00AAAAA0000A1ZT"


async def test_404_is_not_registered_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, 404, {"success": False, "error": "GSTIN not found"})
    assert await GstinApiRegistry(S).gst_lookup("job", GSTIN) is None


async def test_402_is_out_of_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, 402, {"success": False, "error": "no credits"})
    with pytest.raises(RegistryOutOfCredits, match="out of credits"):
        await GstinApiRegistry(S).gst_lookup("job", GSTIN)


@pytest.mark.parametrize("status", [401, 403, 429, 502, 500])
async def test_other_failures_have_clear_messages(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # The live API answered 401 with {"error": ...} and no "success" field: handle both shapes.
    _patch(monkeypatch, status, {"error": "Invalid API key"})
    with pytest.raises(RegistryError) as info:
        await GstinApiRegistry(S).gst_lookup("job", GSTIN)
    assert info.value.status == status
    assert not isinstance(info.value, RegistryOutOfCredits)
    if status in STATUS_MESSAGES:
        assert STATUS_MESSAGES[status] in str(info.value)


async def test_unsupported_lookups() -> None:
    r = GstinApiRegistry(S)
    for coro in (r.mca_lookup("j", "L1"), r.company_search("j", "x"), r.gstins_by_pan("j", "p")):
        with pytest.raises(RegistryUnsupported):
            await coro


def test_gateway_group_settings_for_gstinapi() -> None:
    secrets = S.secrets.model_copy(update={"gstin_api_key": SecretStr("k")})
    gw = Gateway(S.model_copy(update={"secrets": secrets}), pool=None, client=None)
    assert gw.group_max_attempts("gstinapi") == 3
    assert gw.group_retryable("gstinapi", 429) and gw.group_retryable("gstinapi", 502)
    assert not any(gw.group_retryable("gstinapi", s) for s in (400, 401, 402, 403, 404, 500))
    assert gw.group_retryable("crustdata", 500)  # other groups keep the default rule
    assert gw.group_max_attempts("crustdata") == S.gateway.max_attempts
    assert gw.to_inr(2, "gstinapi") == 2 * S.gstinapi.credit_inr
    assert gw.to_inr(2, "crustdata") == 2 * S.limits.credit_inr
    assert gw.group_headers("gstinapi") == {"x-api-key": "k"}
    assert gw.group_rpm("gstinapi") == 60


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
    assert rec is not None and rec.id == "00AAAAA0000A1ZT"


async def test_parses_real_sandbox_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Captured once from gstinapi.in with the free test GSTIN (include=profile)."""
    body = json.loads(
        (Path(__file__).parent / "fixtures" / "registry" / "gstin_test_sample.json").read_text()
    )
    _patch(monkeypatch, 200, body)
    rec = await GstinApiRegistry(S).gst_lookup("job", "00AAAAA0000A1ZT")
    assert rec.legal_name == "GSTINAPI TEST PRIVATE LIMITED"
    assert rec.status == "Active" and rec.extra["pincode"] == "395010"
    assert rec.state is None  # address_details.state is null on this record
