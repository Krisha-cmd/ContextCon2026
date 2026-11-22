from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.evidence.models import (
    Fact,
    FactStatus,
    Source,
    cannot_verify,
    conflict,
    is_unknown,
    quote_in_text,
    verified,
)


def _src(eid: str = "ev_1", **kw: object) -> Source:
    base = {
        "url": "https://example.com/a",
        "source_name": "crustdata",
        "field_path": "company.legal_name",
        "retrieved_at": datetime(2026, 11, 1, tzinfo=UTC),
        "evidence_id": eid,
    }
    return Source(**{**base, **kw})


# --- Source -------------------------------------------------------------------------------


def test_source_needs_field_path_or_quote() -> None:
    with pytest.raises(ValidationError, match="field_path or quote"):
        _src(field_path=None)
    assert _src(field_path=None, quote="opened a warehouse").quote


def test_source_needs_timezone_aware_retrieved_at() -> None:
    with pytest.raises(ValidationError):
        _src(retrieved_at=datetime(2026, 11, 1))


# --- verified -----------------------------------------------------------------------------


def test_fact_without_source_cannot_be_verified() -> None:
    with pytest.raises(ValidationError, match="at least one source"):
        Fact[str](value="Acme Pvt Ltd", status=FactStatus.verified, sources=[])
    with pytest.raises(ValidationError, match="at least one source"):
        verified("Acme Pvt Ltd", [])


@pytest.mark.parametrize("empty", [None, 0, 0.0, "", "   ", [], {}])
def test_unknown_values_cannot_be_verified(empty: object) -> None:
    with pytest.raises(ValidationError, match="known value"):
        verified(empty, _src())


def test_false_is_a_real_fact() -> None:
    assert not is_unknown(False)
    fact = verified(False, _src())
    assert fact.status is FactStatus.verified
    assert fact.value is False


def test_verified_helper() -> None:
    fact = verified("Acme Pvt Ltd", _src())
    assert fact.value == "Acme Pvt Ltd"
    assert fact.sources[0].evidence_id == "ev_1"
    assert fact.missing is None


# --- cannot_verify ------------------------------------------------------------------------


def test_cannot_verify_needs_reason_and_no_value() -> None:
    fact = cannot_verify("registry returned no match")
    assert fact.value is None and fact.missing == "registry returned no match"
    with pytest.raises(ValidationError, match="missing reason"):
        Fact[str](status=FactStatus.cannot_verify)
    with pytest.raises(ValidationError, match="value=None"):
        Fact[str](value="x", status=FactStatus.cannot_verify, missing="m")


# --- conflict -----------------------------------------------------------------------------


def test_conflict_keeps_both_values() -> None:
    a, b = _src("ev_a"), _src("ev_b", source_name="mca")
    fact = conflict([("Acme Pvt Ltd", a), ("Acme Logistics Ltd", b)])
    assert fact.status is FactStatus.conflict
    assert fact.value is None
    assert fact.alternatives == [("Acme Pvt Ltd", a), ("Acme Logistics Ltd", b)]
    assert {s.evidence_id for s in fact.sources} == {"ev_a", "ev_b"}


def test_conflict_survives_json_round_trip() -> None:
    fact = conflict([("A", _src("ev_a")), ("B", _src("ev_b"))])
    back = Fact[str].model_validate_json(fact.model_dump_json())
    assert [v for v, _ in back.alternatives] == ["A", "B"]


def test_conflict_rejects_agreeing_values() -> None:
    with pytest.raises(ValidationError, match="differ"):
        conflict([("A", _src("ev_a")), ("A", _src("ev_b"))])


def test_conflict_rejects_single_source() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        conflict([("A", _src("ev_a"))])
    with pytest.raises(ValidationError, match="distinct sources"):
        conflict([("A", _src("ev_a")), ("B", _src("ev_a"))])


def test_conflict_must_not_pick_a_value() -> None:
    a, b = _src("ev_a"), _src("ev_b")
    with pytest.raises(ValidationError, match="must not pick"):
        Fact[str](value="A", status=FactStatus.conflict, sources=[a, b],
                  alternatives=[("A", a), ("B", b)])  # fmt: skip


def test_alternatives_only_on_conflict() -> None:
    a = _src("ev_a")
    with pytest.raises(ValidationError, match="only a conflict"):
        Fact[str](value="A", status=FactStatus.verified, sources=[a], alternatives=[("A", a)])


# --- quote_in_text ------------------------------------------------------------------------

TEXT = "Acme Logistics   announced a new\n warehouse in Bhiwandi,\tMaharashtra on 3 Nov."


def test_real_quote_passes_with_case_and_whitespace_differences() -> None:
    assert quote_in_text("announced a NEW warehouse in bhiwandi, maharashtra", TEXT)


def test_fake_quote_fails() -> None:
    assert not quote_in_text("announced a new warehouse in Chennai", TEXT)
    assert not quote_in_text("raised Series B funding", TEXT)


def test_empty_quote_fails() -> None:
    assert not quote_in_text("   ", TEXT)
