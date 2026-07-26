# -*- coding: utf-8 -*-
"""The straight-line commute estimator, calibrated against the 14 measured TfL pairs.

PR #28 measured the estimator and then SUPPRESSED it: nothing under 15 estimated minutes was
reported at all. Honest, but it left the estimator wrong and the product unable to answer "how
long to X" for the distances people actually ask about. This module pins the fix.

Every test here fails on the pre-calibration behaviour, and the reason it fails is stated in
its docstring, because the defect this repo keeps shipping is a number that is computed,
stored where a reader could find it, and then never asserted on. The residual arithmetic in
``commute_basis``'s docstring is not trusted here either: it is recomputed from
``CALIBRATION``, so a future edit to the table that invalidates the fit cannot pass.
"""
from __future__ import annotations

import math

import pytest

from core import commute_basis as cb
from core import maps_service as ms


# --------------------------------------------------------------------------- #
# 1. The capability hole #28 left, closed with a defensible number.            #
# --------------------------------------------------------------------------- #

def test_the_sub_kilometre_pair_gets_a_number_instead_of_silence():
    """THE incident pair, from the other end.

    Tavistock Court -> UCL is 0.47 km and takes a measured 12 minutes. The raw formula said 2,
    so #28 refused to say anything at all. Refusing is better than saying 2, but the answer to
    "how long to UCL?" is 12 minutes, and the calibrated model can now say 11 (9-14) and show
    its working.

    FAILS BEFORE: describe_estimate(2, 0.47)["estimated_duration_minutes"] was None.
    """
    out = cb.describe_estimate(2, 0.47)

    assert out["estimated_duration_minutes"] == 11
    assert (out["estimate_low_minutes"], out["estimate_high_minutes"]) == (9, 14)
    assert out["estimate_model"] == cb.CALIBRATED_MODEL_ID
    # 12 is what TfL measured for this pair. The band has to contain it or the number is a
    # different kind of wrong from the one we just fixed.
    assert out["estimate_low_minutes"] <= 12 <= out["estimate_high_minutes"]
    # Still an estimate, still never in the measured field, still carrying its basis.
    assert "duration_minutes" not in out
    assert out["basis"] == cb.BASIS_STRAIGHT_LINE
    assert "0.47 km straight-line distance" in out["basis_note"]
    assert out["caveat"] and "not a journey plan" in out["caveat"].lower()


def test_the_published_band_is_reproducible_from_the_published_number():
    """A range nobody can recompute is one more unchecked figure. 11 x 0.84 -> 9,
    11 x 1.24 -> 14, and those are the two numbers printed."""
    out = cb.describe_estimate(2, 0.47)
    est = out["estimated_duration_minutes"]

    assert out["estimate_low_minutes"] == int(round(est * cb.CALIBRATED_RATIO_LOW))
    assert out["estimate_high_minutes"] == int(round(est * cb.CALIBRATED_RATIO_HIGH))


# --------------------------------------------------------------------------- #
# 2. The fit itself, recomputed from the table rather than transcribed.        #
# --------------------------------------------------------------------------- #

def test_no_sampled_pair_is_still_off_by_more_than_the_suppression_gate():
    """The whole licence for lowering the floor. Recomputed from CALIBRATION, so editing the
    table without refitting cannot ship.

    FAILS BEFORE: commute_basis had no calibrated model to evaluate.
    """
    rows = cb.calibration_residuals()
    assert len(rows) == len(cb.CALIBRATION) == 14

    for row in rows:
        assert row["calibrated_minutes"] is not None, (
            f"{row['label']}: every sampled pair is inside the fitted domain by construction — "
            f"CALIBRATED_MIN_KM/MAX_KM are the extremes of this very table")
        assert row["calibrated_error"] <= cb.MAX_ACCEPTED_RESIDUAL_RATIO, (
            f"{row['label']}: calibrated {row['calibrated_minutes']} vs measured "
            f"{row['tfl_minutes']} is {row['calibrated_error']:.3f}x out; a pair the model "
            f"cannot get within {cb.MAX_ACCEPTED_RESIDUAL_RATIO}x MUST stay suppressed rather "
            f"than be reported")

    worst = max(r["calibrated_error"] for r in rows)
    assert worst == pytest.approx(1.233, abs=0.001), worst


def test_the_suppression_gate_is_enforced_in_source_not_only_here(monkeypatch):
    """"No pair may be reported while it is more than 1.5x out" has to be a property of the
    code, not of this file. The worst residual is computed at import from CALIBRATION, and if it
    ever exceeds the gate the calibration stops being applied at all — falling back to #28's
    suppression rather than shipping a figure its own evidence contradicts."""
    assert cb.CALIBRATION_WORST_ERROR == pytest.approx(1.2330, abs=0.0005)
    assert cb.CALIBRATION_MEETS_GATE is True

    monkeypatch.setattr(cb, "CALIBRATION_MEETS_GATE", False)
    refused = cb.describe_estimate(2, 0.47)
    assert refused["estimated_duration_minutes"] is None
    assert refused["estimate_model"] is None
    assert cb.best_estimate_minutes(0.47, "transit") == 2      # back to the raw formula


def test_the_calibration_beats_the_formula_it_replaces_on_every_short_pair():
    """A calibration that is merely different is not a fix. On every pair the raw formula got
    wrong by more than 1.5x, the calibrated figure must be strictly closer to the truth."""
    rows = cb.calibration_residuals()
    bad_before = [r for r in rows
                  if max(r["legacy_ratio"], 1 / r["legacy_ratio"]) > cb.MAX_ACCEPTED_RESIDUAL_RATIO]

    # The five pairs #28 measured as hopeless: the two sub-km ones plus Bloomsbury, Camden,
    # Islington. 5 of 14.
    assert len(bad_before) == 5, [r["label"] for r in bad_before]
    for row in bad_before:
        before = max(row["legacy_ratio"], 1 / row["legacy_ratio"])
        after = row["calibrated_error"]
        assert after < before, f"{row['label']}: {before:.3f}x -> {after:.3f}x is not an improvement"

    # And across all 14, on the multiplicative loss the fit was performed on.
    def rms_log(key):
        return math.sqrt(sum(math.log(r[key]) ** 2 for r in rows) / len(rows))

    assert rms_log("legacy_ratio") == pytest.approx(0.7368, abs=0.0005)
    assert rms_log("calibrated_ratio") == pytest.approx(0.1049, abs=0.0005)


def test_the_measured_time_lands_inside_the_quoted_band_on_all_14_pairs():
    """The band is a claim about this fit's error. Check it in the data it was drawn from —
    the same guard #28 put on its own band, re-pointed at the new one."""
    for row in cb.calibration_residuals():
        band = cb.calibrated_band(row["calibrated_minutes"])
        assert band is not None, row["label"]
        low, high = band
        assert low <= row["tfl_minutes"] <= high, (
            f"{row['label']}: measured {row['tfl_minutes']} fell outside the reported "
            f"{low}-{high}")

    # And the ratios the band was rounded outward from.
    ratios = [r["calibrated_ratio"] for r in cb.calibration_residuals()]
    assert cb.CALIBRATED_RATIO_LOW <= min(ratios), (cb.CALIBRATED_RATIO_LOW, min(ratios))
    assert max(ratios) <= cb.CALIBRATED_RATIO_HIGH, (max(ratios), cb.CALIBRATED_RATIO_HIGH)
    assert min(ratios) == pytest.approx(0.8471, abs=0.0005)
    assert max(ratios) == pytest.approx(1.2330, abs=0.0005)


def test_the_shipped_constants_are_at_the_least_squares_optimum():
    """The parameters are claimed to be fitted, not chosen. A fudge factor would not survive
    being perturbed in either direction: if any single-parameter nudge improved the objective,
    the constants would not be at an optimum and 'fitted' would be a story."""
    rows = cb.CALIBRATION

    def sse(a, b, q):
        return sum(math.log((a + b * km ** q) / tfl) ** 2 for _l, km, _e, tfl in rows)

    base = sse(cb.CAL_OVERHEAD_MINUTES, cb.CAL_PACE_COEFFICIENT, cb.CAL_DISTANCE_EXPONENT)
    assert base == pytest.approx(0.15416, abs=0.0002)

    for i, step in ((0, 0.5), (1, 0.5), (2, 0.02)):
        for sign in (+1, -1):
            p = [cb.CAL_OVERHEAD_MINUTES, cb.CAL_PACE_COEFFICIENT, cb.CAL_DISTANCE_EXPONENT]
            p[i] += sign * step
            assert sse(*p) > base, (
                f"perturbing parameter {i} by {sign * step} improved the fit, so the shipped "
                f"constants are not at the optimum they claim to be")


def test_the_model_explains_both_ends_of_the_error_which_a_multiplier_cannot():
    """#28's finding was 6.0x low at 0.47 km and 0.75x HIGH at 16.71 km. Two facts follow and
    both are asserted here rather than left in prose:

    1. No single multiplier can fix both, so the correction must vary with distance.
    2. The variation is too large to be street-network detour (circuity ~1.2-1.6), so calling
       the distance-dependent term a 'detour factor' would be mislabelling a mode change.
    """
    paces = [tfl / km for _l, km, _e, tfl in cb.CALIBRATION]
    assert max(paces) / min(paces) > 7, max(paces) / min(paces)
    assert max(paces) / min(paces) > 1.6, "a pure detour-factor model would have to cover this"

    # The correction the model applies really does vary with distance, by a lot.
    def correction(km):
        return cb.calibrated_minutes(km) / cb.legacy_straight_line_minutes(km, "transit")

    near, far = correction(cb.CALIBRATED_MIN_KM), correction(cb.CALIBRATED_MAX_KM)
    assert near > 5.0, near        # 11.06 / 2
    assert far < 1.0, far          # 62.08 / 75
    assert near / far > 6.0, (near, far)

    # And the effective pace falls monotonically, which is the mechanism.
    pace = [cb.CAL_PACE_COEFFICIENT * km ** (cb.CAL_DISTANCE_EXPONENT - 1.0)
            for km in (0.47, 1.0, 2.0, 5.0, 10.0, 16.71)]
    assert pace == sorted(pace, reverse=True)
    assert pace[0] == pytest.approx(15.65, abs=0.01)
    assert pace[-1] == pytest.approx(3.49, abs=0.01)


# --------------------------------------------------------------------------- #
# 3. The floor: lowered as far as the evidence reaches, and not one step more. #
# --------------------------------------------------------------------------- #

def test_the_floor_is_derived_from_the_shortest_measured_pair_not_chosen():
    """#28's floor was 15 minutes. The new one is 11, and 11 is not a preference: it is what
    the fitted model says at 0.47 km, the shortest pair anyone measured. Two thresholds that
    can disagree is how the last review round went wrong, so the minutes figure is computed
    from the distance gate rather than written down beside it."""
    assert cb.MIN_CALIBRATED_ESTIMATE_MINUTES == 11
    assert cb.MIN_CALIBRATED_ESTIMATE_MINUTES == int(round(
        cb.calibrated_minutes(cb.CALIBRATED_MIN_KM)))
    assert cb.MIN_CALIBRATED_ESTIMATE_MINUTES < cb.MIN_TRUSTWORTHY_ESTIMATE_MINUTES

    # The distance gate is the extremes of the table, not a round number near them.
    kms = [km for _l, km, _e, _t in cb.CALIBRATION]
    assert cb.CALIBRATED_MIN_KM == min(kms) == 0.47
    assert cb.CALIBRATED_MAX_KM == max(kms) == 16.71


def test_below_the_shortest_measured_pair_there_is_still_no_number():
    """The floor moved; it did not go away. Under 0.47 km the model is extrapolating below its
    data, so #28's rule stands: no number rather than a wrong one, and the note says which of
    the two reasons applies."""
    for km in (0.05, 0.20, 0.46):
        out = cb.describe_estimate(cb.legacy_straight_line_minutes(km, "transit"), km)
        assert out["estimated_duration_minutes"] is None, km
        assert out["estimate_low_minutes"] is None and out["estimate_high_minutes"] is None
        assert out["estimate_model"] is None
        assert "duration_minutes" not in out
        assert "no number is given" in out["basis_note"].lower()
        assert "closer than the shortest pair anyone measured" in out["basis_note"]
        assert out["straight_line_km"] == km      # the distance is still a stateable fact


def test_above_the_longest_measured_pair_the_uncalibrated_treatment_is_unchanged():
    """Past 16.71 km the fit is extrapolating and its residual band was never measured there,
    so #28's behaviour applies verbatim — raw figure, wider band. Quoting the calibrated band
    outside the data it came from would be the defect this module exists to prevent."""
    km = 20.0
    raw = cb.legacy_straight_line_minutes(km, "transit")
    out = cb.describe_estimate(raw, km)

    assert out["estimate_model"] is None
    assert out["estimated_duration_minutes"] == raw == 88
    assert (out["estimate_low_minutes"], out["estimate_high_minutes"]) == (
        int(round(raw * cb.ESTIMATE_RATIO_LOW)), int(round(raw * cb.ESTIMATE_RATIO_HIGH)))
    assert "UNCALIBRATED" in out["basis_note"]
    assert out["caveat"] == cb.CAVEAT_EN


def test_the_piecewise_estimator_is_monotone_in_distance():
    """The calibrated and uncalibrated branches join at 0.47 km and 16.71 km, and the joins are
    steps (2 -> 11 and 62 -> 75). Monotonicity is the property that actually matters for a
    sort/filter, so it is pinned rather than assumed; the 13-minute step at the top is a known,
    documented consequence of never quoting a band past the data."""
    prev = -1
    km = 0.01
    while km <= 40.0:
        m = cb.best_estimate_minutes(km, "transit")
        assert m is not None and m >= prev, (km, m, prev)
        prev = m
        km = round(km + 0.01, 2)

    assert cb.best_estimate_minutes(0.46, "transit") == 2
    assert cb.best_estimate_minutes(0.47, "transit") == 11
    assert cb.best_estimate_minutes(16.71, "transit") == 62
    assert cb.best_estimate_minutes(16.72, "transit") == 75


# --------------------------------------------------------------------------- #
# 4. Source guards: the calibration is only applied where it was measured.     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["walking", "foot-walking", "bicycling", "cycling-regular",
                                  "driving", "nonsense"])
@pytest.mark.parametrize("km", [0.47, 0.83, 2.13, 5.37, 12.41])
def test_the_legacy_mirror_matches_maps_service(monkeypatch, mode, km):
    """``commute_basis.legacy_straight_line_minutes`` duplicates maps_service's formula so this
    module can recognise its own input and rebuild an uncalibrated figure without importing the
    maps stack. Duplication drifts unless something checks, so this checks — for every mode and
    a spread of distances, against the real producer.

    The distances are deliberately not round: maps_service computes minutes from the UNROUNDED
    haversine and then rounds distance_km to 2 dp, so at a distance where the minutes land on
    an integer boundary (5.00 km cycling is exactly 26.0) truncation can put the two sides one
    minute apart. That gap is real, and it is why LEGACY_MINUTES_TOLERANCE exists; this test
    checks the formula table, so it avoids the boundary rather than papering over it with a
    tolerance that would also hide genuine drift."""
    # Place the destination due north of the origin so the haversine distance is exactly km.
    dlat = km / 111.19492664455873      # 6371 km radius, 1 degree of latitude
    monkeypatch.setattr(ms, "get_from_cache", lambda k: None)
    monkeypatch.setattr(ms, "set_to_cache", lambda k, v: None)
    coords = {"O": {"lat": 51.5, "lng": -0.1}, "D": {"lat": 51.5 + dlat, "lng": -0.1}}
    monkeypatch.setattr(ms, "_get_coordinates", lambda a: coords[a])

    produced = ms.straight_line_travel_estimate("O", "D", mode)
    assert produced["distance_km"] == pytest.approx(km, abs=0.01)
    assert cb.legacy_straight_line_minutes(produced["distance_km"], mode) == produced["minutes"], (
        mode, km, produced)


def test_minutes_that_did_not_come_from_the_transit_formula_are_not_calibrated():
    """The model is a function of DISTANCE, but it is handed MINUTES plus a distance, and a
    caller could pass minutes from some other formula or version. Calibrating those would be
    correcting an estimator nobody measured, so the input has to agree with the transit formula
    at the stated distance to within the one minute that maps_service's 2-dp distance rounding
    can account for."""
    assert cb.legacy_straight_line_minutes(0.47, "transit") == 2

    for ok in (1, 2, 3):                       # 2 +/- LEGACY_MINUTES_TOLERANCE
        assert cb.describe_estimate(ok, 0.47)["estimate_model"] == cb.CALIBRATED_MODEL_ID
    for wrong in (0, 4, 7, 40):
        out = cb.describe_estimate(wrong, 0.47)
        assert out["estimate_model"] is None, wrong
        # 7 is what the WALKING formula gives at 0.47 km: caught, not silently transit-ified.
        assert cb.legacy_straight_line_minutes(0.47, "walking") == 7

    assert cb.LEGACY_MINUTES_TOLERANCE == 1


def test_an_unknown_distance_cannot_be_calibrated():
    """No distance, no model input. Falls back to #28's treatment of the bare figure."""
    for bad in (None, "eight", float("nan"), float("inf"), True):
        out = cb.describe_estimate(41, bad)
        assert out["estimate_model"] is None, bad
        assert out["estimated_duration_minutes"] == 41   # #28: raw figure, 15-min floor, wide band
        assert out["estimate_low_minutes"] == int(round(41 * cb.ESTIMATE_RATIO_LOW))


def test_a_mode_the_sample_never_measured_keeps_the_uncalibrated_treatment():
    """Every reference journey in CALIBRATION is TfL's FASTEST itinerary, i.e. public transport
    or walking. Nothing in it prices a bicycle or a car, so nothing licenses correcting one.

    A 5 km walk is the case that shows why this matters: the raw walking figure of 78 minutes
    is roughly right, and handing it the transit correction (33 minutes) would turn a usable
    number into a wrong one in the name of a fix."""
    assert cb.CALIBRATED_MODES == ("transit",)
    walking = cb.legacy_straight_line_minutes(5.0, "walking")
    assert walking == 78
    out = cb.describe_estimate(walking, 5.0, mode="walking")
    assert out["estimate_model"] is None
    assert out["estimated_duration_minutes"] == 78
    assert int(round(cb.calibrated_minutes(5.0))) == 33     # what it would wrongly have said


def test_a_short_cycling_request_is_withdrawn_at_the_layer_that_knows_the_mode():
    """maps_service does not pass its ``mode`` to describe_estimate, and up to 2.71 km the
    raw cycling and transit formulas agree to within a minute, so the guard above cannot tell
    them apart from the number alone. The tool layer can. Without this the calibration would
    NEWLY answer 'how long to cycle 0.8 km' with 14 minutes, where #28 correctly said nothing.
    """
    assert cb.legacy_straight_line_minutes(0.8, "bicycling") == 4
    assert cb.legacy_straight_line_minutes(0.8, "transit") == 4      # indistinguishable

    as_transit = cb.describe_estimate(4, 0.8)
    assert as_transit["estimated_duration_minutes"] == 14
    assert as_transit["estimate_model"] == cb.CALIBRATED_MODEL_ID

    withdrawn = cb.withdraw_uncalibrated_mode(as_transit, "bicycling")
    assert withdrawn["estimated_duration_minutes"] is None
    assert withdrawn["estimate_model"] is None
    assert "no number is given" in withdrawn["basis_note"].lower()

    # A driving request keeps #28's uncalibrated figure rather than losing the answer: the raw
    # formula for driving is the transit one, so 8 km reads 41 minutes with the wide band.
    driving = cb.withdraw_uncalibrated_mode(cb.describe_estimate(41, 8.0), "driving")
    assert driving["estimated_duration_minutes"] == 41
    assert driving["estimate_model"] is None
    assert (driving["estimate_low_minutes"], driving["estimate_high_minutes"]) == (30, 59)

    # transit passes straight through, and a payload that was never calibrated is untouched.
    assert cb.withdraw_uncalibrated_mode(as_transit, "transit") == as_transit
    raw = cb.describe_estimate(88, 20.0)
    assert cb.withdraw_uncalibrated_mode(raw, "bicycling") == raw


def test_the_tool_withdraws_the_calibration_for_a_cycling_request(monkeypatch):
    """End to end through the tool, because a guard that the payload never reaches is decoration
    — which is exactly what ``route_source`` was."""
    import core.tools.calculate_commute as cc

    monkeypatch.setattr(ms, "calculate_travel_details", lambda f, t, m="transit": dict(
        {"duration_minutes": None, "route_legs": [], "route_summary": "r", "source": "estimate"},
        **cb.describe_estimate(4, 0.8)))

    transit = cc.calculate_commute_impl("A", "B", "transit")
    assert transit["estimated_duration_minutes"] == 14
    assert transit["estimate_model"] == cb.CALIBRATED_MODEL_ID
    assert transit["duration_minutes"] is None          # still never the measured field

    cycling = cc.calculate_commute_impl("A", "B", "bicycling")
    assert cycling["estimated_duration_minutes"] is None
    assert cycling["estimate_model"] is None
    assert "do NOT state" in cycling["recommendation"]


# --------------------------------------------------------------------------- #
# 5. One model, so the filter and the answer cannot disagree.                  #
# --------------------------------------------------------------------------- #

def test_the_listing_filter_and_the_quoted_answer_use_the_same_model():
    """``coord_commute_minutes`` annotates listings as ``travel_time_minutes`` and drives the
    ``max_commute_time`` filter; ``describe_estimate`` is what the answer quotes. They were
    separate copies of the same broken formula, so a 0.47 km pair filtered as 2 minutes and
    would now have been quoted as 11 — a contradiction inside one response.

    FAILS BEFORE: coord_commute_minutes returned 2 for this pair.
    """
    from core import commute

    origin = "51.5245,-0.1272"                      # Tavistock Court WC1H
    dest = {"lat": 51.5246, "lng": -0.1340}         # UCL Gower Street

    # Behavioural first, so this fails on the old code with the number rather than on a missing
    # symbol: the annotation used to read 2 minutes for a journey TfL measures at 12.
    assert commute.coord_commute_minutes(origin, dest) == 11

    km = commute.straight_line_km(origin, dest)
    assert km == pytest.approx(0.4706, abs=0.0005)
    quoted = cb.describe_estimate(cb.legacy_straight_line_minutes(round(km, 2), "transit"),
                                  round(km, 2))["estimated_duration_minutes"]
    assert commute.coord_commute_minutes(origin, dest) == quoted == 11

    # Degradation is unchanged: no coordinates, no number.
    assert commute.coord_commute_minutes("", dest) is None
    assert commute.coord_commute_minutes(origin, None) is None
