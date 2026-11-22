"""Pure helpers that turn rows into what the screens show. No SQL, no HTTP."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.evidence.models import FactStatus

# Facts in the order a reviewer reads them; anything else that is a Fact follows, A-Z.
FACT_ORDER = ("legal_name", "gstin", "cin", "status_active", "adverse_news", "trigger", "buyer")
FACT_LABELS = {
    "legal_name": "Legal name", "gstin": "GSTIN", "cin": "CIN",
    "status_active": "Registered and active", "adverse_news": "Adverse news",
    "trigger": "Trigger (last 30 days)", "buyer": "Buyer",
}  # fmt: skip
DECISION_OF_STATUS = {"active": "proceed", "held": "held", "dropped": "dropped"}
_STATUSES = {s.value for s in FactStatus}
_POINTS = re.compile(r"^([+-]\d+)\s")


@dataclass
class FactView:
    key: str
    label: str
    status: str
    value: Any
    missing: str | None
    stale: bool
    sources: list[dict[str, Any]]
    alternatives: list[Any] = field(default_factory=list)

    @property
    def tone(self) -> str:
        """ok (verified), warn (cannot verify / conflict: amber, a person should look)."""
        return "ok" if self.status == FactStatus.verified.value and not self.stale else "warn"


def is_fact(value: Any) -> bool:
    return isinstance(value, dict) and value.get("status") in _STATUSES and "sources" in value


def facts_of(data: dict[str, Any]) -> list[FactView]:
    keys = [k for k in FACT_ORDER if is_fact(data.get(k))]
    keys += sorted(k for k, v in data.items() if is_fact(v) and k not in FACT_ORDER)
    out = []
    for k in keys:
        f = data[k]
        out.append(FactView(
            key=k, label=FACT_LABELS.get(k, k.replace("_", " ").capitalize()),
            status=f["status"], value=f.get("value"), missing=f.get("missing"),
            stale=bool(f.get("stale")), sources=list(f.get("sources") or []),
            alternatives=list(f.get("alternatives") or []),
        ))  # fmt: skip
    return out


def unchecked(data: dict[str, Any]) -> list[str]:
    """Core facts that were never checked (their step was skipped): shown as gaps."""
    return [FACT_LABELS[k] for k in FACT_ORDER if not is_fact(data.get(k))]


def decision(row: dict[str, Any]) -> dict[str, Any]:
    status = row.get("status") or "active"
    return {"decision": DECISION_OF_STATUS.get(status, status), "reason": row.get("status_reason"),
            "step": row.get("status_step")}  # fmt: skip


def fact_status(data: dict[str, Any], key: str) -> str:
    f = data.get(key)
    return f["status"] if is_fact(f) else "unchecked"


def trust(data: dict[str, Any]) -> dict[str, str]:
    """Registry and adverse-news status, as one word each for the account table."""
    active, news = data.get("status_active"), data.get("adverse_news")
    reg = fact_status(data, "status_active")
    if reg == "verified":
        reg = "active" if active.get("value") is True else "inactive"
    adv = fact_status(data, "adverse_news")
    if adv == "verified":
        adv = "none found" if isinstance(news.get("value"), str) else "issue found"
    return {"registry": reg, "news": adv}


def points(reason: str) -> int:
    m = _POINTS.match(reason)
    return int(m.group(1)) if m else 0


def top_reasons(data: dict[str, Any], n: int) -> list[str]:
    """The n biggest contributions (non-zero), largest first."""
    reasons = [r for r in data.get("score_reasons") or [] if points(r) != 0]
    return sorted(reasons, key=lambda r: -abs(points(r)))[:n]


def short(value: Any) -> str:
    """One line for a fact value (the drawer shows the whole thing)."""
    if isinstance(value, dict):
        for k in ("title", "name", "type"):
            if value.get(k):
                return str(value[k])
        return ", ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, ""))
    if isinstance(value, list):
        return f"{len(value)} item(s)"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "" if value is None else str(value)


# ---- progress -----------------------------------------------------------------------------
@dataclass(frozen=True)
class StepProgress:
    name: str
    per_account: bool
    total: int
    counts: dict[str, int]

    @property
    def state(self) -> str:
        """d (done), p (in progress), f (failed), s (skipped), "" (not started)."""
        c = self.counts
        if not self.total:
            return ""
        if c.get("running") or c.get("pending"):
            return "p"
        if c.get("failed"):
            return "f"
        if c.get("done"):
            return "d"
        return "s"

    @property
    def line(self) -> str:
        c = self.counts
        if not self.total:
            return f"{self.name}: not started"
        parts = [f"{c[k]} {k}" for k in ("done", "running", "pending", "skipped", "failed")
                 if c.get(k)]  # fmt: skip
        scope = f" of {self.total} accounts" if self.per_account else ""
        return f"{self.name}{scope}: " + ", ".join(parts)


def progress(pipeline: list[tuple[str, bool]], rows: list[dict[str, Any]]) -> list[StepProgress]:
    out = []
    for name, per_account in pipeline:
        mine = [r for r in rows if r["step"] == name]
        counts: dict[str, int] = {}
        for r in mine:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out.append(StepProgress(name, per_account, len(mine), counts))
    return out


def summary_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    out = {"proceed": 0, "held": 0, "dropped": 0}
    for r in rows:
        out[decision(r)["decision"]] = out.get(decision(r)["decision"], 0) + 1
    return out
