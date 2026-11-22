"""Request identity: canonical JSON + sha256. Auth headers are never part of the hash."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no whitespace. None -> "null"."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def request_hash(method: str, url: str, params: Any = None, body: Any = None) -> str:
    parts = [method.upper(), url, canonical_json(params), canonical_json(body)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
