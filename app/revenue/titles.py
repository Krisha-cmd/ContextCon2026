"""Title matcher v1: normalized titles, a function dictionary and seniority ranks (config.buyer).

normalize("Sr. VP - SCM & Ops") -> "senior vice president supply chain and operations".
A title matches a target when it covers every function of the target and its seniority rank
is at least the target's (confidence same_rank_or_above) or one below (one_rank_below).
Titles with no known function or seniority never match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import Buyer

_TOKEN = re.compile(r"[a-z0-9&]+")


@dataclass(frozen=True)
class Parsed:
    normalized: str
    functions: frozenset[str]
    rank: int  # 0 = unknown


@dataclass(frozen=True)
class TitleMatch:
    target: str
    confidence: float
    rank: int
    functions: tuple[str, ...]


def normalize(title: str, cfg: Buyer) -> str:
    tokens = _TOKEN.findall(title.casefold().replace(".", " "))
    return " ".join(cfg.abbreviations.get(t, t) for t in tokens)


def _consume[V](text: str, phrases: dict[str, V]) -> list[V]:
    """Values of phrases found in text, longest phrase first; a matched span is used once."""
    found: list[V] = []
    padded = f" {text} "
    for phrase in sorted(phrases, key=len, reverse=True):
        needle = f" {phrase} "
        while needle in padded:
            found.append(phrases[phrase])
            padded = padded.replace(needle, " | ", 1)
    return found


def parse(title: str, cfg: Buyer) -> Parsed:
    norm = normalize(title, cfg)
    functions = frozenset(_consume(norm, cfg.functions))
    ranks = _consume(norm, cfg.seniority)
    return Parsed(norm, functions, max(ranks, default=0))


def match(title: str | None, targets: list[str], cfg: Buyer) -> TitleMatch | None:
    """Best target this title matches, or None."""
    if not title:
        return None
    have = parse(title, cfg)
    if not have.functions or not have.rank:
        return None
    best: tuple[tuple[float, int], TitleMatch] | None = None
    for target in targets:
        want = parse(target, cfg)
        if not want.functions or not want.functions <= have.functions:
            continue
        if have.rank >= want.rank:
            conf = cfg.confidence.same_rank_or_above
        elif have.rank == want.rank - 1:
            conf = cfg.confidence.one_rank_below
        else:
            continue
        key = (conf, -abs(have.rank - want.rank))  # same confidence: the closest seniority
        if best is None or key > best[0]:
            best = (key, TitleMatch(target, conf, have.rank, tuple(sorted(have.functions))))
    return best[1] if best else None
