from __future__ import annotations

from app.spec import kickoff_mapping as km
from app.spec.models import RunSpec


def test_matches_only_objects_with_known_keys() -> None:
    assert km.matches({"states": ["Goa"]})
    assert km.matches({"geo_states": ["Goa"], "limits": {"max_inr_per_job": 10}})
    assert not km.matches({"states": ["Goa"], "region": "west"})
    assert not km.matches({})
    assert not km.matches(["states"])
    assert not km.matches(None)


def test_maps_aliases_triggers_and_limits() -> None:
    fields = km.to_fields(
        {"sectors": ["FMCG"], "triggers": ["New Warehouse", "supply-chain hiring"],
         "budget_inr": 900, "max_minutes": 5}
    )  # fmt: skip
    spec = RunSpec.model_validate(fields)
    assert spec.industries == ["FMCG"]
    assert [t.value for t in spec.trigger_types] == ["new_warehouse", "supply_chain_hiring"]
    assert spec.limits == {"max_inr_per_job": 900, "max_minutes_per_job": 5}


def test_runspec_shaped_json_maps_to_itself() -> None:
    data = RunSpec(geo_states=["Goa"], max_accounts=3).model_dump(mode="json")
    assert km.matches(data)
    assert RunSpec.model_validate(km.to_fields(data)) == RunSpec(geo_states=["Goa"], max_accounts=3)
