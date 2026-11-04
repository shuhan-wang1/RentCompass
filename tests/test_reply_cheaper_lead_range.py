"""The 2026-11-03 live replay, turns 2 and 3.

Turn 2, "could you first help me to establish a typical range for an ensuite and a studio
… near UCL", got hall prices from web search and "I cannot give you a studio range", although
the listing data held Bloomsbury studios from £1,127: ``rent_price_summary`` gives the range
from our own listings without a listing search.

Turn 3, "I think the ones that you listed are so expensive, could you find some other
properties", came after a market range, not listings, so nothing had been shown: the rows
came in ranking order up to £2,860, and the reply was the bare ledger plus seven near-identical
"Returned journey" lines. Now the rows come cheapest first, a lead built from the tool data
frames the ledger, and journeys from many listings to one destination are stated once.
"""
from __future__ import annotations

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
from core.tools import rent_price_summary as rps
from core.tools.search_properties import (
    listing_follow_up,
    search_properties_impl,
    set_rag_coordinator,
)

TURN_3 = "I think the ones that you listed are so expensive, could you find some other properties"


def _row(n, price, rt, area="bloomsbury", period="pcm"):
    return {
        "Address": f"{n} Test Street, London WC1", "URL": f"https://www.onthemarket.com/details/{n}/",
        "Price": f"£{price} {period}", "geo_location": "51.52,-0.13", "Geo_Location": "51.52,-0.13",
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
    monkeypatch.setattr(maps, "calculate_travel_time", lambda o, d, mode="transit", **k: 22)
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    from core.scraping import onthemarket as om
    monkeypatch.setattr(om, "fetch_listing_details", lambda url, **k: {})
    yield
    set_rag_coordinator(None)


def _serve(monkeypatch, rows_by_area, seen=None):
    def fake(location, *a, **k):
        if seen is not None:
            seen.append((location, dict(k)))
        rows = [dict(r) for r in rows_by_area.get(str(location).lower(), [])]
        if k.get("cache_only") and rows_by_area.get("__cold__"):
            rows = []
        return {"rows": rows, "meta": {"source": "scraped", "stale": False, "count": len(rows),
                                       "elapsed_s": 0.01, "requested_city": "london"}}
    monkeypatch.setattr(on_demand, "get_listings", fake)


def _prices(recs):
    return [int(str(r["price"]).strip("£").split("/")[0].replace(",", "")) for r in recs]


# ── 1. "too expensive" with nothing shown: cheapest first, inferred, never a budget ─────────

def test_too_expensive_with_nothing_shown_is_still_a_follow_up():
    assert listing_follow_up(TURN_3) == {"cheaper": True}


def test_turn_three_after_a_range_answer_comes_cheapest_first(stub_env, monkeypatch):
    prices = [2860, 1127, 2470, 1300, 1795, 1550, 2145, 1399]
    _serve(monkeypatch, {"bloomsbury": [_row(i, p, "Studio") for i, p in enumerate(prices, 1)]})
    res = asyncio.run(search_properties_impl(
        area="Bloomsbury", room_type="studio", current_message=TURN_3, confirmed=True, limit=8,
        _shown_listings=[]))
    assert res["status"] == "found", res.get("message")
    got = _prices(res["recommendations"])
    assert got == sorted(got) and got[0] == 1127
    follow = res["follow_up"]
    assert follow["cheapest_first"] is True and follow["cheaper_than_monthly"] is None
    assert follow["price_preference_source"] == "inferred_cheapest_first"
    assert follow["excluded_shown"] == 0
    assert res["search_criteria"]["max_budget"] is None          # inferred, never the budget
    assert "cheapest first" in res["summary"] and "not a budget you set" in res["summary"]
    assert "Left out the 0" not in res["summary"]


def test_find_others_with_nothing_shown_changes_nothing(stub_env, monkeypatch):
    prices = [2860, 1127, 2470, 1300]
    _serve(monkeypatch, {"bloomsbury": [_row(i, p, "Studio") for i, p in enumerate(prices, 1)]})
    res = asyncio.run(search_properties_impl(
        area="Bloomsbury", room_type="studio", current_message="could you find some other properties",
        confirmed=True, limit=8, _shown_listings=[]))
    assert res["status"] == "found"
    assert not res.get("follow_up")


# ── 2. the deterministic lead and the grouped journeys ───────────────────────────────────────

def _eligible(price, minutes=12, address="Guilford Street, London WC1N"):
    return {"candidate": {"address": address, "price": f"£{price}/month",
                          "verified_commute_minutes": minutes}}


def test_lead_counts_area_and_price_span():
    from core.candidate_validation import render_search_lead
    ledger = {"eligible": [_eligible(1127), _eligible(1300), _eligible(2470)], "unknown": [], "excluded": []}
    assert render_search_lead(ledger, {"areas": ["bloomsbury"]}) == (
        "3 listings in Bloomsbury meet everything you asked for, from £1,127 to £2,470 a month.")
    zh = render_search_lead(ledger, {"areas": ["bloomsbury"]}, language="zh")
    assert zh == "Bloomsbury 有 3 套房源满足您提出的全部条件，月租 £1,127 到 £2,470。"


def test_lead_says_cheapest_first_only_when_the_rows_are():
    from core.candidate_validation import render_search_lead
    facts = {"areas": ["bloomsbury", "kings-cross"],
             "follow_up": {"cheapest_first": True, "excluded_shown": 0, "cheaper_than_monthly": None}}
    ordered = {"eligible": [_eligible(1127), _eligible(1300), _eligible(2470)]}
    text = render_search_lead(ordered, facts)
    assert text.startswith("3 listings in Bloomsbury and Kings Cross meet everything you asked for")
    assert "cheapest first" in text and "not a budget you set" in text
    shuffled = {"eligible": [_eligible(1300), _eligible(1127)]}
    assert "cheapest first" not in render_search_lead(shuffled, facts)


def test_lead_reports_what_a_follow_up_changed():
    from core.candidate_validation import render_search_lead
    facts = {"areas": ["bloomsbury"], "follow_up": {"excluded_shown": 10, "cheaper_than_monthly": 1534,
                                                     "widened_to": ["euston"]}}
    text = render_search_lead({"eligible": [_eligible(1500), _eligible(1798)]}, facts)
    assert "I left out the 10 listings you had already seen." in text
    assert "1 of them is under £1,534 a month" in text
    assert "nearby Euston was added" in text
    none_cheaper = render_search_lead({"eligible": [_eligible(1798)]}, facts)
    assert "Nothing under £1,534 a month was left in Bloomsbury" in none_cheaper


def test_lead_when_nothing_is_confirmed_claims_nothing():
    from core.candidate_validation import render_search_lead
    text = render_search_lead({"eligible": [], "unknown": [{"candidate": {"address": "x"}}]},
                              {"areas": ["bloomsbury"]})
    assert text == ("No listing in Bloomsbury could be confirmed against every condition you gave; "
                    "what was found is listed below.")
    assert render_search_lead({"eligible": [], "unknown": [], "excluded": []}, {}) == ""


def _journey(origin, minutes):
    return {"tool": "calculate_commute", "success": True,
            "raw_data": {"success": True, "from_address": origin,
                         "to_address": "University College London, Gower Street, London WC1E 6BT",
                         "duration_minutes": minutes, "duration_text": f"{minutes} mins",
                         "measured": True, "status": "success"}}


def test_listing_journeys_to_one_destination_are_stated_once():
    from core.candidate_validation import render_structured_evidence_fallback
    state = {"tool_artifacts": [_journey("Guilford Street, London WC1N", 13),
                                _journey("Marchmont Street, Bloomsbury WC1N", 11),
                                _journey("Woburn Place, London WC1H", 10)]}
    separate = render_structured_evidence_fallback(state, language="en")
    grouped = render_structured_evidence_fallback(state, language="en", group_listing_journeys=True)
    if separate.count("Returned journey") < 3:
        pytest.skip("the fixture journeys did not render as measured journeys")
    assert "Returned journey" not in grouped
    assert grouped.count("Journey times measured with the TfL planner from each of 3 listing addresses") == 1
    assert "10–13 minutes" in grouped
    # The per-listing guarantee survives: one itinerary never verifies another listing.
    assert "none verifies another listing's travel time" in grouped
    zh = render_structured_evidence_fallback(state, language="zh", group_listing_journeys=True)
    assert "3 个房源地址" in zh and "10–13 分钟" in zh and "不代表其他房源的通勤时长" in zh


def test_one_journey_keeps_its_own_line():
    from core.candidate_validation import render_structured_evidence_fallback
    state = {"tool_artifacts": [_journey("Guilford Street, London WC1N", 13)]}
    assert (render_structured_evidence_fallback(state, language="en", group_listing_journeys=True)
            == render_structured_evidence_fallback(state, language="en"))


# ── 3. rent_price_summary ────────────────────────────────────────────────────────────────────

def test_summary_figures_per_room_type():
    rows = ([_row(i, p, "Studio") for i, p in enumerate([1127, 1300, 1550, 1795, 2470], 1)]
            + [_row(50, 1650, "En-suite Room"), _row(51, 1517, "En-suite Room")]
            + [_row(60, 300, "Room (Shared)", period="pw")]      # weekly -> monthly
            + [_row(1, 1127, "Studio")])                          # the same listing twice
    out = {e["room_type"]: e for e in rps.summarise_rows(rows, "studio+ensuite+shared")}
    studio = out["studio"]
    assert studio["listing_count"] == 5
    assert studio["monthly_rent"] == {"min": 1127, "median": 1550, "max": 2470, "p25": 1300, "p75": 1795}
    assert studio["low_sample"] is False
    assert out["ensuite"]["listing_count"] == 2 and out["ensuite"]["low_sample"] is True
    assert "p25" not in out["ensuite"]["monthly_rent"]
    assert out["shared"]["monthly_rent"]["min"] == 1300            # £300 pw = £1,300 a month
    text = rps._summary_text(list(out.values()), "Bloomsbury", zh=False)
    assert "Studios in Bloomsbury: 5 listings advertised now; the middle half ask £1,300–£1,795" in text
    assert "too few for a typical range" in text
    none = rps._summary_text([{"room_type": "ensuite", "listing_count": 0, "monthly_rent": None,
                               "listings_read": 40}], "Bloomsbury", zh=False)
    assert none.startswith("En-suite rooms in Bloomsbury: none among the 40 listings read")
    assert "some may be missed" in none


def test_summary_reads_the_destinations_area_from_the_cache(monkeypatch):
    seen = []
    _serve(monkeypatch, {"bloomsbury": [_row(i, 1100 + 100 * i, "Studio") for i in range(1, 7)]}, seen)
    res = asyncio.run(rps.rent_price_summary_impl(area="London", destination="UCL",
                                                  room_types="studio+ensuite"))
    assert res["status"] == "ok" and res["area"] == "Bloomsbury"
    assert res["data_freshness"] == "cached"
    assert [loc for loc, _ in seen] == ["bloomsbury"] and seen[0][1]["cache_only"] is True
    assert seen[0][1]["room_type"] == "studio+ensuite"
    studio = next(e for e in res["results"] if e["room_type"] == "studio")
    assert studio["listing_count"] == 6
    assert "£" in res["summary"] and "listings" not in res.get("message", "")


def test_summary_scrapes_within_budget_only_when_the_cache_is_cold(monkeypatch):
    seen = []
    _serve(monkeypatch, {"bloomsbury": [_row(1, 1300, "Studio")], "__cold__": True}, seen)
    res = asyncio.run(rps.rent_price_summary_impl(area="Bloomsbury", room_types="studio"))
    assert res["status"] == "ok" and res["data_freshness"] == "live"
    assert seen[1][1].get("budget_s") and not seen[1][1].get("cache_only")


def test_summary_without_a_place_asks_for_one(monkeypatch):
    res = asyncio.run(rps.rent_price_summary_impl(room_types="studio"))
    assert res["status"] == "need_area" and res["results"] == []


def test_summary_schema_is_strict_compliant():
    from core.strict_schema import to_strict_schema, validate_strict_compliance
    from core.tool_system import create_tool_registry
    spec = {s.name: s for s in create_tool_registry().list_specs()}["rent_price_summary"]
    assert validate_strict_compliance(to_strict_schema(spec.input_schema)) == []


def test_summary_is_served_to_the_model_and_over_mcp():
    from core.tool_system import create_tool_registry
    from uk_rent_agent.web import asgi
    assert "rent_price_summary" in {s.name for s in create_tool_registry().list_specs()}
    names = next(v for k, v in vars(asgi).items()
                 if isinstance(v, (set, frozenset)) and "compare_or_rank_areas" in v)
    assert "rent_price_summary" in names


def test_summary_figures_are_price_evidence_for_the_critic():
    from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_reply_prices
    rows = [_row(i, p, "Studio") for i, p in enumerate([1127, 1300, 1550, 1795, 2470], 1)]
    results = rps.summarise_rows(rows, "studio")
    raw = {"status": "ok", "area": "Bloomsbury", "results": results,
           "summary": rps._summary_text(results, "Bloomsbury", zh=False)}
    draft = ("Studios near UCL in Bloomsbury currently ask about £1,300–£1,795 a month, with a "
             "median of £1,550; the cheapest is £1,127.")
    assert unsupported_reply_prices(draft, [raw]) == []
    verdict = evaluate_grounding(draft, [raw], retrieval_expected=True)
    assert not any(str(i).startswith("unsupported_prices") for i in verdict.issues), verdict.issues
    assert unsupported_reply_prices("The median studio is £1,950 a month.", [raw]) == [1950.0]


def test_the_range_rule_routes_range_questions_away_from_a_listing_search():
    from core import loop_prompts
    rules = loop_prompts.behaviour_rules()
    assert loop_prompts.RENT_RANGE_RULE in rules
    assert "rent_price_summary" in loop_prompts.RENT_RANGE_RULE
    assert "do NOT call search_properties" in loop_prompts.RENT_RANGE_RULE
    # The research guard still comes first.
    assert rules.index(loop_prompts.NO_SEARCH_YET_RULE) < rules.index(loop_prompts.RENT_RANGE_RULE)
