"""Offline tests for the destination-in-message gap fix.

Gap: when a message names a company/workplace AND a bare city together
("...Google office in London", "Deloitte London"), the bare city was grabbed as
the search area and the destination was lost (no commute lock). The fix adds a
conservative destination scan of the raw message (on_demand.extract_destination_
from_text) that takes precedence over the bare-city _extract_area grab in
search_properties_impl step 1.4a.

These tests are fully offline: they NEVER bind port 5001 and NEVER hit the
network. To prove the scan stays tier-1 (curated/keyword) and never falls through
to OSM, every test asserts _osm_classify is never invoked.
"""

import asyncio
import os
import sys

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core.scraping import on_demand
from core.scraping.on_demand import extract_destination_from_text, classify_place
from core.tools.search_properties import search_properties_impl, _extract_area


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if any classify_place call falls through to the OSM/Nominatim
    tier — the whole scan is designed to stay tier-1 (no network) offline."""
    def _boom(name):
        raise AssertionError(f"OSM tier reached for {name!r}; scan should be tier-1 only")
    monkeypatch.setattr(on_demand, "_osm_classify", _boom)
    # Classify cache is process-global; clear so a prior test's memoized result
    # can't hide an accidental network call here.
    on_demand._CLASSIFY_CACHE.clear()


# --------------------------------------------------------------------------
# Helper: extract_destination_from_text
# --------------------------------------------------------------------------
@pytest.mark.parametrize("msg,kind,city", [
    ("find me a place near the Google office in London", "workplace", "london"),
    ("places near Deloitte London", "workplace", "london"),
    ("near the Amazon office in Manchester", "workplace", "manchester"),
    ("flat near Barclays HQ", "workplace", "london"),
    ("I study at UCL", "university", "london"),
    ("room near Imperial College", "university", "london"),
    ("place near the University of Warwick", "university", None),
])
def test_helper_detects_destination(msg, kind, city):
    dest = extract_destination_from_text(msg)
    assert dest is not None, f"expected a destination in: {msg!r}"
    assert dest["kind"] == kind
    assert dest.get("name")
    if city is not None:
        assert dest.get("city") == city
    # A destination must carry a geocodable address for commute routing.
    assert dest.get("address")


@pytest.mark.parametrize("msg", [
    "flat in London",
    "near the park in Camden",
    "places in Shoreditch",
    "studios in Camden under 1500",
    "near the park in Shoreditch",
    "somewhere in Canary Wharf",
    "a place in Manchester",
])
def test_helper_ignores_residential(msg):
    assert extract_destination_from_text(msg) is None, f"false lock on: {msg!r}"


def test_helper_empty():
    assert extract_destination_from_text("") is None
    assert extract_destination_from_text(None) is None


# --------------------------------------------------------------------------
# Integration: search_properties_impl step 1.4a
# --------------------------------------------------------------------------
def _run(msg, **kw):
    return asyncio.run(search_properties_impl(current_message=msg, user_query=msg, **kw))


@pytest.mark.parametrize("msg,dest_substr", [
    ("find me a place near the Google office in London", "Pancras"),
    ("places near Deloitte London", "New Street Square"),
    ("near the Amazon office in Manchester", "Principal Place"),
])
def test_impl_locks_commute_and_clears_bare_city(msg, dest_substr):
    # Precondition proving the gap: _extract_area grabs the bare city first.
    assert _extract_area(msg) in ("London", "Manchester")
    res = _run(msg)
    # The bare city was the destination's OWN city -> not used as the residence. The
    # tool locks the commute and DEFAULTS the search area to the destination's own area
    # (non-blocking), so the soft gate (budget/room_type) fires — never missing_area.
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"
    known = res["known_criteria"]
    assert known["area"]                             # defaulted to the destination's own area
    assert known["no_commute"] is False              # commute mode locked
    assert dest_substr.lower() in str(known["commute_destination"]).lower()
    assert "commute" not in res["missing_fields"]    # commute satisfied by the lock


def test_impl_keeps_distinct_residential_area():
    msg = "near the Google office, I want to live in Camden"
    assert _extract_area(msg) == "Camden"
    res = _run(msg)
    # Camden is a DISTINCT residential area (not the Google office's city) -> kept.
    known = res["known_criteria"]
    assert known["area"] == "Camden"
    assert known["no_commute"] is False
    assert "pancras" in str(known["commute_destination"]).lower()  # Google office
    # With area + commute satisfied, this proceeds to the soft gate, not missing_area.
    assert res.get("clarification_kind") != "missing_area"


@pytest.mark.parametrize("msg,locked", [
    ("flat near Barclays HQ", True),
    ("I study at UCL", True),
    ("room near Imperial College", True),
    ("place near the University of Warwick", True),
])
def test_impl_previously_working_still_lock(msg, locked):
    res = _run(msg)
    # Destination detected -> commute locked + search area defaulted to the destination's
    # own area (non-blocking). Soft gate fires for budget/room_type; commute is satisfied.
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"
    assert res["known_criteria"]["no_commute"] is False
    assert res["known_criteria"]["commute_destination"]
    assert res["known_criteria"]["area"]                 # defaulted, not None
    assert "commute" not in res["missing_fields"]


@pytest.mark.parametrize("msg,expect_area", [
    ("studios in Camden under 1500", "Camden"),
    ("places in Shoreditch", "Shoreditch"),
    ("somewhere in Canary Wharf", "Canary Wharf"),
])
def test_impl_residential_no_false_lock(msg, expect_area):
    res = _run(msg)
    known = res["known_criteria"]
    assert known["area"] == expect_area          # residential area kept, not cleared
    assert known["commute_destination"] is None  # NO commute lock
    # These proceed to the soft gate (asking about commute), never a false lock.
    assert res["clarification_kind"] == "soft_criteria"
    assert "commute" in [f for f in res["missing_fields"]] or "commute" in res["question"].lower()
