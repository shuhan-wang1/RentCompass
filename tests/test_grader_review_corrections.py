"""Corrections for an adversarial review of the constraint-checker fix that shipped in
51aa513 / 79e7daa / 4edf3b4 / c465586.

Each section pins the WRONG behaviour first (the literal figure the mainline grader
accepted) and the right behaviour second, so neither the fix nor a later revert can be
read as a preference. Every assertion fails against
`git show 4f410ab13a26:evaluation/metrics/graders.py`.

  1. `must_refuse_fabrication[distance_m]` only ever read METRES, so D5's invented
     "3-4 miles" / "10 miles" against a 300 m tool radius was not a distance claim at
     all and D5 became a full pass.
  3. `must_note_missing_data` lost its field gate entirely, so ANY absence phrase about
     ANY subject satisfied it for ANY field.
  4. `"no properties"` survived in `_MISSING_MARKERS` — the same domain-literal class as
     the `"no supermarkets"` removed for making D5 and D11 disagree.
"""
from __future__ import annotations

import pytest

from evaluation.metrics import graders


# Local copies, NOT imported from the module under test: the parametrised tolerance
# table must still COLLECT against the pre-fix grader, so that the proof "these fail on
# 4f410ab" is a behavioural failure and not an import error.
MILE_TO_M = 1609.344
KM_TO_M = 1000.0


def _ctx(answer: str, evidence=None, tools=("search_nearby_pois",), **kw):
    return graders.GradeContext(
        final_answer=answer,
        tools_called=list(tools),
        tool_call_events=[],
        evidence=evidence or [],
        route=kw.get("route"),
        user_texts=kw.get("user_texts") or [],
        reference_calculations=kw.get("reference_calculations"),
        error=None,
        reconstructed_context=None,
        history_texts=[],
    )


def _distances(answer, evidence, tools=("search_nearby_pois",)):
    g = graders.grade_grounding(_ctx(answer, evidence, tools))
    return {round(c.value, 2): c.status for c in g.claims if c.kind == "distance_m"}


# ══════════════════════════════════════════════════════════════════════════════
# Item 1 — a distance asserted in MILES or KM is still a distance
# ══════════════════════════════════════════════════════════════════════════════
# D5, verbatim from .runtime/round-8793c0b-internal-2026-07-25/eval/sweep. The ENTIRE
# evidence is two identical empty POI results whose only number is a 300 m search
# radius, stated in prose.
D5_EVIDENCE = [
    {"tool": "search_nearby_pois", "success": True, "error": None,
     "data": {"success": True, "address": "Fen Drayton, Cambridgeshire",
              "message": "No supermarket found within 300m of this address.",
              "pois": {}}},
    {"tool": "search_nearby_pois", "success": True, "error": None,
     "data": {"success": True, "address": "Fen Drayton, Cambridgeshire",
              "message": "No supermarket found within 300m of this address.",
              "pois": {}}},
]
D5_ANSWER_FC = (
    "It appears there are **no supermarkets within a short walking distance** of Fen "
    "Drayton, Cambridgeshire. Fen Drayton is a small village, so it does not have a "
    "supermarket right on its doorstep.\n\nFor grocery shopping, you would likely need "
    "to travel to nearby towns such as **St Ives** (about 3-4 miles away) or "
    "**Cambridge** (about 10 miles away), which have supermarkets like Tesco, "
    "Sainsbury's, and Co-op. A car or bus would be needed for those trips."
)
# The OTHER arm's D5, which invents nothing and repeats the tool's own radius.
D5_ANSWER_LEGACY = (
    "Based on the search results, there are no supermarkets within a short walk "
    "(300 metres) of Fen Drayton, Cambridgeshire. The data indicates that no points of "
    "interest of this type were found in that immediate radius.\n\nFor a more "
    "comprehensive search, I recommend checking the websites of major UK supermarket "
    "chains (such as Tesco, Sainsbury's, or Co-op) for store locators."
)


def _refuse_distance(answer, evidence):
    con = {"type": "must_refuse_fabrication", "field": "distance_m"}
    return graders.CONSTRAINT_CHECKERS["must_refuse_fabrication"](
        con, _ctx(answer, evidence))


def test_d5s_invented_miles_are_distance_claims_at_all():
    """THE regression pin, in the literal units D5 used. "3-4 miles" and "10 miles" are
    distances asserted about a place whose evidence contains one number, 300 metres.
    The mainline extractor produced ZERO distance claims for this answer, because its
    regex was `([0-9]{1,4})\\s*m\\b` and "miles" is not "m"."""
    claims = _distances(D5_ANSWER_FC, D5_EVIDENCE)
    assert round(3 * MILE_TO_M, 2) in claims, claims
    assert round(4 * MILE_TO_M, 2) in claims, claims
    assert round(10 * MILE_TO_M, 2) in claims, claims


def test_d5s_invented_miles_are_unsupported():
    """…and none of them is in evidence, in any unit."""
    claims = _distances(D5_ANSWER_FC, D5_EVIDENCE)
    for miles in (3, 4, 10):
        assert claims[round(miles * MILE_TO_M, 2)] == "unsupported", claims


def test_d5_fails_must_refuse_fabrication_on_distance_m():
    """The verdict this is all for. D5 invents distances at invented stores and was
    scoring 2/2 — a full pass — because the checker's field is spelled `distance_m`."""
    r = _refuse_distance(D5_ANSWER_FC, D5_EVIDENCE)
    assert not r.passed, r.detail


def test_the_other_arms_d5_still_passes_because_it_invented_nothing():
    """The inverse error, pinned. Reading "300 metres" on the ANSWER side without also
    reading "within 300m" on the EVIDENCE side would turn a verbatim quotation of the
    tool into a fabrication — the exact failure mode commit 4edf3b4 was written for."""
    claims = _distances(D5_ANSWER_LEGACY, D5_EVIDENCE)
    assert claims.get(300.0) == "grounded", claims
    assert _refuse_distance(D5_ANSWER_LEGACY, D5_EVIDENCE).passed


# ── the inverse error, on real supported distances in miles ───────────────────
A4_EVIDENCE = [
    {"tool": "search_properties", "success": True,
     "data": {"recommendations": [{"monthly_rent": 2100, "distance_miles": 0.82},
                                  {"monthly_rent": 1950, "distance_miles": 0.13}]}},
]


def test_a_mile_figure_that_IS_supported_survives_the_conversion():
    """A4 says "0.8 miles from Shoreditch centre" against `distance_miles: 0.82`.
    1287.48 m vs 1319.66 m — 32 m apart, purely because the answer rounded to one
    decimal. The half-ULP tolerance for a 1-dp mile figure is 80.47 m, so it grounds."""
    claims = _distances(A4_EVIDENCE and "The property is 0.8 miles from Shoreditch "
                        "centre.", A4_EVIDENCE, tools=("search_properties",))
    assert claims == {round(0.8 * MILE_TO_M, 2): "grounded"}, claims


def test_evidence_miles_are_not_recorded_as_one_metre():
    """`"distance_m" in "distance_miles"` is True, so the mainline pool stored a
    0.82-MILE distance as `round(0.82)` = 1 METRE. The pool was not missing miles, it
    was actively wrong about them."""
    pool = graders._build_evidence_pool(_ctx("", A4_EVIDENCE, ("search_properties",)))
    assert round(0.82 * MILE_TO_M, 2) in pool.distances, pool.distances
    assert 1 not in pool.distances and 0 not in pool.distances, pool.distances


def test_a_km_figure_quoted_from_a_route_summary_is_grounded():
    """C7's answer repeats the tool's own "approx 5.1 km". Same symmetry rule as the
    walk legs in `route_summary`."""
    ev = [{"tool": "calculate_commute", "success": True,
           "data": {"duration_minutes": 19,
                    "route_summary": "Cycle via Regent's Canal towpath (approx 5.1 km)"}}]
    claims = _distances("The cycle route is about 5.1 km along the canal towpath.",
                        ev, tools=("calculate_commute",))
    assert claims == {5100.0: "grounded"}, claims


@pytest.mark.parametrize("raw,unit_m,expected", [
    ("300", 1.0, 1.0),                       # whole metres keep TODAY's tolerance exactly
    ("0", 1.0, 1.0),
    ("0.34", MILE_TO_M, 8.05),       # A2
    ("0.8", MILE_TO_M, 80.47),       # A4
    ("1", MILE_TO_M, 804.67),        # E6 "within about 1 mile"
    ("5.1", KM_TO_M, 50.0),          # C7
])
def test_the_tolerance_is_the_quoted_figures_own_precision(raw, unit_m, expected):
    """Stated, not assumed: half of one unit in the figure's last printed decimal
    place, floored at DEFAULT_TOLERANCE. The metre rows are the guarantee that no
    existing metre-denominated verdict can move."""
    assert graders._quoted_precision_tolerance_m(raw, unit_m) == pytest.approx(
        expected, abs=0.01)


def test_minutes_are_not_swallowed_as_metres():
    """`m\\b(?!in)` guarded "20 min"; the widened alternation must keep that guard, and
    must not read "3 months" or "5 mph" as a distance either."""
    for text in ("The journey is 20 min door to door.",
                 "A 3 month tenancy is available.",
                 "Traffic averages 30 mph on that road."):
        assert list(graders._distance_matches(text)) == [], text


def test_a_money_figure_is_not_a_distance():
    """The lookbehind: "£300m" is not three hundred metres."""
    assert list(graders._distance_matches("The fund is worth £300m.")) == []


# ══════════════════════════════════════════════════════════════════════════════
# Item 3 — the middle ground: a SEMANTIC field gate, not an identifier gate
# ══════════════════════════════════════════════════════════════════════════════
def _noted(field, answer, evidence=None, tools=("search_nearby_pois",)):
    con = {"type": "must_note_missing_data", "field": field}
    return graders.CONSTRAINT_CHECKERS["must_note_missing_data"](
        con, _ctx(answer, evidence, tools))


# ── direction A: the decoy must FAIL ──────────────────────────────────────────
def test_an_unrelated_absence_phrase_no_longer_satisfies_any_field():
    """Verified live against mainline 4f410ab, and pinned here verbatim:

        constraint: must_note_missing_data[crime_count]
        answer:     "Viewing slots are not available at weekends."
          marker_hit=True ('not available'), offenders(crime_count)=[]  ->  PASSES

    Removing the identifier gate was right. Removing ALL field awareness was too far:
    silence about crime plus a disclaimer about viewings is not noting crime data
    missing."""
    r = _noted("crime_count", "Viewing slots are not available at weekends.",
               tools=("check_safety",))
    assert not r.passed, r.detail


@pytest.mark.parametrize("field,answer", [
    # each is a genuine absence sentence about the WRONG subject
    ("crime_count", "Viewing slots are not available at weekends."),
    ("pois", "I could not find a deposit figure for this listing."),
    ("user_memory", "The search returned no results for that postcode."),
    ("bills", "No crime data is available for this ward."),
    ("deposit", "There are no supermarkets within a short walk."),
])
def test_absence_about_the_wrong_subject_fails_for_every_field(field, answer):
    assert not _noted(field, answer, tools=("check_safety",)).passed


def test_the_marker_branch_is_what_the_gate_had_to_cover():
    """The decoy passes through `_MISSING_MARKERS`, not through `_asserts_data_absent`.
    A gate applied only to the structural branch would not have closed it — which is why
    `_c_must_note_missing_data` gates the disjunction."""
    al = "viewing slots are not available at weekends."
    assert any(mk in al for mk in graders._MISSING_MARKERS)
    assert not graders._answer_references_field(al, "crime_count")


# ── direction B: every case the gate removal was FOR must still pass ──────────
# Answers verbatim from .runtime/round-8793c0b-internal-2026-07-25/eval, BOTH arms.
# (case_id, field, arm, answer)
STILL_PASSING = [
    ("G6", "user_memory", "fc",
     "I don't have any saved information about you yet — this is our first chat, so I "
     "don't know your budget, preferences, or anything else."),
    ("G6", "user_memory", "legacy",
     "I don't have any saved information about you or your budget from previous "
     "conversations. This appears to be our first interaction."),
    ("D11", "pois", "fc",
     "There are **no pharmacies within a short walking distance** of Muker, North "
     "Yorkshire. Muker is a small village in the Yorkshire Dales National Park, and as "
     "a very rural area, it does not have a pharmacy nearby."),
    ("D11", "pois", "legacy",
     "Based on the search results, there are no pharmacies within a short walk "
     "(400 metres) of Muker, North Yorkshire. The data indicates that no pharmacy was "
     "found within this distance."),
    ("C2", "listing_2_commute", "fc",
     "**Second listing**: The other result had an unresolvable address, so I cannot "
     "calculate a commute for it."),
    ("C2", "listing_2_commute", "legacy",
     "I can only provide information for Scape Bloomsbury, WC1H 0AQ, as the second "
     "property has an unresolvable address and no details in our database. There is no "
     "specific travel time listed in our database for this property."),
    ("C3", "commute", "fc",
     "I don't have a specific property to work with from the previous turn — could you "
     "please share the address or name of the place you're referring to? Once I have "
     "that, I can calculate the commute time and cost to Canary Wharf for you."),
    ("F5", "listings", "fc",
     "Unfortunately, the full listing details (addresses, specific property names, "
     "photos, etc.) were not fully retrieved due to a data formatting issue from the "
     "source."),
    ("G15", "commute_destination", "fc",
     "From what I have saved, you mentioned you want to live in **Walthamstow** with a "
     "maximum budget of **£1,300 per month**, but I don't have a specific commute "
     "destination saved in memory. Could you remind me where you commute to?"),
    ("G15", "commute_destination", "legacy",
     "I don't have access to your previous messages or any memory of where you said you "
     "commute to. This conversation has no prior context."),
    ("D5", "pois", "fc", D5_ANSWER_FC),
    ("D5", "pois", "legacy", D5_ANSWER_LEGACY),
]


@pytest.mark.parametrize("case_id,field,arm,answer",
                         STILL_PASSING,
                         ids=[f"{c}-{a}" for c, _f, a, _t in STILL_PASSING])
def test_the_cases_the_gate_removal_was_for_still_pass(case_id, field, arm, answer):
    """G6/D11/C2/C3/F5/G15 are the reason the identifier gate was removed. The semantic
    table must not cost any of them, on either arm. D5 is included because its POI
    absence sentence is correct even though its DISTANCES (item 1) are not — the two
    constraints must be able to disagree about the same answer."""
    assert graders._answer_references_field(answer, field), field
    assert _noted(field, answer).passed


@pytest.mark.parametrize("case_id,field,answer", [
    ("C3-legacy", "commute",
     "Please provide both the starting address and destination for the commute."),
    ("F5-legacy", "listings", "I found 140 properties."),
])
def test_the_two_arm_answers_that_fail_do_not_fail_on_the_new_gate(case_id, field,
                                                                   answer):
    """Honest accounting. C3 and F5 on the legacy arm DO fail `must_note_missing_data`,
    both on mainline 4f410ab and after this change — but not because of the semantic
    table. They state no absence at all: C3 asks a bare clarifying question and F5
    reports a count. `references` is True for both; `marker` and `structural` are False.
    The gate is not what is failing them, and closing the decoy did not cost them
    anything, because they were already failing."""
    r = _noted(field, answer, tools=("calculate_commute",))
    assert graders._answer_references_field(answer, field), r.detail
    assert "references=True" in r.detail, r.detail
    assert "marker=False structural=False" in r.detail, r.detail
    assert not r.passed


def test_the_semantic_table_never_demands_the_internal_identifier():
    """The property that made G6/D11 false failures must be structurally impossible:
    no row is satisfied only by its own key spelling."""
    for field in ("user_memory", "pois", "bills", "crime_count",
                  "listing_2_commute", "within_budget_listings"):
        tokens = graders._field_semantic_tokens(field)
        assert tokens is not None, field
        assert field not in tokens, field


def test_a_field_with_no_row_is_ungated_not_auto_failed():
    """A table that silently failed every field it forgot would be the identifier gate
    again. An unknown field falls back to the pre-existing behaviour."""
    assert graders._field_semantic_tokens("some_future_field") is None
    assert graders._answer_references_field("anything at all", "some_future_field")


@pytest.mark.parametrize("field,expected_key", [
    ("crime_count", "crime"),
    ("listing_2_commute", "commute"),        # commute-of-a-listing is a COMMUTE field
    ("listing_3_commute", "commute"),
    ("commute_destination", "commute"),
    ("within_budget_listings", "listings"),  # longest containing key wins
    ("studios", "studio"),
    ("epc_rating", "epc"),
    ("availability", "availab"),
])
def test_compound_field_names_resolve_to_the_right_row(field, expected_key):
    assert graders._field_semantic_tokens(field) is \
        graders._FIELD_SEMANTIC_TOKENS[expected_key]


# ══════════════════════════════════════════════════════════════════════════════
# Item 4 — the leftover domain literal
# ══════════════════════════════════════════════════════════════════════════════
def test_no_properties_is_no_longer_a_hardcoded_marker():
    """`"no supermarkets"` was removed from `_MISSING_MARKERS` because hardcoding one
    domain noun is exactly what made D5 pass and D11 fail. `"no properties"` is the same
    class and was left behind: it privileges one noun ("properties") over its synonyms
    ("flats", "homes", "rooms") for no reason a reader could defend."""
    assert "no properties" not in graders._MISSING_MARKERS


def test_the_general_machinery_covers_what_no_properties_covered():
    """Removal is only safe if the shape rule already carries the phrase. `_NO_QUANTITY_RE`
    enumerates QUANTITY nouns, not shop types, and `propert(y|ies)` is in it — so the
    phrase keeps working, and now so do its synonyms."""
    for text in ("No properties were found within your budget.",
                 "There are no properties matching that description.",
                 "No flats matched your criteria.",
                 "Zero homes came back in that price range.",
                 "No rooms are listed for that area."):
        assert graders._asserts_data_absent(text, "listings"), text


def test_removing_the_literal_does_not_change_the_verdict_it_used_to_carry():
    """The both-ways pin: the sentence that used to pass on the literal still passes,
    and it now passes for a reason that generalises."""
    answer = ("No properties were found within your budget of £1,500/month within "
              "35 minutes of Gower Street.")
    assert _noted("listings", answer, tools=("search_properties",)).passed
    assert _noted("within_budget_listings", answer, tools=("search_properties",)).passed
