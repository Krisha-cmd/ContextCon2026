"""Build tests/fixtures/crustdata/*.json from real probe responses in calls.jsonl.

Responses are real but trimmed (long lists cut) and people data is redacted
(names, headlines, personal profile URLs), because the repo is public.

    python API-research/make_fixtures.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "tests" / "fixtures" / "crustdata"
CALLS = HERE / "calls.jsonl"

# call number in calls.jsonl -> fixture name
PICK = {
    3: "company_search",  # India logistics search, 10 companies, next_cursor
    7: "company_enrich",  # 5 domains, beardsell.co.in returns two matches (twins)
    14: "company_identify",  # domains bluedart.com + mamaearth.in, exact_match true
    16: "company_identify_empty",  # made-up name, exact_match true -> matches []
    21: "person_search",  # Delhivery supply-chain heads (redacted)
    25: "job_search",  # Delhivery jobs, last 30 days
    18: "web_search",  # "Delhivery GST": google.com/goto redirect URLs
    20: "web_fetch",  # one news page (content truncated)
}
MAX_LIST = 3
MAX_CONTENT = 4000
PERSON_KEYS = {"name", "professional_network_name", "headline", "summary", "first_name", "last_name"}
PERSON_DROP = {"education", "skills", "profile_picture_permalink", "profile_picture_url"}
PROFILE_URL =re.compile(r"https?://(www\.)?linkedin\.com/in/[^\"\s]+")


def trim(obj, depth=0):
    """Cut long lists; keep dict keys. Top-level result lists are kept whole."""
    if isinstance(obj, dict):
        return {k: trim(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        keep = obj if depth <= 1 else obj[:MAX_LIST]
        return [trim(v, depth + 1) for v in keep]
    return obj


def redact_people(obj, in_person=False):
    if isinstance(obj, dict):
        person = in_person or "crustdata_person_id" in obj
        out = {}
        for k, v in obj.items():
            if person and k in PERSON_DROP:
                continue
            if person and k in PERSON_KEYS and isinstance(v, str) and v:
                out[k] = "REDACTED"
            elif k == "experience" and person:
                out[k] = redact_people(v, False)  # employer names are fine
            else:
                out[k] = redact_people(v, person)
        return out
    if isinstance(obj, list):
        return [redact_people(v, in_person) for v in obj]
    if isinstance(obj, str):
        return PROFILE_URL.sub("https://www.linkedin.com/in/redacted", obj)
    return obj


def shape(name: str, resp):
    if name == "company_enrich":
        # keep every domain and every match (the twins matter), trim inside company_data
        for item in resp:
            for m in item["matches"]:
                m["company_data"] = trim(m["company_data"], depth=2)
                hc = m["company_data"].get("headcount") or {}
                hc.pop("by_function_timeseries", None)
        return resp
    if name == "web_fetch":
        for item in resp:
            item["content"] = (item.get("content") or "")[:MAX_CONTENT]
        return resp
    return trim(resp)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    calls = {}
    # split on "\n" only: page content contains U+2028 which splitlines() would break on
    for line in CALLS.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["n"] in PICK:
            calls[rec["n"]] = rec
    for n, name in PICK.items():
        rec = calls[n]
        fixture = {
            "endpoint": rec["endpoint"],
            "request": rec["request"]["body"],
            "status": rec["status"],
            "credits_used": rec["credits_used"],
            "recorded_at": rec["timestamp"],
            "response": redact_people(shape(name, rec["response"])),
        }
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(fixture, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{path.name}: {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
