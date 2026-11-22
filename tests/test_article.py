from __future__ import annotations

from datetime import date

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.trust.article import extract

S = load_settings(DEFAULT_CONFIG_PATH)
NAMES = S.news.date_meta_names
MIN = S.news.min_article_chars
BODY = "Acme Foods Private Limited was raided on Monday. " * 6


def test_jsonld_date_in_graph_and_main_text_only() -> None:
    html = f"""<html><head><script type="application/ld+json">
    {{"@graph": [{{"@type": "WebPage"}}, {{"@type": "NewsArticle",
      "datePublished": "2026-11-02T09:30:00+05:30"}}]}}</script>
    <script>var tracking = "fraud";</script><style>.x{{}}</style></head>
    <body><nav>Home | Scam alerts</nav><article><h1>Raid</h1><p>{BODY}</p></article>
    <footer>Footer text</footer></body></html>"""
    art = extract(html, NAMES, MIN)
    assert art.published == date(2026, 11, 2) and art.date_from == "json-ld"
    assert "Acme Foods Private Limited was raided" in art.text
    for junk in ("tracking", "Scam alerts", "Footer text", ".x"):
        assert junk not in art.text


def test_meta_published_time() -> None:
    html = ('<html><head><meta property="article:published_time" content="2026-10-30T10:00:00Z">'
            f"</head><body><p>{BODY}</p></body></html>")  # fmt: skip
    art = extract(html, NAMES, MIN)
    assert art.published == date(2026, 10, 30) and art.date_from == "meta:article:published_time"


def test_no_date_is_unknown_and_short_article_falls_back_to_body() -> None:
    html = f"<html><body><article>Share</article><div>{BODY}</div></body></html>"
    art = extract(html, NAMES, MIN)
    assert art.published is None and art.date_from is None
    assert "raided" in art.text


def test_bad_jsonld_is_ignored_and_plain_text_passes_through() -> None:
    html = (
        '<html><head><script type="application/ld+json">{not json</script></head>'
        "<body>x</body></html>"
    )
    assert extract(html, NAMES, MIN).published is None
    assert extract("  plain   text\n\n here ", NAMES, MIN).text == "plain text\nhere"
