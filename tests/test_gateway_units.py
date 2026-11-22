"""Pure-function tests for app/gateway (no DB, no network)."""

from __future__ import annotations

import httpx
import pytest

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.gateway.core import (
    GatewayError,
    _final_credits,
    backoff_s,
    endpoint_group,
    header_credits,
    is_retryable_status,
    retry_after_s,
)
from app.gateway.hashing import canonical_json, request_hash
from app.gateway.negative import is_negative
from app.gateway.ratelimit import refill_and_take


def test_hash_is_order_insensitive_and_method_case_insensitive() -> None:
    a = request_hash("post", "https://x/y", {"b": 1, "a": 2}, {"z": [1, 2], "k": "v"})
    b = request_hash("POST", "https://x/y", {"a": 2, "b": 1}, {"k": "v", "z": [1, 2]})
    assert a == b
    assert len(a) == 64


def test_hash_differs_on_body_url_and_method() -> None:
    base = request_hash("POST", "https://x/y", None, {"q": 1})
    assert base != request_hash("POST", "https://x/y", None, {"q": 2})
    assert base != request_hash("POST", "https://x/z", None, {"q": 1})
    assert base != request_hash("GET", "https://x/y", None, {"q": 1})


def test_canonical_json() -> None:
    assert canonical_json({"b": 1, "a": None}) == '{"a":null,"b":1}'
    assert canonical_json(None) == "null"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"matches": []}, True),
        ({"results": [], "metadata": {}}, True),
        ({"companies": [], "next_cursor": None}, True),
        ([{"matched_on": "x", "matches": []}], True),
        ([{"matched_on": "x", "matches": []}, {"matched_on": "y", "matches": [{}]}], False),
        ([], True),
        ({"matches": [{"id": 1}]}, False),
        ({"account": {"credits": 1}}, False),  # no result list -> not negative
        ("plain text", False),
    ],
)
def test_is_negative(body: object, expected: bool) -> None:
    assert is_negative(body) is expected


def test_bucket_takes_when_full_and_waits_when_empty() -> None:
    assert refill_and_take(1.0, 0.0, rate_per_s=0.1, capacity=1) == (0.0, 0.0)
    tokens, wait = refill_and_take(0.0, 2.0, rate_per_s=0.1, capacity=1)
    assert tokens == pytest.approx(0.2)
    assert wait == pytest.approx(8.0)


def test_bucket_never_exceeds_capacity() -> None:
    tokens, wait = refill_and_take(0.0, 10_000.0, rate_per_s=1.0, capacity=3)
    assert (tokens, wait) == (2.0, 0.0)


def test_backoff_grows_and_is_capped() -> None:
    s = load_settings(DEFAULT_CONFIG_PATH)
    g = s.gateway
    for attempt in range(1, 4):
        w = backoff_s(s, attempt)
        low = min(g.backoff_max_s, g.backoff_base_s * 2 ** (attempt - 1))
        assert low <= w <= min(g.backoff_max_s, low + g.backoff_jitter_s)
    assert backoff_s(s, 50) == g.backoff_max_s
    assert backoff_s(s, 1, retry_after=g.backoff_max_s * 10) == g.backoff_max_s


def test_retryable_statuses() -> None:
    assert is_retryable_status(429)
    assert is_retryable_status(503)
    assert not is_retryable_status(400)
    assert not is_retryable_status(404)
    assert not is_retryable_status(200)


def test_header_and_retry_after_parsing() -> None:
    r = httpx.Response(200, headers={"X-Credits-Used": "0.03", "Retry-After": "7"})
    assert header_credits(r) == 0.03
    assert retry_after_s(r) == 7.0
    assert header_credits(httpx.Response(200, headers={"X-Credits-Used": "n/a"})) is None
    assert retry_after_s(httpx.Response(429)) is None


def test_final_credits_rule() -> None:
    assert _final_credits(0.4, True, 2.0) == 0.4  # header wins
    assert _final_credits(None, True, 2.0) == 2.0  # estimate on success
    assert _final_credits(None, False, 2.0) == 0.0  # no charge assumed on failure


def test_endpoint_group() -> None:
    assert endpoint_group("crustdata:company/identify") == "crustdata"
    with pytest.raises(GatewayError):
        endpoint_group("company/identify")
