"""Runner step "dedup": cluster candidates that are the same company (union-find).

Strong edges merge: same crustdata_id, same normalized domain, same PAN (from GSTIN).
The name edge (normalized name, same city, token_set_ratio >= dedup.name_ratio) never merges:
clusters it links are held for a person with reason "possible duplicate".
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Hashable, Iterable, Sequence
from itertools import combinations

from rapidfuzz import fuzz

from app.config import Settings, get_settings
from app.discovery.models import Candidate, Cluster, Door
from app.discovery.normalize import normalize_domain
from app.log import get_logger

log = get_logger(__name__)

HOLD_POSSIBLE_DUPLICATE = "possible duplicate"
EDGE_ID, EDGE_DOMAIN, EDGE_PAN, EDGE_NAME = "crustdata_id", "domain", "pan", "name_city"
# GSTIN = 2-digit state code + 10-char PAN + entity no. + 'Z' + check char.
_GSTIN = re.compile(r"^\d{2}([A-Z]{5}\d{4}[A-Z])[0-9A-Z]Z[0-9A-Z]$")
_PUNCT = re.compile(r"[^\w\s]")
# Best member first: door 1 records are the fullest, and for Crustdata twins the lower
# (older) id is the canonical one search returns (API-research: Beardsell 583 vs 15542175).
_DOOR_RANK = {Door.search: 0, Door.jobs: 1, Door.news: 2}
CLUSTER_ID_LEN = 12


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[max(ra, rb)] = min(ra, rb)
        return True


def fold(text: str | None) -> str:
    """Lowercase, strip diacritics ('Tamil Nādu' -> 'tamil nadu'), collapse whitespace."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def _strip_punct(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", fold(text)).split())


def normalize_name(name: str, suffixes: Iterable[str]) -> str:
    """'Beardsell Ltd.' and 'BEARDSELL LIMITED' -> 'beardsell'. Trailing legal suffixes are
    removed repeatedly ('abc india pvt ltd' -> 'abc'), but never down to an empty name."""
    tokens = _strip_punct(name).split()
    suffix_tokens = sorted(
        {tuple(_strip_punct(s).split()) for s in suffixes}, key=len, reverse=True
    )
    changed = True
    while changed:
        changed = False
        for suf in suffix_tokens:
            n = len(suf)
            if n and len(tokens) > n and tuple(tokens[-n:]) == suf:
                tokens = tokens[:-n]
                changed = True
                break
    return " ".join(tokens)


def pan_from_gstin(gstin: str | None) -> str | None:
    """Characters 3-12 of a well-formed GSTIN; None if absent or malformed."""
    if not gstin:
        return None
    m = _GSTIN.match(gstin.strip().upper())
    return m.group(1) if m else None


def _union_on(uf: UnionFind, cands: Sequence[Candidate], key: Callable[[Candidate], Hashable],
              edge: str, used: dict[int, set[str]]) -> None:  # fmt: skip
    first: dict[Hashable, int] = {}
    for i, c in enumerate(cands):
        k = key(c)
        if k is None:
            continue
        if k in first:
            uf.union(first[k], i)
            used.setdefault(i, set()).add(edge)
            used.setdefault(first[k], set()).add(edge)
        else:
            first[k] = i


def _domain_key(c: Candidate) -> str | None:
    return (normalize_domain(c.domain) or None) if c.domain else None


def name_match(a: Candidate, b: Candidate, s: Settings) -> bool:
    """Same company name (token_set_ratio >= threshold) and cities not known to differ.

    An unknown city can't rule a duplicate out, so it still counts; the edge only holds."""
    na = normalize_name(a.name, s.dedup.name_suffixes)
    nb = normalize_name(b.name, s.dedup.name_suffixes)
    if not na or not nb or fuzz.token_set_ratio(na, nb) < s.dedup.name_ratio:
        return False
    ca, cb = fold(a.city), fold(b.city)
    return not (ca and cb and ca != cb)


def _sort_key(c: Candidate) -> tuple[int, int, str]:
    return (_DOOR_RANK[c.door], c.crustdata_id if c.crustdata_id is not None else 2**63, c.key)


def _cluster_id(members: Sequence[Candidate]) -> str:
    digest = hashlib.sha1("|".join(sorted(m.key for m in members)).encode()).hexdigest()
    return f"c_{digest[:CLUSTER_ID_LEN]}"


def dedup(candidates: Sequence[Candidate], settings: Settings | None = None) -> list[Cluster]:
    s = settings or get_settings()
    cands = list(candidates)
    uf, used = UnionFind(len(cands)), {}
    # Strongest edges first.
    _union_on(uf, cands, lambda c: c.crustdata_id, EDGE_ID, used)
    _union_on(uf, cands, _domain_key, EDGE_DOMAIN, used)
    _union_on(uf, cands, lambda c: pan_from_gstin(c.gstin), EDGE_PAN, used)

    groups: dict[int, list[int]] = {}
    for i in range(len(cands)):
        groups.setdefault(uf.find(i), []).append(i)
    roots = sorted(groups)
    ids = {r: _cluster_id([cands[i] for i in groups[r]]) for r in roots}

    # Name edges between different strong clusters: hold both, never merge.
    links: dict[int, set[int]] = {r: set() for r in roots}
    for i, j in combinations(range(len(cands)), 2):
        ri, rj = uf.find(i), uf.find(j)
        if ri != rj and rj not in links[ri] and name_match(cands[i], cands[j], s):
            links[ri].add(rj)
            links[rj].add(ri)

    clusters = [_build(r, groups[r], cands, used, links[r], ids) for r in roots]
    log.info(
        "deduped",
        extra={"candidates": len(cands), "clusters": len(clusters),
               "held": sum(c.held for c in clusters)},
    )  # fmt: skip
    return clusters


def _build(root: int, idx: list[int], cands: Sequence[Candidate], used: dict[int, set[str]],
           linked: set[int], ids: dict[int, str]) -> Cluster:  # fmt: skip
    members = sorted((cands[i] for i in idx), key=_sort_key)
    edges = sorted(set().union(*(used.get(i, set()) for i in idx)))
    dupes = sorted(ids[r] for r in linked)
    return Cluster(
        id=ids[root],
        members=members,
        edges=edges,
        held=bool(dupes),
        hold_reason=HOLD_POSSIBLE_DUPLICATE if dupes else None,
        possible_duplicate_of=dupes,
    )
