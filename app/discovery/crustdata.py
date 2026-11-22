"""Crustdata client (API version 2025-11-01) built on app.gateway.call.

Auth (Authorization: Bearer CRUSTDATA_API_KEY) and x-api-version are added by the gateway for
the "crustdata" endpoint group. Paths and request shapes match API-research/crustdata_probe.py.

Every function returns records plus the gateway request_hash, so callers can build Sources
with field paths relative to a record, e.g. normalize.field_path("enrich", "news", 2, "title").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from app import gateway
from app.config import Settings, get_settings
from app.discovery.normalize import (
    job_date_added,
    known,
    normalize_domain,
    resolve_google_redirect,
)
from app.log import get_logger

log = get_logger(__name__)

PATHS: dict[str, str] = {
    "company_search": "/company/search",
    "company_enrich": "/company/enrich",
    "company_identify": "/company/identify",
    "person_search": "/person/search",
    "job_search": "/job/search",
    "web_search": "/web/search/live",
    "web_fetch": "/web/enrich/live",
}

# Filter field names (from the Crustdata docs / probe; see API-research/summary.md).
PERSON_CURRENT_COMPANY_ID = "experience.employment_details.current.crustdata_company_id"
PERSON_CURRENT_TITLE = "experience.employment_details.current.title"
JOB_DATE_ADDED = "metadata.date_added"
WORDS_OP = "(.)"  # all words, any order


class CrustdataError(RuntimeError):
    def __init__(self, endpoint: str, status: int, body: Any) -> None:
        super().__init__(f"{endpoint} returned HTTP {status}: {str(body)[:300]}")
        self.endpoint, self.status, self.body = endpoint, status, body


class UnresolvableURLError(ValueError):
    """A redirect URL (e.g. google.com/goto?url=CAES...) whose real target is not readable."""


@dataclass(frozen=True)
class Page:
    records: list[dict[str, Any]]
    next_cursor: str | None
    request_hash: str
    total_count: int | None = None


@dataclass(frozen=True)
class EnrichedCompany:
    record: dict[str, Any]  # matches[i].company_data
    request_hash: str
    confidence: float | None


@dataclass(frozen=True)
class EnrichResult:
    records: dict[str, EnrichedCompany] = field(default_factory=dict)
    twins: dict[str, list[int]] = field(default_factory=dict)  # domain -> all ids returned
    unresolved: dict[str, str] = field(default_factory=dict)  # domain -> reason


@dataclass(frozen=True)
class IdentifyResult:
    matches: list[dict[str, Any]]  # [{confidence_score, company_data}], as returned
    request_hash: str


@dataclass(frozen=True)
class FetchResult:
    record: dict[str, Any] | None  # {url, title, content, timestamp, success, http_status}
    request_hash: str
    url: str  # the URL actually fetched (after redirect resolution)


# ---- transport --------------------------------------------------------------------------
async def _post(
    s: Settings, job_id: Any, name: str, body: dict[str, Any], data_type: str, est: float
) -> gateway.GatewayResponse:
    resp = await gateway.call(
        job_id=job_id,
        endpoint=f"crustdata:{name}",
        method="POST",
        url=s.crustdata.base_url + PATHS[name],
        json=body,
        data_type=data_type,
        est_credits=est,
    )
    if not httpx.codes.is_success(resp.status):
        raise CrustdataError(name, resp.status, resp.body)
    return resp


def _sorts(s: Settings, which: str) -> list[dict[str, str]]:
    return [x.model_dump() for x in getattr(s.crustdata, which)]


def _page(resp: gateway.GatewayResponse, records: list[dict[str, Any]]) -> Page:
    body = resp.body
    return Page(
        records, known(body.get("next_cursor")), resp.request_hash, known(body.get("total_count"))
    )


def _require(body: Any, key: str, name: str) -> Any:
    if not isinstance(body, dict) or key not in body:
        raise CrustdataError(name, 200, body)
    return body[key]


# ---- companies --------------------------------------------------------------------------
async def search_companies(job_id: Any, filters: dict[str, Any], cursor: str | None = None) -> Page:
    """One page of company search. Pass the returned next_cursor back to get the next page."""
    s = get_settings()
    size = s.crustdata.page_size.company_search
    body: dict[str, Any] = {
        "filters": filters,
        "fields": list(s.crustdata.company_search_fields),
        "sorts": _sorts(s, "company_search_sorts"),
        "limit": size,
    }
    if cursor:
        body["cursor"] = cursor
    est = size * s.crustdata.est_credits.company_search_per_result
    resp = await _post(s, job_id, "company_search", body, "company", est)
    records = _require(resp.body, "companies", "company_search")
    return _page(resp, records)


def choose_match(
    domain: str, matches: list[dict[str, Any]], known_id: int | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick the enrich match for a domain. Returns (match, reason_if_none). Never matches[0]."""
    if not matches:
        return None, "no_match"
    ids = [m.get("company_data", {}).get("crustdata_company_id") for m in matches]
    if known_id is not None:
        for m, cid in zip(matches, ids, strict=True):
            if cid == known_id:
                return m, None
        return None, f"known_id_{known_id}_not_in_matches_{ids}"
    if len(matches) == 1:
        return matches[0], None
    return None, f"ambiguous_twins_{ids}"


def _batches(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def enrich_companies(
    job_id: Any, domains: list[str], known_ids: dict[str, int] | None = None
) -> EnrichResult:
    """Enrich domains in batches of crustdata.batch_enrich.

    known_ids maps domain -> crustdata_company_id from search. With several matches for a
    domain (twins) we keep only the one with the known id; without a known id twins are left
    unresolved. Twins are logged.
    """
    s = get_settings()
    ids = {normalize_domain(d): v for d, v in (known_ids or {}).items()}
    wanted = list(dict.fromkeys(normalize_domain(d) for d in domains if d))
    result = EnrichResult()
    for batch in _batches(wanted, s.crustdata.batch_enrich):
        body = {"domains": batch, "fields": list(s.crustdata.enrich_fields)}
        est = len(batch) * s.crustdata.est_credits.company_enrich_per_domain
        resp = await _post(s, job_id, "company_enrich", body, "company", est)
        _collect_enrich(resp, batch, ids, result)
    return result


def _collect_enrich(
    resp: gateway.GatewayResponse, batch: list[str], ids: dict[str, int], out: EnrichResult
) -> None:
    if not isinstance(resp.body, list):
        raise CrustdataError("company_enrich", resp.status, resp.body)
    by_domain = {normalize_domain(str(i.get("matched_on", ""))): i for i in resp.body}
    for domain in batch:
        matches = (by_domain.get(domain) or {}).get("matches") or []
        if len(matches) > 1:
            twin_ids = [m.get("company_data", {}).get("crustdata_company_id") for m in matches]
            out.twins[domain] = twin_ids
            log.warning(
                "enrich twins",
                extra={"domain": domain, "ids": twin_ids, "known_id": ids.get(domain)},
            )
        match, reason = choose_match(domain, matches, ids.get(domain))
        if match is None:
            out.unresolved[domain] = reason or "unknown"
            continue
        out.records[domain] = EnrichedCompany(
            match["company_data"], resp.request_hash, known(match.get("confidence_score"))
        )


async def identify_company(
    job_id: Any, domain: str | None = None, linkedin_url: str | None = None
) -> IdentifyResult:
    """Identify by domain or LinkedIn company URL with exact_match=true.

    There is deliberately no name-only identify: name matching returns fuzzy junk and bills
    for it (probe test 8).
    """
    if (domain is None) == (linkedin_url is None):
        raise ValueError("pass exactly one of domain or linkedin_url")
    s = get_settings()
    body: dict[str, Any] = (
        {"domains": [normalize_domain(domain)]}
        if domain is not None
        else {"professional_network_profile_urls": [linkedin_url]}
    )
    body["exact_match"] = True
    est = s.crustdata.est_credits.company_identify_per_input
    resp = await _post(s, job_id, "company_identify", body, "company", est)
    if not isinstance(resp.body, list):
        raise CrustdataError("company_identify", resp.status, resp.body)
    matches = [m for item in resp.body for m in (item.get("matches") or [])]
    return IdentifyResult(matches, resp.request_hash)


# ---- people & jobs ----------------------------------------------------------------------
async def search_people(job_id: Any, company_id: int, titles: list[str]) -> Page:
    """People currently at company_id whose current title contains all words of any title."""
    if not titles:
        raise ValueError("titles must not be empty")
    s = get_settings()
    size = s.crustdata.page_size.person_search
    title_any = [{"field": PERSON_CURRENT_TITLE, "type": WORDS_OP, "value": t} for t in titles]
    body = {
        "filters": {
            "op": "and",
            "conditions": [
                {"field": PERSON_CURRENT_COMPANY_ID, "type": "=", "value": company_id},
                {"op": "or", "conditions": title_any},
            ],
        },
        "limit": size,
    }
    est = size * s.crustdata.est_credits.person_search_per_result
    resp = await _post(s, job_id, "person_search", body, "people", est)
    records = _require(resp.body, "profiles", "person_search")
    return _page(resp, records)


async def search_jobs(job_id: Any, query: dict[str, Any], posted_after: date) -> Page:
    """Jobs matching `query` (a filter condition or group) added on/after posted_after.

    Listings without a readable date_added, or older than posted_after, are dropped.
    """
    s = get_settings()
    size = s.crustdata.page_size.job_search
    since = {"field": JOB_DATE_ADDED, "type": "=>", "value": posted_after.isoformat()}
    body = {
        "filters": {"op": "and", "conditions": [query, since]},
        "sorts": _sorts(s, "job_search_sorts"),
        "limit": size,
    }
    est = size * s.crustdata.est_credits.job_search_per_result
    resp = await _post(s, job_id, "job_search", body, "news", est)
    listings = _require(resp.body, "job_listings", "job_search")
    fresh = [j for j in listings if (d := job_date_added(j)) is not None and d >= posted_after]
    if len(fresh) != len(listings):
        dropped = len(listings) - len(fresh)
        log.warning(
            "job listings dropped (no/old date_added)",
            extra={"dropped": dropped, "posted_after": str(posted_after)},
        )
    return _page(resp, fresh)


# ---- web --------------------------------------------------------------------------------
async def web_search(job_id: Any, query: str) -> Page:
    """Live web search. Each result gets `resolved_url` (None for opaque Google redirects)."""
    s = get_settings()
    body = {
        "query": query,
        "location": s.crustdata.web_location,
        "sources": list(s.crustdata.web_sources),
    }
    est = s.crustdata.est_credits.web_search_per_page
    resp = await _post(s, job_id, "web_search", body, "news", est)
    results = _require(resp.body, "results", "web_search")
    records = [{**r, "resolved_url": resolve_google_redirect(r.get("url") or "")} for r in results]
    return Page(records, None, resp.request_hash)


async def web_fetch(job_id: Any, url: str) -> FetchResult:
    """Fetch one page. Google redirects are resolved first; opaque ones raise."""
    target = resolve_google_redirect(url)
    if not target:
        raise UnresolvableURLError(url)
    s = get_settings()
    est = s.crustdata.est_credits.web_fetch_per_url
    resp = await _post(s, job_id, "web_fetch", {"urls": [target]}, "news", est)
    if not isinstance(resp.body, list):
        raise CrustdataError("web_fetch", resp.status, resp.body)
    # The docs say to match items by url, not by position.
    record = next((r for r in resp.body if isinstance(r, dict) and r.get("url") == target), None)
    return FetchResult(record, resp.request_hash, target)
