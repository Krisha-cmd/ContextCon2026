"""Fakes for identity tests: page fetcher and registry. No network, no credits."""

from __future__ import annotations

from typing import Any

from app.discovery.crustdata import FetchResult
from app.trust.registry import RegistryRecord, RegistryUnsupported


class FakeFetch:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages, self.calls = pages, []

    async def __call__(self, job_id: Any, url: str) -> FetchResult:
        self.calls.append(url)
        content = self.pages.get(url)
        rec = None if content is None else {"url": url, "content": content, "success": True}
        return FetchResult(rec, "h" * 64, url)


def mca(cin: str, name: str, state: str | None = None, status: str = "Active") -> RegistryRecord:
    raw = {"cin": cin, "company_name": name, "status": status, "state": state}
    return RegistryRecord("mca", cin, name, status, state, cin[6:8], f"mca://{cin}", "data", raw)


def gst(gstin: str, name: str, status: str = "Active") -> RegistryRecord:
    raw = {"success": True, "data": {"gstin": gstin, "legal_name": name, "status": status}}
    return RegistryRecord("gst", gstin, name, status, None, gstin[:2],
                          f"https://www.gstinapi.in/v1/gstin/{gstin}", "data", raw)  # fmt: skip


class FakeRegistry:
    """Answers from dicts. Pass unsupported={"mca_lookup", ...} to mimic gstinapi.in."""

    def __init__(self, *, mca_by_cin: dict | None = None, search: dict | None = None,
                 gst_by_gstin: dict | None = None, by_pan: dict | None = None,
                 unsupported: set[str] | None = None) -> None:  # fmt: skip
        self.mca_by_cin = mca_by_cin or {}
        self.search = search or {}
        self.gst_by_gstin = gst_by_gstin or {}
        self.by_pan = by_pan
        self.unsupported = unsupported or set()
        self.calls: list[tuple[str, str]] = []

    def _call(self, name: str, arg: str) -> None:
        self.calls.append((name, arg))
        if name in self.unsupported:
            raise RegistryUnsupported(f"gstinapi.in has no {name}")

    async def mca_lookup(self, job_id: Any, cin: str) -> RegistryRecord | None:
        self._call("mca_lookup", cin)
        return self.mca_by_cin.get(cin)

    async def company_search(self, job_id: Any, name: str) -> list[RegistryRecord]:
        self._call("company_search", name)
        return self.search.get(name, [])

    async def gst_lookup(self, job_id: Any, gstin: str) -> RegistryRecord | None:
        self._call("gst_lookup", gstin)
        return self.gst_by_gstin.get(gstin)

    async def gstins_by_pan(self, job_id: Any, pan: str) -> list[RegistryRecord]:
        self._call("gstins_by_pan", pan)
        if self.by_pan is None:
            raise RegistryUnsupported("gstinapi.in has no GSTIN-by-PAN lookup")
        return self.by_pan.get(pan, [])
