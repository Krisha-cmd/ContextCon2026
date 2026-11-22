"""Regexes that pull a CIN, GSTIN and copyright legal name out of fetched page text."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from app.discovery.dedup import pan_from_gstin

CIN_RE = re.compile(r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
# "© 2026 Honasa Consumer Limited", "Copyright © 2019-2026 Acme Foods Pvt. Ltd."
# The name must start with a capital letter and contain no '.' or ',' (keeps it to one name).
COPYRIGHT_RE = re.compile(
    r"(?i:©|\(c\)|copyright)(?:\s*(?i:©|\(c\)))?\s*\d{4}(?:\s*[-\u2013]\s*\d{4})?\s*,?\s*"
    r"(?P<name>[A-Z][A-Za-z0-9&'\- ]{0,80}?)\s+"
    r"(?P<suffix>(?i:private\s+limited|pvt\.?\s*ltd\.?|limited|llp))(?![A-Za-z])"
)


@dataclass(frozen=True)
class Match:
    value: str
    quote: str  # exact text from the page, for the Source


def _text(content: str) -> str:
    return html.unescape(content or "")


def _distinct(matches: list[Match]) -> list[Match]:
    seen: dict[str, Match] = {}
    for m in matches:
        seen.setdefault(m.value, m)
    return list(seen.values())


def find_cins(content: str) -> list[Match]:
    return _distinct([Match(m.group(0), m.group(0)) for m in CIN_RE.finditer(_text(content))])


def find_gstins(content: str) -> list[Match]:
    return _distinct([Match(m.group(0), m.group(0)) for m in GSTIN_RE.finditer(_text(content))])


def find_legal_names(content: str) -> list[Match]:
    out = []
    for m in COPYRIGHT_RE.finditer(_text(content)):
        name = " ".join(m.group("name").split())
        suffix = " ".join(m.group("suffix").split())
        out.append(Match(f"{name} {suffix}", m.group(0)))
    return _distinct(out)


def same_pan(gstins: list[Match]) -> bool:
    """Several GSTINs on one site are fine if they are one PAN registered in several states."""
    return len({pan_from_gstin(g.value) for g in gstins}) == 1
