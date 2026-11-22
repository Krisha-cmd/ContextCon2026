"""Hook for the T16 rule engine: deterministic parse rules that run before the LLM."""

from __future__ import annotations

from typing import Any


def apply_parse_rules(text: str) -> tuple[dict[str, Any], str]:
    """Return (partial RunSpec fields set by rules, text the rules did not cover).

    TODO(T16): match active parse_rules rows and strip the text they consumed. Rule output wins
    over the LLM's for any field it sets. Until then, nothing is covered.
    """
    return {}, text
