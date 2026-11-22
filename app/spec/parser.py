"""Request text or kickoff JSON -> RunSpec.

1. Kickoff JSON maps directly (app/spec/kickoff_mapping), no LLM.
2. Otherwise deterministic parse rules run first (T16 hook), then Gemini parses the rest
   with RunSpec's JSON schema at temperature 0. Rule output wins over the LLM's.
3. Pydantic validates. On failure, one repair call that includes the error; then SpecInvalid.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.evidence.models import quote_in_text
from app.gateway.vertex import JsonLLM, get_vertex_llm
from app.log import get_logger
from app.spec import kickoff_mapping
from app.spec.models import RunSpec
from app.spec.rules import apply_parse_rules

log = get_logger(__name__)

ENDPOINT = "vertex.parse"

PROMPT = """You turn a B2B sales-prospecting request into one JSON object that matches the
response schema. The client is an Indian 3PL (third-party logistics) company looking for
Indian companies that may need a 3PL partner.

Rules:
- Extract only what the request says. Do not guess, infer or add defaults.
- Leave every field the request does not mention empty: [] for lists, null for single values,
  {{}} for limits.
- Do not invent companies, domains, people, places or numbers. exclude_domains may only
  contain domains written in the request.
- trigger_types may only use the schema's allowed values. A trigger that fits none of them
  goes into free_text_notes in the request's own words.
- geo_states: only Indian states or union territories the request names.
- size_min / size_max are employee headcount.
- limits: only "max_inr_per_job" (rupees) and "max_minutes_per_job", and only if the request
  states a budget or time limit.
- Anything else relevant that fits no field goes into free_text_notes, in the request's words.
{already}
Request:
<<<
{text}
>>>"""

ALREADY = "\nThese fields are already set by rules; leave them out: {fields}\n"

REPAIR = """Your previous answer failed validation.

Previous answer:
{previous}

Validation error:
{error}

Return a corrected JSON object for the same request, following the same rules.

{original}"""


class SpecInvalid(ValueError):  # noqa: N818 - name fixed by the spec
    """The request could not be turned into a valid RunSpec. Shown to the requester."""

    def __init__(self, message: str, errors: list[str]) -> None:
        super().__init__(message)
        self.errors = errors


def _as_json(raw: str | dict[str, Any]) -> Any | None:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _errors(exc: Exception) -> list[str]:
    if isinstance(exc, ValidationError):
        return [
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
        ]
    return [str(exc)]


def _check_grounded(spec: RunSpec, text: str) -> None:
    """Reject values the request never mentions (guards against invented companies)."""
    invented = [d for d in spec.exclude_domains if not quote_in_text(d, text)]
    if invented:
        raise ValueError(f"exclude_domains not in the request: {invented}")


def build_spec(llm_json: str, partial: dict[str, Any], text: str) -> RunSpec:
    """Parse the LLM's JSON, overlay rule output, validate, and check grounding."""
    data = json.loads(llm_json)
    if not isinstance(data, dict):
        raise ValueError("LLM output must be a JSON object")
    spec = RunSpec.model_validate({**data, **partial})
    _check_grounded(spec, text)
    return spec


def build_prompt(text: str, partial: dict[str, Any]) -> str:
    already = ALREADY.format(fields=", ".join(sorted(partial))) if partial else ""
    return PROMPT.format(text=text, already=already)


def from_kickoff(data: dict[str, Any]) -> RunSpec:
    try:
        return RunSpec.model_validate(kickoff_mapping.to_fields(data))
    except ValidationError as exc:
        raise SpecInvalid("kickoff input does not match RunSpec", _errors(exc)) from exc


async def parse_request(
    raw: str | dict[str, Any], *, job_id: Any = None, llm: JsonLLM | None = None
) -> RunSpec:
    data = _as_json(raw)
    if kickoff_mapping.matches(data):
        log.info("spec parsed", extra={"via": "kickoff"})
        return from_kickoff(data)
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if not text.strip():
        raise SpecInvalid("empty request", ["request text is empty"])
    partial, uncovered = apply_parse_rules(text)
    if not uncovered.strip():
        try:
            return RunSpec.model_validate(partial)
        except ValidationError as exc:
            raise SpecInvalid("parse rules produced an invalid spec", _errors(exc)) from exc
    return await _parse_with_llm(text, uncovered, partial, job_id, llm or await get_vertex_llm())


async def _parse_with_llm(
    text: str, uncovered: str, partial: dict[str, Any], job_id: Any, llm: JsonLLM
) -> RunSpec:
    schema = RunSpec.model_json_schema()
    prompt = build_prompt(uncovered, partial)
    first = await llm.generate_json(prompt, schema, job_id=job_id, endpoint=ENDPOINT)
    try:
        spec = build_spec(first, partial, text)
    except (ValidationError, ValueError) as exc:
        log.warning("spec invalid, repairing", extra={"errors": _errors(exc)})
        repair = REPAIR.format(previous=first, error="\n".join(_errors(exc)), original=prompt)
        second = await llm.generate_json(repair, schema, job_id=job_id, endpoint=ENDPOINT)
        try:
            spec = build_spec(second, partial, text)
        except (ValidationError, ValueError) as exc2:
            raise SpecInvalid("could not parse the request", _errors(exc2)) from exc2
    log.info("spec parsed", extra={"via": "llm"})
    return spec
