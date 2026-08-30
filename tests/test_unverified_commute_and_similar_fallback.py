"""The all-or-nothing commute guard and its three companion defects.

Reported 2026-08-13 (London 07:29, HTTP 200, no timeout): the model produced three cheaper
listings with commute notes and the user received one fixed sentence instead —
"The commute condition could not be verified for the listing this round." Four defects
compounded into that one reply:

  1. hard filters the model INVENTED (£2000, 1 bedroom, room_type=shared) narrowed the
     search invisibly — the user had declared none of them;
  2. the search-internal commute annotation labelled a straight-line COORDINATE ESTIMATE as
     a measured "TfL transit" time, so the prose asserted a journey time no evidence backed;
  3. the final commute guard replaced the WHOLE answer rather than the unverified claim,
     discarding prices, areas and the honest caveats that were all still true;
  4. `no_exact_match_but_similar` never reached the panel formatter (which matched only
     `found`), so the listings never painted — and the similar rows carried a NEGATIVE
     "Over budget by £-267" because the recall is not budget-filtered.
"""
from __future__ import annotations

import asyncio
import os
import sys

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import pytest  # noqa: E402

import core.maps_service as maps  # noqa: E402
from core.candidate_validation import (  # noqa: E402
    render_similar_listings,
    validate_commute_response,
)
from core.scraping import on_demand  # noqa: E402
from core.tools.search_properties import (  # noqa: E402
    _clean_explanation,
    _travel_provenance,
    _travel_time_label,
    ground_hard_constraints,
    search_properties_impl,
    set_rag_coordinator,
)


# ══════════════════════════════════════════════════════════════════════════
# 1. Hard constraints the model invented are dropped before the search runs.
# ══════════════════════════════════════════════════════════════════════════
def test_model_invented_budget_bedrooms_and_room_type_are_dropped():
    """The reported turn: the user asked about commute; the model supplied £2000 / 1 bed /
    shared out of nowhere. None is traceable to the message or to accumulated state."""
    params = {"area": "stratford", "max_budget": 2000, "bedrooms": 1, "room_type": "shared"}
    grounded, dropped = ground_hard_constraints(
        params, {}, "Which of these has the shortest commute to Canary Wharf?")

    assert "max_budget" not in grounded
    assert "bedrooms" not in grounded
    assert "room_type" not in grounded
    assert set(dropped) == {"max_budget", "bedrooms", "room_type"}
    assert grounded["area"] == "stratford", "the area is user-visible and stays untouched"


def test_constraints_the_user_stated_this_turn_survive():
    params = {"max_budget": 1500, "bedrooms": 2, "room_type": "ensuite"}
    grounded, dropped = ground_hard_constraints(
        params, {}, "Looking for a 2 bed en-suite in Camden, budget £1500 pcm")

    assert grounded["max_budget"] == 1500
    assert grounded["bedrooms"] == 2
    assert grounded["room_type"] == "ensuite"
    assert dropped == []


def test_accumulated_constraints_survive_and_a_contradiction_is_reset():
    """A value carried from an earlier turn is grounded. A model value that contradicts it
    without appearing in this turn's message is reset to the accumulated one, not dropped —
    the user did state a budget, just not this one."""
    acc = {"max_budget": 1200, "bedrooms": 1, "room_type": "studio"}

    kept, dropped = ground_hard_constraints(dict(acc), acc, "any news?")
    assert (kept["max_budget"], kept["bedrooms"], kept["room_type"]) == (1200, 1, "studio")
    assert dropped == []

    reset, dropped = ground_hard_constraints({"max_budget": 2000}, acc, "any news?")
    assert reset["max_budget"] == 1200
    assert dropped == ["max_budget"]


def test_a_weekly_budget_reaches_the_tool_in_either_unit():
    """"£350 per week" legitimately arrives as 350 or as its monthly conversion."""
    message = "my budget is £350 per week"
    weekly, dropped_w = ground_hard_constraints({"max_budget": 350}, {}, message)
    assert weekly["max_budget"] == 350 and dropped_w == []

    monthly, dropped_m = ground_hard_constraints({"max_budget": 1517}, {}, message)
    assert monthly["max_budget"] == 1517 and dropped_m == []


# ══════════════════════════════════════════════════════════════════════════
# 2. A coordinate estimate is never presented as a measured TfL journey time.
# ══════════════════════════════════════════════════════════════════════════
def test_coordinate_estimate_is_not_labelled_tfl_transit():
    estimated = _clean_explanation("Bright flat.", 17, "Canary Wharf", "estimate")
    assert "TfL" not in estimated
    assert "estimated" in estimated.lower()
    assert "17" in estimated

    routed = _clean_explanation("Bright flat.", 17, "Canary Wharf", "routing")
    assert "TfL transit: 17 min to Canary Wharf" in routed


def test_provenance_comes_from_the_basis_not_from_a_non_null_return(monkeypatch):
    """``calculate_travel_time`` returns a bare int on BOTH branches — a TfL itinerary and its
    own straight-line fallback when TfL has no route (``maps_service.calculate_travel_time``).
    Reading "it returned a number" as "it routed" republishes a haversine guess as measured."""
    measured = {"duration_minutes": 17, "source": "TfL Journey Planner"}
    monkeypatch.setattr(maps, "travel_basis_if_known", lambda *a, **k: measured)
    assert _travel_provenance("12 Rennie St", "Canary Wharf", 17) == "routing"

    # The exact reported shape: TfL had no route, so duration_minutes is None and the number
    # the caller holds came from the straight-line fallback.
    no_route = {"duration_minutes": None, "estimated_duration_minutes": 17,
                "source": "estimate"}
    monkeypatch.setattr(maps, "travel_basis_if_known", lambda *a, **k: no_route)
    assert _travel_provenance("12 Rennie St", "Canary Wharf", 17) == "estimate"

    # Unknown basis (cache miss, stubbed producer) must understate, never overstate.
    monkeypatch.setattr(maps, "travel_basis_if_known", lambda *a, **k: None)
    assert _travel_provenance("12 Rennie St", "Canary Wharf", 17) == "estimate"
    assert _travel_provenance("12 Rennie St", "Canary Wharf", None) is None

    # The itinerary has to vouch for THIS figure. A cached 17 says nothing about a 9.
    monkeypatch.setattr(maps, "travel_basis_if_known", lambda *a, **k: measured)
    assert _travel_provenance("12 Rennie St", "Canary Wharf", 9) == "estimate"


def test_an_unknown_provenance_renders_as_an_estimate_not_as_tfl():
    """The default direction has to match ``commute_basis.is_measured``: anything not
    positively established as an itinerary is not one."""
    for unknown in (None, "", "unknown", "cached"):
        assert "TfL" not in _clean_explanation("Flat.", 17, "Canary Wharf", unknown), unknown
        assert "estimated" in _travel_time_label(17, "Canary Wharf", unknown), unknown
    assert _travel_time_label(17, "Canary Wharf", "routing") == "17 min to Canary Wharf"


@pytest.fixture
def commute_search(monkeypatch):
    """A one-listing London search with the commute annotation stage live."""
    set_rag_coordinator(_PassThroughCoordinator())
    monkeypatch.setattr(maps, "geocode_address", lambda addr: {"lat": 51.5054, "lng": -0.0235})
    monkeypatch.setattr(maps, "calculate_travel_time", lambda o, d, mode="transit": 17)
    monkeypatch.setattr(on_demand, "get_listings", lambda *a, **k: {
        "rows": [_similar_row("12 Rennie Street", 1733)],
        "meta": {"slug": "canary-wharf", "requested_city": "london", "source": "scraped",
                 "stale": False, "count": 1, "elapsed_s": 0.01, "message": ""}})
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    monkeypatch.setenv("SEARCH_GEO_VALIDATION_ENABLED", "0")
    monkeypatch.setenv("RANKER_V2_ENABLED", "0")
    yield
    set_rag_coordinator(None)


def _commute_row(monkeypatch, basis):
    monkeypatch.setattr(maps, "travel_basis_if_known", lambda *a, **k: basis)
    res = asyncio.run(search_properties_impl(
        area="canary wharf", commute_destination="Canary Wharf", confirmed=True,
        reply_language="en"))
    rows = res.get("recommendations") or []
    assert rows, res.get("status")
    return rows[0]


def test_an_unrouted_commute_never_reaches_a_card_labelled_tfl(commute_search, monkeypatch):
    """End-to-end: the label is decided by the pair's basis, not by the annotation stage
    assuming its own producer routed."""
    row = _commute_row(monkeypatch, {"duration_minutes": None, "source": "estimate"})
    assert row["travel_time_source"] == "estimate"
    assert "TfL" not in row["explanation"]
    assert "TfL" not in row["travel_time"]
    assert "estimated" in row["travel_time"]


def test_a_real_itinerary_is_still_labelled_tfl(commute_search, monkeypatch):
    row = _commute_row(monkeypatch, {"duration_minutes": 17, "source": "TfL Journey Planner"})
    assert row["travel_time_source"] == "routing"
    assert "TfL transit: 17 min to Canary Wharf" in row["explanation"]
    assert row["travel_time"] == "17 min to Canary Wharf"


# ══════════════════════════════════════════════════════════════════════════
# 3. The commute guard redacts the unverified claim, not the whole answer.
# ══════════════════════════════════════════════════════════════════════════
_UNVERIFIED_STATE = {
    "extracted_context": {"current_message": "Anything cheaper with a decent commute?"},
    "candidate_validation": {
        "constraints": {"max_commute_minutes": 45},
        "statuses": [{"candidate": {"address": "12 Rennie Street"}, "status": "unknown",
                      "evidence_status": "failed"}],
    },
}

_FIXED = "The commute condition could not be verified for the listing this round."


def test_prices_and_addresses_survive_an_unverifiable_commute_claim():
    text = ("Here are three cheaper options:\n"
            "- 12 Rennie Street, £1,733/month, 17 minutes to Canary Wharf\n"
            "- 40 Marsh Wall, £1,875/month, 24 minutes to Canary Wharf\n"
            "All three are inside your budget.")
    out = validate_commute_response(text, _UNVERIFIED_STATE)

    assert out != _FIXED, "a whole-answer replacement is the defect under test"
    assert "£1,733/month" in out and "£1,875/month" in out
    assert "12 Rennie Street" in out and "40 Marsh Wall" in out
    assert "All three are inside your budget." in out
    assert "17 minutes" not in out and "24 minutes" not in out
    assert "could not be verified" in out


def test_chinese_answer_keeps_price_and_gains_an_honest_note():
    state = dict(_UNVERIFIED_STATE,
                 extracted_context={"current_message": "有没有便宜点的？通勤呢",
                                    "reply_language": "zh"})
    text = "为您找到三套更便宜的房源：\n- 12 Rennie Street，£1,733/月，通勤约 17 分钟。"
    out = validate_commute_response(text, state)

    assert "£1,733/月" in out
    assert "12 Rennie Street" in out
    assert "17 分钟" not in out and "分钟" not in out.split("注：")[0]
    assert "注：" in out


def test_a_commute_verdict_with_no_number_is_redacted_too():
    """A conclusion is a claim. Removing "58 minutes" but keeping "exceeds your commute
    limit" leaves an unevidenced verdict standing under a note saying the unverified figures
    were removed — the guard would be reporting a redaction it did not perform."""
    for verdict in ("This one exceeds your commute limit.",
                    "The journey is longer than you asked for.",
                    "That commute falls outside your limit.",
                    "This commute comfortably meets your requirement."):
        text = f"- 12 Rennie Street, £1,733/month. {verdict}"
        out = validate_commute_response(text, _UNVERIFIED_STATE)
        assert "£1,733/month" in out, verdict
        assert verdict not in out, verdict


def test_a_full_length_model_answer_keeps_its_facts_and_loses_every_commute_claim():
    """The reported turn's shape: prose paragraphs, a numbered list, parenthetical asides,
    a verdict sentence and a closing question — not a simplified comma list."""
    text = (
        "I couldn't find anything that matched every condition, but here are three cheaper "
        "options near Canary Wharf.\n"
        "\n"
        "1. 12 Rennie Street — £1,733/month (about 17 minutes to Canary Wharf). A 1-bed flat "
        "in a modern block, available now.\n"
        "2. 40 Marsh Wall — £1,875/month. The journey takes around 24 minutes, which is well "
        "within your limit. Available from 2026-09-01.\n"
        "3. 3 Ostro Tower — £1,900/month. This one exceeds your commute limit, but it is the "
        "largest of the three.\n"
        "\n"
        "All three are below £2,000/month. Would you like me to look at any of them in more "
        "detail?")
    out = validate_commute_response(text, _UNVERIFIED_STATE)

    assert out != _FIXED
    for kept in ("12 Rennie Street", "40 Marsh Wall", "3 Ostro Tower",
                 "£1,733/month", "£1,875/month", "£1,900/month",
                 "available now", "2026-09-01", "All three are below £2,000/month",
                 "largest of the three", "in more detail?"):
        assert kept in out, kept
    for gone in ("17 minutes", "24 minutes", "within your limit", "exceeds your commute limit"):
        assert gone not in out, gone
    assert "could not be verified" in out


def test_a_verified_listing_keeps_its_figure_when_a_sibling_is_unverified():
    """Redaction is per listing. Deleting an EVIDENCED commute time because a different row
    failed is the same all-or-nothing error one level down."""
    state = {
        "extracted_context": {"current_message": "how are the commutes?"},
        "accumulated_search_criteria": {"max_travel_time": 45},
        "candidate_validation": {
            "constraints": {"max_commute_minutes": 45},
            "statuses": [
                {"candidate": {"address": "12 Rennie Street",
                               "verified_commute_minutes": 17},
                 "status": "eligible", "evidence_status": "success"},
                {"candidate": {"address": "40 Marsh Wall"},
                 "status": "unknown", "evidence_status": "failed"},
            ],
        },
    }
    text = ("Two options:\n"
            "- 12 Rennie Street, £1,733/month, 17 minutes to Canary Wharf, within your "
            "45-minute limit.\n"
            "- 40 Marsh Wall, £1,875/month, about 24 minutes to Canary Wharf.")
    out = validate_commute_response(text, state)

    assert "17 minutes to Canary Wharf" in out, "an evidenced figure must survive"
    assert "45-minute limit" in out, "the user's own cap is not an unverified figure"
    assert "24 minutes" not in out, "the unevidenced sibling still loses its figure"
    assert "£1,875/month" in out, "and keeps everything else"


def test_a_figure_that_disagrees_with_the_evidence_is_still_redacted():
    """Naming a verified listing does not license any number — only ITS measured duration."""
    state = {
        "extracted_context": {"current_message": "how long is the commute?"},
        "candidate_validation": {
            "constraints": {"max_commute_minutes": 45},
            "statuses": [
                {"candidate": {"address": "12 Rennie Street",
                               "verified_commute_minutes": 17},
                 "status": "eligible", "evidence_status": "success"},
                {"candidate": {"address": "40 Marsh Wall"},
                 "status": "unknown", "evidence_status": "failed"},
            ],
        },
    }
    out = validate_commute_response(
        "- 12 Rennie Street, £1,733/month, 9 minutes to Canary Wharf.", state)
    assert "9 minutes" not in out
    assert "£1,733/month" in out


def test_a_reply_that_is_only_a_commute_claim_still_falls_back_to_the_fixed_sentence():
    """Fail-closed is preserved: when nothing survives redaction there is nothing to keep."""
    out = validate_commute_response("It is about 17 minutes to Canary Wharf.", _UNVERIFIED_STATE)
    assert out == _FIXED


def _verified_state(minutes=17, cap=45):
    """A commute the ledger really does evidence — `verified_commute_minutes` and all."""
    return {
        "extracted_context": {"current_message": "how long is the commute?"},
        "candidate_validation": {
            "constraints": {"max_commute_minutes": cap},
            "statuses": [{"candidate": {"address": "12 Rennie Street",
                                        "verified_commute_minutes": minutes},
                          "status": "eligible", "evidence_status": "success"}],
        },
    }


def test_a_verified_commute_answer_is_untouched():
    text = "12 Rennie Street is 17 minutes from Canary Wharf."
    assert validate_commute_response(text, _verified_state()) == text


def test_a_success_status_with_no_measured_duration_is_not_a_pass():
    """`evidence_status: success` with no `verified_commute_minutes` leaves nothing to check
    the prose against. "We could not check" is not "we checked" — this branch used to return
    the answer verbatim, so any figure at all rode out on an empty ledger entry."""
    state = {
        "extracted_context": {"current_message": "how long is the commute?"},
        "candidate_validation": {
            "constraints": {"max_commute_minutes": 45},
            "statuses": [{"candidate": {"address": "12 Rennie Street"}, "status": "eligible",
                          "evidence_status": "success"}],
        },
    }
    out = validate_commute_response(
        "12 Rennie Street, £1,733/month, 99 minutes from Canary Wharf.", state)
    assert "99 minutes" not in out
    assert "£1,733/month" in out


def test_evidence_of_a_duration_does_not_license_the_opposite_conclusion():
    """17 minutes against a 45-minute cap supports "within" and REFUTES "exceeds". A verdict
    carrying no figure passed vacuously — `all([])` is True — so the wrong conclusion shipped
    on the strength of a measurement that says the opposite."""
    state = _verified_state(minutes=17, cap=45)

    for contradicted in ("12 Rennie Street: this commute exceeds your 45-minute limit.",
                         "12 Rennie Street takes 17 minutes, but this exceeds your "
                         "45-minute limit.",
                         "12 Rennie Street is over your commute limit."):
        out = validate_commute_response(contradicted, state)
        assert "exceeds" not in out and "over your commute limit" not in out, contradicted

    # Where a clause boundary separates the facts from the verdict, the facts survive; a
    # sentence that IS the verdict has nothing to salvage and falls back to the fixed line.
    assert "12 Rennie Street takes 17 minutes" in validate_commute_response(
        "12 Rennie Street takes 17 minutes, but this exceeds your 45-minute limit.", state)
    assert validate_commute_response(
        "12 Rennie Street is over your commute limit.", state) == _FIXED

    # The direction the evidence DOES support still ships, untouched.
    supported = "12 Rennie Street takes 17 minutes, within your 45-minute limit."
    assert validate_commute_response(supported, state) == supported


def test_a_verdict_against_a_listing_that_really_is_over_the_cap_still_ships():
    """The check is directional, not a blanket ban on the word "exceeds"."""
    state = _verified_state(minutes=58, cap=45)
    text = "12 Rennie Street takes 58 minutes, which exceeds your 45-minute limit."
    assert validate_commute_response(text, state) == text


def test_a_nested_address_label_does_not_lend_its_evidence_to_another_listing():
    """Listing labels nest: "Park Drive" is a substring of "Park Drive London E14". First-match
    binding attributed the long address's line to the short address's 9 minutes."""
    state = {
        "extracted_context": {"current_message": "how are the commutes?"},
        "candidate_validation": {
            "constraints": {"max_commute_minutes": 45},
            "statuses": [
                {"candidate": {"address": "Park Drive", "verified_commute_minutes": 9},
                 "status": "eligible", "evidence_status": "success"},
                {"candidate": {"address": "Park Drive London E14"},
                 "status": "unknown", "evidence_status": "failed"},
            ],
        },
    }
    out = validate_commute_response(
        "- Park Drive London E14, £2,100/month, 9 minutes to Canary Wharf.", state)
    assert "9 minutes" not in out, "the long address must not borrow the short one's evidence"
    assert "£2,100/month" in out


def test_the_direct_commute_path_checks_the_number_not_just_the_call_count():
    """One successful call licenses ONE duration — the one it returned. Counting artifacts and
    stopping there let a turn that measured 17 minutes answer "it takes 9 minutes"."""
    state = {
        "extracted_context": {"current_message": "how long is the commute to UCL?"},
        "tool_artifacts": [{"tool": "calculate_commute", "success": True,
                            "raw_data": {"duration_minutes": 17}}],
    }
    assert "9 minutes" not in validate_commute_response("It takes 9 minutes.", state)
    # The figure the call actually produced still ships.
    agrees = "It takes 17 minutes."
    assert validate_commute_response(agrees, state) == agrees


def test_a_reply_with_no_commute_claim_is_untouched():
    state = {"extracted_context": {"current_message": "what is the rent?"}}
    text = "The rent is £1,733 per month."
    assert validate_commute_response(text, state) == text


# ══════════════════════════════════════════════════════════════════════════
# 4. no_exact_match_but_similar: honest arithmetic, and the panel repaints.
# ══════════════════════════════════════════════════════════════════════════
class _FakeStore:
    def __init__(self, rows, keep_seeded):
        self.rows = list(rows)
        self._keep_seeded = keep_seeded

    def build_index(self, rows):
        if not self._keep_seeded:
            self.rows = list(rows)

    def search(self, query, top_k=10):
        return [dict(r) for r in self.rows]


class _FakeCoordinator:
    """Seeded pool for the similar-recall fallback; empty exact-match pool so it runs."""

    def __init__(self, rows):
        self.property_store = _FakeStore(rows, keep_seeded=True)

    def enhanced_search(self, query, criteria):
        return [], [], []


class _PassThroughCoordinator:
    """The ordinary shape: whatever was scraped comes back as the exact-match pool."""

    def __init__(self):
        self.property_store = _FakeStore([], keep_seeded=False)

    def enhanced_search(self, query, criteria):
        rows = self.property_store.rows
        for row in rows:
            row.setdefault("similarity_score", 0.6)
        return list(rows), [], []


def _similar_row(addr, price):
    return {"Address": addr, "Price": f"£{price} pcm", "parsed_price": price,
            "Room_Type_Category": "1 bed flat", "URL": f"https://example.test/{price}",
            "geo_location": "51.50,-0.02", "Images": [], "Description": f"{addr} — a flat.",
            "similarity_score": 0.7}


def _install_similar(monkeypatch, prices):
    """Seed the similar-recall pool with these prices and run one budgeted search."""
    rows = [_similar_row(f"{i} Test Street", price) for i, price in enumerate(prices, 1)]
    set_rag_coordinator(_FakeCoordinator(rows))
    return rows


@pytest.fixture
def similar_fallback(monkeypatch):
    rows = [_similar_row("12 Rennie Street", 1733),
            _similar_row("40 Marsh Wall", 1875),
            _similar_row("3 Ostro Tower", 1900)]
    set_rag_coordinator(_FakeCoordinator(rows))
    # One live listing exists (so the honest "nothing was scraped" early return does not
    # fire) but the exact-match pool ends up empty — the reported turn's shape, where the
    # room-type and commute filters cleared everything that was fetched.
    live = [_similar_row("99 Nowhere Wharf", 4200)]
    monkeypatch.setattr(on_demand, "get_listings",
                        lambda *a, **k: {"rows": [dict(r) for r in live], "meta": {
                            "slug": "canary-wharf", "requested_city": "london",
                            "source": "scraped", "stale": False, "count": len(live),
                            "elapsed_s": 0.01, "message": ""}})
    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    monkeypatch.setenv("SEARCH_GEO_VALIDATION_ENABLED", "0")
    monkeypatch.setenv("RANKER_V2_ENABLED", "0")
    yield
    set_rag_coordinator(None)


def test_under_budget_similar_rows_are_never_reported_as_over_budget(similar_fallback):
    """The recall is not budget-filtered, so its rows are routinely CHEAPER than the budget.
    `price - max_budget` then produced "Over budget by £-267" on a flat £267 under it."""
    res = asyncio.run(search_properties_impl(
        area="canary wharf", no_commute=True, confirmed=True, max_budget=2000,
        reply_language="en"))

    assert res["status"] == "no_exact_match_but_similar"
    rows = res["similar_properties"]
    assert rows, "the similar fallback should have produced rows"
    for row in rows:
        assert "£-" not in row["budget_status"], row["budget_status"]
        assert row["over_budget"] >= 0
        assert row["budget_status"] == "✅ Within budget"
    # And the headline must not claim a budget problem that does not exist.
    assert "within your budget of" not in res["message"]
    assert res["budget_increase_needed"] == 0
    assert "increasing your budget" not in res["suggestion"]
    assert "all within your £2000/month budget" in res["suggestion"]


def test_a_cheaper_listing_never_triggers_a_raise_your_budget_headline(similar_fallback,
                                                                      monkeypatch):
    """`suggested_budget` is the cheapest price x a margin, so it can exceed a budget every
    row is already under: £1,900 cheapest against £1,950 suggests £1,995. Deciding from that
    figure told the user to raise a budget the listings already fit."""
    _install_similar(monkeypatch, [1900, 1910, 1920])
    res = asyncio.run(search_properties_impl(
        area="canary wharf", no_commute=True, confirmed=True, max_budget=1950,
        reply_language="en"))

    assert res["status"] == "no_exact_match_but_similar"
    assert res["budget_increase_needed"] == 0
    assert "increasing your budget" not in res["suggestion"]
    assert "No properties were found within your budget" not in res["message"]
    for row in res["similar_properties"]:
        assert row["budget_status"] == "✅ Within budget"


def test_a_mixed_set_says_neither_all_within_budget_nor_raise_your_budget(similar_fallback,
                                                                         monkeypatch):
    """£1,900 / £2,100 / £2,200 against £2,000. The cheapest is inside the budget and the
    other two are not, so both single-verdict sentences contradict the per-row badges."""
    _install_similar(monkeypatch, [1900, 2100, 2200])
    res = asyncio.run(search_properties_impl(
        area="canary wharf", no_commute=True, confirmed=True, max_budget=2000,
        reply_language="en"))

    assert res["status"] == "no_exact_match_but_similar"
    suggestion = res["suggestion"]
    assert "all within your" not in suggestion
    assert "increasing your budget" not in suggestion
    assert "1 within your £2000/month budget and 2 over it" in suggestion
    assert res["budget_increase_needed"] == 0

    statuses = {row["price"]: row["budget_status"] for row in res["similar_properties"]}
    assert statuses["£1900/month"] == "✅ Within budget"
    assert "Over budget by £100" in statuses["£2100/month"]
    assert "Over budget by £200" in statuses["£2200/month"]


def test_an_entirely_over_budget_set_still_suggests_the_raise(similar_fallback, monkeypatch):
    """The original behaviour has to survive: when every recall really is over budget, the
    raise-your-budget suggestion is the useful answer."""
    _install_similar(monkeypatch, [2200, 2300, 2400])
    res = asyncio.run(search_properties_impl(
        area="canary wharf", no_commute=True, confirmed=True, max_budget=2000,
        reply_language="en"))

    assert res["status"] == "no_exact_match_but_similar"
    assert res["budget_increase_needed"] > 0
    assert "increasing your budget" in res["suggestion"]
    assert "No properties were found within your budget of £2000/month" in res["message"]
    for row in res["similar_properties"]:
        assert "Over budget by £" in row["budget_status"]
        assert "£-" not in row["budget_status"]


def test_similar_result_repaints_the_panel_instead_of_shipping_as_chat():
    """format_output_fc matched only `status == "found"`, so a similar-recall result was
    dropped entirely: tool_data stayed empty and /api/alex shipped a `chat` payload."""
    from core.agent_loop import build_fc_nodes

    class _NoTools:
        def list_specs(self):
            return []

        def get(self, name):
            return None

    raw = {
        "success": True,
        "status": "no_exact_match_but_similar",
        "message": "No listing matched every condition near canary wharf.",
        "suggestion": "However, I found 3 similar properties, all within your budget.",
        "recommendations": [{"rank": 1, "address": "12 Rennie Street", "price": "£1733/month",
                             "budget_status": "✅ Within budget",
                             "match_type": "similar_suggestion"}],
        "similar_properties": [{"rank": 1, "address": "12 Rennie Street",
                                "price": "£1733/month",
                                "budget_status": "✅ Within budget",
                                "match_type": "similar_suggestion"}],
        "search_criteria": {"area": "canary wharf", "max_budget": 2000},
        "area_recommendations": [],
    }
    nodes = build_fc_nodes(_NoTools())
    out = nodes["format_output_fc"]({
        "tool_artifacts": [{"tool": "search_properties", "raw_data": raw}],
        "user_preferences": {},
        "accumulated_search_criteria": {},
        "final_response": "Here are three cheaper options.",
        "response_type": "answer",
        "extracted_context": {},
    })

    assert out["response_type"] == "search"
    recs = out["tool_data"]["recommendations"]
    assert [r["address"] for r in recs] == ["12 Rennie Street"]
    assert recs[0]["candidate_status"] == "excluded", "a similar row is never an eligible match"
    assert recs[0]["status_reason"] == "similar_suggestion"
    # The reply states WHY there is no exact match and keeps the price.
    assert "No listing matched every condition" in out["final_response"]
    assert "£1733/month" in out["final_response"]


def test_similar_listings_render_keeps_price_and_does_not_claim_a_match():
    text = render_similar_listings(
        [{"address": "12 Rennie Street", "price": "£1733/month",
          "travel_time": "~17 min to Canary Wharf (estimated, unverified)"}],
        language="en")
    assert "do not meet every condition" in text
    assert "£1733/month" in text
    assert "estimated, unverified" in text
