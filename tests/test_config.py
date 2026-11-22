from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_CONFIG_PATH, load_settings, read_yaml


def test_loads_repo_config() -> None:
    s = load_settings(DEFAULT_CONFIG_PATH)
    assert s.limits.max_inr_per_job == 2500
    assert s.limits.max_minutes_per_job == 20
    assert s.limits.credit_inr == 25
    assert s.windows.trigger_days == 30
    assert s.windows.stale_buyer_months == 6
    assert s.crustdata.rpm == 5
    assert s.crustdata.timeout_s == 180
    assert s.crustdata.batch_enrich == 5
    assert s.cache_ttl_hours.negative == 6
    assert s.cache_ttl_hours.company == 168
    assert s.matching.match_threshold == 0.9
    assert s.matching.nomatch_threshold == 0.5
    assert s.shortlist.pre_rank_top == 10
    assert s.shortlist.enrich_top == 8
    assert s.output.fields == ()


def test_settings_are_frozen() -> None:
    s = load_settings(DEFAULT_CONFIG_PATH)
    with pytest.raises(ValidationError):
        s.limits.max_inr_per_job = 1  # type: ignore[misc]


def test_secrets_come_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUSTDATA_API_KEY", "k-123")
    monkeypatch.setenv("GCP_PROJECT", "proj-x")
    monkeypatch.setenv("REGISTRY_API_KEY", "r-456")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    s = load_settings(DEFAULT_CONFIG_PATH)
    assert s.secrets.crustdata_api_key is not None
    assert s.secrets.crustdata_api_key.get_secret_value() == "k-123"
    assert s.secrets.gcp_project == "proj-x"
    assert s.secrets.registry_api_key.get_secret_value() == "r-456"  # type: ignore[union-attr]
    assert "k-123" not in repr(s)  # SecretStr masks values


def _write(tmp_path: Path, data: dict) -> Path:
    import yaml

    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_rejects_unknown_keys(tmp_path: Path) -> None:
    data = read_yaml(DEFAULT_CONFIG_PATH)
    data["limits"]["surprise"] = 1
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, data))


def test_rejects_missing_section(tmp_path: Path) -> None:
    data = read_yaml(DEFAULT_CONFIG_PATH)
    del data["matching"]
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, data))


def test_rejects_secrets_in_yaml(tmp_path: Path) -> None:
    data = read_yaml(DEFAULT_CONFIG_PATH)
    data["secrets"] = {"crustdata_api_key": "oops"}
    with pytest.raises(ValueError, match="secrets"):
        load_settings(_write(tmp_path, data))


def test_config_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = read_yaml(DEFAULT_CONFIG_PATH)
    data["limits"]["max_inr_per_job"] = 99
    monkeypatch.setenv("CONFIG_PATH", str(_write(tmp_path, data)))
    assert load_settings().limits.max_inr_per_job == 99
