"""Direct, LLM-free mapping from kickoff input JSON to RunSpec fields.

TODO(kickoff format): the key names below are provisional guesses. Replace FIELD_MAP and
LIMIT_MAP with the real kickoff keys once the kit is out, and add its samples to
tests/fixtures/requests/. RunSpec's own field names are accepted too.
"""

from __future__ import annotations

from typing import Any

from app.spec.models import RunSpec

# kickoff key -> RunSpec field. Identity entries let a RunSpec-shaped JSON map directly.
FIELD_MAP: dict[str, str] = {
    **{name: name for name in RunSpec.model_fields if name != "limits"},
    "sectors": "industries",
    "states": "geo_states",
    "headcount_min": "size_min",
    "headcount_max": "size_max",
    "triggers": "trigger_types",
    "personas": "buyer_titles",
    "exclude": "exclude_domains",
    "notes": "free_text_notes",
}

# kickoff key -> config limit it overrides (RunSpec.limits only lets these tighten).
LIMIT_MAP: dict[str, str] = {
    "budget_inr": "max_inr_per_job",
    "max_minutes": "max_minutes_per_job",
}

KNOWN_KEYS = frozenset(FIELD_MAP) | frozenset(LIMIT_MAP) | {"limits"}


def matches(data: Any) -> bool:
    """True when `data` is a non-empty object whose keys are all known kickoff keys.
    Anything else is treated as free text and parsed by the LLM."""
    return isinstance(data, dict) and bool(data) and set(data) <= KNOWN_KEYS


def _trigger(value: str) -> str:
    """'New Warehouse' / 'new-warehouse' -> 'new_warehouse'. RunSpec rejects unknown ones."""
    return "_".join(value.strip().lower().replace("-", " ").split())


def to_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Map kickoff JSON to RunSpec keyword arguments. Validation is RunSpec's job."""
    out: dict[str, Any] = {}
    limits: dict[str, Any] = dict(data.get("limits") or {})
    for key, value in data.items():
        if key in LIMIT_MAP:
            limits[LIMIT_MAP[key]] = value
        elif key in FIELD_MAP:
            out[FIELD_MAP[key]] = value
    if isinstance(out.get("trigger_types"), list):
        out["trigger_types"] = [
            _trigger(t) if isinstance(t, str) else t for t in out["trigger_types"]
        ]
    if limits:
        out["limits"] = limits
    return out
