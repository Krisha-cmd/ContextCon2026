from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.evidence.models import Source, verified
from app.output.render import output_fields, render_accounts
from app.spec.models import Account


def _account() -> Account:
    src = Source(
        url="https://api.example/x",
        source_name="crustdata",
        field_path="company.legal_name",
        retrieved_at=datetime(2026, 11, 1, tzinfo=UTC),
        evidence_id="ev_1",
    )
    return Account(id=uuid4(), name="Acme", legal_name=verified("Acme Pvt Ltd", src))


def test_configured_fields_drive_output() -> None:
    out = render_accounts([_account()], ["name", "legal_name"])
    assert list(out[0]) == ["name", "legal_name"]
    fact = out[0]["legal_name"]
    assert fact["value"] == "Acme Pvt Ltd"
    assert fact["sources"][0]["evidence_id"] == "ev_1"  # facts never leave without sources


def test_empty_config_renders_all_fields() -> None:
    assert output_fields(()) == list(Account.model_fields)


def test_unknown_configured_field_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        output_fields(["name", "revenue_crore"])
