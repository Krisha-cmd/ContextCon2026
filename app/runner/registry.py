"""Step registry: @step(name, per_account=..., order=...)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

StepFn = Callable[..., Any]  # (ctx: StepContext) -> JSON-serializable output (sync or async)


@dataclass(frozen=True)
class StepDef:
    name: str
    per_account: bool
    order: int
    fn: StepFn


class Registry:
    def __init__(self) -> None:
        self._steps: dict[str, StepDef] = {}

    def step(self, name: str, *, per_account: bool, order: int) -> Callable[[StepFn], StepFn]:
        def register(fn: StepFn) -> StepFn:
            if name in self._steps:
                raise ValueError(f"step {name!r} already registered")
            if any(s.order == order for s in self._steps.values()):
                raise ValueError(f"step order {order} already used")
            self._steps[name] = StepDef(name, per_account, order, fn)
            return fn

        return register

    def get(self, name: str) -> StepDef:
        return self._steps[name]

    def names(self) -> list[str]:
        return [s.name for s in self.ordered()]

    def ordered(self) -> list[StepDef]:
        return sorted(self._steps.values(), key=lambda s: s.order)

    def first(self) -> StepDef:
        steps = self.ordered()
        if not steps:
            raise ValueError("no steps registered")
        return steps[0]

    def after(self, name: str) -> StepDef | None:
        order = self._steps[name].order
        return next((s for s in self.ordered() if s.order > order), None)

    def first_job_level_after(self, order: int) -> StepDef | None:
        return next((s for s in self.ordered() if s.order > order and not s.per_account), None)


REGISTRY = Registry()


def step(name: str, *, per_account: bool, order: int) -> Callable[[StepFn], StepFn]:
    """Register a step in the default pipeline registry."""
    return REGISTRY.step(name, per_account=per_account, order=order)
