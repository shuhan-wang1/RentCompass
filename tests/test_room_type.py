"""Deliverable 2 — room_type end-to-end.

  * extraction (zh + en) -> canonical 'studio' | 'ensuite' | 'shared' | None;
  * _matches_room_type against the scraped Room_Type_Category / Description / Amenities;
  * schema round-trip through the tool's pydantic input model (guards pitfall 1: a
    param not declared in the schema is silently dropped by model_validate/model_dump);
  * filtering behaviour through search_properties_impl (studio also implies 0 beds).
"""

import asyncio
import os
import sys

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
from core.tools import search_properties as sp
from core.tools.search_properties import (
    _extract_room_type,
    _matches_room_type,
    _normalize_room_type,
    search_properties_impl,
    search_properties_tool,
    set_rag_coordinator,
)


# ── extraction ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("I want an en-suite room", "ensuite"),
    ("en suite please", "ensuite"),
    ("我要独立卫浴", "ensuite"),
    ("独卫的房间", "ensuite"),
    ("a studio near UCL", "studio"),
    ("单间公寓就行", "studio"),
    ("looking for a shared room", "shared"),
    ("flatshare is fine", "shared"),
    ("合租房", "shared"),
    ("just a nice flat", None),
    ("", None),
])
def test_extract_room_type(text, expected):
    assert _extract_room_type(text) == expected


def test_studio_wins_over_ensuite_when_both_present():
    # Studio is a distinct property form and is checked first.
    assert _extract_room_type("a studio with en-suite bathroom") == "studio"


def test_normalize_room_type_maps_synonyms_and_rejects_unknown():
    assert _normalize_room_type("en-suite") == "ensuite"
    assert _normalize_room_type("Studio") == "studio"
    assert _normalize_room_type("penthouse") is None
    assert _normalize_room_type(None) is None


# ── _matches_room_type ────────────────────────────────────────────────────
def test_matches_room_type_studio():
    assert _matches_room_type({"Room_Type_Category": "Studio"}, "studio") is True
    assert _matches_room_type({"Room_Type_Category": "1 bed Flat"}, "studio") is False


def test_matches_room_type_ensuite():
    assert _matches_room_type({"Room_Type_Category": "En-suite Room"}, "ensuite") is True
    assert _matches_room_type(
        {"Room_Type_Category": "1 bed Flat", "Detailed_Amenities": "Private en-suite bathroom"},
        "ensuite") is True
    assert _matches_room_type({"Room_Type_Category": "Studio"}, "ensuite") is False


def test_matches_room_type_shared():
    assert _matches_room_type({"Room_Type_Category": "1 bed Flat share"}, "shared") is True
    assert _matches_room_type({"Room_Type_Category": "Room (Shared)"}, "shared") is True
    assert _matches_room_type({"Room_Type_Category": "2 bed Apartment"}, "shared") is False


def test_matches_room_type_none_is_permissive():
    assert _matches_room_type({"Room_Type_Category": "Studio"}, None) is True


# ── schema round-trip (pitfall 1) ─────────────────────────────────────────
def test_room_type_declared_and_survives_input_model():
    props = search_properties_tool.parameters["properties"]
    for key in ("room_type", "confirmed", "criteria_gate_shown"):
        assert key in props, f"{key} must be declared or pydantic drops it"

    dumped = search_properties_tool.input_model.model_validate(
        {"area": "Camden", "room_type": "ensuite", "confirmed": True}
    ).model_dump(exclude_none=True)
    assert dumped["room_type"] == "ensuite"
    assert dumped["confirmed"] is True


# ── filtering through the tool ────────────────────────────────────────────
def _row(addr, price, rt):
    return {
        "Address": addr, "URL": "https://www.onthemarket.com/details/x/",
        "Price": f"£{price} pcm", "geo_location": "51.52,-0.13", "Geo_Location": "51.52,-0.13",
        "Room_Type_Category": rt, "Description": f"{rt} to rent. Bus 10 min.", "Images": [],
    }


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


@pytest.fixture
def stub_env(monkeypatch):
    set_rag_coordinator(_FakeCoordinator())
    import core.maps_service as maps
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda o, d, mode="transit": 22)
    yield
    set_rag_coordinator(None)


def _run(**kwargs):
    kwargs.setdefault("confirmed", True)  # bypass the soft gate; we test filtering
    return asyncio.run(search_properties_impl(**kwargs))


def test_room_type_studio_filters_out_non_studio(stub_env, monkeypatch):
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {
        "rows": [_row("Studio flat, London", 1200, "Studio"),
                 _row("One-bed flat, London", 1300, "1 bed Flat")],
        "meta": {"source": "scraped", "stale": False, "count": 2, "elapsed_s": 0.01,
                 "requested_city": "london"},
    })
    res = _run(area="Camden", room_type="studio")
    assert res["status"] == "found"
    addrs = " ".join(r["address"] for r in res["recommendations"])
    assert "Studio flat" in addrs
    assert "One-bed flat" not in addrs
    # studio implies a 0-bedroom search
    assert res["search_criteria"]["bedrooms"] == 0
    assert res["search_criteria"]["room_type"] == "studio"


def test_room_type_ensuite_filters(stub_env, monkeypatch):
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {
        "rows": [_row("Ensuite room, London", 900, "En-suite Room"),
                 _row("Whole flat, London", 1600, "2 bed Apartment")],
        "meta": {"source": "scraped", "stale": False, "count": 2, "elapsed_s": 0.01,
                 "requested_city": "london"},
    })
    res = _run(area="Camden", room_type="ensuite")
    assert res["status"] == "found"
    addrs = " ".join(r["address"] for r in res["recommendations"])
    assert "Ensuite room" in addrs
    assert "Whole flat" not in addrs
