"""Main text and publish date from a fetched page, with the standard library only.

Text: <article> if the page has a non-trivial one, else <body>, without script/style/nav/
header/footer/aside/form/svg. Plain text (no tags) comes back unchanged, whitespace collapsed.
Date: JSON-LD datePublished, then <meta> tags (article:published_time, ...). None if absent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any

from app.discovery.normalize import parse_date

SKIP_TAGS = frozenset({"script", "style", "noscript", "nav", "header", "footer", "aside",
                       "form", "svg", "template", "iframe"})  # fmt: skip
BLOCK_TAGS = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
                        "section", "article", "blockquote"})  # fmt: skip
VOID_TAGS = frozenset({"br", "meta", "link", "img", "input", "hr", "source", "wbr"})
LD_JSON = "application/ld+json"
JSONLD_DATE_KEY = "datePublished"
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\s*\n\s*")
_HTML_HINT = re.compile(r"<\s*(html|body|head|p|div|article|meta)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Article:
    text: str
    published: date | None
    date_from: str | None  # "json-ld" | "meta:<name>" | None


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.article_depth = 0
        self.body: list[str] = []
        self.article: list[str] = []
        self.meta: list[tuple[str, str]] = []  # (lowercased name/property/itemprop, content)
        self.ld_blocks: list[str] = []
        self._ld: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            self._meta(a)
            return
        if tag == "script" and a.get("type", "").lower() == LD_JSON:
            self._ld = []
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "article":
            self.article_depth += 1
        if tag in BLOCK_TAGS:
            self._emit("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld is not None:
            self.ld_blocks.append("".join(self._ld))
            self._ld = None
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "article" and self.article_depth:
            self.article_depth -= 1
        if tag in BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._ld is not None:
            self._ld.append(data)
        elif not self.skip_depth:
            self._emit(data)

    def _emit(self, text: str) -> None:
        self.body.append(text)
        if self.article_depth:
            self.article.append(text)

    def _meta(self, a: dict[str, str]) -> None:
        content = a.get("content", "").strip()
        for key in ("property", "name", "itemprop"):
            if a.get(key) and content:
                self.meta.append((a[key].strip().lower(), content))


def clean(text: str) -> str:
    return _NL.sub("\n", _WS.sub(" ", text)).strip()


def _walk_dates(node: Any) -> Iterable[Any]:
    if isinstance(node, dict):
        if JSONLD_DATE_KEY in node:
            yield node[JSONLD_DATE_KEY]
        for v in node.values():
            yield from _walk_dates(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dates(v)


def jsonld_date(blocks: list[str]) -> date | None:
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for value in _walk_dates(data):
            d = parse_date(value)
            if d is not None:
                return d
    return None


def meta_date(
    meta: list[tuple[str, str]], names: tuple[str, ...]
) -> tuple[date | None, str | None]:
    for name in names:
        for key, content in meta:
            if key == name.lower():
                d = parse_date(content)
                if d is not None:
                    return d, f"meta:{name}"
    return None, None


def extract(content: str, date_meta_names: tuple[str, ...], min_article_chars: int) -> Article:
    if not _HTML_HINT.search(content):
        return Article(clean(content), None, None)
    p = _Parser()
    p.feed(content)
    p.close()
    article = clean("".join(p.article))
    text = article if len(article) >= min_article_chars else clean("".join(p.body))
    d = jsonld_date(p.ld_blocks)
    if d is not None:
        return Article(text, d, "json-ld")
    d, source = meta_date(p.meta, date_meta_names)
    return Article(text, d, source)
