"""r18 acceptance follow-ups in the criteria layer (r6b).

1. rid 5a8ce09f (conv abd37493): after a Hackney search, "I also work in Shoreditch and I
   cycle to work." was answered "Refined the 3 listings already shown ... kept only listings
   in Shoreditch. 2 removed, 1 remain." No model ran: the deterministic refinement planner
   (refine_results.plan_refinement, both archs) took the workplace for an area filter,
   because _extract_area reads "work IN Shoreditch" as the location cue "in Shoreditch".
   The parser itself already stored commute_destination Shoreditch and kept area Hackney.
2. "I work at Old Street right now, budget £2000" stored the destination
   "Old Street right now, budget £2000" (same on main).
3. rids 640bdda0 / c14fe21d: an English user's cards carried budget_status "⚠️ 超预算 £200".
4. rid 640bdda0: search_properties' user_query / current_message carry the whole message
   ("I'm on Universal Credit and my son goes to St Mary's"). Server-side that is fine. The
   only place either leaves the server is the tool's natural-language extraction call (a
   second model call, made when no area resolves): it received the raw text. Listing
   fetches get the area slug, geocoding gets place names, and the RAG ranking is local.
"""
from __future__ import annotations

import asyncio

import pytest

from core.langgraph_agent import _apply_explicit_criteria_updates
from core.refine_results import plan_refinement
from core.tools.search_properties import (
    PropertyFilter, _extract_area, _housing_clauses_only, names_place_as_workplace,
    workplace_place_at,
)
from tests.test_hard_budget import _install_listings, _row, _run, stub_env  # noqa: F401

# r18 conv abd37493: the three listings shown before turn 3 (turn_snapshots.view_results).
R18_SHOWN = [
    {"address": "Flat Share - ALL Bills & Wifi Included - Bethnal Green", "area": "Hackney",
     "price": "£875/month"},
    {"address": "Plot Cosy Studio21July, Cosy Studio21July at Shoreditch, 419-437, Tower Hamlets Road E2",
     "area": "Hackney", "price": "£1749/month"},
    {"address": "Jacob House, London, E8", "area": "Hackney", "price": "£1950/month"},
]
R18_WORK = "I also work in Shoreditch and I cycle to work."
R18_BEFORE = {"area": "Hackney", "areas": ["Hackney"], "max_budget": 2000, "budget_period": "month",
              "constraint_sources": {"area": {"source": "user_message", "value": "Hackney",
                                              "text": "I'm looking for a flat in Hackney, budget £2,000, and I have a cat."}}}


# ── 1. a workplace is a commute destination, never a filter on the shown listings ───────
def test_r18_workplace_statement_is_not_a_refinement():
    assert plan_refinement(R18_WORK, R18_SHOWN) is None
    after = _apply_explicit_criteria_updates(dict(R18_BEFORE), R18_WORK)
    assert after["commute_destination"] == "Shoreditch"
    assert after["area"] == "Hackney" and after["areas"] == ["Hackney"]
    # With no other place, the search still centres on the workplace, as before.
    assert _extract_area(R18_WORK) == "Shoreditch"


def test_a_workplace_never_becomes_the_stored_home_area():
    # Recorded round messages that stored the workplace as the search area.
    for message in ("Please remember that my partner works in Canary Wharf.",
                    "Compare Camden, Islington and Clapham for a young professional working in Canary Wharf"):
        after = _apply_explicit_criteria_updates({}, message)
        assert after.get("area") is None and not after.get("areas"), message
    assert _apply_explicit_criteria_updates({}, "I live in Hackney")["area"] == "Hackney"


@pytest.mark.parametrize("message", [
    "only show the ones in Shoreditch", "what about Shoreditch?", "keep the ones in Shoreditch",
])
def test_real_refinements_still_filter_the_shown_listings(message):
    spec, kept = plan_refinement(message, R18_SHOWN)
    assert spec["filters"] == [{"kind": "area", "value": "Shoreditch"}]
    assert [row["price"] for row in kept] == ["£1749/month"]


MIXED = [{"address": "Mare St, E8", "area": "Hackney", "price": "£1500/month"},
         {"address": "Upper St, N1", "area": "Islington", "price": "£1900/month"}]


@pytest.mark.parametrize("message", [
    "I work in Hackney", "My office is near Hackney", "My partner works in Islington",
])
def test_a_workplace_naming_a_shown_area_label_is_not_a_subset(message):
    assert plan_refinement(message, MIXED) is None


def test_home_and_residential_wording_still_filter_or_switch():
    assert plan_refinement("only the Hackney ones", MIXED)[0]["filters"] == [{"kind": "area", "value": "hackney"}]
    # Working from home names the home.
    assert plan_refinement("I work from home in Hackney", MIXED)[0]["filters"] == [
        {"kind": "area", "value": "Hackney"}]
    assert _extract_area("flats in Hackney please, I work in Shoreditch") == "Hackney"
    assert workplace_place_at("i also work in shoreditch", len("i also work "))
    assert names_place_as_workplace(R18_WORK, "Shoreditch")
    assert not names_place_as_workplace("I live in Shoreditch and work in Shoreditch too", "Shoreditch")
    assert not workplace_place_at("find a flat near a good school in hackney", len("find a flat near a good school "))


# ── 2. the destination ends at its clause, then trailing time words go ─────────────────
@pytest.mark.parametrize("message, destination", [
    ("I work at Old Street right now, budget £2000", "Old Street"),
    ("I work at Old Street right now and my budget is £2000", "Old Street"),
    ("I work at Old Street right now; budget £2000", "Old Street"),
    ("I work at Old Street, £2000 max", "Old Street"),
    ("I work at Bank currently, and my budget is £2,000", "Bank"),
    # A comma before a capitalised word or a postcode continues the place.
    ("I work at Bank, London", "Bank, London"),
    ("I'd commute to Camden Road Station, N7.", "Camden Road Station, N7"),
    ("I commute to 1 Canada Square, London E14", "1 Canada Square, London E14"),
])
def test_destination_ends_at_its_clause(message, destination):
    assert _apply_explicit_criteria_updates({}, message)["commute_destination"] == destination


# ── 3. budget labels follow the reply language ───────────────────────────────────────────
@pytest.mark.parametrize("is_cjk, within, over", [
    (False, "✅ Within budget", "⚠️ Over budget by £200"),
    (True, "✅ 在预算内", "⚠️ 超预算 £200"),
])
def test_budget_status_follows_the_reply_language(is_cjk, within, over):
    perfect, soft = PropertyFilter.apply_hard_filters(
        [{"price": 1500}, {"price": 2200}], 2000, None, "Camden", commute_filter=False, is_cjk=is_cjk)
    assert [row["budget_status"] for row in perfect + soft] == [within, over]


def test_an_english_search_shows_english_budget_labels(stub_env, monkeypatch):
    _install_listings(monkeypatch, [_row("A, London", 1499), _row("C, London", 1582)])
    res = _run(area="Camden", max_budget=1500, reply_language="en")
    labels = [row["budget_status"] for row in res["recommendations"] + res["over_budget_alternatives"]]
    assert labels == ["✅ Within budget", "⚠️ Over budget by £82"]
    zh = _run(area="Camden", max_budget=1500, reply_language="zh")
    assert [row["budget_status"] for row in zh["recommendations"] + zh["over_budget_alternatives"]] == [
        "✅ 在预算内", "⚠️ 超预算 £82"]


# ── 4. the only off-server use of the message gets the housing clauses only ─────────────
R18_LIVE3 = ("Find a 1-bed in Camden under £1,800 a month, DSS accepted please — I'm on Universal "
             "Credit and my son goes to St Mary's.")
NO_AREA = ("Find a 1-bed under £1,800 a month near my office, I'm on Universal Credit and my son "
           "goes to St Mary's Primary School.")


def test_housing_clauses_only():
    assert _housing_clauses_only(NO_AREA) == "Find a 1-bed under £1,800 a month near my office."
    assert _housing_clauses_only("2-bed in Hackney under £2,500") == "2-bed in Hackney under £2,500"
    assert _housing_clauses_only("I'm on Universal Credit") == ""


def test_the_extraction_model_call_never_sees_the_sensitive_clauses(stub_env, monkeypatch):
    from core import llm_interface
    seen = []
    monkeypatch.setattr(llm_interface, "clarify_and_extract_criteria",
                        lambda text: seen.append(text) or {})
    _install_listings(monkeypatch, [_row("A, London", 1499)])
    _run(user_query=NO_AREA, current_message=NO_AREA, bedrooms=1, max_budget=1800, reply_language="en")
    assert seen == ["Find a 1-bed under £1,800 a month near my office."]
    # With the area resolved (the r18 live-3 call itself) no model call is made at all.
    seen.clear()
    _run(user_query="1-bed flat in Camden under £1,800 a month, DSS/Universal Credit accepted",
         current_message=R18_LIVE3, area="Camden", bedrooms=1, max_budget=1800, reply_language="en")
    assert seen == []
