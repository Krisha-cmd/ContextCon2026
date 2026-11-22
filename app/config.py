"""Load config.yaml into a frozen Settings object. Secrets come only from env (or .env)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

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


class CrustdataPageSize(_Frozen):
    company_search: int
    person_search: int
    job_search: int


class CrustdataEstCredits(_Frozen):
    company_search_per_result: float
    company_enrich_per_domain: float
    company_identify_per_input: float
    person_search_per_result: float
    job_search_per_result: float
    web_search_per_page: float
    web_fetch_per_url: float


class Sort(_Frozen):
    field: str
    order: Literal["asc", "desc"]


class Crustdata(_Frozen):
    rpm: int
    timeout_s: float
    batch_enrich: int
    base_url: str
    api_version: str
    page_size: CrustdataPageSize
    company_search_fields: tuple[str, ...]
    company_search_sorts: tuple[Sort, ...]
    enrich_fields: tuple[str, ...]
    job_search_sorts: tuple[Sort, ...]
    web_location: str
    web_sources: tuple[str, ...]
    est_credits: CrustdataEstCredits


class Gateway(_Frozen):
    max_attempts: int
    backoff_base_s: float
    backoff_max_s: float
    backoff_jitter_s: float
    bucket_capacity: float
    default_rpm: int
    default_timeout_s: float
    endpoint_rpm: dict[str, float]


class Vertex(_Frozen):
    model: str
    location: str
    temperature: float
    thinking_budget: int | None
    timeout_s: float
    usd_per_m_input_tokens: float
    usd_per_m_output_tokens: float
    inr_per_usd: float
    est_inr_per_call: float


class CompanyFilters(_Frozen):
    country_field: str
    country_values: tuple[str, ...]
    industry_field: str
    industry_op: str
    state_field: str
    state_op: str
    headcount_field: str


class Discovery(_Frozen):
    default_max_accounts: int
    search_candidate_multiplier: int
    search_max_pages: int
    company_filters: CompanyFilters
    job_title_field: str
    job_title_op: str
    job_title_keywords: tuple[str, ...]
    job_country_field: str
    job_country_value: str
    news_query_template: str
    news_max_queries: int


class Dedup(_Frozen):
    name_ratio: float
    name_suffixes: tuple[str, ...]


class PreRankWeights(_Frozen):
    industry_fit: float
    headcount_in_range: float
    state_match: float
    trigger_present: float


class PreRank(_Frozen):
    industry_ratio: float
    weights: PreRankWeights


class GstinApi(_Frozen):
    base_url: str
    rpm: int
    timeout_s: float
    max_attempts: int
    retry_statuses: tuple[int, ...]
    credit_inr: float
    est_credits_per_lookup: float
    include_profile: bool


class RegistryChoice(_Frozen):
    gst_provider: Literal["gstinapi", "provider", "fixture", "unavailable"]
    mca_provider: Literal["gstinapi", "provider", "fixture", "unavailable"]
    fixture_dir: str
    gst_active_statuses: tuple[str, ...]
    gst_drop_statuses: tuple[str, ...]
    mca_active_statuses: tuple[str, ...]
    mca_drop_statuses: tuple[str, ...]


class RegistryApiPaths(_Frozen):
    gst_search: str
    gst_lookup: str
    mca_lookup: str


class RegistryApi(_Frozen):
    rpm: int
    timeout_s: float
    max_attempts: int
    retry_statuses: tuple[int, ...]
    credit_inr: float
    est_credits_per_lookup: float
    auth_header: str
    auth_prefix: str
    paths: RegistryApiPaths


class Identity(_Frozen):
    site_paths: tuple[str, ...]


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
    lease_s: float
    heartbeat_s: float
    max_attempts: int
    retry_backoff_base_s: float
    retry_backoff_max_s: float
    optional_steps: tuple[str, ...]


class Log(_Frozen):
    level: str


class Secrets(BaseSettings):
    """Read from environment variables of the same name (case-insensitive) or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    crustdata_api_key: SecretStr | None = None
    gcp_project: str | None = None
    registry_api_key: SecretStr | None = None
    registry_base_url: str | None = None  # not secret, but per-deployment like the key
    registry_provider: str | None = None  # overrides registry.gst_provider / mca_provider
    gstin_api_key: SecretStr | None = None  # gstinapi.in x-api-key
    database_url: SecretStr | None = None


class Settings(_Frozen):
    limits: Limits
    windows: Windows
    crustdata: Crustdata
    gateway: Gateway
    gstinapi: GstinApi
    registry: RegistryChoice
    registryapi: RegistryApi
    identity: Identity
    vertex: Vertex
    cache_ttl_hours: CacheTtlHours
    matching: Matching
    shortlist: Shortlist
    discovery: Discovery
    dedup: Dedup
    pre_rank: PreRank
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
