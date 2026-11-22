from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.evidence.models import FactStatus
from app.spec.models import Account, Decision, RunSpec, TriggerType


def test_runspec_parses_trigger_enum_and_normalizes_domains() -> None:
    spec = RunSpec(
        trigger_types=["funding", "new_warehouse"],
        exclude_domains=["https://www.Delhivery.com/about", "blue-dart.com", " "],
    )
    assert spec.trigger_types == [TriggerType.funding, TriggerType.new_warehouse]
    assert spec.exclude_domains == ["blue-dart.com", "delhivery.com"]


def test_runspec_rejects_unknown_trigger_and_bad_size_range() -> None:
    with pytest.raises(ValidationError):
        RunSpec(trigger_types=["ipo"])
    with pytest.raises(ValidationError, match="size_min"):
        RunSpec(size_min=500, size_max=50)


def test_limit_overrides_only_tighten() -> None:
    s = load_settings(DEFAULT_CONFIG_PATH)
    tighter = RunSpec(limits={"max_inr_per_job": 1000}).effective_limits(s)
    assert tighter.max_inr_per_job == 1000
    looser = RunSpec(limits={"max_inr_per_job": 10**9}).effective_limits(s)
    assert looser.max_inr_per_job == s.limits.max_inr_per_job
    assert looser.credit_inr == s.limits.credit_inr


def test_limit_overrides_reject_unknown_and_conversion_keys() -> None:
    with pytest.raises(ValidationError, match="non-overridable"):
        RunSpec(limits={"credit_inr": 1})
    with pytest.raises(ValidationError, match="non-overridable"):
        RunSpec(limits={"max_bananas": 1})


def test_account_facts_default_to_cannot_verify() -> None:
    acc = Account(id=uuid4(), name="Acme")
    for name in ("legal_name", "gstin", "cin", "status_active", "adverse_news", "trigger", "buyer"):
        assert getattr(acc, name).status is FactStatus.cannot_verify
    assert acc.decision is None


def test_account_decision_needs_reason() -> None:
    with pytest.raises(ValidationError, match="decision_reason"):
        Account(id=uuid4(), name="Acme", decision=Decision.held)
    acc = Account(id=uuid4(), name="Acme", decision="dropped", decision_reason="adverse news")
    assert acc.decision is Decision.dropped
