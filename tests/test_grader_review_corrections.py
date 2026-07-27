"""Corrections for an adversarial review of the constraint-checker fix that shipped in
51aa513 / 79e7daa / 4edf3b4 / c465586.

Each section pins the WRONG behaviour first (the literal figure the mainline grader
accepted) and the right behaviour second, so neither the fix nor a later revert can be
read as a preference. Every assertion fails against
`git show 4f410ab13a26:evaluation/metrics/graders.py`.

  1. `must_refuse_fabrication[distance_m]` only ever read METRES, so D5's invented
     "3-4 miles" / "10 miles" against a 300 m tool radius was not a distance claim at
     all and D5 became a full pass.
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
