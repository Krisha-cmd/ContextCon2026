"""Fakes for identity tests: page fetcher and registry. No network, no credits."""

from __future__ import annotations

from typing import Any

from app.discovery.crustdata import FetchResult
from app.trust.registry import GstRecord, McaRecord, RegistryUnavailable


class FakeFetch:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages, self.calls = pages, []

    async def __call__(self, job_id: Any, url: str) -> FetchResult:
        self.calls.append(url)
        content = self.pages.get(url)
        rec = None if content is None else {"url": url, "content": content, "success": True}
        return FetchResult(rec, "h" * 64, url)


def mca(cin: str, name: str, state: str | None = None, status: str = "Active") -> McaRecord:
    raw = {"cin": cin, "company_name": name, "status": status, "state": state}
    return McaRecord(cin, name, status, None, state, raw, f"mca://{cin}")


def gst(gstin: str, name: str, status: str = "Active", state: str | None = None) -> GstRecord:
    raw = {"success": True, "data": {"gstin": gstin, "legal_name": name, "status": status}}
    return GstRecord(gstin, name, None, state, status, None, None, raw,
                     f"https://www.gstinapi.in/v1/gstin/{gstin}")  # fmt: skip


class FakeRegistry:
    """Answers from dicts. unsupported={"mca_lookup", "gst_search"} mimics gstinapi.in, whose
    only call is gst_lookup; gst_search is unsupported unless `gst_by_name` is given."""

    def __init__(self, *, mca_by_cin: dict | None = None, search: dict | None = None,
                 gst_by_gstin: dict | None = None, gst_by_name: dict | None = None,
                 unsupported: set[str] | None = None) -> None:  # fmt: skip
        self.mca_by_cin = mca_by_cin or {}
        self.search = search or {}
        self.gst_by_gstin = gst_by_gstin or {}
        self.gst_by_name = gst_by_name
        self.unsupported = set(unsupported or ())
        if gst_by_name is None:
            self.unsupported.add("gst_search")
        self.calls: list[tuple[str, str]] = []

    def _down(self, name: str, arg: str) -> RegistryUnavailable | None:
        self.calls.append((name, arg))
        return RegistryUnavailable(f"provider has no {name}") if name in self.unsupported else None

    async def mca_lookup(self, job_id: Any, cin: str | None = None, name: str | None = None):
        if down := self._down("mca_lookup", cin or name or ""):
            return down
        if cin:
            return [self.mca_by_cin[cin]] if cin in self.mca_by_cin else []
        return self.search.get(name, [])

    async def gst_lookup(self, job_id: Any, gstin: str):
        return self._down("gst_lookup", gstin) or self.gst_by_gstin.get(gstin)

    async def gst_search(self, job_id: Any, legal_name: str, state: str | None = None):
        if down := self._down("gst_search", legal_name):
            return down
        return (self.gst_by_name or {}).get(legal_name, [])
