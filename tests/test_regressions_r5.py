"""Regression tests (T-xx / U-xx ids in each test name)."""
from __future__ import annotations

import pytest


def _state_node(accumulated: dict, message: str, history=None) -> dict:
    from core.langgraph_agent import _make_extract_preferences_node
    state = {"user_preferences": {}, "accumulated_search_criteria": dict(accumulated),
             "extracted_context": {"current_message": message,
                                   "history": history or [{"user": "Find a flat", "assistant": "ok"}]}}
    update = _make_extract_preferences_node()(state)
    return update.get("accumulated_search_criteria", state["accumulated_search_criteria"])


_CAMDEN = {"area": "Camden", "areas": ["Camden"], "bedrooms": 1, "max_budget": 2200,
           "room_type": "whole", "max_travel_time": 30}


# T-01 ── a postcode in a workplace sentence is the destination, never the home area ──
@pytest.mark.parametrize("message", [
    "I work near EC2M 7PY",
    "My office is near SW1A 1AA",
    "I work at EC2M 7PY, find me a flat",
    "I study near WC1E 6BT",
    "My job is based in EC2M 7PY",
    "我在EC2M 7PY附近上班",
    "我在WC1E 6BT上学",
    "EC2M 7PY is where I work",
])
def test_t_01_a_workplace_postcode_keeps_the_home_area(message):
    from core.langgraph_agent import _postcode_search_location
    assert _postcode_search_location(message) is None
    acc = _state_node(_CAMDEN, message)
    assert acc["area"] == "Camden" and acc["areas"] == ["Camden"]


@pytest.mark.parametrize("message,postcode", [
    ("And near SW1A 1AA postcode?", "SW1A 1AA"),
    ("What about EC1A 4HQ?", "EC1A 4HQ"),
    ("Find me a flat near SW1A 1AA", "SW1A 1AA"),
    ("I'd like to live near E1 6AN", "E1 6AN"),
    ("I work near EC2M 7PY, any flats near SW1A 1AA?", "SW1A 1AA"),
])
def test_t_01_a_postcode_named_as_where_to_look_is_still_the_area(message, postcode):
    from core.langgraph_agent import _postcode_search_location
    assert _postcode_search_location(message) == postcode
    assert _state_node(_CAMDEN, message)["area"] == postcode


# T-02 ── a home-search postcode is not also locked as the commute destination ─────────
def test_t_02_a_postcode_search_area_is_not_locked_as_the_destination(monkeypatch):
    import asyncio
    from core.scraping import on_demand
    from core.tools.search_properties import search_properties_impl
    palace = "SW1A 1AA, City of Westminster, Greater London, England, United Kingdom"

    def fake_classify(name):
        key = (name or "").strip().lower()
        if key.replace(" ", "") == "sw1a1aa":
            return {"kind": "workplace", "slug": "sw1a-1aa", "city": "london", "address": palace,
                    "source": "osm"}
        return {"kind": "area", "slug": key, "city": None, "address": None, "source": "landmark"}
    monkeypatch.setattr(on_demand, "classify_place", fake_classify)
    monkeypatch.setattr(on_demand, "is_destination",
                        lambda r: (r.get("kind") if isinstance(r, dict) else r) in ("university", "workplace"),
                        raising=False)
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the soft gate fires before any scrape")))
    res = asyncio.run(search_properties_impl(area="SW1A 1AA", current_message="And near SW1A 1AA postcode?"))
    assert res["known_criteria"]["commute_destination"] is None
    assert res["known_criteria"]["area"] == "SW1A 1AA"
    # A postcode the message names as the workplace is still locked (unchanged behaviour).
    res = asyncio.run(search_properties_impl(area="SW1A 1AA", current_message="I work at SW1A 1AA"))
    assert res["known_criteria"]["commute_destination"] == palace
