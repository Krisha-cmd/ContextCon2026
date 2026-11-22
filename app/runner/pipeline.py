"""The pipeline, in order. Each step is a placeholder until its module is built.

Placeholders return {"implemented": False} and change nothing, so a job runs end to end
(parse -> ... -> draft) and finishes with no accounts. per_account flags are provisional:
enrich is job-level so it can batch (crustdata.batch_enrich) across accounts.
"""

from __future__ import annotations

from typing import Any

from app.runner.context import StepContext
from app.runner.registry import step

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
ORDER_STEP = 10  # gap between orders so steps can be inserted later without renumbering


def _placeholder(name: str):
    async def run(ctx: StepContext) -> dict[str, Any]:
        return {"implemented": False, "step": name}

    run.__name__ = f"{name}_placeholder"
    return run


for _i, (_name, _per_account) in enumerate(PIPELINE, start=1):
    step(_name, per_account=_per_account, order=_i * ORDER_STEP)(_placeholder(_name))
