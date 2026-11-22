"""Decide whether a response body is a "negative" (nothing found) result."""

from __future__ import annotations

from typing import Any

# Result-list keys used by the Crustdata responses we call (see API-research/summary.md).
RESULT_KEYS = ("matches", "results", "companies", "profiles", "job_listings")


def _dict_is_empty_result(body: dict[str, Any]) -> bool:
    lists = [body[k] for k in RESULT_KEYS if isinstance(body.get(k), list)]
    return bool(lists) and all(len(v) == 0 for v in lists)


def is_negative(body: Any) -> bool:
    """True when the body has result lists and all of them are empty.

    Handles both `{"matches": []}` and identify/enrich arrays `[{"matches": []}, ...]`.
    Bodies without a recognised result list are not negative.
    """
    if isinstance(body, dict):
        return _dict_is_empty_result(body)
    if isinstance(body, list):
        if not body:
            return True
        return all(isinstance(item, dict) and _dict_is_empty_result(item) for item in body)
    return False
