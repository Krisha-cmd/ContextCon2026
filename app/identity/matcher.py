"""Scored matcher for pairs the rule ladder can't settle (T07b).

features(left, right) -> numeric features; score(features) -> calibrated P(same entity), or
None when no model has been trained. band(p) maps p to match / held / no_match using
config.matching. Training lives in app/identity/train.py.

Records are dicts with any of: name, state, city, domain, email, pan, gstin, directors.
Tri-state features are +1 (agree), -1 (disagree), 0 (unknown on either side): unknown is not
evidence either way.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from app.config import ROOT, Settings, get_settings
from app.discovery.dedup import fold, normalize_name, pan_from_gstin
from app.discovery.normalize import normalize_domain
from app.identity.india import same_state
from app.log import get_logger

log = get_logger(__name__)

FEATURES: tuple[str, ...] = (
    "jaro_winkler",
    "token_set_ratio",
    "exact_name",
    "state_match",
    "city_match",
    "domain_email_match",
    "pan_equal",
    "director_overlap",
    "name_len_diff",
)
PERCENT = 100.0
_PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")


class Band(StrEnum):
    match = "match"
    held = "held"  # cannot_verify: a person decides
    no_match = "no_match"


def _tri(value: bool | None) -> float:
    return 0.0 if value is None else (1.0 if value else -1.0)


def _city_parts(city: str | None) -> set[str]:
    return {fold(p) for p in (city or "").split(",") if fold(p)}


def city_match(a: str | None, b: str | None) -> bool | None:
    """GST 'city' can be 'Saidapet, Chennai': any shared comma part counts as the same city."""
    pa, pb = _city_parts(a), _city_parts(b)
    if not pa or not pb:
        return None
    return bool(pa & pb)


def domain_email_match(domain: str | None, email: str | None) -> bool | None:
    if not domain or not email or "@" not in email:
        return None
    d = normalize_domain(domain)
    host = normalize_domain(email.rsplit("@", 1)[1])
    return bool(d) and (host == d or host.endswith("." + d))


def _pan(rec: dict[str, Any]) -> str | None:
    pan = (rec.get("pan") or "").strip().upper()
    return pan if _PAN_RE.match(pan) else pan_from_gstin(rec.get("gstin"))


def pan_equal(left: dict[str, Any], right: dict[str, Any]) -> bool | None:
    a, b = _pan(left), _pan(right)
    return None if not a or not b else a == b


def director_overlap(a: list[str] | None, b: list[str] | None) -> float:
    """Jaccard of normalized director names mapped to [-1, 1]; 0 when either list is unknown."""
    sa = {" ".join(fold(n).split()) for n in (a or []) if fold(n)}
    sb = {" ".join(fold(n).split()) for n in (b or []) if fold(n)}
    if not sa or not sb:
        return 0.0
    return 2.0 * len(sa & sb) / len(sa | sb) - 1.0


def features(
    left: dict[str, Any], right: dict[str, Any], settings: Settings | None = None
) -> dict[str, float]:
    """left: our account; right: a registry record (or another account)."""
    suffixes = (settings or get_settings()).dedup.name_suffixes
    ln = normalize_name(left.get("name") or "", suffixes)
    rn = normalize_name(right.get("name") or "", suffixes)
    both = bool(ln and rn)
    return {
        "jaro_winkler": JaroWinkler.normalized_similarity(ln, rn) if both else 0.0,
        "token_set_ratio": fuzz.token_set_ratio(ln, rn) / PERCENT if both else 0.0,
        "exact_name": 1.0 if both and ln == rn else 0.0,
        "state_match": _tri(same_state(left.get("state"), right.get("state"))),
        "city_match": _tri(city_match(left.get("city"), right.get("city"))),
        "domain_email_match": _tri(domain_email_match(left.get("domain"), right.get("email"))),
        "pan_equal": _tri(pan_equal(left, right)),
        "director_overlap": director_overlap(left.get("directors"), right.get("directors")),
        "name_len_diff": abs(len(ln) - len(rn)) / max(len(ln), len(rn), 1),
    }


def vector(feats: dict[str, float]) -> list[float]:
    return [float(feats[name]) for name in FEATURES]


# ---- model ------------------------------------------------------------------------------
def model_path(settings: Settings | None = None) -> Path:
    p = Path((settings or get_settings()).matching.model_path)
    return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict[str, Any] | None:
    bundle = joblib.load(path)
    if tuple(bundle.get("features", ())) != FEATURES:
        log.warning("matcher model ignored: feature list changed; retrain", extra={"path": path})
        return None
    return bundle


def load_model(settings: Settings | None = None) -> dict[str, Any] | None:
    """The saved bundle {model, features, trained_at, ...}, or None if there is none."""
    path = model_path(settings)
    if not path.exists():
        return None
    return _load(str(path), path.stat().st_mtime)


def score(feats: dict[str, float], settings: Settings | None = None) -> float | None:
    """Calibrated P(same entity), or None when no model is trained yet."""
    bundle = load_model(settings)
    if bundle is None:
        return None
    return float(bundle["model"].predict_proba([vector(feats)])[0][1])


def band(p: float, settings: Settings | None = None) -> Band:
    m = (settings or get_settings()).matching
    if p >= m.match_threshold:
        return Band.match
    if p <= m.nomatch_threshold:
        return Band.no_match
    return Band.held
