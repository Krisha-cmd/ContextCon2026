"""Load config.yaml into a frozen Settings object. Secrets come only from env (or .env)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Limits(_Frozen):
    max_inr_per_job: float
    max_minutes_per_job: int
    credit_inr: float


class Windows(_Frozen):
    trigger_days: int
    stale_buyer_months: int


class Crustdata(_Frozen):
    rpm: int
    timeout_s: float
    batch_enrich: int
    base_url: str
    api_version: str


class Gateway(_Frozen):
    max_attempts: int
    backoff_base_s: float
    backoff_max_s: float
    backoff_jitter_s: float
    bucket_capacity: float
    default_rpm: int
    default_timeout_s: float


class CacheTtlHours(_Frozen):
    registry: int
    news: int
    company: int
    people: int
    negative: int


class Matching(_Frozen):
    match_threshold: float
    nomatch_threshold: float


class Shortlist(_Frozen):
    pre_rank_top: int
    enrich_top: int


class Output(_Frozen):
    fields: tuple[str, ...]


class Db(_Frozen):
    pool_min: int
    pool_max: int
    connect_timeout_s: int
    schema_lock_key: int


class Runner(_Frozen):
    poll_interval_s: float


class Log(_Frozen):
    level: str


class Secrets(BaseSettings):
    """Read from environment variables of the same name (case-insensitive) or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    crustdata_api_key: SecretStr | None = None
    gcp_project: str | None = None
    registry_api_key: SecretStr | None = None
    database_url: SecretStr | None = None


class Settings(_Frozen):
    limits: Limits
    windows: Windows
    crustdata: Crustdata
    gateway: Gateway
    cache_ttl_hours: CacheTtlHours
    matching: Matching
    shortlist: Shortlist
    output: Output
    db: Db
    runner: Runner
    log: Log
    secrets: Secrets


def read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at top level")
    return data


def load_settings(path: Path | None = None) -> Settings:
    """Build Settings from a YAML file plus env secrets. Not cached; use get_settings()."""
    cfg_path = path or Path(os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    data = read_yaml(cfg_path)
    if "secrets" in data:
        raise ValueError("secrets must not be stored in config.yaml; use env vars")
    return Settings(**data, secrets=Secrets())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
