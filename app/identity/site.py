"""Legal-name bridge: fetch the company's own pages and read its legal name, CIN and GSTIN.

Pages are fetched in config identity.site_paths order and fetching stops at the first page
that yields a legal name or CIN. Every hit is saved as evidence with the exact quote.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.discovery.crustdata import CrustdataError, FetchResult
from app.evidence.models import Source, quote_in_text
from app.gateway import GatewayError
from app.identity import patterns
from app.log import get_logger

log = get_logger(__name__)

SITE_SOURCE = "company_site"


@dataclass(frozen=True)
class Hit:
    value: str
    source: Source


@dataclass
class SiteFindings:
    legal_name: Hit | None = None
    cin: Hit | None = None
    gstin: Hit | None = None
    pages: list[str] = field(default_factory=list)  # URLs fetched
    notes: list[str] = field(default_factory=list)  # ambiguities and failures, for people

    def hits(self) -> list[Hit]:
        return [h for h in (self.legal_name, self.cin, self.gstin) if h is not None]


FetchFn = Callable[[Any, str], Awaitable[FetchResult]]
SaveFn = Callable[..., Source]


def page_urls(domain: str, paths: Sequence[str]) -> list[str]:
    return [f"https://{domain}{p}" for p in paths]


async def _fetch(fetch: FetchFn, job_id: Any, url: str, notes: list[str]) -> str | None:
    try:
        got = await fetch(job_id, url)
    except (CrustdataError, GatewayError) as exc:
        notes.append(f"fetch failed {url}: {exc}")
        return None
    rec = got.record or {}
    if rec.get("success") is False or not rec.get("content"):
        notes.append(f"no content at {url}")
        return None
    return rec["content"]


async def _hit(save: SaveFn, url: str, content: str, m: patterns.Match, what: str) -> Hit:
    if not quote_in_text(m.quote, content):  # regex output, but never cite what isn't there
        raise AssertionError(f"{what} quote not in page text")
    src = await asyncio.to_thread(
        save, {"url": url, "content": content}, url, SITE_SOURCE, quote=m.quote
    )
    return Hit(m.value, src)


async def _one(
    save: SaveFn,
    url: str,
    content: str,
    found: list[patterns.Match],
    what: str,
    notes: list[str],
    ok_many: bool = False,
) -> Hit | None:
    if not found:
        return None
    if len(found) > 1 and not ok_many:
        notes.append(f"several {what}s on {url}: {[m.value for m in found]}")
        return None
    return await _hit(save, url, content, found[0], what)


async def scan_site(
    job_id: Any, domain: str, paths: Sequence[str], fetch: FetchFn, save: SaveFn
) -> SiteFindings:
    out = SiteFindings()
    for url in page_urls(domain, paths):
        content = await _fetch(fetch, job_id, url, out.notes)
        out.pages.append(url)
        if content is None:
            continue
        await _scan_page(out, save, url, content)
        if out.legal_name or out.cin:
            break
    found = [k for k in ("legal_name", "cin", "gstin") if getattr(out, k) is not None]
    log.info("site scanned", extra={"domain": domain, "pages": len(out.pages), "found": found})
    return out


async def _scan_page(out: SiteFindings, save: SaveFn, url: str, content: str) -> None:
    if out.cin is None:
        out.cin = await _one(save, url, content, patterns.find_cins(content), "CIN", out.notes)
    if out.legal_name is None:
        out.legal_name = await _one(
            save, url, content, patterns.find_legal_names(content), "legal name", out.notes
        )
    if out.gstin is None:
        gstins = patterns.find_gstins(content)
        out.gstin = await _one(
            save,
            url,
            content,
            gstins,
            "GSTIN",
            out.notes,
            ok_many=bool(gstins) and patterns.same_pan(gstins),
        )
