"""The pipeline, in order. Each step is a placeholder until its module is built.

Placeholders return {"implemented": False} and change nothing, so a job runs end to end
(parse -> ... -> draft) and finishes with no accounts. per_account flags are provisional:
enrich is job-level so it can batch (crustdata.batch_enrich) across accounts.
"""

from __future__ import annotations

from typing import Any

from app.discovery.steps import run_dedup, run_discover, run_pre_rank
from app.identity.step import run_identity
from app.outreach.draft import run_draft
from app.revenue.buyer import run_buyer
from app.revenue.enrich import run_enrich
from app.revenue.score import run_score
from app.revenue.trigger import run_trigger
from app.runner.context import StepContext
from app.runner.registry import StepFn, step
from app.spec.step import run_parse
from app.trust.news import run_news
from app.trust.step import run_registry

PIPELINE: list[tuple[str, bool]] = [  # (name, per_account), in run order
    ("parse", False),
    ("discover", False),
    ("dedup", False),
    ("pre_rank", False),
    ("identity", True),
    ("registry", True),
    ("news", True),
    ("enrich", False),
    ("trigger", True),
    ("buyer", True),
    ("score", True),
    ("draft", True),
]
# Built steps replace their placeholder here.
IMPLEMENTED: dict[str, StepFn] = {
    "parse": run_parse,
    "discover": run_discover,
    "dedup": run_dedup,
    "pre_rank": run_pre_rank,
    "identity": run_identity,
    "registry": run_registry,
    "news": run_news,
    "enrich": run_enrich,
    "trigger": run_trigger,
    "buyer": run_buyer,
    "score": run_score,
    "draft": run_draft,
}
ORDER_STEP = 10  # gap between orders so steps can be inserted later without renumbering


def _placeholder(name: str):
    async def run(ctx: StepContext) -> dict[str, Any]:
        return {"implemented": False, "step": name}

    run.__name__ = f"{name}_placeholder"
    return run


for _i, (_name, _per_account) in enumerate(PIPELINE, start=1):
    _fn = IMPLEMENTED.get(_name) or _placeholder(_name)
    step(_name, per_account=_per_account, order=_i * ORDER_STEP)(_fn)
