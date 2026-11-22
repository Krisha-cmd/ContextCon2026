"""Runner step "score" (per account): an additive points model (weights in config.score).

Every contribution, zero ones included, becomes one plain-text line in score_reasons, e.g.
"+28 new warehouse: Acme opens Pune warehouse, 2 days ago (source: https://...)". Each line's
points are whole numbers and the score is exactly their sum. Unknown inputs earn nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb
from rapidfuzz import fuzz

from app.config import Settings
from app.discovery.dedup import fold
from app.log import get_logger
from app.revenue.common import UPDATE_DATA, fact_value, run_spec
from app.runner.context import StepContext
from app.spec.models import RunSpec
from app.trust.news import NO_ADVERSE_NEWS

log = get_logger(__name__)


@dataclass(frozen=True)
class Part:
    points: int
    text: str

    def line(self) -> str:
        return f"{self.points:+d} {self.text}"


def source_label(fact: Any, prefer_url: bool = True) -> str:
    sources = fact.get("sources") if isinstance(fact, dict) else None
    if not sources:
        return "no source"
    src = sources[0]
    return (src.get("url") if prefer_url else None) or src.get("source_name") or "no source"


def _record_label(data: dict[str, Any]) -> str:
    return source_label({"sources": data.get("sources") or []}, prefer_url=False)


def trigger_part(data: dict[str, Any], s: Settings) -> Part:
    value = fact_value(data, "trigger")
    if not isinstance(value, dict):
        return Part(0, "no verified trigger")
    pts = round(s.score.trigger * float(value.get("strength") or 0))
    kind = str(value.get("type", "trigger")).replace("_", " ")
    age = value.get("age_days")
    when = "today" if age == 0 else f"{age} day{'s' if age != 1 else ''} ago"
    return Part(pts, f"{kind}: {value.get('title')}, {when} "
                     f"(source: {source_label(data.get('trigger'))})")  # fmt: skip


def fit_parts(data: dict[str, Any], spec: RunSpec, s: Settings) -> list[Part]:
    src = f"(source: {_record_label(data)})"
    parts = []
    industries = [i for i in data.get("industries") or [] if i]
    if not spec.industries:
        parts.append(Part(0, "industry: none asked for"))
    elif any(fuzz.token_set_ratio(fold(w), fold(h)) >= s.pre_rank.industry_ratio
             for w in spec.industries for h in industries):  # fmt: skip
        text = f"industry fit: {', '.join(industries)} {src}"
        parts.append(Part(round(s.score.industry_fit), text))
    else:
        parts.append(Part(0, f"industry: {', '.join(industries) or 'unknown'} not asked for"))
    hc = data.get("headcount")
    if spec.size_min is None and spec.size_max is None:
        parts.append(Part(0, "size: no range asked for"))
    elif isinstance(hc, int) and hc > 0 and (spec.size_min or 0) <= hc <= (spec.size_max or hc):
        parts.append(Part(round(s.score.size_fit), f"size fit: {hc} employees {src}"))
    else:
        parts.append(Part(0, f"size: {hc if isinstance(hc, int) and hc > 0 else 'unknown'} "
                             "employees, outside the range"))  # fmt: skip
    state = data.get("state")
    if not spec.geo_states:
        parts.append(Part(0, "state: none asked for"))
    elif state and fold(state) in {fold(x) for x in spec.geo_states}:
        parts.append(Part(round(s.score.state_fit), f"state fit: {state} {src}"))
    else:
        parts.append(Part(0, f"state: {state or 'unknown'} not asked for"))
    return parts


def buyer_parts(data: dict[str, Any], s: Settings) -> list[Part]:
    value = fact_value(data, "buyer")
    if not isinstance(value, dict):
        return [Part(0, "no verified buyer")]
    conf = float(value.get("confidence") or 0)
    src = source_label(data.get("buyer"))
    parts = [Part(round(s.score.buyer * conf),
                  f"buyer: {value.get('title')} (match {conf:.2f}) (source: {src})")]  # fmt: skip
    if value.get("stale"):
        when = value.get("latest_date") or "unknown"
        parts.append(Part(-round(s.score.buyer_stale_penalty),
                          f"buyer profile stale (last updated {when})"))  # fmt: skip
    return parts


def trust_parts(data: dict[str, Any], s: Settings) -> list[Part]:
    parts = []
    if fact_value(data, "status_active") is True:
        src = source_label(data.get("status_active"), prefer_url=False)
        parts.append(Part(round(s.score.trust_active), f"registered and active (source: {src})"))
    else:
        parts.append(Part(0, "registry status not verified active"))
    if fact_value(data, "adverse_news") == NO_ADVERSE_NEWS:
        src = source_label(data.get("adverse_news"), prefer_url=False)
        parts.append(Part(round(s.score.no_adverse_news), f"{NO_ADVERSE_NEWS} (source: {src})"))
    else:
        parts.append(Part(0, "adverse news check not verified"))
    return parts


def score_account(data: dict[str, Any], spec: RunSpec, s: Settings) -> tuple[int, list[str]]:
    parts = [trigger_part(data, s), *fit_parts(data, spec, s), *buyer_parts(data, s),
             *trust_parts(data, s)]  # fmt: skip
    return sum(p.points for p in parts), [p.line() for p in parts]


async def run_score(ctx: StepContext) -> dict[str, Any]:
    account = ctx.account or {}
    data = account.get("data") or {}
    total, reasons = score_account(data, run_spec(ctx.job), ctx.config)
    patch = {"score": total, "score_reasons": reasons}
    ctx.write(UPDATE_DATA, (Jsonb(patch), account.get("id")), tbl="accounts",
              record_id=account.get("id"), before={k: data.get(k) for k in patch}, after=patch,
              reason=f"score: {total}")  # fmt: skip
    log.info("score", extra={"score": total})
    return patch
