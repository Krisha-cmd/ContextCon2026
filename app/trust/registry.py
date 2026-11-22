"""Indian registry adapter. Today the only provider is gstinapi.in, which offers exactly one
call: GET /v1/gstin/{gstin}. MCA lookups, company search by name and GSTINs-by-PAN are part of
the Registry protocol but raise RegistryUnsupported until a provider for them is wired in.

All HTTP goes through app.gateway (group "gstinapi": x-api-key header, 60 rpm, retries only on
429/502, at most 3 attempts, 24 h cache for successes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app import gateway
from app.config import Settings, get_settings
from app.discovery.normalize import known
from app.log import get_logger

log = get_logger(__name__)

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
ENDPOINT_GSTIN = "gstinapi:gstin"

# What the requester / operator is told for each gstinapi.in status code.
STATUS_MESSAGES: dict[int, str] = {
    400: "GSTIN format is invalid (no credit charged)",
    401: "GST registry API key is missing or invalid; check GSTIN_API_KEY",
    402: "GST registry account is out of credits; top up before running more lookups",
    403: "GST registry account is deactivated",
    404: "GSTIN is not registered in the GST database",
    429: "GST registry rate limit exceeded; retried and still limited",
    502: "GST provider temporarily unavailable; retried and still down",
}


class RegistryError(RuntimeError):
    """The registry could not answer (auth, credits, outage). Not a fact about the company."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RegistryOutOfCredits(RegistryError):  # noqa: N818 - reads better than ...Error here
    pass


class RegistryUnsupported(RegistryError):  # noqa: N818
    """The configured provider has no such lookup."""


class InvalidGstin(ValueError):  # noqa: N818
    pass


@dataclass(frozen=True)
class RegistryRecord:
    """One registry answer plus what is needed to cite it (url + field path + raw body)."""

    kind: str  # "gst" | "mca"
    id: str  # GSTIN or CIN
    legal_name: str | None
    status: str | None
    state: str | None  # state name if the provider gives one
    state_code: str | None  # GST numeric code or MCA RoC code
    url: str
    field_path: str
    raw: Any
    extra: dict[str, Any] = field(default_factory=dict)


class Registry(Protocol):
    async def gst_lookup(self, job_id: Any, gstin: str) -> RegistryRecord | None: ...
    async def gstins_by_pan(self, job_id: Any, pan: str) -> list[RegistryRecord]: ...
    async def mca_lookup(self, job_id: Any, cin: str) -> RegistryRecord | None: ...
    async def company_search(self, job_id: Any, name: str) -> list[RegistryRecord]: ...


def normalize_gstin(gstin: str) -> str:
    """Uppercase and validate before calling, so a typo never costs a request."""
    g = gstin.strip().upper()
    if not GSTIN_RE.match(g):
        raise InvalidGstin(f"not a valid GSTIN: {gstin!r}")
    return g


def gst_record(body: dict[str, Any], url: str) -> RegistryRecord:
    data = body.get("data") or {}
    return RegistryRecord(
        kind="gst",
        id=data.get("gstin") or body.get("gstin"),
        legal_name=known(data.get("legal_name")),
        status=known(data.get("status")),
        state=known((data.get("address_details") or {}).get("state")),
        state_code=known(data.get("state_code")),
        url=url,
        field_path="data",
        raw=body,
        extra={
            k: known(data.get(k))
            for k in (
                "trade_name",
                "taxpayer_type",
                "business_constitution",
                "registration_date",
                "cancellation_date",
                "pincode",
                "block_status",
            )
        },
    )


def error_for(status: int, body: Any) -> RegistryError:
    detail = body.get("error") if isinstance(body, dict) else None
    msg = STATUS_MESSAGES.get(status, f"GST registry returned HTTP {status}")
    if detail:
        msg = f"{msg} ({detail})"
    cls = RegistryOutOfCredits if status == 402 else RegistryError
    return cls(msg, status)


class GstinApiRegistry:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

    def gstin_url(self, gstin: str) -> str:
        url = f"{self.s.gstinapi.base_url}/v1/gstin/{gstin}"
        return url + ("?include=profile" if self.s.gstinapi.include_profile else "")

    async def gst_lookup(self, job_id: Any, gstin: str) -> RegistryRecord | None:
        """The GSTIN's registration, or None when it is not registered (404)."""
        g = normalize_gstin(gstin)
        url = self.gstin_url(g)
        resp = await gateway.call(
            job_id=job_id,
            endpoint=ENDPOINT_GSTIN,
            method="GET",
            url=url,
            data_type="registry",
            est_credits=self.s.gstinapi.est_credits_per_lookup,
        )
        body = resp.body if isinstance(resp.body, dict) else {}
        if "credits_remaining" in body:
            log.info(
                "gstinapi credits",
                extra={
                    "credits_remaining": body["credits_remaining"],
                    "from_cache": resp.from_cache,
                },
            )
        if resp.status == 404:
            return None
        if resp.status == 400:
            raise InvalidGstin(STATUS_MESSAGES[400])
        if resp.status != 200 or body.get("success") is False or not body.get("data"):
            raise error_for(resp.status, body)
        return gst_record(body, url)

    async def gstins_by_pan(self, job_id: Any, pan: str) -> list[RegistryRecord]:
        raise RegistryUnsupported("gstinapi.in has no GSTIN-by-PAN lookup")

    async def mca_lookup(self, job_id: Any, cin: str) -> RegistryRecord | None:
        raise RegistryUnsupported("gstinapi.in has no MCA (CIN) lookup")

    async def company_search(self, job_id: Any, name: str) -> list[RegistryRecord]:
        raise RegistryUnsupported("gstinapi.in has no MCA company search by name")
