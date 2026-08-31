# -*- coding: utf-8 -*-
"""The calibration reaches LISTING SELECTION, and this is the seam where that is pinned.

PR #53 made ``maps_service.calculate_travel_time`` a thin view over the basis-aware producer,
so on the straight-line branch it returns ``commute_basis.best_estimate_minutes`` — the
CALIBRATED figure inside the fitted domain — instead of the raw formula. That function is not
only a disclosure path: ``search_properties``' step-4 annotation stage calls it per candidate
and then applies ``travel_time <= max_commute_time``. So a calibration fix moved which
properties a user is SHOWN.

tests/test_commute_calibration.py already pins the model, the derived 11-minute floor, the 1.5x
residual gate and the annotate-vs-quote agreement of ``commute.coord_commute_minutes``.
tests/test_tool_layer_budget_and_basis.py already pins that ``calculate_travel_time`` returns
the same figure the two commute tools quote. None of them exercises the FILTER: nothing asserted
that the cap is compared against the calibrated figure, in which direction selection moved, or
where the two models cross over. That is what this file is for.

WHAT FAILS ON THE OLD BEHAVIOUR
    ``test_a_thirty_minute_request_no_longer_keeps_a_listing_the_fit_puts_at_thirty_two``
    runs the real search twice over the same two listings: once with the pre-#53 producer
    (the raw formula, reconstructed from ``legacy_straight_line_minutes`` — that IS what
    ``calculate_travel_time`` returned) and once with the shipped one. The old producer keeps
    the 4.8 km listing and annotates it "28 min"; the fitted model puts the same pair at 32.
    The new producer drops it. Both halves are asserted, so the test states the change rather
    than only the end state.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import types

# --- Pin the real source roots ahead of tests/ (stale shadow copies of `core` live there).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from uk_rent_agent.domain import constants as C
from core import commute_basis as cb
from core import maps_service as ms
from core.scraping import on_demand
import core.tools.search_properties as sp


# Straight north of the destination, so the haversine distance is exactly `km`.
_DEST = (51.5000, -0.1000)
_DEG_PER_KM = 1.0 / 111.19492664455873

# The two probe listings. 3.0 km is inside the cap under BOTH models; 4.8 km is the case that
# moved — raw 28, fitted 32, against a 30-minute request.
_NEAR_KM = 3.0
_FAR_KM = 4.8


def _lat(km: float) -> float:
    return _DEST[0] + km * _DEG_PER_KM


def _row(km: float, name: str, price: int) -> dict:
    return {
        "Address": f"{name}, London",
        "Price": f"£{price} pcm",
        "Room_Type_Category": "1 bed flat",
        "URL": f"https://www.onthemarket.com/details/{int(km * 100)}/",
        "geo_location": f"{_lat(km)},-0.1",
        "Images": [],
        "Description": f"{name} — a flat.",
        "Detailed_Amenities": "",
        "Available From": "",
    }


class _Store:
    """The embedding store's two used methods. `rows` is bound per-test."""

    def __init__(self, rows):
        self._rows = rows

    def build_index(self, rows):        # noqa: D401 - shape only
        return None

    def search(self, query, top_k=10):
        return [dict(r) for r in self._rows]


class _Coordinator:
    def __init__(self, rows):
        self.property_store = _Store(rows)
        self._rows = rows

    def enhanced_search(self, query, criteria):
        return ([dict(r) for r in self._rows], {}, {})


@pytest.fixture
def offline_search(monkeypatch):
    """The real ``search_properties_impl`` with every network edge closed.

    Deliberately NOT stubbing ``calculate_travel_time``: the whole point is that the real
    producer chain (``calculate_travel_basis`` -> ``describe_estimate`` /
    ``best_estimate_minutes``) is what the filter now reads. Only the geocoder and TfL are
    faked, and TfL is faked to "no journey" because that is the branch the estimator serves.
    """
    # ``run`` installs a per-test coordinator below.  Preserve whatever coordinator the
    # process had before this fixture so randomized module order cannot leak our special
    # store (whose ``build_index`` is intentionally a no-op) into later search tests.
    saved_coordinator = sp._RAG_COORDINATOR

    monkeypatch.setenv("DESC_ENRICH_ENABLED", "0")
    monkeypatch.setenv("AREA_RECOS_ENABLED", "0")
    monkeypatch.setenv("SEARCH_GEO_VALIDATION_ENABLED", "0")
    monkeypatch.setenv("RANKER_V2_ENABLED", "0")

    monkeypatch.setattr(on_demand, "classify_place",
                        lambda n: {"kind": "area", "slug": (n or "").lower(),
                                   "city": "london", "address": None})
    monkeypatch.setattr(on_demand, "is_destination", lambda k: False, raising=False)

    monkeypatch.setattr(ms, "get_from_cache", lambda k: None)
    monkeypatch.setattr(ms, "set_to_cache", lambda k, v: None)
    monkeypatch.setattr(ms, "_tfl_journey", lambda o, d, mode="transit": None)
    monkeypatch.setattr(ms, "_tfl_travel_time", lambda o, d, mode="transit": None)

    named = {}

    def coords(addr):
        a = (addr or "").lower()
        if "workplace" in a:
            return {"lat": _DEST[0], "lng": _DEST[1]}
        for key, km in named.items():
            if key in a:
                return {"lat": _lat(km), "lng": _DEST[1]}
        return None

    # ``search_properties`` imports ``geocode_address`` inside the coroutine, so patching the
    # producer module is what reaches it; there is no module-level name to patch.
    monkeypatch.setattr(ms, "_get_coordinates", coords)
    monkeypatch.setattr(ms, "geocode_address", coords)

    def run(rows, name_to_km, **kwargs):
        named.clear()
        named.update(name_to_km)
        monkeypatch.setattr(
            on_demand, "get_listings",
            lambda location, *a, **k: {
                "rows": [dict(r) for r in rows],
                "meta": {"requested_city": "london", "stale": False,
                         "source": "hit" if k.get("cache_only") else "scraped",
                         "count": len(rows), "timed_out": False}})
        sp.set_rag_coordinator(_Coordinator(rows))
        params = dict(area="Camden", areas=["Camden"], confirmed=True, bedrooms=1,
                      reply_language="en", commute_destination="Workplace")
        params.update(kwargs)
        return asyncio.run(sp.search_properties_impl(**params))

    try:
        yield run
    finally:
        sp.set_rag_coordinator(saved_coordinator)


def _surfaced(result) -> dict:
    """address -> travel_time string, over every listing list the payload surfaces."""
    out = {}
    for value in result.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "price" in value[0]:
            for prop in value:
                out[prop.get("address")] = prop.get("travel_time")
    return out


def _surfaced_minutes(result) -> dict:
    """address -> the integer minutes on the card, ignoring how the figure is LABELLED.

    Selection and one-number consistency are about the figure. The label around it carries
    the figure's basis and is pinned separately by
    ``test_an_unrouted_figure_is_surfaced_as_an_estimate_not_as_a_measured_time`` — these
    pairs have no TfL journey, so every figure here is an estimate and says so.
    """
    out = {}
    for address, text in _surfaced(result).items():
        match = re.search(r"(\d+)\s*min", str(text or ""))
        out[address] = int(match.group(1)) if match else None
    return out


def _raw_producer(origin_address, destination_address, mode="transit"):
    """``calculate_travel_time`` as it behaved BEFORE PR #53: the raw straight-line formula.

    Reconstructed from the mirror that ``test_the_legacy_mirror_matches_maps_service`` already
    cross-checks against ``maps_service.straight_line_travel_estimate``, so this control is not
    a hand-written approximation of the old code — it is the same arithmetic.
    """
    o = ms._get_coordinates(origin_address)
    d = ms._get_coordinates(destination_address)
    if not o or not d:
        return None
    from core.commute import straight_line_km
    return cb.legacy_straight_line_minutes(round(straight_line_km(o, d), 2), mode)


# =========================================================================== #
# 1. The filter figure IS the calibrated figure — the selection change itself  #
# =========================================================================== #

def test_a_thirty_minute_request_no_longer_keeps_a_listing_the_fit_puts_at_thirty_two(
        offline_search, monkeypatch):
    """THE selection change, both sides of it, on the real search path.

    4.8 km with no TfL journey. The raw straight-line formula reads 28 minutes; the model
    fitted to 14 measured TfL journeys puts the same pair at 32. The user asked for 30.

    FAILS BEFORE: with the pre-#53 producer the listing is SURFACED and annotated
    "28 min to Workplace" — inside a 30-minute request, for a journey the only measurement
    anyone has says is 32. Nothing in the payload says the 28 was a straight-line guess,
    because a bare thresholding figure carries no basis.
    """
    rows = [_row(_NEAR_KM, "Near St", 1200), _row(_FAR_KM, "Far St", 1200)]
    names = {"near st": _NEAR_KM, "far st": _FAR_KM}

    # The two models on this pair, stated so the assertions below are about numbers and not
    # about which function got called.
    assert cb.legacy_straight_line_minutes(_FAR_KM, "transit") == 28
    assert cb.best_estimate_minutes(_FAR_KM, "transit") == 32
    assert cb.legacy_straight_line_minutes(_NEAR_KM, "transit") == 17
    assert cb.best_estimate_minutes(_NEAR_KM, "transit") == 25

    # --- OLD behaviour: the raw formula decided selection -------------------
    # Swap only the producer, and put the real one back by name rather than with
    # monkeypatch.undo(), which would also unwind the fixture's offline patches.
    shipped_producer = ms.calculate_travel_time
    monkeypatch.setattr(ms, "calculate_travel_time", _raw_producer)
    old = _surfaced_minutes(offline_search(rows, names, max_budget=3000, max_commute_time=30))
    assert old == {"Near St, London": 17, "Far St, London": 28}, (
        "control failed: the pre-#53 producer must keep the 4.8 km listing at 28 minutes, "
        "otherwise this test is not measuring the change it claims to")

    # --- SHIPPED behaviour: the calibrated figure decides selection ---------
    monkeypatch.setattr(ms, "calculate_travel_time", shipped_producer)
    new = _surfaced_minutes(offline_search(rows, names, max_budget=3000, max_commute_time=30))
    assert new == {"Near St, London": 25}, (
        "a 30-minute request must not surface a listing the fitted model puts at 32 minutes")


def test_the_figure_the_filter_used_is_the_figure_the_listing_is_annotated_with(offline_search):
    """One model, one number, all the way to the card.

    The listing that survives carries the SAME minutes the cap was compared against. Two
    figures for one pair inside one search is the defect ``commute_basis`` exists to end, and
    the filter is the half of it that a reader cannot see.
    """
    rows = [_row(_NEAR_KM, "Near St", 1200)]
    surfaced = _surfaced_minutes(offline_search(rows, {"near st": _NEAR_KM},
                                                max_budget=3000, max_commute_time=30))
    expected = cb.best_estimate_minutes(_NEAR_KM, "transit")
    assert surfaced == {"Near St, London": expected}
    assert expected == 25
    # And it is the published figure too, not a third number.
    quoted = cb.describe_estimate(
        cb.legacy_straight_line_minutes(_NEAR_KM, "transit"), _NEAR_KM, mode="transit")
    assert quoted["estimated_duration_minutes"] == expected


def test_a_loosening_pair_is_admitted_too_so_the_change_is_not_only_a_tightening(offline_search):
    """Past the crossover the fit runs BELOW the raw formula, so selection widens there.

    12 km reads 56 minutes raw and 52 fitted. Under a 55-minute cap the old producer excluded
    the listing and the new one admits it. Reporting only the exclusions would misprice the
    change: it is a two-sided shift, not a blanket tightening.
    """
    assert cb.legacy_straight_line_minutes(12.0, "transit") == 56
    assert cb.best_estimate_minutes(12.0, "transit") == 52

    rows = [_row(12.0, "Distant Rd", 1200)]
    surfaced = _surfaced_minutes(offline_search(rows, {"distant rd": 12.0},
                                                max_budget=3000, max_commute_time=55))
    assert surfaced == {"Distant Rd, London": 52}


def test_an_unrouted_figure_is_surfaced_as_an_estimate_not_as_a_measured_time(offline_search):
    """The label has to carry the basis, because this pair HAS no measured journey.

    ``calculate_travel_time`` answers these pairs from its own straight-line fallback (TfL
    returns no journey), and the annotation stage used to stamp every non-null return
    "TfL transit: N min" — republishing a haversine guess as a measured itinerary on the card
    and in the model channel. This is the assertion that keeps the number and its basis
    together on the way out, the way ``commute_basis`` already keeps them together inside.
    """
    rows = [_row(_NEAR_KM, "Near St", 1200)]
    result = offline_search(rows, {"near st": _NEAR_KM}, max_budget=3000, max_commute_time=30)
    listing = result["recommendations"][0]

    assert listing["travel_time_source"] == "estimate"
    assert listing["travel_time"] == "~25 min to Workplace (estimated, unverified)"
    assert "TfL" not in listing["travel_time"]
    assert "TfL" not in listing["explanation"]


# =========================================================================== #
# 2. Direction and crossover — measured, so a refit that moves them is visible #
# =========================================================================== #

def test_the_selection_change_is_a_tightening_below_the_crossover_and_a_loosening_above():
    """WHERE the filter moved, and by how much, over the whole fitted domain.

    The fitted curve is above the raw formula on short trips (up to +10 minutes at 1-2 km) and
    below it on long ones (-13 minutes at 16.71 km). The integer crossover is 8.5 km. Every
    listing closer than that can only LOSE eligibility; every listing further can only gain it.
    Pinned as the property plus the crossing point, so a re-fit cannot silently invert the
    blast radius of this change.
    """
    def delta(km):
        return (cb.best_estimate_minutes(km, "transit")
                - cb.legacy_straight_line_minutes(km, "transit"))

    sweep = [round(cb.CALIBRATED_MIN_KM + 0.01 * i, 2) for i in range(0, 1700)]
    sweep = [k for k in sweep if k <= cb.CALIBRATED_MAX_KM]

    # Both figures are integer-rounded, so the sign near the crossing wobbles between 0 and
    # +-1. The property that matters is one-sidedness on each side of it, and the crossing is
    # located rather than assumed: 8.71 km is the last distance the fit runs HIGH, 8.98 the
    # first it runs LOW, and the unrounded curves cross at 8.72.
    assert [k for k in sweep if delta(k) > 0][-1] == 8.71
    assert [k for k in sweep if delta(k) < 0][0] == 8.98
    assert all(delta(k) >= 0 for k in sweep if k <= 8.71), "the fit must not loosen a short cap"
    assert all(delta(k) <= 0 for k in sweep if k >= 8.72), "the fit must not tighten a long cap"

    assert max(delta(k) for k in sweep) == 10            # 1-2 km, the worst short-range case
    assert min(delta(k) for k in sweep) == -13           # 16.71 km, the deliberate step

    # Outside the fitted domain nothing changed at all: no calibration, no selection change.
    for km in (0.10, 0.30, 0.46, 16.72, 20.0, 40.0):
        assert delta(km) == 0, km


def test_below_the_shortest_measured_pair_selection_is_unchanged_even_though_the_quote_is_refused():
    """The asymmetry that surprises people, pinned rather than left as a docstring.

    Under 0.47 km ``describe_estimate`` REFUSES to publish a figure, but
    ``best_estimate_minutes`` falls back to the raw formula rather than returning None — on
    purpose: a filter that dropped a listing because the honest answer was "no number" would
    be a silent, invisible failure. So sub-domain pairs are disclosed differently and selected
    IDENTICALLY, and that is a decision, not an oversight.
    """
    for km in (0.10, 0.30, 0.46):
        raw = cb.legacy_straight_line_minutes(km, "transit")
        assert cb.best_estimate_minutes(km, "transit") == raw, km
        published = cb.describe_estimate(raw, km, mode="transit")
        assert published["estimated_duration_minutes"] is None, km
        assert published["estimate_model"] is None, km


def test_only_transit_selection_moved_because_only_transit_was_measured():
    """The filter figure is produced with a mode, and the calibration is fitted on TfL's
    fastest itineraries only. A cycling or driving threshold must read exactly what it read
    before, at every distance in the fitted domain."""
    for mode in ("bicycling", "cycling-regular", "walking", "foot-walking", "driving"):
        assert mode not in cb.CALIBRATED_MODES
        for km in (0.5, 1.0, 4.8, 8.5, 16.71):
            assert (cb.best_estimate_minutes(km, mode)
                    == cb.legacy_straight_line_minutes(km, mode)), (mode, km)


def test_the_selection_change_is_switched_off_with_the_residual_gate_not_separately():
    """``best_estimate_minutes`` consults ``CALIBRATION_MEETS_GATE``, so if a future edit to the
    sample or the constants pushes the worst residual past 1.5x, listing SELECTION reverts to
    the raw formula in the same breath as the published figure. One switch, not two."""
    assert cb.CALIBRATION_MEETS_GATE is True
    assert cb.CALIBRATION_WORST_ERROR <= cb.MAX_ACCEPTED_RESIDUAL_RATIO

    import unittest.mock as mock
    with mock.patch.object(cb, "CALIBRATION_MEETS_GATE", False):
        for km in (0.5, 4.8, 16.71):
            assert (cb.best_estimate_minutes(km, "transit")
                    == cb.legacy_straight_line_minutes(km, "transit")), km


# =========================================================================== #
# 3. Measured blast radius on the round of record (8793c0b, both arms)         #
# =========================================================================== #

# Every commute figure the 98-case round of record actually attached to a LISTING the user was
# shown, with the listing's recorded ``geo_location`` and the cap that was in force. Read out of
# the retained ``grader_input.jsonl`` of
# .runtime/round-8793c0b-internal-2026-07-25/eval/{sweep,sweep-legacy}. That round predates
# ``commute_basis`` entirely (8793c0b has no such module), so these ARE the pre-calibration
# figures — the "before" of this change, measured on live traffic rather than reconstructed.
#
# Both arms annotate against UCL Gower Street (fc passes the token "UCL", legacy the resolved
# "Gower Street, London WC1E 6BT"), whose reference coordinate is the one
# scripts/sample_commute_calibration.py uses for the same destination. Having the origin
# coordinate is what makes measured-vs-estimate DECIDABLE instead of assumed: the raw formula's
# value for the recorded pair is computable, and TfL runs 1.8x-6x above it at short range.
#
# `cap` is None where the search ran with no real ``max_commute_time`` (annotate only) and
# NO_COMMUTE_LIMIT is legacy's no-limit sentinel.
_UCL_GOWER = {"lat": 51.5246, "lng": -0.1340}

ROUND_OF_RECORD_LISTINGS = (
    # arm      case   recorded  cap                    recorded geo_location
    ("fc",     "A2",         1, None,                  "51.525628, -0.130007"),   # Tavistock Ct
    ("fc",     "A12",        1, None,                  "51.526164, -0.129896"),   # Upper Woburn Pl
    ("fc",     "A12",        1, None,                  "51.525628, -0.130007"),   # Tavistock Ct
    ("fc",     "A12",        1, None,                  "51.52329, -0.136272"),    # University St
    ("fc",     "A12",        2, None,                  "51.526694, -0.127596"),   # Cartwright Gdns
    ("fc",     "A12",        6, None,                  "51.52321, -0.115965"),    # Doughty St
    ("fc",     "G7",        21, None,                  "51.530822, -0.121558"),   # Pentonville Rd
    ("fc",     "E1",         5, 40,                    "51.522656, -0.118046"),   # Rugby House
    ("fc",     "E1",        21, 40,                    "51.530822, -0.121558"),   # Pentonville Rd
    ("fc",     "E1",        37, 40,                    "51.5728, -0.111018"),     # Lancaster Rd
    ("fc",     "E1",        39, 40,                    "51.566702, -0.122377"),   # Marlborough Rd
    ("legacy", "A2",         1, C.NO_COMMUTE_LIMIT,    "51.525628, -0.130007"),   # Tavistock Ct
    ("legacy", "E1",        28, 40,                    "51.560516, -0.094972"),   # Riversdale Rd
)

# The AREA figures of the same round. ``recommend_areas`` drops a candidate whose commute
# exceeds the cap, so these gate AREA selection exactly as the rows above gate listings — but
# the destination centroid was NOT recorded beside them (E2 -> King's College London,
# E5/E10 -> Imperial College London), so measured-vs-estimate is not decidable here. They are
# therefore held to the WORST CASE: assume every one came from the raw formula and invert it.
ROUND_OF_RECORD_AREAS = (
    ("fc", "E2",  23, 30),    # area_recommendations: Holborn
    ("fc", "E5",   8, 35),    # area_recommendations: South Kensington
    ("fc", "E5",  19, 35),    # area_recommendations: Earl's Court
    ("fc", "E10", 12, 60),    # compare_or_rank_areas, AREA_RECO_DEFAULT_COMMUTE
    ("fc", "E10", 13, 60),
    ("fc", "E10", 17, 60),
    ("fc", "E10", 26, 60),
    ("fc", "E10", 29, 60),
)

# E6 is deliberately absent from both tables: in BOTH arms it was reached through the
# similar-but-over-budget fallback, which applies SIMILAR_COMMUTE_SLACK rather than the cap
# (37 and 34 minutes were surfaced against a 35-minute request). It gets its own test below.

# The gap between the recorded figure and the raw formula at the recorded coordinate that is
# still attributable to destination-geocode drift rather than to a different producer. The
# destination is a token ("UCL") resolved by a live geocoder, so a few hundred metres of drift
# is expected; the raw formula's steepest slope is 5.9 min/km, so 1 minute covers ~170 m.
_GEOCODE_DRIFT_MINUTES = 1


def _raw_at_recorded_geo(geo: str) -> int:
    """What the raw straight-line transit formula says for the recorded (origin, UCL) pair."""
    from core.commute import straight_line_km
    km = straight_line_km(geo, _UCL_GOWER)
    assert km is not None, geo
    return cb.legacy_straight_line_minutes(round(km, 2), "transit")


def _distance_interval(raw_minutes: int) -> tuple:
    """The distance range the raw transit formula must have been given to emit `raw_minutes`.

    ``int((km * 1.3 / 20) * 60 + min(10, km * 2))`` is ``int(5.9 * km)`` up to 5 km and
    ``int(3.9 * km + 10)`` beyond it, so the inverse is an interval, not a point.
    """
    if raw_minutes < 29:
        return raw_minutes / 5.9, (raw_minutes + 1) / 5.9
    return (raw_minutes - 10) / 3.9, (raw_minutes + 1 - 10) / 3.9


@pytest.mark.parametrize(
    "arm,case,recorded,cap,geo", ROUND_OF_RECORD_LISTINGS,
    ids=[f"{a}-{c}-{m}" for a, c, m, _cap, _g in ROUND_OF_RECORD_LISTINGS])
def test_no_listing_in_the_round_of_record_changes_eligibility_under_the_calibration(
        arm, case, recorded, cap, geo):
    """The measured blast radius: 0 listings change eligibility, in EITHER arm.

    This is the number the owner ruling turns on, so it is asserted rather than reported.

    Each recorded figure is first attributed to a producer, from the evidence and not from a
    guess: a figure within ``_GEOCODE_DRIFT_MINUTES`` of the raw formula at the listing's own
    coordinate came from the straight-line branch (that branch IS the raw formula on 8793c0b);
    a figure well above it came from a real TfL itinerary, which this change does not touch at
    all — ``calculate_travel_time`` still returns ``duration_minutes`` verbatim whenever
    ``is_measured`` recognises the source.

    Then the straight-line ones are re-derived under the shipped model and checked against the
    cap that was in force. A re-fit that would start excluding one of these turns this red.
    """
    raw_here = _raw_at_recorded_geo(geo)
    from_estimator = abs(recorded - raw_here) <= _GEOCODE_DRIFT_MINUTES

    if not from_estimator:
        assert recorded > raw_here + _GEOCODE_DRIFT_MINUTES, (
            f"{arm} {case}: recorded {recorded} is BELOW the raw formula's {raw_here} by more "
            "than geocode drift, so it came from neither producer and the attribution above is "
            "wrong")
        return          # measured journey: the calibration cannot reach it

    lo, hi = _distance_interval(recorded)
    ends = {cb.best_estimate_minutes(round(lo, 2), "transit"),
            cb.best_estimate_minutes(round(hi - 0.005, 2), "transit")}
    assert None not in ends
    if cap is None or cap >= C.NO_COMMUTE_LIMIT:
        return                                  # annotate only: no cap to cross
    assert max(ends) <= cap, (
        f"{arm} {case}: recorded {recorded} min under a {cap} min cap becomes {sorted(ends)} "
        f"under the calibration — this listing would now be excluded, so the blast radius is "
        f"no longer zero and the ruling needs re-measuring")


def test_the_only_round_of_record_figure_that_could_have_flipped_came_from_tfl():
    """The single near-miss, named, because "zero" is only trustworthy with its margin.

    fc E1's over-budget alternative (Marlborough Road N19) was annotated 39 minutes against a
    40-minute cap. If that 39 had come from the estimator it would now read 40-41 and the
    listing would drop out — the one row in the whole 98x2 round with no headroom. It did not:
    the raw formula at its recorded coordinate reads 28, so 39 is 11 minutes above the
    straight-line branch and is a TfL itinerary, which the change leaves alone.

    Stated as a test so that "the blast radius is zero" cannot be quoted without its one
    caveat, and so that a future edit to the attribution rule has to face this row.
    """
    marlborough = "51.566702, -0.122377"
    assert _raw_at_recorded_geo(marlborough) == 28
    assert 39 - 28 > _GEOCODE_DRIFT_MINUTES         # therefore measured, not estimated

    # And the counterfactual, so the near-miss is a number and not an adjective.
    lo, hi = _distance_interval(39)
    counterfactual = {cb.best_estimate_minutes(round(lo, 2), "transit"),
                      cb.best_estimate_minutes(round(hi - 0.005, 2), "transit")}
    assert counterfactual == {40, 41}
    assert max(counterfactual) > 40


@pytest.mark.parametrize(
    "arm,case,recorded,cap", ROUND_OF_RECORD_AREAS,
    ids=[f"{a}-{c}-{m}" for a, c, m, _cap in ROUND_OF_RECORD_AREAS])
def test_no_area_in_the_round_of_record_changes_eligibility_under_the_calibration(
        arm, case, recorded, cap):
    """``recommend_areas._validate_candidate`` drops a candidate whose commute exceeds the cap,
    so the same change moves AREA selection. Held to the worst case (assume every recorded
    figure came from the estimator), and every one still clears its cap with headroom."""
    lo, hi = _distance_interval(recorded)
    ends = {cb.best_estimate_minutes(round(lo, 2), "transit"),
            cb.best_estimate_minutes(round(hi - 0.005, 2), "transit")}
    assert None not in ends
    assert max(ends) <= cap, (
        f"{arm} {case}: area recorded at {recorded} min under a {cap} min cap becomes "
        f"{sorted(ends)} under the calibration — it would now be dropped")


def test_the_round_of_record_never_exercised_the_calibrated_branch_at_all():
    """The honest limit on the measurement above, written down where it cannot be lost.

    Every ``calculate_commute`` / ``calculate_commute_cost`` / ``get_transport_info`` record in
    both retained arms carries ``route_source`` 'tfl' / 'TfL Journey Planner' / 'osm_cycling'.
    Not one carries 'estimate'. So the 98-case corpus never puts a straight-line figure on a
    PUBLISHED commute number, and it cannot validate the disclosure half of this change either
    — only the selection half, through the annotation figures above.

    The consequence, asserted so it is a property and not a claim: the corpus has no coverage
    for the calibrated publication path, so the tests in this file and in
    tests/test_commute_calibration.py are the ONLY evidence for it.
    """
    measured = cb.describe_measured(24, "TfL Journey Planner")
    assert measured["basis"] == cb.BASIS_MEASURED
    assert "duration_minutes" in measured and measured["caveat"] is None
    # A measured record is untouched by the calibration: same field, same number, no model.
    assert "estimate_model" not in measured
    assert cb.is_measured("tfl") and cb.is_measured("TfL Journey Planner")
    assert not cb.is_measured("estimate") and not cb.is_measured(None)


# =========================================================================== #
# 4. The boundary: an over-cap listing may be shown, never shown silently      #
# =========================================================================== #

def test_the_similar_suggestion_fallback_may_exceed_the_cap_but_never_without_saying_so(
        offline_search):
    """The one path that surfaces a listing OVER the user's limit, and its disclosure contract.

    When nothing matches, the RAG fallback keeps a candidate up to
    ``SIMILAR_COMMUTE_SLACK`` x the cap — 1.5x, i.e. a 30-minute request can surface a
    45-minute property. That is a deliberate product decision and it is NOT this change's to
    revert, but the calibration moves which listings land in that window, so the disclosure it
    depends on is pinned here: the payload must state the limit that was not met, and every
    surfaced listing must carry its own figure. A silent over-cap listing is the failure.

    4.8 km = 32 minutes fitted, inside 30 x 1.5. 12 km = 52 minutes, outside it, so the slack
    is a real bound and not "anything goes".
    """
    assert C.SIMILAR_COMMUTE_SLACK == 1.5
    rows = [_row(_FAR_KM, "Far St", 2400), _row(12.0, "Distant Rd", 2500)]
    res = offline_search(rows, {"far st": _FAR_KM, "distant rd": 12.0},
                         max_budget=1200, max_commute_time=30)

    assert res["status"] == "no_exact_match_but_similar"
    # The limit that was NOT met is stated, in the same sentence as the budget that was not met.
    assert "within 30 min of Workplace" in res["message"]

    surfaced = _surfaced_minutes(res)
    assert surfaced == {"Far St, London": 32}, (
        "the over-cap candidate must be admitted WITH its figure, and the 52-minute one must "
        "fall outside the 1.5x slack")
    # ...and the figure states that it is an estimate, since this pair has no TfL journey.
    assert "estimated" in _surfaced(res)["Far St, London"]
    # It is never dressed up as a match.
    for value in res.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "price" in value[0]:
            assert all(p.get("match_type") == "similar_suggestion" for p in value)


def test_chinese_commute_caps_do_not_require_ascii_spacing():
    """CJK adjacency and Chinese units both reach the deterministic cap path."""
    from core.tools.search_properties import _extract_commute_minutes as extract

    assert extract("under 30 minutes") == 30
    assert extract("within 30 min") == 30
    assert extract("通勤 30 min") == 30
    assert extract("通勤30 min以内") == 30
    assert extract("通勤30分钟以内") == 30
    assert extract("通勤不超过35分钟") == 35
    assert extract("Imperial 35分钟") == 35
