# -*- coding: utf-8 -*-
"""Derived location figures that were computed and then never asserted on.

Three instances of the same defect, each pinned here with the output that was actually
observed rather than with a happy path:

  1. ``calculate_commute`` returned ``route_source`` ('tfl' for a real journey plan,
     'estimate' for a straight-line haversine guess) and NOTHING consumed it -- the grep
     for ``route_source`` matched only the file that produced it, and no prompt mentioned
     it. The model was handed ``{"duration_minutes": 6, "route_source": "estimate"}`` and
     told the user "6 minutes" as a fact. Measured the same day, the real TfL journey for a
     0.47 km central-London pair is 12 minutes; the estimator says 2.

  2. The same WC1H property was reported as nearest to "Covent Garden" in one turn and
     "Russell Square" (correct, 214 m per TfL) in another. No tool ever supplied a nearest
     station, so nothing constrained or checked the model.

  3. POI distances are measured from the geocoded QUERY STRING. Ask "how is Hackney?" and
     "Tesco 110m" means 110 m from a borough centroid, not from any home.

Every test below fails on the pre-fix behaviour.
"""
from __future__ import annotations

import pytest

from core import maps_service as ms
from core.commute_basis import (
    BASIS_MEASURED,
    BASIS_STRAIGHT_LINE,
    CALIBRATED_MODEL_ID,
    CALIBRATION,
    ESTIMATE_RATIO_HIGH,
    ESTIMATE_RATIO_LOW,
    MIN_TRUSTWORTHY_ESTIMATE_MINUTES,
    describe_estimate,
    estimate_band,
    is_measured,
)


# --------------------------------------------------------------------------- #
# 1. A straight-line guess must not occupy the measured-journey field.         #
# --------------------------------------------------------------------------- #

def test_the_production_payload_is_not_passed_through(monkeypatch):
    """THE incident, byte for byte.

    ``{"duration_minutes": 6, "route_source": "estimate"}`` is what reached the model. The
    tool must not re-emit that 6 as ``duration_minutes``, because downstream -- prompt,
    grader, UI -- ``duration_minutes`` means "a journey planner measured this".
    """
    import core.tools.calculate_commute as cc

    monkeypatch.setattr(ms, "calculate_travel_details", lambda f, t, m="transit": {
        "duration_minutes": 6,          # the pre-fix shape: a guess in the measured field
        "route_legs": [],
        "route_summary": "No detailed route available (estimated; outside TfL coverage).",
        "source": "estimate",
    })
    out = cc.calculate_commute_impl("Tavistock Court, WC1H", "UCL, Gower Street")

    assert out["duration_minutes"] is None, (
        "a 'source: estimate' figure must never be returned as duration_minutes -- that is "
        "the field the answer quotes as the commute")
    assert out["route_source"] == "estimate"
    # And the 6 must not survive anywhere as an unqualified number of minutes.
    assert out.get("estimated_duration_minutes") is None
    assert "do NOT state" in out["recommendation"]


def test_calculate_travel_details_separates_the_two_kinds_of_minutes(monkeypatch):
    """The producer, not just the tool: a guess goes to its own field with its own basis.

    INVERTED 2026-07-26. This used to assert ``estimated_duration_minutes == 40``, i.e. that the
    raw formula's figure was passed through untouched — which pinned the uncalibrated estimator
    as intended output. It is 42 now: 8.00 km is inside the fitted domain, so the figure goes
    through t(d) = 3.7 + 11.4 x 8^0.58 = 42.0 minutes. The raw 40 must NOT survive as the
    reported estimate; the structural separation this test is really about is unchanged.
    """
    monkeypatch.setattr(ms, "_get_coordinates", lambda a: {"lat": 51.5, "lng": -0.1})
    monkeypatch.setattr(ms, "_tfl_journey", lambda o, d, mode="transit": None)
    monkeypatch.setattr(ms, "straight_line_travel_estimate",
                        lambda o, d, m="transit": {"minutes": 40, "distance_km": 8.0})

    out = ms.calculate_travel_details("A", "B")

    assert out["duration_minutes"] is None
    assert out["estimated_duration_minutes"] == 42
    assert out["estimate_model"] == CALIBRATED_MODEL_ID
    assert out["basis"] == BASIS_STRAIGHT_LINE
    assert out["straight_line_km"] == 8.0
    assert out["estimate_low_minutes"] and out["estimate_high_minutes"]
    assert out["caveat"] and "not a journey plan" in out["caveat"].lower()


def test_a_measured_journey_still_reports_minutes(monkeypatch):
    """Non-regression: the TfL path is the one case that may fill duration_minutes."""
    monkeypatch.setattr(ms, "_get_coordinates", lambda a: {"lat": 51.5, "lng": -0.1})
    monkeypatch.setattr(ms, "_tfl_journey",
                        lambda o, d, mode="transit": {"duration": 24, "legs": []})

    out = ms.calculate_travel_details("A", "B")

    assert out["duration_minutes"] == 24
    assert out["basis"] == BASIS_MEASURED
    assert out["estimated_duration_minutes"] is None
    assert out["caveat"] is None


def test_route_source_is_actually_consumed(monkeypatch):
    """The defect in one assertion: the source field was computed and then ignored.

    Two calls whose payloads differ ONLY in ``source`` must not produce the same answer.
    Before the fix they produced identical output, which is what made the label decorative.
    """
    import core.tools.calculate_commute as cc

    def payload(source):
        return {"duration_minutes": 24, "route_legs": [], "route_summary": "r",
                "source": source, "estimated_duration_minutes": 24,
                "estimate_low_minutes": 18, "estimate_high_minutes": 35}

    monkeypatch.setattr(ms, "calculate_travel_details", lambda f, t, m="transit": payload("tfl"))
    measured = cc.calculate_commute_impl("A", "B")
    monkeypatch.setattr(ms, "calculate_travel_details",
                        lambda f, t, m="transit": payload("estimate"))
    guessed = cc.calculate_commute_impl("A", "B")

    assert measured["duration_minutes"] == 24
    assert guessed["duration_minutes"] is None
    assert measured != guessed


def test_acceptability_is_not_asserted_from_a_guess(monkeypatch):
    """``is_acceptable = duration <= 45`` on an estimate is a second unbacked claim built
    on the first. A guess supports neither the verdict nor the category."""
    import core.tools.calculate_commute as cc

    monkeypatch.setattr(ms, "calculate_travel_details", lambda f, t, m="transit": {
        "duration_minutes": None, "route_legs": [], "route_summary": "r",
        "source": "estimate", "estimated_duration_minutes": 40,
        "estimate_low_minutes": 30, "estimate_high_minutes": 58,
        "basis": BASIS_STRAIGHT_LINE, "caveat": "c", "straight_line_km": 8.0})
    out = cc.calculate_commute_impl("A", "B")

    assert out["is_acceptable"] is None
    assert out["duration_category"] is None
    assert out["estimated_duration_minutes"] == 40
    assert "estimated" in out["recommendation"].lower()


def test_unlabelled_sources_are_not_promoted_to_measured():
    """Defaulting an unrecognised label to 'measured' is how a guess becomes a fact."""
    assert is_measured("TfL Journey Planner") is True
    assert is_measured("tfl") is True
    for weak in (None, "", "estimate", "straight_line_estimate", "guess", "cache", "unknown"):
        assert is_measured(weak) is False, weak


# --------------------------------------------------------------------------- #
# 2. The short-hop refusal, and the measurement that justifies it.             #
# --------------------------------------------------------------------------- #

def test_the_estimator_really_does_produce_the_flagged_single_digits():
    """Guard the guard. The eval flagged fabricated commute_minutes of 6.0 / 8.0 / 5.0 as
    unsupported. Those are not arbitrary hallucinations -- they are the exact shape of what
    the straight-line estimator emits for a central-London hop, against a measured truth of
    11-12 minutes."""
    rows = {label: (est, tfl) for label, _km, est, tfl in CALIBRATION}
    est, tfl = rows["Tavistock Court WC1H -> UCL Gower Street"]
    assert est == 2 and tfl == 12                     # measured 2026-07-26
    assert tfl / est >= 5, "the estimator is 6x low on this pair, not merely imprecise"
    est2, tfl2 = rows["Woburn Place WC1H -> UCL Gower Street"]
    assert (est2, tfl2) == (2, 11)


def test_short_estimates_are_refused_rather_than_rounded():
    """A 2-minute answer to a 12-minute journey is wrong, not imprecise. Same reasoning as
    safety_reference.MIN_PLAUSIBLE_MONTHLY: an implausible output means the method failed
    here, not that the journey is quick.

    PARTLY INVERTED 2026-07-26 — and this is the correction that mattered. The loop below used
    to pin "0.47 km yields NO number" as intended, which quietly made the SHORTNESS the defect.
    It was not: the defect was the formula. 2 minutes is exactly what the transit formula
    produces at 0.47 km, so that case is now CALIBRATED to 11 (see
    test_commute_calibration.py). The rest of the loop still refuses, for a different and
    narrower reason: 5/6/8/14 minutes are not what the transit formula produces at 0.47 km, so
    they came from some other estimator and there is nothing measured to correct them with.
    """
    assert describe_estimate(2, 0.47)["estimated_duration_minutes"] == 11

    for tiny in (5, 6, 8, MIN_TRUSTWORTHY_ESTIMATE_MINUTES - 1):
        assert estimate_band(tiny) is None, tiny
        described = describe_estimate(tiny, 0.47)
        assert described["estimated_duration_minutes"] is None, tiny
        assert "duration_minutes" not in described, (
            "the refused case must not reintroduce the measured-journey field")
    assert estimate_band(MIN_TRUSTWORTHY_ESTIMATE_MINUTES) is not None

    # And a distance shorter than anything measured is still refused outright, which is where
    # #28's rule now lives.
    below = describe_estimate(1, 0.20)
    assert below["estimated_duration_minutes"] is None
    assert "duration_minutes" not in below


def test_the_calibration_supports_the_floor_it_is_used_for():
    """The floor and the band are claims about measured error; check they hold in the data
    they were derived from, so a future edit to CALIBRATION cannot silently invalidate
    them."""
    short = [(e, t) for _l, _k, e, t in CALIBRATION if e < MIN_TRUSTWORTHY_ESTIMATE_MINUTES]
    long_ = [(e, t) for _l, _k, e, t in CALIBRATION if e >= MIN_TRUSTWORTHY_ESTIMATE_MINUTES]
    assert short and long_, "the calibration must exercise both sides of the floor"

    # Below the floor the estimator was low on EVERY sampled pair, by a lot.
    assert all(t > e for e, t in short)
    assert min(t / e for e, t in short) >= 1.7

    # At or above it, the measured error is what the reported band claims.
    for e, t in long_:
        assert ESTIMATE_RATIO_LOW <= t / e <= ESTIMATE_RATIO_HIGH, (e, t)
        low, high = estimate_band(e)
        assert low <= t <= high, f"measured {t} fell outside the reported {low}-{high}"


def test_every_estimate_carries_its_basis_and_caveat():
    """INVERTED 2026-07-26: the refused example was ``describe_estimate(2, 0.47)``, which is now
    the flagship CALIBRATED case. Refusal is demonstrated on a distance shorter than any
    measured pair instead, which is where refusal now belongs. Both branches — kept and refused,
    calibrated and uncalibrated — still have to carry a basis and a caveat."""
    kept = describe_estimate(40, 8.0)
    assert kept["basis"] == BASIS_STRAIGHT_LINE
    assert kept["caveat"] and "straight-line" in kept["caveat"].lower()
    assert "not a measured journey time" in kept["basis_note"].lower()

    calibrated = describe_estimate(2, 0.47)
    assert calibrated["basis"] == BASIS_STRAIGHT_LINE
    assert calibrated["caveat"] and "straight-line" in calibrated["caveat"].lower()
    assert "not a measured journey time" in calibrated["basis_note"].lower()

    refused = describe_estimate(1, 0.20)     # closer than the 0.47 km shortest sampled pair
    assert refused["caveat"] and refused["basis"] == BASIS_STRAIGHT_LINE
    assert "no number is given" in refused["basis_note"].lower()


def test_estimate_minutes_stay_visible_to_the_number_grader():
    """The eval's evidence pool keys off field names containing 'duration' or 'minutes'.
    Moving the figure out of duration_minutes must not make a legitimately-quoted estimate
    look fabricated."""
    described = describe_estimate(40, 8.0)
    numeric_keys = [k for k, v in described.items() if isinstance(v, (int, float))]
    for k in ("estimated_duration_minutes", "estimate_low_minutes", "estimate_high_minutes"):
        assert k in numeric_keys
        assert "duration" in k or "minutes" in k


# --------------------------------------------------------------------------- #
# 3. Nearest station: supplied by the data layer, or explicitly not known.     #
# --------------------------------------------------------------------------- #

def _fake_stoppoints(monkeypatch, stops):
    """Patch requests.get to answer TfL's /StopPoint with ``stops``."""
    import requests

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"stopPoints": stops}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())


# TfL StopPoint distances measured 2026-07-26 for 51.5245,-0.1272 (Tavistock Court, WC1H).
_WC1H_STOPS = [
    {"commonName": "Goodge Street Underground Station", "distance": 635.0,
     "modes": ["tube"], "naptanId": "940GZZLUGDG", "stopType": "NaptanMetroStation"},
    {"commonName": "Russell Square Underground Station", "distance": 214.0,
     "modes": ["tube"], "naptanId": "940GZZLURSQ", "stopType": "NaptanMetroStation"},
    {"commonName": "London Euston Rail Station", "distance": 665.0,
     "modes": ["national-rail", "overground"], "naptanId": "910GEUSTON",
     "stopType": "NaptanRailStation"},
]


def test_the_wc1h_property_resolves_to_russell_square(monkeypatch):
    """The incident. "Covent Garden" is ~1.3 km from this point and appears nowhere in the
    repo -- it was invented in the gap where no tool supplied an answer. The data layer now
    answers, from TfL's own index, with the distance attached."""
    from core.place_reference import nearest_stations

    _fake_stoppoints(monkeypatch, _WC1H_STOPS)
    found = nearest_stations(51.5245, -0.1272)

    assert found[0]["name"] == "Russell Square Underground Station"
    assert found[0]["distance_m"] == 214
    assert found[0]["source"] == "TfL StopPoint API"
    assert [s["distance_m"] for s in found] == sorted(s["distance_m"] for s in found)
    assert not any("Covent Garden" in s["name"] for s in found)


def test_nearest_means_nearest_not_metro_first(monkeypatch):
    """Deliberate divergence from get_transport_info._resolve_station, which sorts
    metro-before-rail because it is picking a FARE-CHARGEABLE station. For "which station is
    nearest" a farther Tube station must not beat a nearer rail one."""
    from core.place_reference import nearest_stations

    _fake_stoppoints(monkeypatch, [
        {"commonName": "Far Tube", "distance": 900.0, "modes": ["tube"],
         "naptanId": "a", "stopType": "NaptanMetroStation"},
        {"commonName": "Near Rail", "distance": 150.0, "modes": ["national-rail"],
         "naptanId": "b", "stopType": "NaptanRailStation"},
    ])
    assert nearest_stations(51.5, -0.1)[0]["name"] == "Near Rail"


def test_a_stop_with_no_distance_cannot_be_ranked_nearest(monkeypatch):
    """`s.get('distance') or 0` promotes an unmeasured stop to the front. Dropping it is
    honest; ranking it first invents a proximity that was never returned."""
    from core.place_reference import nearest_stations

    _fake_stoppoints(monkeypatch, [
        {"commonName": "Unmeasured", "distance": None, "modes": ["tube"],
         "naptanId": "a", "stopType": "NaptanMetroStation"},
        {"commonName": "Russell Square Underground Station", "distance": 214.0,
         "modes": ["tube"], "naptanId": "b", "stopType": "NaptanMetroStation"},
    ])
    found = nearest_stations(51.5, -0.1)
    assert [s["name"] for s in found] == ["Russell Square Underground Station"]


def test_no_station_nearby_is_a_stated_fact_not_a_missing_key(monkeypatch):
    from core.place_reference import nearest_station_for_address

    monkeypatch.setattr(ms, "_get_coordinates", lambda a: {"lat": 53.4, "lng": -2.2})
    _fake_stoppoints(monkeypatch, [])
    out = nearest_station_for_address("Fallowfield, Manchester")

    assert out["nearest_station"] is None
    assert "no tube/rail station" in out["note"].lower()
    assert "rather than naming one" in out["note"].lower()


def test_a_failed_lookup_is_not_reported_as_no_station(monkeypatch):
    """'TfL has no station here' and 'we could not ask TfL' license different sentences."""
    import requests
    from core.place_reference import nearest_station_for_address, nearest_stations

    monkeypatch.setattr(ms, "_get_coordinates", lambda a: {"lat": 51.5, "lng": -0.1})

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(requests, "get", boom)
    assert nearest_stations(51.5, -0.1) is None

    out = nearest_station_for_address("Tavistock Court, WC1H")
    assert out["nearest_station"] is None
    assert "could not be checked" in out["note"].lower()


def test_the_station_reaches_the_tool_payload(monkeypatch):
    """Data-layer supply only counts if it survives to what the model reads."""
    import core.tools.search_nearby_pois as sp

    monkeypatch.setattr(sp, "geocode_address", lambda addr, **_kw: (51.5245, -0.1272))
    monkeypatch.setattr(sp, "query_osm_pois", lambda *a, **k: [])
    _fake_stoppoints(monkeypatch, _WC1H_STOPS)

    res = sp.search_nearby_pois_impl(address="Tavistock Court, WC1H", poi_type="tube_station")

    assert res["nearest_station"]["name"] == "Russell Square Underground Station"
    assert res["nearest_station"]["distance_m"] == 214
    assert "Russell Square" in res["note"]


def test_the_no_fabrication_rule_covers_station_names():
    """Backstop, and the record of WHY it was needed.

    AT THE TIME THE DEFECT SHIPPED: the enumerated field list did not include stations, and
    the programmatic critic validated money figures ONLY — so nothing anywhere objected to
    'Covent Garden'.

    NO LONGER TRUE, and corrected here on 2026-07-27. Station-name grounding landed:
    ``uk_rent_agent.agent.critic`` now also runs ``station_name_claims`` /
    ``ungrounded_station_names``, checking a name the answer asserts is a *station* against
    the same evidence surface the prices are checked against (see §4 of that module's
    docstring). This test remains the BACKSTOP for the prompt-side half of that fix; the
    critic-side half is covered by tests/test_critic_price_pool_scoping.py and friends.

    The stale sentence asserted nothing, so nothing failed when it went false — which is
    precisely why it had to be corrected by hand rather than caught.
    """
    from core import loop_prompts

    rules = loop_prompts.behaviour_rules().lower()
    assert "station" in loop_prompts.NO_FABRICATION_RULE.lower()
    assert "never name a nearest tube/rail station" in rules


# --------------------------------------------------------------------------- #
# 4. What the distances are measured FROM.                                     #
# --------------------------------------------------------------------------- #

def test_an_area_query_says_the_origin_is_an_area_centre():
    from core.place_reference import query_reference

    for area in ("Hackney", "Bloomsbury, London", "Camden"):
        ref = query_reference(area)
        assert ref["precision"] == "area", area
        assert ref["is_specific_address"] is False
        assert "centre" in ref["measured_from"].lower()
        assert "not a property address" in ref["measured_from"].lower()


def test_a_real_address_is_not_described_as_an_area_centre():
    from core.place_reference import query_reference

    for addr in ("45 Fairfield Road, E3 2QB",
                 "Scape Bloomsbury, 19-29 Woburn Place, London WC1H 0AQ"):
        ref = query_reference(addr)
        assert ref["is_specific_address"] is True, addr
        assert "not a property address" not in ref["measured_from"].lower()
        # Straight-line vs walking is still not what a reader assumes.
        assert "straight-line" in ref["measured_from"].lower()


def test_the_tesco_110m_answer_names_its_reference_point(monkeypatch):
    """The observed output: asked "how is Hackney?", the answer said "Tesco 110m". The
    number is real; 110 m from a borough centroid is not what a reader takes it to mean.
    The summary string the model reads must carry the reference point -- putting it only in
    a sibling field is exactly what route_source was."""
    import core.tools.search_nearby_pois as sp

    monkeypatch.setattr(sp, "geocode_address", lambda addr, **_kw: (51.5450, -0.0553))
    monkeypatch.setattr(sp, "query_osm_pois", lambda *a, **k: [
        {"name": "Tesco", "icon": "T", "distance_m": 110, "distance_display": "110m"}])

    res = sp.search_nearby_pois_impl(address="Hackney", poi_type="supermarket")

    assert "110m" in res["summary"]
    assert "centre of the area" in res["summary"].lower()
    assert res["reference_point"]["precision"] == "area"
    assert res["reference_point"]["is_specific_address"] is False


def test_an_empty_poi_result_also_names_the_reference_point(monkeypatch):
    import core.tools.search_nearby_pois as sp

    monkeypatch.setattr(sp, "geocode_address", lambda addr, **_kw: (51.5450, -0.0553))
    monkeypatch.setattr(sp, "query_osm_pois", lambda *a, **k: [])

    res = sp.search_nearby_pois_impl(address="Hackney", poi_type="supermarket")
    assert "centre of the area" in res["message"].lower()


def test_every_osm_place_row_carries_what_it_was_measured_from(monkeypatch):
    """Per-row rather than once per response, on purpose: a single sibling field is what
    got dropped last time. A consumer cannot render '110m' without also holding the origin."""
    monkeypatch.setattr(ms, "get_from_cache", lambda k: None)
    monkeypatch.setattr(ms, "set_to_cache", lambda k, v: None)
    monkeypatch.setattr(ms, "_free_geocode", lambda a: {
        "lat": 51.5450, "lng": -0.0553, "postcode": "E9 6QW", "geocoder": "nominatim",
        "resolved_name": "Hackney, London Borough of Hackney, Greater London, England",
        "match_type": "suburb", "place_rank": 19})
    monkeypatch.setattr(ms, "overpass_request", lambda *a, **k: {"elements": [
        {"type": "node", "lat": 51.5451, "lon": -0.0554, "tags": {"name": "Tesco"}}]})

    places = ms.get_nearby_places_osm("Hackney", "supermarket", radius_m=1500)

    assert places and places[0]["name"] == "Tesco"
    assert places[0]["distance_basis"] == "straight_line"
    assert places[0]["reference_precision"] == "area"
    assert "not a property address" in places[0]["measured_from"].lower()


def test_the_geocoder_records_how_precise_its_match_was(monkeypatch):
    """Nominatim already returns addresstype/place_rank; the geocoder was throwing them
    away, which is why nothing downstream could tell an address from a borough."""
    class _R:
        status_code = 200

        @staticmethod
        def json():
            return [{"lat": "51.5450", "lon": "-0.0553", "display_name": "Hackney, London",
                     "addresstype": "suburb", "place_rank": 19, "class": "place",
                     "type": "suburb", "address": {"postcode": "E9 6QW"}}]

    monkeypatch.setattr(ms, "get_from_cache", lambda k: None)
    monkeypatch.setattr(ms, "set_to_cache", lambda k, v: None)
    monkeypatch.setattr(ms.requests, "get", lambda *a, **k: _R())

    geo = ms._free_geocode("Hackney")
    assert geo["match_type"] == "suburb"
    assert geo["place_rank"] == 19
    assert geo["geocoder"] == "nominatim"
    assert geo["resolved_name"] == "Hackney, London"

    from core.place_reference import reference_point
    ref = reference_point("Hackney")
    assert ref["precision"] == "area"
    assert ref["is_specific_address"] is False


@pytest.mark.parametrize("rank,expected", [(30, "address"), (26, "street"), (19, "area")])
def test_place_rank_drives_the_precision_call(rank, expected):
    from core.place_reference import _classify_precision

    assert _classify_precision({"geocoder": "nominatim", "match_type": "place",
                                "place_rank": rank}) == expected
