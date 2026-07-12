"""Move-in / availability-date feature tests.

Covers the production-grade "when can the tenancy start" feature end to end, with NO
live network (the scrape/enrichment layer is stubbed/monkeypatched):

  1. `_extract_move_in_date` — EN + ZH phrasings and the key negatives (bare month,
     unrelated numbers, the modal "may"), with a frozen "today" via the injectable
     `now=` seam.
  2. `parse_availability_date` / `_extract_available_from` — fuzzy availability text.
  3. Row contract — every recommendation carries `available_from` + the right
     `availability_status` for (a) no criterion, (b) available-before, (c)
     available-after, (d) unknown; enrichment `_available_from` wins over the rich field.
  4. Demotion ordering — a later-availability listing sorts last but is never excluded.
  5. search_direct validation — a bad move_in_date → ApiError 400; a good one lands in
     the tool call's resolved criteria; `_compose_search_line` renders it.
  6. Gate — a query missing ONLY move_in never triggers the gate; when the gate fires
     for budget/room_type reasons its message + `missing_optional_fields` include
     move_in, and `missing_fields` stays exactly the recommended set.
"""

import ast
import asyncio
import os
import re
import sys
from datetime import date, datetime

import pytest

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

from core.scraping import on_demand
from core.scraping import onthemarket
from core.scraping.onthemarket import parse_availability_date, _extract_available_from
from core.tools.search_properties import (
    search_properties_impl, set_rag_coordinator,
    _extract_move_in_date, _availability_status, _resolve_available_from,
    _valid_iso_date,
)

# Frozen "today" for every date-resolution assertion.
NOW = date(2026, 7, 12)


# --------------------------------------------------------------------------
# Stubs (same shape as test_soft_criteria_gate.py)
# --------------------------------------------------------------------------
def _row(addr, price, geo="51.52,-0.13", rt="1 bed Flat", avail=None, url=None):
    r = {
        "Address": addr, "URL": url or "https://www.onthemarket.com/details/x/",
        "Price": f"£{price} pcm", "geo_location": geo, "Geo_Location": geo,
        "Room_Type_Category": rt, "Description": "Bright flat near transport. Bus 10 min.",
        "Images": [],
    }
    if avail is not None:
        # Simulate what normalize.py leaves on the rich row ("" would become
        # "Contact agent"; a real value passes through).
        r["Available From"] = avail
    return r


class _FakeStore:
    def __init__(self):
        self.rows = []

    def build_index(self, rows):
        self.rows = list(rows)

    def search(self, query, top_k=10):
        return list(self.rows)


class _FakeCoordinator:
    def __init__(self):
        self.property_store = _FakeStore()

    def enhanced_search(self, query, criteria):
        rows = self.property_store.rows
        for r in rows:
            r.setdefault("similarity_score", 0.6)
        return list(rows), [], []


def _install_listings(monkeypatch, rows):
    m = {"slug": "x", "requested_location": "x", "requested_city": "london",
         "source": "scraped", "stale": False, "count": len(rows), "elapsed_s": 0.01, "message": ""}
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {"rows": list(rows), "meta": m})


def _no_scrape(monkeypatch):
    monkeypatch.setattr(on_demand, "get_listings",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate must fire before scraping")))


@pytest.fixture
def stub_env(monkeypatch):
    set_rag_coordinator(_FakeCoordinator())
    import core.maps_service as maps
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda origin, dest, mode="transit": 22)
    # Availability comes from the fixture rows / monkeypatched enrichment — never the net.
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    yield
    set_rag_coordinator(None)


def _run(**kwargs):
    return asyncio.run(search_properties_impl(**kwargs))


# ══════════════════════════════════════════════════════════════════════════
# 1. _extract_move_in_date — deterministic EN + ZH extraction
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("text,expected", [
    ("move in from September", "2026-09-01"),
    ("moving in on 1 Sep", "2026-09-01"),
    ("starting from 2026-09-01", "2026-09-01"),
    ("available from september", "2026-09-01"),
    ("from sept 1st", "2026-09-01"),
    ("I want to move in on 15/08/2026", "2026-08-15"),      # UK day-first
    ("available August 2026", "2026-08-01"),
    ("move in May", "2027-05-01"),                          # next occurrence (May passed)
    ("moving in December", "2026-12-01"),
    ("9月入住", "2026-09-01"),
    ("九月份入住", "2026-09-01"),
    ("2026年9月1日入住", "2026-09-01"),
    ("下个月入住", "2026-08-01"),                             # first of next month
    ("九月初搬进去", "2026-09-01"),
])
def test_extract_move_in_positive(text, expected):
    assert _extract_move_in_date(text, now=NOW) == expected


@pytest.mark.parametrize("text", [
    "",
    "September",                                # bare month, no move-in context
    "I love living in London in the autumn",   # month-less prose
    "my budget is 1500 and a 40 min commute",  # unrelated numbers
    "I may move in next week",                  # modal "may", not the month
    "a 2 bed flat with parking",
])
def test_extract_move_in_negative(text):
    assert _extract_move_in_date(text, now=NOW) is None


def test_extract_move_in_uses_today_when_now_omitted():
    # No crash + a real ISO date when the seam is not injected (production path).
    got = _extract_move_in_date("move in from December")
    assert re.match(r"^\d{4}-12-01$", got)


def test_valid_iso_date_guard():
    assert _valid_iso_date("2026-09-01") == "2026-09-01"
    assert _valid_iso_date("2026-02-31") is None   # impossible calendar date
    assert _valid_iso_date("2026/09/01") is None   # wrong shape
    assert _valid_iso_date("garbage") is None
    assert _valid_iso_date(None) is None


# ══════════════════════════════════════════════════════════════════════════
# 2. Availability-date parsing (scraper side)
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("text,expected", [
    ("Availability date: 1 Aug 2026", "2026-08-01"),
    ("Available from 15th August", "2026-08-15"),
    ("Available August 2026", "2026-08-01"),
    ("01/09/2026", "2026-09-01"),
    ("2026-09-01", "2026-09-01"),
    ("available now", "Available now"),
    ("available immediately", "Available now"),
    ("asap", "Available now"),
    ("", ""),
    ("garbage text with no date", ""),
    ("2 parking spaces available", ""),   # no month/date -> unknown, not a bogus date
])
def test_parse_availability_date(text, expected):
    assert parse_availability_date(text, now=NOW) == expected


def test_extract_available_from_json_letting_details():
    data = {"props": {"initialReduxState": {"property": {
        "lettingDetails": {"items": ["Availability date: 10 Aug 2026", "Furnished"]},
        "summary": "Nice flat"}}}}
    assert _extract_available_from(data, now=NOW) == "2026-08-10"


def test_extract_available_from_summary_prose_fallback():
    data = {"props": {"initialReduxState": {"property": {
        "lettingDetails": {"items": ["Furnished"]},
        "summary": "Available from August 2026 we are delighted to offer..."}}}}
    assert _extract_available_from(data, now=NOW) == "2026-08-01"


def test_extract_available_from_unknown():
    data = {"props": {"initialReduxState": {"property": {"summary": "A lovely home"}}}}
    assert _extract_available_from(data, now=NOW) == ""


def test_resolve_available_from_maps_contact_agent_to_empty():
    assert _resolve_available_from({"Available From": "Contact agent"}) == ""
    assert _resolve_available_from({"Available From": ""}) == ""
    assert _resolve_available_from({"Available From": "2026-08-01"}) == "2026-08-01"
    assert _resolve_available_from({"_available_from": "Available now"}) == "Available now"


# ══════════════════════════════════════════════════════════════════════════
# 3 + 4. Row contract + demotion ordering
# ══════════════════════════════════════════════════════════════════════════
def _fixture_rows():
    return [
        _row("A Before Rd, London", 1200, avail="2026-08-01", url="https://x/a"),   # before
        _row("B After Rd, London", 1300, avail="2026-10-01", url="https://x/b"),    # after
        _row("C Unknown Rd, London", 1400, avail="Contact agent", url="https://x/c"),  # unknown
    ]


def _by_addr(recs):
    return {r["address"].split(",")[0].strip(): r for r in recs}


def test_rows_have_availability_no_criterion(stub_env, monkeypatch):
    _install_listings(monkeypatch, _fixture_rows())
    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000, bedrooms=1)
    assert res["status"] == "found"
    recs = res["recommendations"]
    assert all("available_from" in r and "availability_status" in r for r in recs)
    m = _by_addr(recs)
    # available_from always present + honest; status blank without a move-in criterion.
    assert m["A Before Rd"]["available_from"] == "2026-08-01"
    assert m["B After Rd"]["available_from"] == "2026-10-01"
    assert m["C Unknown Rd"]["available_from"] == ""
    assert all(r["availability_status"] == "" for r in recs)


def test_rows_annotated_and_demoted_with_criterion(stub_env, monkeypatch):
    _install_listings(monkeypatch, _fixture_rows())
    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000, bedrooms=1,
               move_in_date="2026-09-01")
    assert res["status"] == "found"
    recs = res["recommendations"]
    m = _by_addr(recs)
    # (b) available-before -> fit; (c) available-after -> warn; (d) unknown -> blank.
    assert m["A Before Rd"]["availability_status"] == "✅ 可入住"
    assert m["B After Rd"]["availability_status"] == "⚠️ 2026-10-01 起租"
    assert m["C Unknown Rd"]["availability_status"] == ""
    # Demotion: the late listing is last, but NONE are excluded.
    assert {"A Before Rd", "B After Rd", "C Unknown Rd"} == set(m)
    assert recs[-1]["address"].startswith("B After Rd")
    # ranks are contiguous 1..N after the re-order
    assert [r["rank"] for r in recs] == list(range(1, len(recs) + 1))


def test_available_now_counts_as_fit(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("Now Rd, London", 1200, avail="Available now", url="https://x/n")])
    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000, bedrooms=1,
               move_in_date="2026-09-01")
    r = res["recommendations"][0]
    assert r["available_from"] == "Available now"
    assert r["availability_status"] == "✅ 可入住"


def test_enrichment_available_from_wins(monkeypatch):
    # Enrichment (detail page) availability takes priority over the rich-schema field.
    set_rag_coordinator(_FakeCoordinator())
    import core.maps_service as maps
    monkeypatch.setattr(maps, "geocode_address", lambda a: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "1")
    monkeypatch.setattr(onthemarket, "fetch_listing_details",
                        lambda url, **k: {"description": "full desc", "available_from": "2026-08-05"})
    _install_listings(monkeypatch, [_row("E Rd, London", 1200, avail="Contact agent", url="https://x/e")])
    try:
        res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000, bedrooms=1,
                   move_in_date="2026-09-01")
        r = res["recommendations"][0]
        assert r["available_from"] == "2026-08-05"        # enrichment beat "Contact agent"
        assert r["availability_status"] == "✅ 可入住"
        assert r["description"] == "full desc"
    finally:
        set_rag_coordinator(None)


def test_availability_status_helper_units():
    assert _availability_status("2026-08-01", "2026-09-01") == "✅ 可入住"
    assert _availability_status("2026-09-01", "2026-09-01") == "✅ 可入住"     # on the day
    assert _availability_status("2026-10-01", "2026-09-01") == "⚠️ 2026-10-01 起租"
    assert _availability_status("Available now", "2026-09-01") == "✅ 可入住"
    assert _availability_status("", "2026-09-01") == ""                       # unknown
    assert _availability_status("2026-10-01", None) == ""                     # no criterion


# ══════════════════════════════════════════════════════════════════════════
# 5. search_direct validation (helpers extracted from app.py without heavy import)
# ══════════════════════════════════════════════════════════════════════════
_APP_PATH = os.path.join(_ROOT, "app", "app.py")
_WANTED = {"ApiError", "_coerce_optional_iso_date", "_compose_search_line"}


def _load_app_helpers():
    with open(_APP_PATH, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_APP_PATH)
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and getattr(n, "name", None) in _WANTED]
    module = ast.Module(body=picked, type_ignores=[])
    ns = {"re": re, "datetime": datetime}
    exec(compile(module, _APP_PATH, "exec"), ns)  # noqa: S102 - trusted local source
    missing = _WANTED - ns.keys()
    assert not missing, f"failed to extract {missing} from app.py"
    return ns


_APP = _load_app_helpers()
_ApiError = _APP["ApiError"]
_coerce_optional_iso_date = _APP["_coerce_optional_iso_date"]
_compose_search_line = _APP["_compose_search_line"]


def test_search_direct_move_in_absent_is_none():
    assert _coerce_optional_iso_date(None, "move_in_date") is None
    assert _coerce_optional_iso_date("", "move_in_date") is None
    assert _coerce_optional_iso_date("   ", "move_in_date") is None


def test_search_direct_move_in_good_passes():
    assert _coerce_optional_iso_date("2026-09-01", "move_in_date") == "2026-09-01"


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-31", "01/09/2026", "next week", "2026-9-1", 20260901])
def test_search_direct_move_in_bad_400(bad):
    with pytest.raises(_ApiError) as ei:
        _coerce_optional_iso_date(bad, "move_in_date")
    assert ei.value.status == 400
    assert "move_in_date" in ei.value.message


def test_compose_search_line_includes_move_in():
    line = _compose_search_line("Camden", 1500, "month", 1, False, None, None, "2026-09-01")
    assert "move-in ≥2026-09-01" in line
    # omitted when absent
    assert "move-in" not in _compose_search_line("Camden", 1500, "month", 1, False, None, None)


def test_good_move_in_lands_in_resolved_criteria(stub_env, monkeypatch):
    # The value the endpoint mirrors into accumulated criteria is the tool's resolved
    # search_criteria / known_criteria snapshot — assert move_in_date is carried there.
    _install_listings(monkeypatch, [_row("A, London", 1200, avail="2026-08-01", url="https://x/a")])
    res = _run(area="Camden", no_commute=True, confirmed=True, max_budget=3000, bedrooms=1,
               move_in_date="2026-09-01")
    assert res["search_criteria"]["move_in_date"] == "2026-09-01"
    assert res["known_criteria"]["move_in_date"] == "2026-09-01"


# ══════════════════════════════════════════════════════════════════════════
# 6. Soft gate — move_in rides along but never triggers on its own
# ══════════════════════════════════════════════════════════════════════════
def test_gate_not_triggered_when_only_move_in_missing(stub_env, monkeypatch):
    # All recommended fields present, only move_in absent -> gate must NOT fire.
    _install_listings(monkeypatch, [_row("A, London", 1200)])
    res = _run(area="Camden", max_budget=1500, room_type="ensuite",
               commute_destination="UCL", max_commute_time=30)
    assert res.get("clarification_kind") != "soft_criteria"
    assert res["status"] in ("found", "no_results")


def test_gate_fires_for_recommended_and_lists_move_in_optionally(stub_env, monkeypatch):
    _no_scrape(monkeypatch)
    res = _run(area="Camden", current_message="find me a place in Camden")
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"
    # missing_fields stays exactly the recommended set (old contract preserved)
    assert set(res["missing_fields"]) == {"budget", "room_type", "commute"}
    # move_in exposed via the SEPARATE optional list + mentioned in the message
    assert res["missing_optional_fields"] == ["move_in"]
    assert "move in" in res["question"].lower()
    # known_criteria carries the move_in_date key (null here)
    assert "move_in_date" in res["known_criteria"]


def test_gate_move_in_supplied_this_turn_not_optionally_listed(stub_env, monkeypatch):
    _no_scrape(monkeypatch)
    res = _run(area="Camden", current_message="find me a place in Camden, moving in September")
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"
    assert res["missing_optional_fields"] == []            # move_in was extracted this turn
    # Resolved against the real "today" (no seam through the tool) -> a valid Sept-01 ISO.
    got = res["known_criteria"]["move_in_date"]
    assert got is not None and got.endswith("-09-01")
