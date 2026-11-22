"""Provider-agnostic adapter for the Indian GST and MCA registries.

RegistryClient is the only interface the pipeline sees. Implementations:
- GstinApiRegistry: gstinapi.in, GST lookups only (GET /v1/gstin/{gstin}).
- ProviderRegistry: a generic HTTP registry (REGISTRY_BASE_URL / REGISTRY_API_KEY). The
  request/response mapping is one pair of functions per endpoint, so a new provider is a
  small change in the "provider mapping" section below.
- UnavailableRegistry: answers every question with RegistryUnavailable.
- FixtureRegistry: tests/fixtures/registry/*.json, no network.
- SplitRegistry: routes GST questions to one client and MCA questions to another.

Every HTTP call goes through app.gateway (data_type "registry"). An outage, a missing key or
a lookup the provider doesn't offer returns a RegistryUnavailable marker, never an exception
and never "not found": the trust step turns it into cannot_verify("registry unavailable").
BudgetExceeded is left to propagate so the runner can stop optional work.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import quote

from app import gateway
from app.config import ROOT, Settings, get_settings
from app.discovery.dedup import normalize_name
from app.discovery.normalize import known, parse_date
from app.gateway import GatewayError
from app.log import get_logger

log = get_logger(__name__)

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
CIN_RE = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
DATA_TYPE = "registry"
ENDPOINT_GSTIN = "gstinapi:gstin"
GROUP_PROVIDER = "registryapi"
PROVIDER_NAMES = ("gstinapi", "provider", "fixture", "unavailable")
HTTP_OK, HTTP_BAD_REQUEST, HTTP_NOT_FOUND = 200, 400, 404

# What the operator is told for each gstinapi.in status code.
STATUS_MESSAGES: dict[int, str] = {
    400: "GSTIN format is invalid (no credit charged)",
    401: "GST registry API key is missing or invalid; check GSTIN_API_KEY",
    402: "GST registry account is out of credits; top up before running more lookups",
    403: "GST registry account is deactivated",
    404: "GSTIN is not registered in the GST database",
    429: "GST registry rate limit exceeded; retried and still limited",
    502: "GST provider temporarily unavailable; retried and still down",
}


class InvalidGstin(ValueError):  # noqa: N818 - reads better than ...Error here
    pass


class InvalidCin(ValueError):  # noqa: N818
    pass


@dataclass(frozen=True)
class RegistryUnavailable:
    """The registry could not answer. Not a fact about the company."""

    reason: str
    status: int | None = None


@dataclass(frozen=True)
class GstRecord:
    gstin: str
    legal_name: str | None
    trade_name: str | None
    state: str | None
    status: str | None  # provider text, e.g. Active / Cancelled / Suspended
    registration_date: date | None
    last_updated: date | None  # only if the provider gives one
    raw: Any
    url: str  # what was called; cited in the Source
    field_path: str = "data"  # where the record sits in `raw`
    kind: ClassVar[str] = "gst"

    @property
    def id(self) -> str:
        return self.gstin


@dataclass(frozen=True)
class McaRecord:
    cin: str
    name: str | None
    status: str | None  # provider text, e.g. Active / Strike Off / Under Liquidation / Dormant
    incorporation_date: date | None
    registered_state: str | None
    raw: Any
    url: str
    field_path: str = "data"
    kind: ClassVar[str] = "mca"

    @property
    def id(self) -> str:
        return self.cin

    @property
    def legal_name(self) -> str | None:
        return self.name

    @property
    def state(self) -> str | None:
        return self.registered_state


type RegistryRecord = GstRecord | McaRecord


class RegistryClient(Protocol):
    """job_id is passed on every call: the gateway charges each call to a job's budget."""

    async def gst_search(
        self, job_id: Any, legal_name: str, state: str | None = None
    ) -> list[GstRecord] | RegistryUnavailable: ...

    async def gst_lookup(self, job_id: Any, gstin: str) -> GstRecord | RegistryUnavailable | None:
        """None means the registry answered: this GSTIN is not registered."""
        ...

    async def mca_lookup(
        self, job_id: Any, cin: str | None = None, name: str | None = None
    ) -> list[McaRecord] | RegistryUnavailable: ...


def normalize_gstin(gstin: str) -> str:
    """Uppercase and validate before calling, so a typo never costs a request."""
    g = gstin.strip().upper()
    if not GSTIN_RE.match(g):
        raise InvalidGstin(f"not a valid GSTIN: {gstin!r}")
    return g


def normalize_cin(cin: str) -> str:
    c = cin.strip().upper()
    if not CIN_RE.match(c):
        raise InvalidCin(f"not a valid CIN: {cin!r}")
    return c


def _mca_args(cin: str | None, name: str | None) -> tuple[str | None, str | None]:
    if bool(cin) == bool(name):
        raise ValueError("mca_lookup needs exactly one of cin or name")
    return (normalize_cin(cin) if cin else None), (name.strip() if name else None)


# ---- gstinapi.in ---------------------------------------------------------------------------
def gstinapi_record(body: dict[str, Any], url: str) -> GstRecord:
    data = body.get("data") or {}
    return GstRecord(
        gstin=data.get("gstin") or body.get("gstin"),
        legal_name=known(data.get("legal_name")),
        trade_name=known(data.get("trade_name")),
        state=known((data.get("address_details") or {}).get("state")),
        status=known(data.get("status")),
        registration_date=parse_date(data.get("registration_date")),
        last_updated=parse_date(data.get("last_updated")),
        raw=body,
        url=url,
    )


def gstinapi_error(status: int, body: Any) -> RegistryUnavailable:
    detail = body.get("error") if isinstance(body, dict) else None
    msg = STATUS_MESSAGES.get(status, f"GST registry returned HTTP {status}")
    return RegistryUnavailable(f"{msg} ({detail})" if detail else msg, status)


class GstinApiRegistry:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

    def gstin_url(self, gstin: str) -> str:
        url = f"{self.s.gstinapi.base_url}/v1/gstin/{gstin}"
        return url + ("?include=profile" if self.s.gstinapi.include_profile else "")

    async def gst_lookup(self, job_id: Any, gstin: str) -> GstRecord | RegistryUnavailable | None:
        g = normalize_gstin(gstin)
        url = self.gstin_url(g)
        try:
            resp = await gateway.call(
                job_id=job_id,
                endpoint=ENDPOINT_GSTIN,
                method="GET",
                url=url,
                data_type=DATA_TYPE,
                est_credits=self.s.gstinapi.est_credits_per_lookup,
            )
        except GatewayError as exc:
            return RegistryUnavailable(f"GST registry unreachable: {exc}")
        body = resp.body if isinstance(resp.body, dict) else {}
        if "credits_remaining" in body:
            log.info(
                "gstinapi credits",
                extra={"credits_remaining": body["credits_remaining"], "cached": resp.from_cache},
            )
        if resp.status == HTTP_NOT_FOUND:
            return None
        if resp.status == HTTP_BAD_REQUEST:
            raise InvalidGstin(STATUS_MESSAGES[HTTP_BAD_REQUEST])
        if resp.status != HTTP_OK or body.get("success") is False or not body.get("data"):
            return gstinapi_error(resp.status, body)
        return gstinapi_record(body, url)

    async def gst_search(
        self, job_id: Any, legal_name: str, state: str | None = None
    ) -> list[GstRecord] | RegistryUnavailable:
        return RegistryUnavailable("gstinapi.in has no GST search by name")

    async def mca_lookup(
        self, job_id: Any, cin: str | None = None, name: str | None = None
    ) -> list[McaRecord] | RegistryUnavailable:
        _mca_args(cin, name)
        return RegistryUnavailable("gstinapi.in has no MCA lookup")


# ---- provider mapping (PLACEHOLDER shapes until the provider's docs are in) ----------------
# Each endpoint has one request builder and one response mapper. Swapping providers means
# editing these six functions plus registryapi.paths in config.yaml.
@dataclass(frozen=True)
class Req:
    method: str
    path: str
    params: dict[str, Any] | None = None
    json: Any = None


def gst_search_request(legal_name: str, state: str | None, paths: Any) -> Req:
    params = {"legal_name": legal_name} | ({"state": state} if state else {})
    return Req("GET", paths.gst_search, params=params)


def gst_search_response(body: Any, url: str) -> list[GstRecord]:
    rows = body.get("data") if isinstance(body, dict) else None
    return [_provider_gst(r, body, url, f"data[{i}]") for i, r in enumerate(rows or [])]


def gst_lookup_request(gstin: str, paths: Any) -> Req:
    return Req("GET", paths.gst_lookup.format(gstin=quote(gstin)))


def gst_lookup_response(body: Any, url: str) -> GstRecord | None:
    row = body.get("data") if isinstance(body, dict) else None
    return _provider_gst(row, body, url, "data") if row else None


def mca_lookup_request(cin: str | None, name: str | None, paths: Any) -> Req:
    return Req("GET", paths.mca_lookup, params={"cin": cin} if cin else {"name": name})


def mca_lookup_response(body: Any, url: str) -> list[McaRecord]:
    rows = body.get("data") if isinstance(body, dict) else None
    if isinstance(rows, dict):
        rows = [rows]
    return [_provider_mca(r, body, url, f"data[{i}]") for i, r in enumerate(rows or [])]


def _provider_gst(row: dict[str, Any], body: Any, url: str, path: str) -> GstRecord:
    return GstRecord(
        gstin=row.get("gstin"),
        legal_name=known(row.get("legal_name")),
        trade_name=known(row.get("trade_name")),
        state=known(row.get("state")),
        status=known(row.get("status")),
        registration_date=parse_date(row.get("registration_date")),
        last_updated=parse_date(row.get("last_updated")),
        raw=body,
        url=url,
        field_path=path,
    )


def _provider_mca(row: dict[str, Any], body: Any, url: str, path: str) -> McaRecord:
    return McaRecord(
        cin=row.get("cin"),
        name=known(row.get("company_name")),
        status=known(row.get("status")),
        incorporation_date=parse_date(row.get("date_of_incorporation")),
        registered_state=known(row.get("registered_state")),
        raw=body,
        url=url,
        field_path=path,
    )


class ProviderRegistry:
    def __init__(self, base_url: str, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.base_url = base_url.rstrip("/")
        self.paths = self.s.registryapi.paths

    async def _send(
        self, job_id: Any, name: str, req: Req
    ) -> tuple[Any, str] | RegistryUnavailable | None:
        """(body, url) on 200; None on 404; RegistryUnavailable otherwise."""
        url = self.base_url + req.path
        try:
            resp = await gateway.call(
                job_id=job_id,
                endpoint=f"{GROUP_PROVIDER}:{name}",
                method=req.method,
                url=url,
                params=req.params,
                json=req.json,
                data_type=DATA_TYPE,
                est_credits=self.s.registryapi.est_credits_per_lookup,
            )
        except GatewayError as exc:
            return RegistryUnavailable(f"registry provider unreachable: {exc}")
        if resp.status == HTTP_NOT_FOUND:
            return None
        if resp.status != HTTP_OK:
            msg = f"registry provider returned HTTP {resp.status}"
            return RegistryUnavailable(msg, resp.status)
        return resp.body, url

    async def gst_search(
        self, job_id: Any, legal_name: str, state: str | None = None
    ) -> list[GstRecord] | RegistryUnavailable:
        req = gst_search_request(legal_name, state, self.paths)
        sent = await self._send(job_id, "gst_search", req)
        if sent is None or isinstance(sent, RegistryUnavailable):
            return [] if sent is None else sent
        return gst_search_response(*sent)

    async def gst_lookup(self, job_id: Any, gstin: str) -> GstRecord | RegistryUnavailable | None:
        req = gst_lookup_request(normalize_gstin(gstin), self.paths)
        sent = await self._send(job_id, "gst_lookup", req)
        if sent is None or isinstance(sent, RegistryUnavailable):
            return sent
        return gst_lookup_response(*sent)

    async def mca_lookup(
        self, job_id: Any, cin: str | None = None, name: str | None = None
    ) -> list[McaRecord] | RegistryUnavailable:
        c, n = _mca_args(cin, name)
        sent = await self._send(job_id, "mca_lookup", mca_lookup_request(c, n, self.paths))
        if sent is None or isinstance(sent, RegistryUnavailable):
            return [] if sent is None else sent
        return mca_lookup_response(*sent)


# ---- no provider / fixtures / routing ------------------------------------------------------
class UnavailableRegistry:
    def __init__(self, reason: str = "no registry provider configured") -> None:
        self.reason = reason

    async def gst_search(self, job_id: Any, legal_name: str, state: str | None = None):
        return RegistryUnavailable(self.reason)

    async def gst_lookup(self, job_id: Any, gstin: str):
        return RegistryUnavailable(self.reason)

    async def mca_lookup(self, job_id: Any, cin: str | None = None, name: str | None = None):
        return RegistryUnavailable(self.reason)


class FixtureRegistry:
    """Reads <dir>/*.json. A gstinapi.in body ({"data": {"gstin": ...}}) is one GST record;
    {"mca": [{cin, company_name, status, date_of_incorporation, registered_state}]} adds MCA
    records. Other files are ignored. Names match after normalize_name (dedup.name_suffixes)."""

    def __init__(self, directory: Path | str, suffixes: tuple[str, ...] = ()) -> None:
        self.dir = Path(directory)
        self.suffixes = suffixes
        self.gst: dict[str, GstRecord] = {}
        self.mca: dict[str, McaRecord] = {}
        for path in sorted(self.dir.glob("*.json")):
            self._load(path)

    def _load(self, path: Path) -> None:
        body = json.loads(path.read_text(encoding="utf-8"))
        url = f"fixture://registry/{path.name}"
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and data.get("gstin"):
            rec = gstinapi_record(body, url)
            self.gst[rec.gstin] = rec
        for rec in mca_lookup_response({"data": body.get("mca") or []}, url):
            self.mca[rec.cin] = rec

    def _same(self, a: str | None, b: str) -> bool:
        return bool(a) and normalize_name(a, self.suffixes) == normalize_name(b, self.suffixes)

    async def gst_search(self, job_id: Any, legal_name: str, state: str | None = None):
        return [
            r
            for r in self.gst.values()
            if self._same(r.legal_name, legal_name) and (state is None or r.state == state)
        ]

    async def gst_lookup(self, job_id: Any, gstin: str):
        return self.gst.get(normalize_gstin(gstin))

    async def mca_lookup(self, job_id: Any, cin: str | None = None, name: str | None = None):
        c, n = _mca_args(cin, name)
        if c:
            return [self.mca[c]] if c in self.mca else []
        return [r for r in self.mca.values() if self._same(r.name, n or "")]


class SplitRegistry:
    def __init__(self, gst: RegistryClient, mca: RegistryClient) -> None:
        self.gst, self.mca = gst, mca

    async def gst_search(self, job_id: Any, legal_name: str, state: str | None = None):
        return await self.gst.gst_search(job_id, legal_name, state)

    async def gst_lookup(self, job_id: Any, gstin: str):
        return await self.gst.gst_lookup(job_id, gstin)

    async def mca_lookup(self, job_id: Any, cin: str | None = None, name: str | None = None):
        return await self.mca.mca_lookup(job_id, cin=cin, name=name)


def build_client(name: str, s: Settings) -> RegistryClient:
    """One named implementation. A real provider without its env settings is unavailable."""
    if name == "gstinapi":
        if s.secrets.gstin_api_key is None:
            return UnavailableRegistry("GST registry not configured: GSTIN_API_KEY is not set")
        return GstinApiRegistry(s)
    if name == "provider":
        if not (s.secrets.registry_base_url and s.secrets.registry_api_key):
            return UnavailableRegistry(
                "registry provider not configured: REGISTRY_BASE_URL / REGISTRY_API_KEY not set"
            )
        return ProviderRegistry(s.secrets.registry_base_url, s)
    if name == "fixture":
        return FixtureRegistry(ROOT / s.registry.fixture_dir, s.dedup.name_suffixes)
    if name == "unavailable":
        return UnavailableRegistry()
    raise ValueError(f"unknown registry provider {name!r}; expected one of {PROVIDER_NAMES}")


def make_registry(settings: Settings | None = None) -> RegistryClient:
    """The client chosen by config (registry.gst_provider / mca_provider) or env
    REGISTRY_PROVIDER, which overrides both."""
    s = settings or get_settings()
    override = s.secrets.registry_provider
    gst_name = override or s.registry.gst_provider
    mca_name = override or s.registry.mca_provider
    if gst_name == mca_name:
        return build_client(gst_name, s)
    return SplitRegistry(build_client(gst_name, s), build_client(mca_name, s))
