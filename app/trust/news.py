"""Runner step "news" (per account): adverse news about the proved legal entity.

Candidates are the account's cached Crustdata enrich news (only if already cached; nothing is
bought here) plus one web search: the company name AND risk words. Plain code decides what is
fetched: at most news.max_fetch pages per account. For each page: resolve redirects, fetch,
take the main text and the publish date (JSON-LD, then <meta>; none -> record_date unknown).

Relevance (lexicon v1): the text names the company (its legal name, or its trade name plus its
city appears) AND a risk keyword sits within news.proximity_chars of that name. The quote kept
spans both. Results we could not fetch are checked on their search snippet / enrich title.
Optional v2 (news.use_llm): Gemini labels articles that name the company; a label is used only
when its supporting quote really is in the text. The LLM can add a finding, never remove one.

adverse_news: issue found -> verified (the findings), account held: a person decides, because
this is an ethics-sensitive call. Nothing relevant -> "no adverse news found", sourced by the
searches performed (query as field_path). Search failed -> cannot_verify.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from app import db
from app.config import Settings
from app.discovery import crustdata
from app.discovery.crustdata import CrustdataError, UnresolvableURLError
from app.discovery.normalize import field_path, known, resolve_google_redirect
from app.evidence.models import Fact, FactStatus, Source, cannot_verify, quote_in_text, verified
from app.evidence.store import save_evidence
from app.gateway import GatewayError
from app.gateway.vertex import JsonLLM, get_vertex_llm
from app.log import get_logger
from app.runner.context import StepContext
from app.trust.article import Article, extract

log = get_logger(__name__)

NO_ADVERSE_NEWS = "no adverse news found"  # exact wording; never "clean"
LLM_ENDPOINT = "vertex.news_classify"
SOURCE_SEARCH = "crustdata.web_search"
SOURCE_ENRICH = "crustdata.company_enrich"
SOURCE_ARTICLE = "web.article"
VIA_LEGAL, VIA_TRADE_CITY = "legal_name", "trade_name+city"
_UPDATE_ACCOUNT = "UPDATE accounts SET data = data || %s WHERE id = %s"
_ENTITY_CACHE = (
    "SELECT payload FROM entity_cache WHERE entity_id = %s AND data_type = %s "
    "AND expires_at > now()"
)
ENRICH_CACHE_TYPE = "company"

SaveFn = Callable[..., Source]
CachedEnrich = Callable[[dict[str, Any]], dict[str, Any] | None]


class Label(StrEnum):
    fraud = "fraud"
    insolvency = "insolvency"
    regulatory = "regulatory"
    other_negative = "other_negative"
    not_negative = "not_negative"  # about the company, nothing adverse
    not_about_company = "not_about_company"


NEGATIVE = (Label.fraud, Label.insolvency, Label.regulatory, Label.other_negative)


class NewsLabel(BaseModel):
    label: Label
    quote: str


LLM_PROMPT = """You label one news article for a due-diligence check on an Indian company.
Company: {name}{city}
Labels: fraud, insolvency, regulatory, other_negative (adverse news about THIS company),
not_negative (about this company, nothing adverse), not_about_company (another company, a
namesake, or a general story).
Return the label and one sentence copied word for word from the article that supports it.

Article:
{text}"""


# ---- names --------------------------------------------------------------------------------
@dataclass(frozen=True)
class Names:
    legal: str | None  # verified legal name from identity
    legal_core: str | None  # legal name without its legal suffix
    trade: str | None  # the account's own (brand) name
    city: str | None

    @property
    def query_name(self) -> str | None:
        return self.legal_core or self.trade


def _phrase(text: str) -> str:
    return r"\s+".join(re.escape(w) for w in text.split())


def split_legal(legal: str, suffixes: tuple[str, ...]) -> tuple[str, bool]:
    """("Acme Foods", True) for "Acme Foods Private Limited"; longest suffix first."""
    low = " ".join(legal.split()).casefold()
    for suf in sorted(suffixes, key=len, reverse=True):
        s = suf.casefold()
        if low.endswith(" " + s):
            return " ".join(legal.split())[: -len(s) - 1].strip(" ,"), True
    return " ".join(legal.split()), False


def names_from(account: dict[str, Any], s: Settings) -> Names:
    data = account.get("data") or {}
    fact = data.get("legal_name")
    legal = None
    if isinstance(fact, dict) and fact.get("status") == FactStatus.verified.value:
        legal = known(fact.get("value"))
    core = split_legal(legal, s.news.legal_suffixes)[0] if legal else None
    trade = known((account.get("name") or "").strip()) or known(data.get("name"))
    return Names(legal, core or None, trade, known(data.get("city")))


def legal_regex(legal: str, suffixes: tuple[str, ...]) -> re.Pattern[str]:
    """The legal name with any spelling of a legal suffix ("Pvt Ltd" for "Private Limited")."""
    core, had_suffix = split_legal(legal, suffixes)
    if not had_suffix:
        return re.compile(rf"(?<!\w){_phrase(core)}(?!\w)", re.IGNORECASE)
    alts = "|".join(_phrase(x) for x in sorted(suffixes, key=len, reverse=True))
    return re.compile(rf"(?<!\w){_phrase(core)},?\s+(?:{alts})(?!\w)", re.IGNORECASE)


def word_regex(text: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){_phrase(text)}(?!\w)", re.IGNORECASE)


def keyword_regex(s: Settings) -> re.Pattern[str]:
    words = sorted(s.news.risk_keywords, key=len, reverse=True)
    suffixes = "|".join(re.escape(x) for x in s.news.keyword_suffixes)
    alts = "|".join(_phrase(w) for w in words)
    return re.compile(rf"(?<!\w)({alts})(?:{suffixes})?(?!\w)", re.IGNORECASE)


# ---- lexicon v1 ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Hit:
    quote: str
    keyword: str
    category: str
    via: str


def mentions(text: str, names: Names, s: Settings) -> list[tuple[int, int, str]]:
    """Spans where the text names the company: the legal name, or the trade name when the
    account's city also appears in the text."""
    out: list[tuple[int, int, str]] = []
    if names.legal:
        reg = legal_regex(names.legal, s.news.legal_suffixes)
        out += [(m.start(), m.end(), VIA_LEGAL) for m in reg.finditer(text)]
    if names.trade and names.city and word_regex(names.city).search(text):
        trade = word_regex(names.trade)
        out += [(m.start(), m.end(), VIA_TRADE_CITY) for m in trade.finditer(text)]
    return sorted(out)


def find_hit(text: str, names: Names, s: Settings) -> Hit | None:
    """The first company mention with a risk keyword within proximity_chars of it."""
    spans = mentions(text, names, s)
    if not spans:
        return None
    keywords = list(keyword_regex(s).finditer(text))
    lookup = {k.casefold(): v for k, v in s.news.risk_keywords.items()}
    for ns, ne, via in spans:
        for k in keywords:
            gap = k.start() - ne if k.start() >= ne else ns - k.end()
            if gap <= s.news.proximity_chars:
                word = " ".join(k.group(1).split()).casefold()
                quote = text[min(ns, k.start()) : max(ne, k.end())]
                return Hit(quote, word, lookup.get(word, Label.other_negative.value), via)
    return None


# ---- candidates ---------------------------------------------------------------------------
@dataclass
class Candidate:
    origin: str  # "search" | "enrich"
    index: int
    url: str | None  # fetchable URL (redirect resolved), None if opaque
    title: str | None
    blurb: str  # title + snippet (search) or title (enrich): checked if the page isn't read
    raw: Any
    path: str  # field path of the item in its source record


def search_candidates(records: list[dict[str, Any]]) -> list[Candidate]:
    out = []
    for i, r in enumerate(records):
        url = r.get("resolved_url") or resolve_google_redirect(r.get("url") or "")
        title, snippet = known(r.get("title")), known(r.get("snippet"))
        blurb = "\n".join(x for x in (title, snippet) if x)
        out.append(Candidate("search", i, url, title, blurb, r, field_path("results", i)))
    return out


def enrich_candidates(record: dict[str, Any] | None) -> list[Candidate]:
    out = []
    for i, item in enumerate((record or {}).get("news") or []):
        if not isinstance(item, dict):
            continue
        title = known(item.get("article_title"))
        url = known(item.get("article_url"))
        out.append(Candidate("enrich", i, url, title, title or "", item, field_path("news", i)))
    return out


def merge(*groups: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out = []
    for c in (c for g in groups for c in g):
        if c.url and c.url in seen:
            continue
        if c.url:
            seen.add(c.url)
        out.append(c)
    return out


def db_cached_enrich(account: dict[str, Any]) -> dict[str, Any] | None:
    """An enrich record already on the account or in entity_cache. Never calls Crustdata."""
    data = account.get("data") or {}
    if isinstance(data.get("enrich"), dict):
        return data["enrich"]
    ids = data.get("crustdata_ids") or ([data["crustdata_id"]] if data.get("crustdata_id") else [])
    if not ids:
        return None
    with db.tx() as conn:
        for cid in ids:
            row = conn.execute(_ENTITY_CACHE, (f"crustdata:{cid}", ENRICH_CACHE_TYPE)).fetchone()
            if row and isinstance(row["payload"], dict):
                return row["payload"]
    return None


async def _default_llm() -> JsonLLM:
    return await get_vertex_llm()


@dataclass
class NewsDeps:
    web_search: Callable[..., Awaitable[crustdata.Page]] = crustdata.web_search
    web_fetch: Callable[..., Awaitable[crustdata.FetchResult]] = crustdata.web_fetch
    save: SaveFn = save_evidence
    cached_enrich: CachedEnrich = db_cached_enrich
    get_llm: Callable[[], Awaitable[JsonLLM]] = _default_llm


# ---- one page -----------------------------------------------------------------------------
@dataclass
class Finding:
    value: dict[str, Any]
    source: Source


@dataclass
class Run:
    job_id: Any
    names: Names
    s: Settings
    deps: NewsDeps
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fetched: int = 0


async def _save(run: Run, raw: Any, url: str, name: str, **kw: Any) -> Source:
    return await asyncio.to_thread(run.deps.save, raw, url, name, **kw)


async def classify(run: Run, text: str) -> NewsLabel | None:
    """v2: the LLM's label, only if its quote really is in the text."""
    city = f" ({run.names.city})" if run.names.city else ""
    prompt = LLM_PROMPT.format(
        name=run.names.legal or run.names.trade, city=city, text=text[: run.s.news.llm_max_chars]
    )
    try:
        llm = await run.deps.get_llm()
        raw = await llm.generate_json(
            prompt, NewsLabel.model_json_schema(), job_id=run.job_id, endpoint=LLM_ENDPOINT
        )
        label = NewsLabel.model_validate_json(raw)
    except (GatewayError, ValidationError) as exc:
        run.notes.append(f"LLM label skipped: {type(exc).__name__}")
        return None
    if not quote_in_text(label.quote, text):
        log.warning("news label quote not in text", extra={"label": label.label.value})
        run.notes.append(f"LLM label {label.label.value} rejected: quote not in the article")
        return None
    return label


def _value(c: Candidate, url: str, art: Article | None, quote: str, **kw: Any) -> dict[str, Any]:
    published = art.published if art else None
    return {
        "url": url,
        "title": c.title,
        "quote": quote,
        "record_date": published.isoformat() if published else None,
        "date_from": art.date_from if art else None,
        "found_in": c.origin,
        **kw,
    }


async def read_page(run: Run, c: Candidate) -> bool:
    """Fetch one page and look for an issue. False if the page could not be read."""
    run.fetched += 1
    try:
        fr = await run.deps.web_fetch(run.job_id, c.url)
    except (UnresolvableURLError, CrustdataError, GatewayError) as exc:
        run.notes.append(f"not fetched {c.url}: {type(exc).__name__}")
        return False
    content = known((fr.record or {}).get("content"))
    if not (fr.record and fr.record.get("success") is not False and isinstance(content, str)):
        run.notes.append(f"no content from {c.url}")
        return False
    art = extract(content, run.s.news.date_meta_names, run.s.news.min_article_chars)
    hit = find_hit(art.text, run.names, run.s)
    label = None
    if run.s.news.use_llm and (hit or mentions(art.text, run.names, run.s)):
        label = await classify(run, art.text)
    if hit:
        extra = {"category": hit.category, "keyword": hit.keyword, "via": hit.via,
                 "llm_label": label.label.value if label else None}  # fmt: skip
        await _add(run, fr.record, fr.url, c, art, hit.quote, extra)
    elif label and label.label in NEGATIVE:
        extra = {"category": label.label.value, "keyword": None, "via": "llm", "llm_label":
                 label.label.value}  # fmt: skip
        await _add(run, fr.record, fr.url, c, art, label.quote, extra)
    return True


async def _add(
    run: Run, raw: Any, url: str, c: Candidate, art: Article, quote: str, extra: dict[str, Any]
) -> None:
    src = await _save(run, raw, url, SOURCE_ARTICLE, quote=quote, record_date=art.published)
    run.findings.append(Finding(_value(c, url, art, quote, **extra), src))


async def check_blurb(run: Run, c: Candidate, search_url: str) -> None:
    """A result we could not read: apply the lexicon to its snippet / title instead."""
    hit = find_hit(c.blurb, run.names, run.s) if c.blurb else None
    if hit is None:
        return
    url = c.url or (c.raw.get("url") if isinstance(c.raw, dict) else None) or search_url
    name = SOURCE_SEARCH if c.origin == "search" else SOURCE_ENRICH
    src = await _save(run, c.raw, url, name, field_path=c.path, quote=hit.quote)
    extra = {"category": hit.category, "keyword": hit.keyword, "via": hit.via, "llm_label": None,
             "page_read": False}  # fmt: skip
    run.findings.append(Finding(_value(c, url, None, hit.quote, **extra), src))


# ---- the step -----------------------------------------------------------------------------
@dataclass
class Search:
    query: str
    source: Source | None = None
    error: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)


async def search(run: Run, query: str, url: str) -> Search:
    try:
        page = await run.deps.web_search(run.job_id, query)
    except (CrustdataError, GatewayError) as exc:
        return Search(query, error=f"{type(exc).__name__}: {exc}")
    src = await _save(run, page.records, url, SOURCE_SEARCH, field_path=query)
    return Search(query, src, records=page.records)


def decide(run: Run, searches: list[Search]) -> Fact[Any]:
    if run.findings:
        return verified([f.value for f in run.findings], [f.source for f in run.findings])
    failed = [x for x in searches if x.error]
    if failed or not searches:
        why = "; ".join(f"{x.query!r}: {x.error}" for x in failed) or "no search was run"
        return cannot_verify(f"news search failed: {why}")
    return verified(NO_ADVERSE_NEWS, [x.source for x in searches if x.source])


async def gather(run: Run, account: dict[str, Any]) -> list[Search]:
    s = run.s
    search_url = s.crustdata.base_url + crustdata.PATHS["web_search"]
    query = s.news.query_template.format(name=run.names.query_name)
    searches = [await search(run, query, search_url)]
    enrich = await asyncio.to_thread(run.deps.cached_enrich, account)
    cands = merge(search_candidates(searches[0].records), enrich_candidates(enrich))
    unread = []
    for c in cands:
        if c.url and run.fetched < s.news.max_fetch and await read_page(run, c):
            continue
        unread.append(c)
    for c in unread:
        await check_blurb(run, c, search_url)
    if enrich is None:
        run.notes.append("no cached enrich news for this account")
    return searches


async def run_news(ctx: StepContext, deps: NewsDeps | None = None) -> dict[str, Any]:
    s = ctx.config
    account = ctx.account or {}
    data = account.get("data") or {}
    run = Run(ctx.job_id, names_from(account, s), s, deps or NewsDeps())
    searches = await gather(run, account) if run.names.query_name else []
    if not run.names.query_name:
        fact = cannot_verify("no company name to search news for")
    else:
        fact = decide(run, searches)
    summary = {
        "queries": [x.query for x in searches],
        "pages_fetched": run.fetched,
        "findings": len(run.findings),
        "notes": run.notes,
    }
    patch = {"adverse_news": fact.model_dump(mode="json"), "news": summary}
    evs = tuple(sorted({src.evidence_id for src in fact.sources}))
    ctx.write(
        _UPDATE_ACCOUNT,
        (Jsonb(patch), account.get("id")),
        tbl="accounts",
        record_id=account.get("id"),
        before={k: data.get(k) for k in patch},
        after=patch,
        reason=f"news: {fact.status.value}",
        evidence_ids=evs,
    )
    if run.findings:
        ctx.hold(f"adverse news: {len(run.findings)} item(s) for a person to review", evs)
    elif fact.status is FactStatus.cannot_verify and s.news.hold_on_cannot_verify:
        ctx.hold(f"news: {fact.missing}", evs)
    log.info("news", extra={"status": fact.status.value, "findings": len(run.findings)})
    return {**summary, "adverse_news": patch["adverse_news"]}
