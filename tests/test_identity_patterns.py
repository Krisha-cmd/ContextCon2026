from __future__ import annotations

from app.identity import patterns
from app.identity.india import cin_state, gst_state, same_state
from app.identity.site import scan_site
from tests.discovery_helpers import FakeSave
from tests.identity_helpers import FakeFetch


def _values(matches: list[patterns.Match]) -> list[str]:
    return [m.value for m in matches]


def test_cin_and_gstin_patterns() -> None:
    text = "CIN: L74999DL2016PLC000001 | GSTIN 33AAACC1206D1ZN | junk L7499DL2016PLC00001"
    assert _values(patterns.find_cins(text)) == ["L74999DL2016PLC000001"]
    assert _values(patterns.find_gstins(text)) == ["33AAACC1206D1ZN"]


def test_copyright_legal_name_variants() -> None:
    cases = {
        "© 2026 Honasa Consumer Limited. All rights reserved.": "Honasa Consumer Limited",
        "Copyright © 2019-2026 Acme Foods Pvt. Ltd.": "Acme Foods Pvt. Ltd.",
        "&copy; 2025 Sharma Logistics Private Limited": "Sharma Logistics Private Limited",
        "(c) 2024, Blue Ocean LLP": "Blue Ocean LLP",
    }
    for text, want in cases.items():
        assert _values(patterns.find_legal_names(text)) == [want], text


def test_copyright_without_legal_suffix_or_year_is_ignored() -> None:
    assert patterns.find_legal_names("© 2026 Mamaearth. All rights reserved.") == []
    assert patterns.find_legal_names("© Honasa Consumer Limited") == []


def test_several_gstins_ok_only_for_one_pan() -> None:
    same = patterns.find_gstins("27AAPCA1234B1Z5 and 29AAPCA1234B1Z1")
    assert patterns.same_pan(same)
    assert not patterns.same_pan(patterns.find_gstins("27AAPCA1234B1Z5 33AAACC1206D1ZN"))


def test_state_codes() -> None:
    assert gst_state("33AAACC1206D1ZN") == "Tamil Nadu"
    assert cin_state("L74999DL2016PLC000001") == "Delhi"
    assert same_state("Haryāna", "haryana") is True
    assert same_state("Orissa", "Odisha") is True
    assert same_state(None, "Delhi") is None


async def test_scan_site_skips_ambiguous_cins_and_keeps_quotes() -> None:
    pages = {
        "https://x.in": "Group: L74999DL2016PLC000001, U63090MH2011PTC000001",
        "https://x.in/privacy": "Contact 27AAPCA1234B1Z5. © 2026 X Foods Limited",
    }
    found = await scan_site("j", "x.in", ["", "/privacy", "/terms"], FakeFetch(pages), FakeSave())
    assert found.cin is None and any("several CINs" in n for n in found.notes)
    assert found.legal_name.value == "X Foods Limited"
    assert found.legal_name.source.quote == "© 2026 X Foods Limited"
    assert found.gstin.value == "27AAPCA1234B1Z5"
    assert found.pages == ["https://x.in", "https://x.in/privacy"]  # stopped at the hit
