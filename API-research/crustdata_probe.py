"""
crustdata_probe.py - probe the Crustdata API (x-api-version 2025-11-01) before building on it.

Endpoints / params taken from https://docs.crustdata.com (llms-full.txt, per-endpoint reference
pages, /general/pricing, /general/credits, /general/rate-limits).

Env (or a .env file next to this script):
  CRUSTDATA_API_KEY      required
  CREDIT_USD             required, dollar value of 1 credit
  TINY_TRANSPORTER_NAME  optional, a small real Indian transporter for test 8 (skipped if unset)

Outputs (next to this script): calls.jsonl, summary.md, endpoints_snapshot.json
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
BASE = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
BUDGET_CREDITS = 3000.0
CALLS_FILE = HERE / "calls.jsonl"
SUMMARY_FILE = HERE / "summary.md"

# Documented list prices (https://docs.crustdata.com/general/pricing). Used ONLY when the
# X-Credits-Used header is missing; such values are flagged credits_source="computed".
PRICE = {
    "company_search_per_result": 0.03,
    "company_search_premium_group": 0.2,  # headcount, funding, hiring, followers ...
    "company_search_taxonomy": 0.1,
    "company_enrich_per_record": 2.0,
    "company_identify_per_match": 0.2,
    "person_search_per_result_no_fields": 0.23,
    "person_enrich_base": 1.0,
    "job_search_per_result": 0.03,
    "web_search_per_page": 1.0,
    "web_fetch_per_page": 1.0,
}


# --------------------------------------------------------------------------- setup
def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    key = os.environ.get("CRUSTDATA_API_KEY")
    usd = os.environ.get("CREDIT_USD")
    if not key or not usd:
        sys.exit("Set CRUSTDATA_API_KEY and CREDIT_USD (env or API-research/.env).")
    return key, float(usd)


API_KEY, CREDIT_USD = load_env()
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "x-api-version": API_VERSION,
    "Content-Type": "application/json",
}


class BudgetExceeded(Exception):
    pass


STATE = {"calls": [], "credits": 0.0}
RPM, LAST_CALL = {}, {}


def count_results(data):
    if isinstance(data, list):
        # identify / enrich style: [{matched_on, matches: [...]}] ; web fetch: [{success,...}]
        if data and isinstance(data[0], dict) and "matches" in data[0]:
            return sum(len(x.get("matches") or []) for x in data)
        return len(data)
    if isinstance(data, dict):
        for k in ("companies", "profiles", "job_listings", "results", "endpoints", "watches", "items", "data"):
            if isinstance(data.get(k), list):
                return len(data[k])
    return None


def call(method, path, test, body=None, params=None, est_credits=0.0, retry_429=True):
    """Make one API call, enforce budget, log to calls.jsonl. Returns (status, data, rec)."""
    if STATE["credits"] + est_credits > BUDGET_CREDITS:
        raise BudgetExceeded(f"next call ({path}, est {est_credits}) would exceed {BUDGET_CREDITS} credits")
    # Pace calls to the per-endpoint RPM reported by /account/endpoints (effective_rate_limit_rpm).
    rpm = RPM.get(path)
    if rpm:
        gap = 60.0 / rpm + 0.5
        wait = LAST_CALL.get(path, 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
    LAST_CALL[path] = time.time()
    url = BASE + path
    t0 = time.perf_counter()
    try:
        r = requests.request(method, url, headers=HEADERS, json=body, params=params, timeout=120)
        status, text, hdrs = r.status_code, r.text, r.headers
    except requests.RequestException as e:
        status, text, hdrs = None, f"EXCEPTION: {e!r}", {}
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    try:
        data = json.loads(text)
    except Exception:
        data = text

    hdr_credits = hdrs.get("X-Credits-Used") if hdrs else None
    if hdr_credits is not None:
        try:
            credits, src = float(hdr_credits), "header:X-Credits-Used"
        except ValueError:
            credits, src = est_credits, f"computed (unparseable header {hdr_credits!r})"
    else:
        credits = est_credits if (status and 200 <= status < 300) else 0.0
        src = "computed from pricing page (no X-Credits-Used header)"

    rec = {
        "n": len(STATE["calls"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "test": test,
        "endpoint": f"{method} {path}",
        "request": {"params": params, "body": body},
        "status": status,
        "latency_ms": latency_ms,
        "results_count": count_results(data),
        "credits_used": credits,
        "credits_source": src,
        "request_id": hdrs.get("X-Request-Id") if hdrs else None,
        "rate_limit": {k: hdrs.get(k) for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")} if hdrs else {},
        "response": data,
    }
    STATE["calls"].append(rec)
    STATE["credits"] += credits
    with CALLS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(f"  [{rec['n']:>3}] {method} {path} -> {status} {latency_ms}ms results={rec['results_count']} credits={credits} (total {STATE['credits']:.2f})")

    if status == 429 and retry_429:
        wait = 5
        try:
            wait = max(1, min(60, int(float(hdrs.get("X-RateLimit-Reset") or hdrs.get("Retry-After") or 5))))
        except ValueError:
            pass
        print(f"  429 rate-limited, waiting {wait}s then retrying once")
        time.sleep(wait)
        return call(method, path, test, body, params, est_credits, retry_429=False)
    if STATE["credits"] > BUDGET_CREDITS:
        raise BudgetExceeded(f"total credits {STATE['credits']} exceeded {BUDGET_CREDITS}")
    return status, data, rec


# --------------------------------------------------------------------------- helpers
def short(obj, n=300):
    s = json.dumps(obj, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj
    return s if len(s) <= n else s[:n] + "…"


def walk(obj, prefix=""):
    """Yield (path, value) leaves. Empty dict/list are leaves."""
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            yield from walk(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list) and obj:
        for i, v in enumerate(obj[:3]):  # sample first 3 list items
            yield from walk(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


DATE_KEY = re.compile(r"(date|_at$|_at\]|updated|indexed|refresh|timestamp|last_|_on$|year)", re.I)


def date_fields(obj):
    out = {}
    for p, v in walk(obj):
        leaf = p.split(".")[-1]
        if DATE_KEY.search(leaf):
            key = re.sub(r"\[\d+\]", "[]", p)
            out.setdefault(key, v)
    return out


def is_empty(v):
    return v is None or v == [] or v == {} or v == ""


def get(d, *path, default=None):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and len(d) > p:
            d = d[p]
        else:
            return default
    return default if d is None else d


def find_first(obj, pred):
    for p, v in walk(obj):
        if pred(p, v):
            return p, v
    return None, None


RESULTS = []  # per-test rows


def record(tid, step, name, passed, detail, calls_before):
    calls = STATE["calls"][calls_before:]
    row = {
        "id": tid, "step": step, "name": name, "passed": passed, "detail": detail,
        "calls": [c["n"] for c in calls],
        "latency_ms": [c["latency_ms"] for c in calls],
        "credits": round(sum(c["credits_used"] for c in calls), 4),
        "statuses": [c["status"] for c in calls],
    }
    RESULTS.append(row)
    print(f"== Test {tid} {name}: {passed}")
    return row


def run_test(tid, step, name, fn):
    before = len(STATE["calls"])
    print(f"\n--- Test {tid}: {name}")
    try:
        passed, detail = fn()
    except BudgetExceeded:
        raise
    except Exception as e:  # keep going; record the failure honestly
        passed, detail = "FAIL (exception)", f"{type(e).__name__}: {e}"
    return record(tid, step, name, passed, detail, before)


# --------------------------------------------------------------------------- shared context
CTX = {
    "india_logistics_filter": {
        "op": "and",
        "conditions": [
            {"field": "locations.country", "type": "in", "value": ["India"]},
            {"field": "taxonomy.professional_network_industry", "type": "=",
             "value": "Transportation, Logistics, Supply Chain and Storage"},
        ],
    },
    "companies": [],  # results of test 1
}


# --------------------------------------------------------------------------- A. market research
def t1_company_search():
    fields = ["basic_info", "headcount", "funding", "locations"]
    body = {"filters": CTX["india_logistics_filter"], "fields": fields, "limit": 10}
    est = 10 * (PRICE["company_search_per_result"] + PRICE["company_search_taxonomy"] + 2 * PRICE["company_search_premium_group"])
    st, data, _ = call("POST", "/company/search", "1", body, est_credits=est)
    notes = []
    if st == 400:
        # Group names may not be accepted by search; docs show dot-paths. Retry with dot-paths
        # taken from /account/endpoints fields.enabled for /company/search.
        notes.append(f"group-name fields rejected: {short(data, 200)}")
        enabled = CTX.get("search_fields_enabled") or []
        dot = [f for f in enabled if f.split(".")[0] in ("basic_info", "headcount", "funding", "locations")]
        if not dot:
            dot = ["basic_info.name", "basic_info.primary_domain", "headcount.total", "locations.headquarters", "locations.country"]
            notes.append("no field list from /account/endpoints; used dot-paths shown in docs (no funding path documented)")
        body["fields"] = dot
        st, data, _ = call("POST", "/company/search", "1", body, est_credits=est)
    comps = get(data, "companies", default=[]) if isinstance(data, dict) else []
    CTX["companies"] = comps
    CTX["search_total_count"] = get(data, "total_count")
    rows = []
    non_india = 0
    for c in comps:
        country = get(c, "locations", "country") or get(c, "locations", "headquarters")
        # API returns ISO-3 ("IND") in locations.country
        if country and str(country).strip().upper() not in ("IND", "INDIA") and "india" not in str(country).lower():
            non_india += 1
        rows.append(f"{get(c,'basic_info','name')} | {get(c,'basic_info','primary_domain')} | hc={get(c,'headcount','total')} | "
                    f"funding_keys={list((c.get('funding') or {}).keys())[:5]} | loc={short(c.get('locations'), 80)}")
    ok = st == 200 and len(comps) > 0 and non_india == 0
    detail = (f"status {st}; total_count={CTX['search_total_count']}; returned {len(comps)}; non-India HQ={non_india}. "
              + (" ".join(notes) + " " if notes else "")
              + ("Results: " + " ;; ".join(rows) if rows else f"Response: {short(data)}"))
    return ("PASS" if ok else "FAIL"), detail


def t2_pagination():
    base = {"filters": CTX["india_logistics_filter"], "fields": ["basic_info.name"], "limit": 5,
            "sorts": [{"field": "crustdata_company_id", "order": "asc"}]}
    est = 5 * (PRICE["company_search_per_result"] + PRICE["company_search_taxonomy"])
    st, d1, _ = call("POST", "/company/search", "2", base, est_credits=est)
    note = ""
    if st == 400:
        note = f"sort on crustdata_company_id rejected ({short(d1,150)}); retried without sorts. "
        base.pop("sorts")
        st, d1, _ = call("POST", "/company/search", "2", base, est_credits=est)
    cur = get(d1, "next_cursor")
    if st != 200 or not cur:
        return "FAIL", f"{note}page1 status {st}, next_cursor={cur!r}. Response: {short(d1)}"
    st2, d2, _ = call("POST", "/company/search", "2", {**base, "cursor": cur}, est_credits=est)
    ids1 = [c.get("crustdata_company_id") for c in get(d1, "companies", default=[])]
    ids2 = [c.get("crustdata_company_id") for c in get(d2, "companies", default=[])]
    dup = set(ids1) & set(ids2)
    ok = st2 == 200 and ids2 and not dup
    return ("PASS" if ok else "FAIL"), (f"{note}page1 ids={ids1}; page2 status {st2} ids={ids2}; duplicates={sorted(dup)}; "
                                        f"page2 next_cursor present={bool(get(d2,'next_cursor'))}")


def t3_competitors():
    body = {"filters": {"field": "competitors.websites", "type": "in", "value": ["delhivery.com"]},
            "fields": ["basic_info.name", "basic_info.primary_domain", "locations.country"], "limit": 10}
    st, data, _ = call("POST", "/company/search", "3", body, est_credits=10 * PRICE["company_search_per_result"])
    comps = get(data, "companies", default=[]) if isinstance(data, dict) else []
    names = [f"{get(c,'basic_info','name')} ({get(c,'basic_info','primary_domain')}, {get(c,'locations','country')})" for c in comps]
    ok = st == 200 and len(comps) > 0
    return ("PASS" if ok else "FAIL"), f"status {st}; total_count={get(data,'total_count')}; {len(comps)} companies: " + ("; ".join(names) if names else short(data))


ENRICH_GROUPS = ["basic_info", "competitors", "news", "hiring", "headcount", "funding", "people"]


def t4_enrich():
    domains = ["delhivery.com"]
    for c in CTX["companies"]:
        d = get(c, "basic_info", "primary_domain")
        if d and d not in domains:
            domains.append(d)
        if len(domains) == 5:
            break
    body = {"domains": domains, "fields": ENRICH_GROUPS}
    st, data, _ = call("POST", "/company/enrich", "4", body, est_credits=len(domains) * PRICE["company_enrich_per_record"])
    if st != 200 or not isinstance(data, list):
        return "FAIL", f"status {st}: {short(data)}"
    parts, enriched = [], {}
    group_null = {g: 0 for g in ENRICH_GROUPS}
    all_dates = {}
    fresh = {}
    n_matched = 0
    total_records = sum(len(get(i, "matches", default=[])) for i in data)
    for item in data:
        m = get(item, "matches", default=[])
        if not m:
            parts.append(f"{item.get('matched_on')}: NO MATCH")
            continue
        n_matched += 1
        if len(m) > 1:
            parts.append(f"{item.get('matched_on')}: {len(m)} MATCHES returned (each billed): "
                         f"{[(get(x,'company_data','crustdata_company_id'), get(x,'company_data','basic_info','name'), x.get('confidence_score')) for x in m]}")
        cd = m[0].get("company_data") or {}
        enriched[item.get("matched_on")] = cd
        fresh[item.get("matched_on")] = company_freshness(cd)
        nulls = []
        for g in ENRICH_GROUPS:
            gv = cd.get(g)
            if is_empty(gv) or (isinstance(gv, dict) and all(is_empty(v) for v in gv.values())):
                group_null[g] += 1
                nulls.append(f"{g}=<entire group empty>")
                continue
            leaves = list(walk(gv))
            empty = [p for p, v in leaves if is_empty(v)]
            if empty:
                nulls.append(f"{g}: {len(empty)}/{len(leaves)} leaves null [{', '.join(empty[:6])}{'…' if len(empty) > 6 else ''}]")
        for k, v in date_fields(cd).items():
            all_dates.setdefault(k, []).append(v)
        parts.append(f"{item.get('matched_on')} -> id {cd.get('crustdata_company_id')} '{get(cd,'basic_info','name')}' "
                     f"(conf {m[0].get('confidence_score')}); updated_at={cd.get('updated_at')}, indexed_at={cd.get('indexed_at')}; "
                     f"nulls: {'; '.join(nulls) if nulls else 'none'}")
    CTX["enriched"] = enriched
    CTX["enrich_group_null"] = group_null
    CTX["enrich_dates"] = all_dates
    CTX["enrich_fresh"] = fresh
    CTX["enrich_inputs"] = len(domains)
    CTX["enrich_records"] = total_records
    ok = n_matched == len(domains)
    return ("PASS" if ok else ("PARTIAL" if n_matched else "FAIL")), (
        f"{n_matched}/{len(domains)} inputs matched, {total_records} records returned. Groups fully empty per company: {group_null}. "
        + " || ".join(parts) + f" || Freshness per company: {fresh}")


def company_freshness(cd):
    """Latest date per field family (scans all list items, not a sample)."""
    def mx(vals):
        vals = [str(v)[:10] for v in vals if v]
        return max(vals) if vals else None
    hc = cd.get("headcount") or {}
    bft = hc.get("by_function_timeseries") or {}
    bft_dates = [pt.get("date") for grp in bft.values() if isinstance(grp, dict)
                 for series in grp.values() if isinstance(series, list) for pt in series if isinstance(pt, dict)]
    news = cd.get("news") if isinstance(cd.get("news"), list) else []
    people = cd.get("people") or {}
    hiring = cd.get("hiring") or {}
    return {
        "updated_at": cd.get("updated_at", "<absent>"), "indexed_at": cd.get("indexed_at", "<absent>"),
        "headcount.timeseries max": mx(p.get("date") for p in (hc.get("timeseries") or []) if isinstance(p, dict)),
        "headcount.by_function_timeseries max": mx(bft_dates),
        "news max article_publish_date": mx(n.get("article_publish_date") for n in news if isinstance(n, dict)),
        "news count": len(news),
        "funding.last_fundraise_date": get(cd, "funding", "last_fundraise_date"),
        "funding.total_investment_usd": get(cd, "funding", "total_investment_usd"),
        "hiring.openings_count": hiring.get("openings_count"),
        "hiring.recent_openings": len(hiring.get("recent_openings") or []),
        "people counts": {k: len(v) for k, v in people.items() if isinstance(v, list)},
    }


# --------------------------------------------------------------------------- B. trust & compliance
def identify(test, key, values, exact_match=None):
    body = {key: values}
    if exact_match is not None:
        body["exact_match"] = exact_match
    st, data, _ = call("POST", "/company/identify", test, body, est_credits=len(values) * PRICE["company_identify_per_match"])
    return st, data


def top_matches(data, k=3):
    out = []
    for item in data if isinstance(data, list) else []:
        ms = item.get("matches") or []
        out.append({
            "input": item.get("matched_on"), "match_type": item.get("match_type"), "n_matches": len(ms),
            "top": [(get(m, "company_data", "crustdata_company_id"), get(m, "company_data", "basic_info", "name"),
                     get(m, "company_data", "basic_info", "primary_domain"), m.get("confidence_score")) for m in ms[:k]],
        })
    return out


def t5_identify_messy():
    ids, parts = {}, []
    st, d = identify("5", "domains", ["delhivery.com"])
    tm = top_matches(d)
    parts.append(f"domain: status {st} {tm}")
    ids["delhivery.com"] = tm[0]["top"][0][0] if tm and tm[0]["top"] else None
    # LinkedIn URL: taken from the API's own record if present, else the public URL.
    _, li = find_first(d, lambda p, v: isinstance(v, str) and "linkedin.com/company" in v)
    li_src = "from identify response"
    if not li:
        li = "https://www.linkedin.com/company/delhivery"
        li_src = "not in identify response; used public URL"
    st, d = identify("5", "names", ["DELHIVERY LTD"])
    tm = top_matches(d)
    parts.append(f"name 'DELHIVERY LTD': status {st} {tm}")
    ids["DELHIVERY LTD"] = tm[0]["top"][0][0] if tm and tm[0]["top"] else None
    st, d = identify("5", "professional_network_profile_urls", [li])
    tm = top_matches(d)
    parts.append(f"linkedin {li} ({li_src}): status {st} {tm}")
    ids["linkedin"] = tm[0]["top"][0][0] if tm and tm[0]["top"] else None
    CTX["delhivery_id"] = ids["delhivery.com"]
    same = len(set(ids.values())) == 1 and None not in ids.values()
    return ("PASS" if same else "FAIL"), f"top-match ids: {ids}. " + " | ".join(parts)


def t6_lookalikes():
    st1, d1 = identify("6", "names", ["Honasa Consumer", "Mamaearth"])
    st2, d2 = identify("6", "names", ["Blue Dart", "Blue Dart Aviation"])
    a, b = top_matches(d1, 5), top_matches(d2, 5)

    def tid(x, i):
        return x[i]["top"][0][0] if len(x) > i and x[i]["top"] else None
    pair1 = (tid(a, 0), tid(a, 1))
    pair2 = (tid(b, 0), tid(b, 1))
    detail = (f"Honasa vs Mamaearth (status {st1}): top ids {pair1} -> {'SAME entity' if pair1[0] == pair1[1] else 'different entities'}; {a} || "
              f"Blue Dart vs Blue Dart Aviation (status {st2}): top ids {pair2} -> {'SAME entity' if pair2[0] == pair2[1] else 'different entities'}; {b}")
    # No ground truth in docs for parent/brand linkage -> informational, report what came back.
    ok = st1 == 200 and st2 == 200 and None not in pair1 + pair2
    return ("PASS (informational)" if ok else "FAIL"), detail + ". No parent/brand-relationship field was returned or documented for identify."


def t7_exact_match():
    # bluedart.com (Blue Dart / Blue Dart Aviation) and mamaearth.in (a prior run showed two entities,
    # 'Mamaearth' and 'Mamaearth Dummy', both with primary_domain mamaearth.in).
    doms = ["bluedart.com", "mamaearth.in"]
    st1, d1 = identify("7", "domains", doms, exact_match=False)
    st2, d2 = identify("7", "domains", doms, exact_match=True)
    a, b = top_matches(d1, 10), top_matches(d2, 10)
    parts = []
    for i, dom in enumerate(doms):
        ra = a[i] if len(a) > i else {}
        rb = b[i] if len(b) > i else {}
        parts.append(f"{dom}: exact_match=false -> {ra.get('n_matches')} matches {ra.get('top')}; "
                     f"exact_match=true -> {rb.get('n_matches')} matches {rb.get('top')}")
    return ("PASS (informational)" if st1 == 200 and st2 == 200 else "FAIL"), f"status {st1}/{st2}. " + " | ".join(parts)


def t8_not_found():
    parts, ok = [], True
    fake = "Zqvantrix Freightlines Pvt Ltd"
    before = len(STATE["calls"])
    st, d = identify("8", "names", [fake])
    tm = top_matches(d)
    cr = STATE["calls"][-1]["credits_used"]
    n = tm[0]["n_matches"] if tm else None
    parts.append(f"made-up '{fake}': status {st}, matches={n}, credits={cr}, top={tm[0]['top'] if tm else short(d)}")
    ok &= st == 200 and n == 0 and cr == 0
    # Same made-up name with exact_match=true: does it suppress fuzzy junk matches?
    st, d = identify("8", "names", [fake], exact_match=True)
    tm = top_matches(d)
    cr = STATE["calls"][-1]["credits_used"]
    parts.append(f"made-up with exact_match=true: status {st}, matches={tm[0]['n_matches'] if tm else None}, credits={cr}")
    tiny = os.environ.get("TINY_TRANSPORTER_NAME")
    if tiny:
        st, d = identify("8", "names", [tiny])
        tm = top_matches(d)
        cr = STATE["calls"][-1]["credits_used"]
        n = tm[0]["n_matches"] if tm else None
        parts.append(f"tiny real transporter '{tiny}': status {st}, matches={n}, credits={cr}, top={tm[0]['top'] if tm else short(d)}")
    else:
        parts.append("tiny real transporter: SKIPPED (TINY_TRANSPORTER_NAME not set; no verified name available)")
    return ("PASS" if ok else "FAIL"), " | ".join(parts)


DATE_IN_TEXT = [
    ("meta article:published_time", re.compile(r'article:published_time"?\s+content="([^"]+)"', re.I)),
    ("json-ld datePublished", re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I)),
    ("json-ld dateModified", re.compile(r'"dateModified"\s*:\s*"([^"]+)"', re.I)),
    ("<time datetime>", re.compile(r'<time[^>]*datetime="([^"]+)"', re.I)),
    ("ISO date in text", re.compile(r"\b(20\d\d-\d\d-\d\d)\b")),
    ("written date in text", re.compile(r"\b(\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* 20\d\d|"
                                        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? 20\d\d)\b")),
]


def t9_web():
    parts = []
    st1, d1, _ = call("POST", "/web/search/live", "9", {"query": "Delhivery GST", "location": "IN", "sources": ["web"]},
                      est_credits=PRICE["web_search_per_page"])
    st2, d2, _ = call("POST", "/web/search/live", "9", {"query": "Delhivery fraud news", "location": "IN", "sources": ["news"]},
                      est_credits=PRICE["web_search_per_page"])
    for label, st, d in (("GST/web", st1, d1), ("fraud/news", st2, d2)):
        res = get(d, "results", default=[]) if isinstance(d, dict) else []
        keys = sorted({k for r in res for k in (r.keys() if isinstance(r, dict) else [])})
        dated = date_fields(res)
        relevant = sum(1 for r in res if "delhivery" in f"{r.get('title','')} {r.get('snippet','')}".lower())
        parts.append(f"{label}: status {st}, {len(res)} results ({relevant} mention 'Delhivery' in title/snippet), "
                     f"result keys={keys}, date-like keys={list(dated) or 'none'}, "
                     f"top={[(r.get('title'), r.get('url')) for r in res[:3]]}")
    res2 = get(d2, "results", default=[]) if isinstance(d2, dict) else []
    res1 = get(d1, "results", default=[]) if isinstance(d1, dict) else []
    all_urls = [r.get("url") for r in res2 + res1 if isinstance(r, dict) and r.get("url")]
    redirects = [u for u in all_urls if "google.com/goto" in u]
    parts.append(f"{len(redirects)}/{len(all_urls)} result URLs are google.com/goto redirect links, not the source URL")
    target = next((u for u in all_urls if "google.com/goto" not in u), None)
    if not target:
        return "FAIL", " | ".join(parts) + " | no URL to fetch"
    st3, d3, _ = call("POST", "/web/enrich/live", "9", {"urls": [target]}, est_credits=PRICE["web_fetch_per_page"])
    item = d3[0] if isinstance(d3, list) and d3 else {}
    html = item.get("content") or ""
    found = []
    for label, rx in DATE_IN_TEXT:
        m = rx.search(html)
        if m:
            found.append(f"{label}={m.group(1)!r}")
    parts.append(f"fetch {target}: status {st3}, success={item.get('success')}, timestamp(fetch time)={item.get('timestamp')}, "
                 f"title={item.get('title')!r}, content_len={len(html)}, publish-date evidence in page: {found or 'NONE'}")
    ok = st1 == 200 and st2 == 200 and st3 == 200 and item.get("success") and bool(found)
    return ("PASS" if ok else "FAIL"), " | ".join(parts)


# --------------------------------------------------------------------------- C. revenue
def revenue_companies():
    """Delhivery + the largest other company from test 1 (by headcount)."""
    out = [("Delhivery", "delhivery.com", CTX.get("delhivery_id"))]
    others = [c for c in CTX["companies"] if get(c, "basic_info", "primary_domain") not in (None, "delhivery.com")]
    others.sort(key=lambda c: get(c, "headcount", "total", default=0) or 0, reverse=True)
    if others:
        c = others[0]
        out.append((get(c, "basic_info", "name"), get(c, "basic_info", "primary_domain"), c.get("crustdata_company_id")))
    return out


def t10_person_search():
    parts, found = [], []
    for name, domain, _ in revenue_companies():
        body = {
            "filters": {"op": "and", "conditions": [
                {"field": "experience.employment_details.current.company_website_domain", "type": "=", "value": domain},
                {"op": "or", "conditions": [
                    {"field": "experience.employment_details.current.title", "type": "(.)", "value": "supply chain"},
                    {"field": "experience.employment_details.current.title", "type": "(.)", "value": "logistics"},
                ]},
                {"op": "or", "conditions": [
                    {"field": "experience.employment_details.current.title", "type": "(.)", "value": "head"},
                    {"field": "experience.employment_details.current.title", "type": "(.)", "value": "vp"},
                    {"field": "experience.employment_details.current.title", "type": "(.)", "value": "director"},
                ]},
            ]},
            "limit": 5,
        }
        st, d, _ = call("POST", "/person/search", "10", body, est_credits=5 * PRICE["person_search_per_result_no_fields"])
        profs = get(d, "profiles", default=[]) if isinstance(d, dict) else []
        if st == 200 and not profs:
            # Fallback: match current employer by name words instead of website domain.
            parts.append(f"{name}: 0 results filtering by current.company_website_domain={domain}; retrying by company_name (.) '{name}'")
            body["filters"]["conditions"][0] = {"field": "experience.employment_details.current.company_name", "type": "(.)", "value": name}
            st, d, _ = call("POST", "/person/search", "10", body, est_credits=5 * PRICE["person_search_per_result_no_fields"])
            profs = get(d, "profiles", default=[]) if isinstance(d, dict) else []
        rows = []
        for p in profs:
            _, url = find_first(p, lambda k, v: isinstance(v, str) and "linkedin.com/in/" in v)
            rows.append(f"{get(p,'basic_profile','name')} — {get(p,'basic_profile','current_title')}")
            if url:
                found.append((url, get(p, "basic_profile", "name")))
        parts.append(f"{name} ({domain}): status {st}, total_count={get(d,'total_count')}, returned {len(profs)}: {rows or short(d, 200)}")
    CTX["person_urls"] = found
    with_hits = sum(1 for p in parts if "returned " in p and "returned 0" not in p)
    n_cos = len(revenue_companies())
    verdict = "PASS" if with_hits == n_cos else ("PARTIAL" if with_hits else "FAIL")
    return f"{verdict} ({with_hits}/{n_cos} companies with results)", " | ".join(parts)


def t11_person_enrich():
    urls = CTX.get("person_urls") or []
    if not urls:
        return "SKIPPED", "no profile URL from test 10"
    url, name = urls[0]
    st, d, _ = call("POST", "/person/enrich", "11", {"professional_network_profile_urls": [url]},
                    est_credits=PRICE["person_enrich_base"])
    item = d[0] if isinstance(d, list) and d else {}
    m = (item.get("matches") or [{}])[0] if item.get("matches") else {}
    pd = m.get("person_data") or {}
    groups = sorted(pd.keys())
    contact_like = [p for p, v in walk(pd) if re.search(r"email|phone", p, re.I) and not is_empty(v)]
    nulls = [p for p, v in walk(pd) if is_empty(v)]
    return ("PASS" if st == 200 and pd and not contact_like else "FAIL"), (
        f"{name} {url}: status {st}, match_status={item.get('match_status')}, conf={m.get('confidence_score')}, groups={groups}, "
        f"updated_at={pd.get('updated_at')}, basic_profile.last_updated={get(pd,'basic_profile','last_updated')}, "
        f"contact fields with values={contact_like or 'none'}, null leaves={len(nulls)} [{', '.join(nulls[:10])}]")


def t12_jobs():
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    parts, ok = [], True
    for name, domain, _ in revenue_companies():
        body = {"filters": {"op": "and", "conditions": [
                    {"field": "company.basic_info.primary_domain", "type": "=", "value": domain},
                    {"field": "metadata.date_added", "type": "=>", "value": since}]},
                "sorts": [{"field": "metadata.date_added", "order": "desc"}],
                "limit": 5}
        st, d, _ = call("POST", "/job/search", "12", body, est_credits=5 * PRICE["job_search_per_result"])
        jobs = get(d, "job_listings", default=[]) if isinstance(d, dict) else []
        rows = [f"{get(j,'job_details','title')} | added {get(j,'metadata','date_added')} | {get(j,'location','raw') or get(j,'location','city')}" for j in jobs]
        old = [r for j, r in zip(jobs, rows) if str(get(j, "metadata", "date_added", default=""))[:10] < since]
        ok &= st == 200 and not old
        parts.append(f"{name} ({domain}) since {since}: status {st}, total_count={get(d,'total_count')}, returned {len(jobs)}, "
                     f"older-than-30d={len(old)}: {rows or short(d, 200)}")
    return ("PASS" if ok else "FAIL"), " | ".join(parts)


WATCH_SHAPE = {
    "POST /watch/company/search": {
        "filters": "<same filter grammar as /company/search>",
        "fields": ["optional"], "sorts": ["optional"],
        "config": {"trigger": {"type": "interval", "every_hours": "N"}, "max_results_per_run": "1-10000 (default 25)",
                   "payload_delivery_type": "inline | link", "is_realtime": "bool (every_hours>=24 if true)"},
        "notifications": [{"type": "webhook", "url": "..."}, {"type": "slack", "webhook_url": "..."}],
    }
}


def t13_watchers():
    eps = CTX.get("endpoints_list") or []
    watch_eps = [(e.get("path"), e.get("status"), e.get("base_credits"), e.get("effective_rate_limit_rpm"))
                 for e in eps if "watch" in str(e.get("path", ""))]
    # Listing existing watches (read-only). Nothing is created.
    st, d, _ = call("GET", "/watch/company/search", "13", est_credits=0)
    return ("PASS (read-only)" if st == 200 or watch_eps else "FAIL"), (
        f"watch endpoints in /account/endpoints: {watch_eps or 'none listed'}. GET /watch/company/search status {st}: {short(d, 250)}. "
        f"Documented create shape (NOT called): {json.dumps(WATCH_SHAPE)}. Documented types: company/person/job/social_post discovery "
        f"(/watch/{{dataset}}/search) and person/company entity watches (/watch/person, /watch/company).")


# --------------------------------------------------------------------------- account
def balance(label):
    st, d, _ = call("GET", "/account/credits", f"balance-{label}")
    return get(d, "account", "credits") if st == 200 else None, d


def snapshot_endpoints():
    st, d, _ = call("GET", "/account/endpoints", "balance-before")
    (HERE / "endpoints_snapshot.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    eps = get(d, "endpoints", default=[]) if isinstance(d, dict) else []
    CTX["endpoints_list"] = eps
    CTX["token_limit_rpm"] = get(d, "token_limit_rpm")
    for e in eps:
        if e.get("path") and e.get("effective_rate_limit_rpm"):
            RPM[e["path"]] = e["effective_rate_limit_rpm"]
        if e.get("path") in ("/company/search", "company/search"):
            CTX["search_fields_enabled"] = get(e, "fields", "enabled", default=[])
    return eps


# --------------------------------------------------------------------------- summary
def unit_cost(endpoint_sub, per="result", fallback=None):
    """Observed credits per result / per call / per input identifier / per matched identifier."""
    cs = [c for c in STATE["calls"] if endpoint_sub in c["endpoint"] and c["status"] == 200 and c["credits_source"].startswith("header")]
    if not cs:
        return fallback, "pricing page (not observed)"
    tot = sum(c["credits_used"] for c in cs)
    if per == "call":
        return tot / len(cs), f"observed avg over {len(cs)} calls"
    if per in ("input", "matched_input"):
        n = 0
        for c in cs:
            resp = c["response"] if isinstance(c["response"], list) else []
            if per == "input":
                n += len(resp)
            else:
                n += sum(1 for i in resp if i.get("matches"))
        return (tot / n if n else fallback), f"observed {tot:.3f} cr / {n} {'inputs' if per == 'input' else 'matched inputs'}"
    n = sum(c["results_count"] or 0 for c in cs)
    return (tot / n if n else fallback), (f"observed {tot:.3f} cr / {n} results" if n else "no results observed")


def write_summary(bal_before, bal_after, bal_raw, aborted):
    L = []
    w = L.append
    total_logged = round(STATE["credits"], 4)
    diff = round(bal_before - bal_after, 4) if bal_before is not None and bal_after is not None else "unclear"
    w(f"# Crustdata API probe — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
    w(f"API version `{API_VERSION}`, base `{BASE}`. CREDIT_USD = {CREDIT_USD}. Budget cap {BUDGET_CREDITS:.0f} credits."
      + (f" **Run aborted: {aborted}**" if aborted else "") + "\n")
    w("Credits per call come from the `X-Credits-Used` response header. If a value says \"computed\", it was estimated from the pricing page. Raw responses are in `calls.jsonl`.\n")

    steps = {"A": "A. Market research", "B": "B. Trust and compliance", "C": "C. Revenue"}
    for s, title in steps.items():
        w(f"\n## {title}\n")
        w("| # | Test | Result | Latency (ms) | Credits | $ | Calls |")
        w("|---|---|---|---|---|---|---|")
        for r in [r for r in RESULTS if r["step"] == s]:
            lat = "+".join(str(int(x)) for x in r["latency_ms"]) or "-"
            w(f"| {r['id']} | {r['name']} | {r['passed']} | {lat} | {r['credits']} | ${r['credits'] * CREDIT_USD:.4f} | {r['calls']} (HTTP {r['statuses']}) |")
        w("")
        for r in [r for r in RESULTS if r["step"] == s]:
            w(f"**Test {r['id']}: what came back.** {r['detail'].replace('|', '/')}\n")

    w("\n## Totals\n")
    w("| Metric | Value |\n|---|---|")
    w(f"| API calls made | {len(STATE['calls'])} |")
    w(f"| Credits used (sum of logged per-call credits) | {total_logged} |")
    w(f"| Balance before / after (`GET /account/credits`) | {bal_before} / {bal_after} |")
    w(f"| Credits used (balance difference) | {diff} |")
    w(f"| Total $ (logged credits × CREDIT_USD) | ${total_logged * CREDIT_USD:.4f} |")
    w(f"| Total $ (balance diff × CREDIT_USD) | {('$%.4f' % (diff * CREDIT_USD)) if isinstance(diff, float) else 'unclear'} |")
    n429 = sum(1 for c in STATE["calls"] if c["status"] == 429)
    errs = [(c["n"], c["endpoint"], c["status"]) for c in STATE["calls"] if c["status"] not in (200, 201, 204)]
    w(f"| HTTP 429s | {n429} |")
    w(f"| Non-2xx calls | {errs or 'none'} |")
    w(f"| token_limit_rpm (from /account/endpoints) | {CTX.get('token_limit_rpm')} |")

    # ----- per-job estimate
    cs_u, cs_src = unit_cost("/company/search", fallback=PRICE["company_search_per_result"] + 0.5)
    ce_u, ce_src = unit_cost("/company/enrich", per="input", fallback=PRICE["company_enrich_per_record"])
    ci_u, ci_src = unit_cost("/company/identify", per="matched_input", fallback=PRICE["company_identify_per_match"])
    pe_u, pe_src = unit_cost("/person/enrich", per="input", fallback=PRICE["person_enrich_base"])
    ws_u, ws_src = unit_cost("/web/search/live", per="call", fallback=PRICE["web_search_per_page"])
    wf_u, wf_src = unit_cost("/web/enrich/live", per="call", fallback=PRICE["web_fetch_per_page"])
    ps_u, ps_src = unit_cost("/person/search", fallback=PRICE["person_search_per_result_no_fields"])
    js_u, js_src = unit_cost("/job/search", fallback=PRICE["job_search_per_result"])
    lines = [
        ("A", "Company search with basic_info/headcount/funding/locations, 20 results", 20, cs_u, cs_src),
        ("A", "Company enrich with 7 groups, 20 input domains (duplicate matches also bill)", 20, ce_u, ce_src),
        ("B", "Company identify, 20 matched identifiers", 20, ci_u, ci_src),
        ("B", "Web search, 2 queries per account (GST and fraud), 40 pages", 40, ws_u, ws_src),
        ("B", "Web fetch, 1 page per account, 20 pages", 20, wf_u, wf_src),
        ("C", "Person search, 5 profiles per account with no fields, 100 results", 100, ps_u, ps_src),
        ("C", "Person enrich, base profile, 2 per account, 40 records", 40, pe_u, pe_src),
        ("C", "Job search, 10 jobs per account, 200 results", 200, js_u, js_src),
    ]
    w("\n## Estimated cost of one full job (20 accounts through all 3 steps)\n")
    w("Assumed volumes are listed in each row. Unit costs come from this run where a call was observed; otherwise from the pricing page.\n")
    w("| Step | Item | Units | Credits/unit | Unit source | Credits | $ |\n|---|---|---|---|---|---|---|")
    tot = 0.0
    for s, item, units, u, src in lines:
        u = u or 0
        tot += units * u
        w(f"| {s} | {item} | {units} | {u:.4f} | {src} | {units * u:.2f} | ${units * u * CREDIT_USD:.4f} |")
    w(f"| | **Total** | | | | **{tot:.2f}** | **${tot * CREDIT_USD:.4f}** |")
    w("\nNot included: watchers. Per the docs, a company discovery watch costs 2 credits per new company at the 30-day SLA and 150 at the 1-day SLA. A company entity watch costs 5 per changed company.\n")

    # ----- auto observations
    w("\n## Observations (generated from responses)\n")
    gn = CTX.get("enrich_group_null")
    if gn:
        w(f"- Company enrich: number of companies where each group came back entirely empty, out of {len(CTX.get('enriched', {}))}: `{gn}`.")
    fresh = CTX.get("enrich_fresh") or {}
    if fresh:
        w("- Company enrich freshness per company (latest date found per field family; `<absent>` = key not in response):\n")
        cols = list(next(iter(fresh.values())).keys())
        w("| domain | " + " | ".join(cols) + " |")
        w("|---|" + "---|" * len(cols))
        for dom, f in fresh.items():
            w(f"| {dom} | " + " | ".join(str(f[c]) for c in cols) + " |")
        w("")
    dates = CTX.get("enrich_dates") or {}
    if dates:
        collapsed = {}
        for k, v in dates.items():
            collapsed.setdefault(re.sub(r"by_function_timeseries\.[^.]+\.[^\[]+", "by_function_timeseries.*.*", k), v)
        w("- Company enrich: date-like field paths seen: " + ", ".join(f"`{k}`" for k in collapsed))
    w("- Web search: the docs define no per-result publish date. See test 9 for the result keys that actually came back.")
    for c in STATE["calls"]:
        if c["status"] == 429:
            w(f"- Rate limited on call {c['n']} ({c['endpoint']}): `{short(c['response'], 200)}`")
    L.append("\n<!-- MANUAL-OBSERVATIONS -->\n")
    SUMMARY_FILE.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- main
TESTS = [
    ("1", "A", "Company search: India logistics/3PL", t1_company_search),
    ("2", "A", "Pagination (2 pages, no duplicate IDs)", t2_pagination),
    ("3", "A", "Companies like X (competitors.websites = delhivery.com)", t3_competitors),
    ("4", "A", "Company enrich (≤5, 7 groups)", t4_enrich),
    ("5", "B", "Identify with messy inputs (Delhivery)", t5_identify_messy),
    ("6", "B", "Lookalikes / parent-brand", t6_lookalikes),
    ("7", "B", "Shared domain exact_match true vs false", t7_exact_match),
    ("8", "B", "Not found → empty, no error, 0 credits", t8_not_found),
    ("9", "B", "Web search + fetch (GST / fraud news, date present)", t9_web),
    ("10", "C", "Person search: supply chain / logistics heads", t10_person_search),
    ("11", "C", "Person enrich (base profile only)", t11_person_enrich),
    ("12", "C", "Job search: last 30 days, 2 companies", t12_jobs),
    ("13", "C", "Watchers: types + request shape (no create)", t13_watchers),
]


def main():
    if CALLS_FILE.exists():
        CALLS_FILE.rename(CALLS_FILE.with_name(f"calls.{int(time.time())}.jsonl"))
    aborted = None
    print("Balance before:")
    bal_before, raw_before = balance("before")
    print(f"  credits = {bal_before}")
    snapshot_endpoints()
    try:
        for tid, step, name, fn in TESTS:
            run_test(tid, step, name, fn)
    except BudgetExceeded as e:
        aborted = str(e)
        print("BUDGET STOP:", e)
    print("\nBalance after:")
    time.sleep(3)  # let billing settle
    bal_after, raw_after = balance("after")
    print(f"  credits = {bal_after}")
    write_summary(bal_before, bal_after, (raw_before, raw_after), aborted)
    print(f"\nTotal logged credits: {STATE['credits']:.4f}; balance diff: "
          f"{(bal_before - bal_after) if bal_before is not None and bal_after is not None else 'unclear'}")
    print(f"Wrote {CALLS_FILE} and {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
