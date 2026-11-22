"""Hook for T07b (learned matcher). features() is used now so labelled pairs in match_labels
carry them; score() returns None until a model is trained, and the rule ladder decides."""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from app.config import get_settings
from app.discovery.dedup import normalize_name
from app.identity.india import same_state


def features(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """left: our account ({name, state, domain, cin, gstin}); right: a registry record."""
    suffixes = get_settings().dedup.name_suffixes
    ln = normalize_name(left.get("name") or "", suffixes)
    rn = normalize_name(right.get("name") or "", suffixes)
    return {
        "name_equal": bool(ln) and ln == rn,
        "name_ratio": fuzz.token_set_ratio(ln, rn) if ln and rn else None,
        "state_equal": same_state(left.get("state"), right.get("state")),
        "cin_equal": _eq(left.get("cin"), right.get("cin")),
        "gstin_equal": _eq(left.get("gstin"), right.get("gstin")),
    }


def _eq(a: Any, b: Any) -> bool | None:
    return None if not a or not b else a == b


def score(feats: dict[str, Any]) -> float | None:
    """TODO(T07b): return a match probability from a trained model. None = no model yet."""
    return None
