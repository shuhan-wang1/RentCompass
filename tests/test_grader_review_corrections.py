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
  6. Both the 5-week and the 6-week deposit cap were listed as derivable, so whichever
     one the model applied was "supported" — B7 shipped £5,192.31 where the statute
     gives £6,230.77 and `no_fabricated_number` saw nothing wrong.
  5. `_field_to_kind` had no row resolving to the `safety_score` claim kind, so
     `no_fabricated_number[safety_score]` was an unconditional pass (HANDOFF §0 #4).
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


# ══════════════════════════════════════════════════════════════════════════════
# Item 6 — only the STATUTORILY CORRECT deposit multiple is derivable
# ══════════════════════════════════════════════════════════════════════════════
# Tenant Fees Act 2019: five weeks' rent, rising to six when the ANNUAL rent is £50,000
# or more (inclusive). Listing both multiples as derivable made whichever one the model
# applied "supported", which is the whole defect.
B7_ANSWER = (
    "In the UK, the standard deposit for a rented property is typically capped at "
    "**5 weeks' rent** (for properties with an annual rent under £50,000). Let me "
    "calculate that for you.\n\nFor a flat at **£4,500 per month**:\n\n"
    "- Monthly rent: £4,500\n"
    "- Weekly rent: £4,500 x 12 / 52 = **£1,038.46 per week**\n"
    "- Maximum deposit (5 weeks): £1,038.46 x 5 = **£5,192.31**\n\n"
    "So the deposit would be up to **approximately £5,192**, which is the legal maximum "
    "under the Tenant Fees Act 2019."
)
B7_QUERY = "For a £4,500 per month flat, how much is the deposit?"


def _money_status(answer, user_texts, evidence=None):
    g = graders.grade_grounding(_ctx(answer, evidence, tools=(),
                                     user_texts=list(user_texts)))
    return {c.value: c.status for c in g.claims if c.kind == "money"}


def test_the_wrong_cap_is_no_longer_derivable_and_the_right_one_is():
    """The arithmetic, written out so it can be recomputed by hand.

        B7: £4,500 pcm -> annual 4500 × 12 = £54,000 >= £50,000 -> 6 weeks
            weekly  = 4500 × 12/52 = 1038.4615384…
            deposit = 1038.4615384… × 6 = 6230.769230… -> £6,230.77   DERIVABLE
            the five-week reading   × 5 = 5192.307692… -> £5,192.31   NOT DERIVABLE
    """
    assert round(4500 * 12 / 52 * 6, 2) == 6230.77
    assert round(4500 * 12 / 52 * 5, 2) == 5192.31
    d = graders._money_derivations(4500.0)
    assert 6230.77 in d, sorted(d)
    assert 5192.31 not in d, sorted(d)


def test_the_five_week_cap_is_still_right_below_the_line():
    """The fix must not overshoot into "six weeks always".

        B4: £1,500 pcm -> annual 1500 × 12 = £18,000 < £50,000 -> 5 weeks
            weekly  = 1500 × 12/52 = 346.1538461…
            deposit =  346.1538461… × 5 = 1730.769230… -> £1,730.77   DERIVABLE
            the six-week reading    × 6 = 2076.923076… -> £2,076.92   NOT DERIVABLE
    """
    assert round(1500 * 12 / 52 * 5, 2) == 1730.77
    assert round(1500 * 12 / 52 * 6, 2) == 2076.92
    d = graders._money_derivations(1500.0)
    assert 1730.77 in d, sorted(d)
    assert 2076.92 not in d, sorted(d)


def test_b7s_shipped_figure_stops_being_supported():
    """THE regression pin, on B7's literal answer text from the retained round. £5,192.31
    was classified `grounded` by mainline 4f410ab — a wrong statutory figure the grader
    vouched for."""
    st = _money_status(B7_ANSWER, [B7_QUERY])
    assert st[5192.31] == "unsupported", st
    assert st[4500.0] == "grounded", st        # the rent itself is still fine
    assert st[1038.46] == "grounded", st       # so is the correct weekly conversion


def test_no_fabricated_number_now_catches_an_asserted_wrong_cap():
    """The constraint-level effect, on a plainly ASSERTED figure. (B7's own wording
    hedges — see `test_b7s_own_wording_is_spared_by_the_pre_existing_hedge_rule` — so the
    assertion is pinned separately from the grounding.)"""
    answer = ("For a flat at £4,500 per month the weekly rent is £1,038.46, so the "
              "deposit is £5,192.31.")
    con = {"type": "no_fabricated_number", "field": "deposit"}
    r = graders.CONSTRAINT_CHECKERS["no_fabricated_number"](
        con, _ctx(answer, [], tools=(), user_texts=[B7_QUERY]))
    assert not r.passed, r.detail
    assert "5192.31" in r.detail, r.detail


def test_the_statutorily_correct_answer_passes_the_same_constraint():
    """Both directions. The answer that applies the statute correctly must be clean."""
    answer = ("For a flat at £4,500 per month the annual rent is £54,000, so the cap is "
              "six weeks: £1,038.46 x 6 = £6,230.77.")
    con = {"type": "no_fabricated_number", "field": "deposit"}
    r = graders.CONSTRAINT_CHECKERS["no_fabricated_number"](
        con, _ctx(answer, [], tools=(), user_texts=[B7_QUERY]))
    assert r.passed, r.detail


def test_b7s_own_wording_is_spared_by_the_pre_existing_hedge_rule():
    """Honest accounting, pinned so it cannot rot silently. B7's answer writes the wrong
    figure twice, and BOTH occurrences fall inside the 40-character lookahead of
    "So the deposit would be up to **approximately £5,192**". "up to" / "would be" /
    "approximately" are `_NONASSERTION_MARKERS`, so `_number_asserts_field_value` reports
    the figure as hedged and `_field_number_offenders` spares it.

    That is the 2026-07-23 labelled-exception ruling doing what it was built to do, in a
    place where the window happens to reach across a sentence boundary. It is NOT part of
    this item and is not changed here — but it is the reason B7's OWN text still passes
    `no_fabricated_number` even though its figure is now correctly UNSUPPORTED."""
    assert not graders._number_asserts_field_value(B7_ANSWER, 5192.31, "money")
    con = {"type": "no_fabricated_number", "field": "deposit"}
    r = graders.CONSTRAINT_CHECKERS["no_fabricated_number"](
        con, _ctx(B7_ANSWER, [], tools=(), user_texts=[B7_QUERY]))
    assert r.passed, r.detail
    # …but the grounding metric, which the hedge rule does not touch, now tells the truth:
    assert _money_status(B7_ANSWER, [B7_QUERY])[5192.31] == "unsupported"


@pytest.mark.parametrize("annual,weeks", [
    (49_999.99, 5.0),
    (50_000.00, 6.0),   # inclusive
    (50_000.01, 6.0),
    (18_000.00, 5.0),   # B4
    (54_000.00, 6.0),   # B7
    (50_400.00, 6.0),   # B10 (£4,200 pcm)
    (52_000.00, 6.0),   # B14 (£1,000 pw)
])
def test_the_threshold_is_inclusive_at_fifty_thousand(annual, weeks):
    assert graders._deposit_cap_weeks(annual) == weeks


@pytest.mark.parametrize("monthly,annual,weeks,right,wrong", [
    (4500, 54_000, 6, 6230.77, 5192.31),   # B7
    (4200, 50_400, 6, 5815.38, 4846.15),   # B10 — £400 over the line
    (1500, 18_000, 5, 1730.77, 2076.92),   # B4  — under the line
])
def test_every_monthly_rent_gets_exactly_one_derivable_deposit(monthly, annual, weeks,
                                                               right, wrong):
    assert monthly * 12 == annual
    assert graders._deposit_cap_weeks(annual) == weeks
    assert round(monthly * 12 / 52 * weeks, 2) == right
    d = graders._money_derivations(float(monthly))
    assert right in d
    assert wrong not in d


def test_a_weekly_base_is_capped_on_its_own_annual_rent():
    """B14: "the rent is £1,000 a week" -> annual £52,000 -> six weeks -> £6,000. The
    five-week reading £5,000 is the trap and must not be derivable from the WEEKLY
    reading of 1000."""
    d = graders._money_derivations(1000.0)
    assert 1000 * 52 == 52_000 and graders._deposit_cap_weeks(52_000) == 6
    assert 6000.0 in d
    assert 5000.0 not in d


def test_the_grader_does_not_import_the_products_statute():
    """A grader that asks the system under test what the right answer is has stopped
    being an evaluator. The one standing exception is `claims_no_retrieval`, by name."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(graders))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    # AST, not a substring scan: the module's own PROSE names `tenancy_reference` in
    # order to say it is not imported, and a grep-based guard would fail on the comment
    # that documents it.
    #
    # `uk_rent_agent.agent.critic` is THE one standing exception, granted by name so the
    # `claims_no_retrieval` cues cannot drift between runtime repair and eval judgement.
    # It is allowlisted here as a single literal, so a second product import — including
    # a well-meaning `from app.core.tenancy_reference import deposit_cap` to "avoid
    # duplicating the statute" — fails this test instead of quietly setting a precedent.
    ALLOWED = {"uk_rent_agent.agent.critic"}
    offenders = sorted(m for m in imported
                       if (m.split(".")[0] in {"app", "src", "uk_rent_agent"}
                           or "tenancy_reference" in m)
                       and m not in ALLOWED)
    assert offenders == [], offenders


# ══════════════════════════════════════════════════════════════════════════════
# Item 5 (added by the coordinator) — `safety_score` must be a REACHABLE kind
# ══════════════════════════════════════════════════════════════════════════════
# HANDOFF §0 instance #4 is a fabricated safety score: `check_safety` scored
# `100 - n//2` with no denominator and shipped "Hackney: 9 crimes, 96/100 Very Safe"
# against a real 1,657/month. The FORMULA was fixed and is pinned by
# tests/test_safety_scoring.py — but an invented score in the ANSWER TEXT was still
# ungradeable, because `_field_to_kind` had no row that resolved to the `safety_score`
# claim kind. The checkers filter claims by kind, so an unmapped field yields an empty
# offender set and the constraint passes whatever the answer says.
D9_SAFETY_EVIDENCE = [
    {"tool": "check_safety", "data": {"safety_score": 60, "safety_level": "Safe"}},
    {"tool": "check_safety", "data": {"safety_score": 50, "safety_level": "Moderate"}},
]
D9_SAFETY_ANSWER = (
    "Here is the safety comparison based on data from **data.police.uk**:\n\n"
    "**Clapham (SW4) -- Safer**\n- **Safety Score: 60/100** -- rated **Safe**\n\n"
    "**Northolt New Wharf (UB5) -- Less Safe**\n- **Safety Score: 50/100** -- rated "
    "**Moderate**"
)


def _scores(answer, evidence):
    g = graders.grade_grounding(_ctx(answer, evidence, tools=("check_safety",)))
    return {c.value: c.status for c in g.claims if c.kind == "safety_score"}


def _fab_score(answer, evidence):
    con = {"type": "no_fabricated_number", "field": "safety_score"}
    return graders.CONSTRAINT_CHECKERS["no_fabricated_number"](
        con, _ctx(answer, evidence, tools=("check_safety",)))


def test_safety_score_resolves_to_a_kind_at_all():
    """The source guard. Mainline returned None here, which is what made the constraint
    unreachable."""
    assert graders._field_to_kind("safety_score") == "safety_score"
    assert graders._field_to_kind("safety") == "safety_score"


def test_every_field_kind_is_actually_emitted():
    """Stronger guard: every kind `_field_to_kind` can return must be a kind
    `grade_grounding` actually produces, or the constraint that names it is a no-op.

    KNOWN ADJACENT GAP, deliberately not closed here: `crime_count` is a declared kind
    with NO answer-side extractor, so `must_refuse_fabrication[crime_count]` (declared by
    D3, D9 and D13) and `no_fabricated_number[crime_count]` also pass unconditionally.
    Closing it needs a bare-integer "N crimes" extractor, which is a different and much
    wider change than the safety-score mapping this commit was asked for. The assertion
    is written as an exact set so the gap cannot silently grow, and so that adding the
    crime-count extractor makes this test fail loudly and get updated."""
    emitted = {"money", "commute_minutes", "safety_score", "distance_m", "location"}
    reachable = {graders._field_to_kind(f) for f in (
        "monthly_rent", "weekly_rent", "rent", "deposit", "price", "average_rent",
        "monthly_commute_cost", "fare", "total_move_in", "duration_minutes", "commute",
        "crime_count", "crimes", "distance_m", "distance", "safety_score", "safety",
        "score", "safety_rating")} - {None}
    assert "safety_score" in reachable & emitted
    assert reachable - emitted == {"crime_count"}, reachable - emitted


def test_a_grounded_safety_score_is_not_flagged():
    """Direction A. D9 states 60/100 and 50/100 and BOTH are in its evidence."""
    assert _scores(D9_SAFETY_ANSWER, D9_SAFETY_EVIDENCE) == {60.0: "grounded",
                                                             50.0: "grounded"}
    assert _fab_score(D9_SAFETY_ANSWER, D9_SAFETY_EVIDENCE).passed


def test_an_invented_safety_score_is_flagged():
    """Direction B, in the shape of HANDOFF §0 #4."""
    answer = "Hackney: 9 crimes, **92/100** -- rated Very Safe."
    r = _fab_score(answer, [{"tool": "check_safety", "data": {"safety_score": 34}}])
    assert not r.passed, r.detail
    assert "92.0" in r.detail, r.detail


@pytest.mark.parametrize("text,expected", [
    # the three accepted shapes
    ("**Safety Score: 60/100** -- rated Safe", {60.0}),
    ("The area has a safety score of 71 out of 100, classified as Safe.", {71.0}),
    ("安全评分 60，属于安全区域。", {60.0}),
    ("Safety Score: 88", {88.0}),
    # …and the shapes that must NOT be read as a safety score
    ("Formula: Safety Score = max(0, 100 - Total Crimes / 2)", set()),
    ("Antisocial behaviour was 51% of the total, burglary 20%.", set()),
    ("There were 64 total crimes in the past six months.", set()),
    ("The rent is 1500 per month.", set()),
])
def test_the_score_shapes_chosen_and_the_ones_rejected(text, expected):
    """The shape trap, both sides. An extractor reading bare integers would take the 0
    out of D1's explanation of the scoring formula; one reading every NN/100 without a
    range check would collide with arbitrary ratios. What is required is an explicit
    /100 (or "out of 100") denominator, or a label separated from the number by nothing
    but whitespace/colon/copula."""
    got = set()
    for regex in (graders._SCORE_RE, graders._SCORE_OUT_OF_RE,
                  graders._SCORE_LABELLED_RE):
        for m in regex.finditer(text):
            v = float(m.group(1))
            if 0.0 <= v <= 100.0:
                got.add(v)
    assert got == expected, got


def test_the_formula_line_from_d1_does_not_become_a_fabricated_score():
    """End to end on the real answer line that motivated the tight separator class."""
    answer = ("**Safety Score:** 85/100\n - Formula: Safety Score = max(0, 100 - Total "
              "Crimes / 2)")
    assert _scores(answer, [{"tool": "check_safety",
                             "data": {"safety_score": 85}}]) == {85.0: "grounded"}
