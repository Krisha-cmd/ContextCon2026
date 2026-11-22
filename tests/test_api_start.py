"""app/api/start.py and the pre-submit estimate: no DB, no network."""

from __future__ import annotations

from typing import Any

import pytest

from app.api import start
from app.api.main import estimate_inr
from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.gateway import GatewayResponse
from app.runner.jobs import effective_limits

S = load_settings(DEFAULT_CONFIG_PATH)


def test_spec_from_text_and_kickoff() -> None:
    assert start.spec_from("  find leads ", None, 1) == {"request": "find leads"}
    assert start.spec_from(None, b'{"sectors": ["D2C"]}', 1) == {"sectors": ["D2C"]}
    for bad, msg in [(b"[1]", "non-empty object"), (b"{", "not valid JSON"),
                     (b"{}", "non-empty object"), (b" " * 2048, "larger than")]:  # fmt: skip
        with pytest.raises(start.RequestInvalid, match=msg):
            start.spec_from(None, bad, 1)
    with pytest.raises(start.RequestInvalid, match="type a request"):
        start.spec_from(" ", None, 1)


def test_form_limits_only_tighten() -> None:
    lim = effective_limits(S, start.form_limits(100, 10**6))
    assert lim["max_inr_per_job"] == 100
    assert lim["max_minutes_per_job"] == S.limits.max_minutes_per_job
    assert effective_limits(S, {"max_inr_per_job": -5}) == S.limits.model_dump()


async def test_cloud_run_trigger_goes_through_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    s = S.model_copy(update={"secrets": S.secrets.model_copy(update={"gcp_project": "p1"})})
    calls: list[dict[str, Any]] = []

    async def fake_call(**kw: Any) -> GatewayResponse:
        calls.append(kw)
        return GatewayResponse(200, {"name": "op"}, False, 0.0, 5, "h" * 64)

    monkeypatch.setattr(start.gateway, "call", fake_call)
    assert await start.trigger_cloud_run(s, "job-1") == 200
    [c] = calls
    assert c["endpoint"] == "cloudrun:run" and c["method"] == "POST"
    assert c["url"] == (f"https://run.googleapis.com/v2/projects/p1/locations/{S.cloudrun.region}"
                        f"/jobs/{S.cloudrun.job}:run")  # fmt: skip
    env = c["json"]["overrides"]["containerOverrides"][0]["env"]
    assert env == [{"name": "RUN_JOB_ID", "value": "job-1"}] and c["data_type"] == "control"
    no_project = S.model_copy(
        update={"secrets": S.secrets.model_copy(update={"gcp_project": None})}
    )
    with pytest.raises(RuntimeError, match="GCP_PROJECT"):
        start.run_request(no_project, "j")


def test_estimate_counts_every_paid_stage() -> None:
    est = estimate_inr(S)
    cheaper = S.model_copy(update={"shortlist": S.shortlist.model_copy(update={"enrich_top": 1})})
    assert est > estimate_inr(cheaper) > 0
