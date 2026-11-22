from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.api.main import spec_invalid_handler
from app.spec import parser
from app.spec.models import RunSpec, TriggerType
from app.spec.parser import ENDPOINT, SpecInvalid, build_prompt, parse_request

FIXTURES = Path(__file__).parent / "fixtures" / "requests"
SAMPLES = sorted(FIXTURES.glob("*.json"))


class FakeLLM:
    """Returns canned replies in order and records every prompt."""

    def __init__(self, *replies: Any) -> None:
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.prompts: list[str] = []
        self.calls: list[dict[str, Any]] = []

    async def generate_json(
        self, prompt: str, schema: dict[str, Any], *, job_id: Any, endpoint: str
    ) -> str:
        self.prompts.append(prompt)
        self.calls.append({"schema": schema, "job_id": job_id, "endpoint": endpoint})
        if not self.replies:
            raise AssertionError("LLM called more often than expected")
        return self.replies.pop(0)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


GOOD = {"industries": ["FMCG"], "geo_states": ["Gujarat"], "trigger_types": ["funding"]}
TEXT = "FMCG companies in Gujarat that raised funding"


# --- fixtures -----------------------------------------------------------------------------


def test_there_are_five_samples() -> None:
    assert len(SAMPLES) >= 5


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
async def test_sample_requests(path: Path) -> None:
    case = _load(path)
    llm = FakeLLM() if case["llm_output"] is None else FakeLLM(case["llm_output"])
    spec = await parse_request(case["input"], llm=llm)
    assert spec == RunSpec.model_validate(case["expected"])
    assert llm.replies == []  # every canned reply used, and no extra calls
    if case["llm_output"] is None:
        assert llm.prompts == []  # kickoff JSON never reaches the LLM


# --- LLM call shape -----------------------------------------------------------------------


async def test_llm_gets_runspec_schema_endpoint_and_job() -> None:
    llm = FakeLLM(GOOD)
    await parse_request(TEXT, job_id="job-1", llm=llm)
    assert llm.calls == [
        {"schema": RunSpec.model_json_schema(), "job_id": "job-1", "endpoint": ENDPOINT}
    ]
    assert ENDPOINT == "vertex.parse"


def test_prompt_states_the_extraction_rules() -> None:
    prompt = build_prompt("anything", {}).lower()
    assert "extract only what the request says" in prompt
    assert "leave every field the request does not mention empty" in prompt
    assert "do not invent companies" in prompt
    assert "anything" in prompt


# --- validation and repair ----------------------------------------------------------------


async def test_invalid_output_gets_one_repair_with_the_error() -> None:
    llm = FakeLLM({**GOOD, "trigger_types": ["ipo"]}, GOOD)
    spec = await parse_request(TEXT, llm=llm)
    assert spec.trigger_types == [TriggerType.funding]
    assert len(llm.prompts) == 2
    assert "trigger_types" in llm.prompts[1] and "ipo" in llm.prompts[1]


async def test_non_json_output_is_repaired() -> None:
    llm = FakeLLM("Sure! Here is the JSON:", GOOD)
    assert (await parse_request(TEXT, llm=llm)).geo_states == ["Gujarat"]


async def test_second_failure_raises_spec_invalid() -> None:
    bad = {**GOOD, "size_min": 500, "size_max": 5}
    with pytest.raises(SpecInvalid) as info:
        await parse_request(TEXT, llm=FakeLLM(bad, bad))
    assert any("size_min" in e for e in info.value.errors)


async def test_invented_domain_is_rejected() -> None:
    invented = {**GOOD, "exclude_domains": ["reliance.com"]}
    with pytest.raises(SpecInvalid, match="could not parse") as info:
        await parse_request(TEXT, llm=FakeLLM(invented, invented))
    assert any("reliance.com" in e for e in info.value.errors)


async def test_empty_request_is_invalid_without_llm() -> None:
    with pytest.raises(SpecInvalid):
        await parse_request("   ", llm=FakeLLM())


# --- kickoff JSON -------------------------------------------------------------------------


async def test_invalid_kickoff_json_raises_without_llm() -> None:
    llm = FakeLLM()
    with pytest.raises(SpecInvalid) as info:
        await parse_request({"triggers": ["IPO"]}, llm=llm)
    assert llm.prompts == []
    assert any("trigger_types" in e for e in info.value.errors)


async def test_kickoff_json_as_string_maps_directly() -> None:
    llm = FakeLLM()
    spec = await parse_request(json.dumps({"states": ["Goa"]}), llm=llm)
    assert spec.geo_states == ["Goa"] and llm.prompts == []


async def test_json_with_unknown_keys_goes_to_llm() -> None:
    llm = FakeLLM(GOOD)
    await parse_request({"region": "Gujarat", "what": "FMCG that raised funding"}, llm=llm)
    assert len(llm.prompts) == 1


# --- rule-engine hook ---------------------------------------------------------------------


async def test_rule_output_wins_over_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parser, "apply_parse_rules", lambda t: ({"geo_states": ["Kerala"]}, t))
    llm = FakeLLM(GOOD)
    spec = await parse_request(TEXT, llm=llm)
    assert spec.geo_states == ["Kerala"]
    assert "geo_states" in llm.prompts[0]  # LLM is told the field is already set


async def test_fully_covered_text_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parser, "apply_parse_rules", lambda t: ({"geo_states": ["Kerala"]}, " "))
    llm = FakeLLM()
    assert (await parse_request("Kerala", llm=llm)).geo_states == ["Kerala"]
    assert llm.prompts == []


def test_default_rules_cover_nothing() -> None:
    from app.spec.rules import apply_parse_rules

    assert apply_parse_rules("abc") == ({}, "abc")


# --- API ----------------------------------------------------------------------------------


async def test_api_shows_spec_invalid_to_requester() -> None:
    resp = await spec_invalid_handler(None, SpecInvalid("could not parse", ["size_min: bad"]))
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body == {"error": "spec_invalid", "message": "could not parse",
                    "details": ["size_min: bad"]}  # fmt: skip
