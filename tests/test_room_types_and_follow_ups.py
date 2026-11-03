"""Several room types in one request, and "find some others" follow-ups.

"ensuite and studio" used to resolve to the studio alone: one value travelled through the
extractor, the tool argument, the OnTheMarket channel plan and the filter. A request for
several types now travels as one '+'-joined value ('studio+ensuite') end to end, and every
row says which of the requested types it is.

"These are too expensive, could you find some other properties" used to print the same
rows again. A follow-up asking for other listings now leaves the shown ones out, and a
"too expensive" one puts cheaper rows first without inventing a budget.
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
from core.scraping.on_demand import plan_channels, search_plan
from core.tools import search_properties as sp
from core.tools.search_properties import (
    SHOWN_LISTINGS,
    _extract_room_type,
    _matched_room_type,
    _matches_room_type,
    _normalize_room_type,
    _room_type_label,
    ground_hard_constraints,
    listing_follow_up,
    room_type_parts,
    search_properties_impl,
    search_properties_tool,
    set_rag_coordinator,
    shown_listing_records,
)


# ── extraction ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("ensuite and studio", "studio+ensuite"),
    ("2. ensuite and studio", "studio+ensuite"),
    ("a studio or an en-suite room", "studio+ensuite"),
    ("studio/ensuite", "studio+ensuite"),
    ("either a shared room or a studio", "studio+shared"),
    ("独卫或者studio", "studio+ensuite"),
    ("studio和独卫都可以", "studio+ensuite"),
    # One home described, not two offered.
    ("a studio with en-suite bathroom", "studio"),
    ("an en-suite room in a shared house", "ensuite"),
    ("not a studio, an ensuite please", "ensuite"),
    ("studio", "studio"),
    ("单间公寓就行", "studio"),
    ("单间就行", "shared"),
    ("just a nice flat", None),
])
def test_extract_room_types(text, expected):
    assert _extract_room_type(text) == expected


def test_the_reported_message_asks_for_both():
    text = ("1. I am not sure about the specific budget, could you first help me to establish a "
            "typical range for an ensuite and apartment for student in UCL? 2. ensuite and studio "
            "3. Yes I would need to communicate to UCL, I would take 40min with public transportation")
    assert _extract_room_type(text) == "studio+ensuite"
    # A studio-or-room request is not a 0-bedroom search: rooms sit in multi-bed shares.
    assert sp._extract_bedrooms(text) is None


def test_studio_or_one_bed_keeps_its_existing_handling():
    # The bedroom count is no room type; the search takes the studio and the 1-bed
    # stays the user's own note (studio_alternative_note), as before.
    assert _extract_room_type("studio or 1 bed") == "studio"


@pytest.mark.parametrize("value,expected", [
    ("studio", "studio"),
    ("Studio", "studio"),
    ("en-suite", "ensuite"),
    ("ensuite+studio", "studio+ensuite"),
    ("studio + ensuite", "studio+ensuite"),
    (["ensuite", "studio"], "studio+ensuite"),
    ("studio and en-suite", "studio+ensuite"),
    ("penthouse", None),
    (None, None),
    ("", None),
])
def test_normalize_room_type_is_backward_compatible(value, expected):
    assert _normalize_room_type(value) == expected


def test_labels_name_every_requested_type():
    assert _room_type_label("studio+ensuite", False) == "Studio or en-suite room"
    assert "或" in _room_type_label("studio+ensuite", True)
    assert _room_type_label("studio", False) == "Studio"


# ── schema (strict mode) ──────────────────────────────────────────────────
def test_room_type_stays_a_strict_valid_string():
    from core.strict_schema import to_strict_function_calling_format, validate_strict_compliance
    from core.tool_system import create_tool_registry
    spec = {s.name: s for s in create_tool_registry().list_specs()}["search_properties"]
    fc = to_strict_function_calling_format(spec)
    assert validate_strict_compliance(fc["function"]["parameters"]) == []
    prop = search_properties_tool.parameters["properties"]["room_type"]
    assert {"type": "string"} in prop["anyOf"] and "studio+ensuite" in prop["description"]
    dumped = search_properties_tool.input_model.model_validate(
        {"area": "Bloomsbury", "room_type": "studio+ensuite"}).model_dump(exclude_none=True)
    assert dumped["room_type"] == "studio+ensuite"


# ── channel plan ──────────────────────────────────────────────────────────
def test_plan_is_the_union_of_the_requested_types():
    assert search_plan(0, 6, "studio+ensuite") == "rooms+studio"
    channels = plan_channels("rooms+studio")
    assert set(channels) == {"studio", "house-flat-share", "student", "property"}
    assert channels[-1] == "property"            # the all-property fallback stays last
    # Single types are unchanged.
    assert search_plan(0, 6, "ensuite") == "rooms"
    assert search_plan(0, 0, "studio") == "studio"
    assert plan_channels("rooms") == ("house-flat-share", "student", "property")
    assert plan_channels("studio") == ("studio",)
    assert plan_channels("2-bed") == ("2-bed-property",)
    assert search_plan(1, 1, None) == "1-bed"


def test_a_combined_plan_has_its_own_cache_key():
    assert on_demand._query_key("bloomsbury", "rooms+studio") != on_demand._query_key("bloomsbury", "studio")


# ── filtering ─────────────────────────────────────────────────────────────
def test_a_row_passes_when_it_is_any_requested_type_and_says_which():
    studio = {"Room_Type_Category": "Studio"}
    ensuite = {"Room_Type_Category": "En-suite Room"}
    flat = {"Room_Type_Category": "2 bed Apartment", "Description": "main bedroom en-suite"}
    assert _matches_room_type(studio, "studio+ensuite")
    assert _matches_room_type(ensuite, "studio+ensuite")
    assert not _matches_room_type(flat, "studio+ensuite")
    assert _matched_room_type(studio, "studio+ensuite") == "studio"
    assert _matched_room_type(ensuite, "studio+ensuite") == "ensuite"
    assert _matched_room_type(flat, "studio+ensuite") is None


def test_validator_accepts_any_requested_type():
    from core.candidate_validation import validate_candidates

    def reasons(actual):
        result = validate_candidates(
            [{"room_type": actual, "address": "x", "price": 1200}],
            {"room_type": "studio+ensuite", "property_features": ["studio", "en-suite"]})
        status = result["statuses"][0]
        return status["reasons"] + status["unknown_reasons"]

    for actual in ("studio", "ensuite"):
        assert not any("room type" in r or "feature" in r for r in reasons(actual)), actual
    assert any("room type does not match studio or ensuite" in r for r in reasons("shared"))
    # A single type keeps its old reason text.
    single = validate_candidates([{"room_type": "shared", "address": "x", "price": 900}],
                                 {"room_type": "studio"})
    assert "room type does not match studio" in single["statuses"][0]["reasons"]


# ── accumulated criteria (state node) ─────────────────────────────────────
def test_state_node_keeps_both_types_and_withdraws_one():
    from core.langgraph_agent import _apply_explicit_criteria_updates
    acc = _apply_explicit_criteria_updates({}, "ensuite and studio")
    assert acc["room_type"] == "studio+ensuite"
    assert not acc.get("bedrooms")
    acc = _apply_explicit_criteria_updates(acc, "never mind the studio")
    assert acc["room_type"] == "ensuite"
    acc = _apply_explicit_criteria_updates({"room_type": "studio"}, "never mind the studio")
    assert acc["room_type"] is None


def test_grounding_keeps_the_stated_set_over_a_narrower_model_value():
    p, dropped = ground_hard_constraints({"room_type": "studio"}, {}, "ensuite and studio")
    assert p["room_type"] == "studio+ensuite" and "room_type" in dropped
    p, _ = ground_hard_constraints({"room_type": "ensuite+studio"}, {}, "ensuite and studio")
    assert p["room_type"] == "studio+ensuite"


def test_a_too_expensive_follow_up_never_becomes_a_budget():
    p, dropped = ground_hard_constraints(
        {"max_budget": 1500, "area": "Bloomsbury"}, {"room_type": "studio+ensuite"},
        "I think the ones that you listed are so expensive, could you find some other properties")
    assert "max_budget" not in p and "max_budget" in dropped
    assert p["room_type"] == "studio+ensuite"


# ── follow-up detection ───────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("I think the ones that you listed are so expensive, could you find some other properties",
     {"cheaper": True}),
    ("could you find some other properties", {"cheaper": False}),
    ("show me more", {"cheaper": False}),
    ("anything cheaper?", {"cheaper": True}),
    ("换一批", {"cheaper": False}),
    ("太贵了，有便宜点的吗", {"cheaper": True}),
    ("tell me more about the first one", None),
    ("these are not too expensive, I like them", None),
    ("studios in Camden please", None),
    ("", None),
])
def test_listing_follow_up(text, expected):
    assert listing_follow_up(text) == expected


def test_a_listing_is_the_same_listing_whichever_channel_linked_it():
    assert (sp._listing_identity("https://www.onthemarket.com/details/123/?channel=student")
            == sp._listing_identity("https://www.onthemarket.com/details/123/"))
    assert sp._listing_identity("", "1 Test Street", "£1,500/month") == ("address", "1 test street", 1500)
    assert sp._listing_identity("", "1 Test Street", None) is None


def test_shown_records_come_from_every_shown_list():
    ec = {"view_results": [{"url": "https://x/1", "address": "A", "price": "£1,500/month"}],
          "recommended_registry": [{"url": "https://x/2", "address": "B", "price": 1600}],
          "shown_alternatives": [{"url": "", "address": "C", "price": "£1,700/month"}]}
    urls = {r["url"] for r in shown_listing_records(ec)}
    assert urls == {"https://x/1", "https://x/2", ""}


# ── through the tool ──────────────────────────────────────────────────────
def _row(n, price, rt, area="bloomsbury"):
    return {
        "Address": f"{n} Test Street, London WC1", "URL": f"https://www.onthemarket.com/details/{n}/",
        "Price": f"£{price} pcm", "geo_location": "51.52,-0.13", "Geo_Location": "51.52,-0.13",
        "Room_Type_Category": rt, "Description": f"{rt} to rent. Bus 10 min.", "Images": [],
        "_search_area": area,
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
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    # No detail-page fetches: the rows' URLs are not real listings.
    from core.scraping import onthemarket as om
    monkeypatch.setattr(om, "fetch_listing_details", lambda url, **k: {})
    yield
    set_rag_coordinator(None)


def _serve(monkeypatch, rows_by_area, seen=None):
    def fake(location, *a, **k):
        if seen is not None:
            seen.append((location, k.get("room_type")))
        rows = [dict(r) for r in rows_by_area.get(str(location).lower(), [])]
        return {"rows": rows, "meta": {"source": "scraped", "stale": False, "count": len(rows),
                                       "elapsed_s": 0.01, "requested_city": "london"}}
    monkeypatch.setattr(on_demand, "get_listings", fake)


def _run(**kwargs):
    kwargs.setdefault("confirmed", True)
    return asyncio.run(search_properties_impl(**kwargs))


def test_a_studio_or_ensuite_search_returns_both_and_labels_them(stub_env, monkeypatch):
    seen = []
    rows = ([_row(i, 1700 + i * 10, "Studio") for i in range(1, 9)]
            + [_row(100 + i, 1000 + i * 10, "En-suite Room") for i in range(1, 9)]
            # A room in a 4-bed share: it must survive, the band is not a studio's 0 bedrooms.
            + [_row(200, 950, "4 bed House share", )]
            + [_row(300, 2900, "2 bed Apartment")])
    rows[-2]["Description"] = "Large en-suite room in a 4 bed house share."
    _serve(monkeypatch, {"bloomsbury": rows}, seen)
    res = _run(area="Bloomsbury", room_type="studio+ensuite",
               current_message="ensuite and studio", limit=10)
    assert res["status"] == "found", res.get("message")
    types = [r["room_type"] for r in res["recommendations"]]
    assert "studio" in types and "ensuite" in types
    assert all(t in ("studio", "ensuite") for t in types)
    assert types[0] != types[1]          # the page takes turns between the two types
    assert not any("2 bed Apartment" in str(r.get("property_type")) for r in res["recommendations"])
    assert res["search_criteria"]["room_type"] == "studio+ensuite"
    assert res["search_criteria"]["bedrooms"] is None
    assert seen and all(rt == "studio+ensuite" for _loc, rt in seen)


def _turn_one(monkeypatch):
    rows = [_row(i, 1500 + i * 100, "Studio") for i in range(1, 17)]   # £1,600 .. £3,100
    _serve(monkeypatch, {"bloomsbury": rows})
    first = _run(area="Bloomsbury", room_type="studio", current_message="studios in Bloomsbury",
                 limit=6)
    assert first["status"] == "found"
    return rows, first["recommendations"]


def test_too_expensive_find_others_shows_no_row_twice_and_cheaper_first(stub_env, monkeypatch):
    rows, shown = _turn_one(monkeypatch)
    shown_urls = {r["url"] for r in shown}
    shown_prices = sorted(int(r["price"].strip("£").split("/")[0]) for r in shown)
    median = shown_prices[len(shown_prices) // 2 - 1: len(shown_prices) // 2 + 1]
    message = ("I think the ones that you listed are so expensive, could you find some other "
               "properties")
    second = _run(area="Bloomsbury", room_type="studio", current_message=message, limit=6,
                  _shown_listings=shown)
    assert second["status"] == "found", second.get("message")
    recs = second["recommendations"]
    assert recs and not ({r["url"] for r in recs} & shown_urls)
    prices = [int(r["price"].strip("£").split("/")[0]) for r in recs]
    cap = second["follow_up"]["cheaper_than_monthly"]
    assert min(median) <= cap <= max(median)
    under = [p for p in prices if p < cap]
    assert prices[:len(under)] == sorted(under)       # the cheaper rows lead, cheapest first
    assert second["follow_up"]["excluded_shown"] == len(shown)
    assert second["follow_up"]["price_preference_source"] == "inferred_from_shown"
    # Inferred, never the user's budget.
    assert second["search_criteria"]["max_budget"] is None
    assert "not a budget you set" in second["summary"]


def test_shown_listings_reach_the_tool_through_the_context(stub_env, monkeypatch):
    rows, shown = _turn_one(monkeypatch)
    token = SHOWN_LISTINGS.set(shown)
    try:
        again = _run(area="Bloomsbury", room_type="studio",
                     current_message="could you find some other properties", limit=6)
    finally:
        SHOWN_LISTINGS.reset(token)
    assert not ({r["url"] for r in again["recommendations"]} & {r["url"] for r in shown})


def test_without_a_follow_up_the_shown_rows_may_return(stub_env, monkeypatch):
    rows, shown = _turn_one(monkeypatch)
    again = _run(area="Bloomsbury", room_type="studio", current_message="studios in Bloomsbury",
                 limit=6, _shown_listings=shown)
    assert again.get("follow_up") is None
    assert {r["url"] for r in again["recommendations"]} & {r["url"] for r in shown}


def test_when_everything_was_shown_it_says_so(stub_env, monkeypatch):
    rows = [_row(i, 1500 + i * 100, "Studio") for i in range(1, 4)]
    _serve(monkeypatch, {"bloomsbury": rows})
    shown = [{"url": r["URL"], "address": r["Address"], "price": 0} for r in rows]
    res = _run(area="Bloomsbury", room_type="studio", current_message="find some others",
               _shown_listings=shown)
    assert res["status"] == "no_results"
    assert "already been shown" in res["message"]
    assert res["recommendations"] == []


def test_a_follow_up_with_few_new_rows_reads_a_nearby_area(stub_env, monkeypatch):
    home = [_row(i, 1500 + i * 100, "Studio", area="camden") for i in range(1, 4)]
    nearby = [_row(50 + i, 1400 + i * 50, "Studio", area="bloomsbury") for i in range(1, 6)]
    seen = []
    _serve(monkeypatch, {"camden": home, "bloomsbury": nearby}, seen)
    shown = [{"url": r["URL"], "address": r["Address"], "price": 1600} for r in home[:2]]
    res = _run(area="Camden", room_type="studio", commute_destination="UCL",
               current_message="could you find some other properties", limit=6,
               _shown_listings=shown)
    assert res["status"] == "found", res.get("message")
    assert any(loc.lower() == "bloomsbury" for loc, _rt in seen)
    assert res["follow_up"]["widened_to"] == ["bloomsbury"]
    assert not ({r["url"] for r in res["recommendations"]} & {r["url"] for r in shown})
    # The widened area is not stored as the user's own search area.
    assert [a.lower() for a in res["search_criteria"]["areas"]] == ["camden"]


def test_agent_loop_sets_the_shown_listings_for_tool_dispatch():
    from core.agent_loop import _with_shown_listings
    captured = {}

    async def node(state):
        captured["shown"] = SHOWN_LISTINGS.get()
        return {}

    state = {"extracted_context": {"view_results": [
        {"url": "https://x/1", "address": "A", "price": "£1,500/month"}]}}
    asyncio.run(_with_shown_listings(node)(state))
    assert captured["shown"] == [{"url": "https://x/1", "address": "A", "price": "£1,500/month"}]
    assert SHOWN_LISTINGS.get() is None
