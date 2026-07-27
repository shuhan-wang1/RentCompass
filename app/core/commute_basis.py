"""How a commute figure was obtained, and what a straight-line guess is actually worth.

WHY THIS FILE EXISTS
--------------------
``maps_service.calculate_travel_details`` has two completely different ways of producing
"minutes": a real TfL Journey Planner itinerary, and — when TfL returns no journey — a
haversine straight-line guess (distance x 1.3 at 20 km/h plus a small wait term). Both were
returned in the SAME field, ``duration_minutes``, distinguished only by a sibling
``source`` key. Nothing consumed that key: ``grep -rn route_source --include='*.py' app/``
matched only the file that produced it, and no prompt mentioned it. So the model was handed

    {"duration_minutes": 6, "route_source": "estimate"}

and told the user "6 minutes" as a fact. This is the same defect as the safety score
(``safety_reference.py``): the evidence for the claim was computed and then never asserted
on.

WHAT THE STRAIGHT-LINE GUESS IS ACTUALLY WORTH
----------------------------------------------
Measured, not asserted. ``scripts/sample_commute_calibration.py`` takes 14 real London
origin/destination pairs, computes the estimator's figure from coordinates, and asks the TfL
Journey Planner for the true fastest journey between the same two points. Results below in
``CALIBRATION``.

The headline finding is not "the estimate is noisy". It is that the estimate is
**systematically and grossly low on short trips**, because straight-line distance / speed
models none of what dominates a short urban journey: walking to the station, waiting for a
service, and interchanging.

    Tavistock Court WC1H -> UCL Gower Street:  estimator 2 min,  TfL 12 min   (6.0x)
    Woburn Place WC1H    -> UCL Gower Street:  estimator 2 min,  TfL 11 min   (5.5x)
    Camden NW1           -> UCL Gower Street:  estimator 10 min, TfL 18 min   (1.8x)

Every sampled pair whose estimate came out under 15 minutes was low by at least 1.78x. That
range — single-digit "minutes" for a central-London hop — is exactly the shape of the
fabricated ``commute_minutes`` values (6.0 / 8.0 / 5.0) that eval cases C6 and C11 flagged
as unsupported. Above 15 estimated minutes the error collapses to roughly +-30%.

CALIBRATION, NOT ONLY SUPPRESSION (2026-07-26, this file's second pass)
-----------------------------------------------------------------------
The first pass refused every figure under 15 estimated minutes. Honest, but it left the
product unable to answer "how long to walk to X" for exactly the distances people ask about,
and it left the formula underneath still wrong. So the 14 pairs are now used for what they
were collected for: FITTING the estimator, not merely condemning it.

WHAT THE DATA ACTUALLY SAY ABOUT THE SHAPE OF THE ERROR
    Observed door-to-door pace (TfL minutes / straight-line km) across the 14 pairs runs
    from 25.53 min/km at 0.47 km to 3.35 min/km at 16.71 km — a 7.62x spread. That number
    settles the model choice on its own:

      * A single global multiplier cannot fit it (it would have to be 6.0x at 0.47 km and
        0.75x at 16.71 km simultaneously).
      * A "street-network detour factor" cannot fit it either. Circuity of a real street
        network is roughly 1.2-1.6; explaining a 7.62x pace spread geometrically would need a
        detour factor sweeping past 9x. The pace decline is therefore MODAL (walk -> bus ->
        tube/rail as the trip lengthens), not geometric, and any parameterisation that calls
        it "detour" is mislabelling it.

    That was checked by fitting, not asserted. Two mechanistic parameterisations were tried
    and both land on physically inadmissible parameters, i.e. they are unidentifiable from 14
    (straight-line distance, door-to-door time) pairs:

      * overhead + d*(P_far + (P_near-P_far)*exp(-d/L)) fits to P_near = -62 min/km.
      * min(walk_overhead + walk_pace*d, ride_overhead + ride_pace*d) fits to a walk pace of
        5.5 min/km (10.9 km/h — that is running) and a 31-minute ride overhead.

    Only the AGGREGATE curve is identified. Detour and modal speed appear in the data solely
    as their product, and separating them needs per-leg data this sample does not carry (see
    WHAT 14 PAIRS CANNOT SETTLE below).

THE FITTED MODEL
    calibrated minutes  t(d) = 3.7 + 11.4 * d ** 0.58        d = straight-line km

    A fixed overhead term (3.7 min: crossings, stairs, entrance-to-platform, waiting) plus a
    distance-dependent effective pace 11.4 * d**-0.42 min/km, which falls from 15.65 min/km at
    0.47 km to 3.49 min/km at 16.71 km — a 4.48x decline across the fitted range. Fitted by
    minimising the sum of squared LOG ratios (the error that matters here is multiplicative)
    over all 14 pairs, then rounded to two significant figures per parameter. Reproduce with
    ``python scripts/sample_commute_calibration.py --refit`` (no network, no API spend).

    Per-pair residuals for the constants actually shipped below. "err" is TfL / estimator, so
    >1 means the estimator is LOW; cal is round(t(d)) and its residual is against unrounded
    t(d), which is what the band was measured from.

        pair                                        km   TfL  legacy  err   cal   err   band
        Tavistock Court -> UCL Gower Street       0.47   12       2  6.00    11  1.085   9-14
        Woburn Place -> UCL Gower Street          0.50   11       2  5.50    11  0.971   9-14
        Bloomsbury -> KCL Strand                  1.64   16       9  1.78    19  0.847  16-24
        Camden -> UCL Gower Street                1.71   18      10  1.80    19  0.935  16-24
        Islington -> UCL Gower Street             2.14   23      12  1.92    21  1.074  18-26
        Shoreditch -> KCL Strand                  2.97   24      17  1.41    25  0.955  21-31
        Hackney E8 -> KCL Strand                  5.61   40      31  1.29    35  1.153  29-43
        Bow -> UCL Gower Street                   7.56   44      39  1.13    41  1.085  34-51
        Peckham -> UCL Gower Street               7.56   50      39  1.28    41  1.233  34-51
        Canary Wharf -> UCL Gower Street          7.94   41      40  1.03    42  0.985  35-52
        Stratford -> UCL Gower Street             9.22   39      45  0.87    45  0.866  38-56
        Wembley -> UCL Gower Street              10.68   46      51  0.90    49  0.944  41-61
        Richmond -> UCL Gower Street             13.70   58      63  0.92    56  1.041  47-69
        Croydon -> UCL Gower Street              16.71   56      75  0.75    62  0.902  52-77

    Worst absolute error: legacy 6.00x, and over 1.5x on 5 of 14 pairs; calibrated 1.233x, and
    over 1.5x on 0 of 14. RMS of the log ratio: legacy 0.7368, calibrated 0.1049. The measured
    TfL time falls inside the quoted band on all 14 pairs. No sampled pair is off by more than
    ``MAX_ACCEPTED_RESIDUAL_RATIO``, so no sampled pair has to stay suppressed on residual
    grounds — what stays suppressed is what the sample does not cover (below).

    WHY THREE PARAMETERS AND NOT TWO. Dropping the overhead term (t = 15.2853 * d**0.485) fits
    almost as well — SSE_log 0.16420 against 0.15415 — and both AICc (-53.84 against -50.68) and
    leave-one-out CV (mean squared log error 0.01580 against 0.01688) mildly PREFER the two-
    parameter form; ``--refit`` prints that comparison. The overhead is kept anyway, and the
    reason is not fit: a pure power law sends t -> 0 as d -> 0, which is the "2 minutes for a
    12-minute walk" failure all over again if anyone ever lowers the distance floor. The three-
    parameter form degrades to 3.7 minutes instead. The difference between the two is inside the
    noise of a 14-point sample either way; the tie is broken on which one fails safely.

WHAT 14 PAIRS CANNOT SETTLE — and exactly what would
    1. Detour vs modal speed. Door-to-door time against straight-line distance identifies
       only their product. TfL's Journey Planner already returns per-leg mode, duration and
       distance; ``sample_commute_calibration.py`` threw the legs away and kept the total. Re-
       running the SAME 14 queries and keeping ``journey.legs`` would separate walked network
       metres from in-vehicle minutes at no extra request count. Owner decision required.
    2. Distances above 16.71 km and below 0.47 km. Not sampled, so not calibrated, so not
       used — see the domain guard. 3-4 pairs in the 18-30 km band and 3-4 in the 0.15-0.45 km
       band would extend it.
    3. Non-transit modes. Every reference journey is TfL's fastest itinerary, i.e. public
       transport or walking. Nothing here calibrates driving or cycling, so those keep the
       old uncalibrated treatment (``CALIBRATED_MODES``).
    No pairs were invented and no live sweep was run to paper over any of this.

THE RULES THAT FALL OUT
-----------------------
1. A straight-line figure is calibrated ONLY where the sample supports it: transit mode, a
   known distance inside ``[CALIBRATED_MIN_KM, CALIBRATED_MAX_KM]``, and input minutes that
   really are the legacy transit formula's output for that distance. Inside that domain the
   figure is ``round(t(d))`` with the band measured from the fit residuals.
2. Anywhere else — unknown distance, distance outside the sampled range, a mode the sample
   does not cover, or minutes that did not come from the transit formula — the FIRST pass's
   behaviour applies unchanged: the raw figure, the 15-minute refusal floor, and the wider
   uncalibrated band. Nothing gets a number the sample cannot price.
3. The refusal floor is DERIVED, not chosen. In minutes it is
   ``MIN_CALIBRATED_ESTIMATE_MINUTES`` = round(t(CALIBRATED_MIN_KM)) = 11, down from 15, and
   it is 11 only because 0.47 km is the shortest pair anyone measured. The gate in the code is
   on distance; the minutes figure is a label for it, so the two can never disagree.
4. Either way the number is returned in ``estimated_duration_minutes`` — NEVER in
   ``duration_minutes`` — accompanied by its range, its basis and a caveat.
   ``duration_minutes`` means "a journey planner measured this" and nothing else.

A KNOWN, DELIBERATE DISCONTINUITY
    At 16.71 km the calibrated branch says 62 minutes and the uncalibrated branch one metre
    further out says 75. The step is real and is the price of rule 2: past the last measured
    pair there is no residual band for the calibrated curve, and quoting one would be the
    very defect this module exists to prevent. The piecewise function is still monotone in
    distance (``test_the_piecewise_estimator_is_monotone_in_distance`` pins that). Sampling
    the 18-30 km band removes the step; padding the table would not.

THE LIMITATION THAT MUST TRAVEL WITH THE NUMBER
-----------------------------------------------
The estimator only ever runs when TfL returned no journey, which in practice means OUTSIDE
TfL coverage (Manchester, Leeds, ...) — and outside TfL coverage there is, by definition, no
TfL reference to calibrate against. So this calibration measures the estimator's *formula*
against real public-transport journeys in the one city where a reference exists. It is the
best available check, not a validation in the domain where the fallback actually fires. That
limitation is part of ``CAVEAT_EN`` / ``CAVEAT_ZH`` (and of ``CALIBRATED_CAVEAT_EN`` /
``CALIBRATED_CAVEAT_ZH``) and must be stated with the number. Calibrating the formula does not
narrow it: the fit is still London-only, and a Manchester pair gets a London-fitted correction.

STILL BROKEN ELSEWHERE, AND NOT FIXED HERE
------------------------------------------
``maps_service.calculate_travel_time`` returns a BARE int that silently falls back to the raw
straight-line formula, and ``tools/calculate_commute_cost.py`` puts that int straight into
``commute.duration_minutes`` and derives ``duration_category`` / ``is_acceptable`` / a
monthly-hours figure from it. So for one pair ``calculate_commute`` can now say "estimated 11
minutes (9-14), straight-line basis" while ``calculate_commute_cost`` says "2 minutes" as a
fact. That gap predates this change and is unchanged by it; closing it means giving
``calculate_travel_time`` a basis-aware cached return in ``maps_service`` (already noted at
``calculate_commute_cost.py`` :293-300), which is outside this change's files. Owner decision.
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------- #
# Basis labels. `duration_minutes` is reserved for BASIS_MEASURED, full stop.  #
# --------------------------------------------------------------------------- #
BASIS_MEASURED = "tfl_journey_plan"
BASIS_STRAIGHT_LINE = "straight_line_estimate"

# `source` strings historically emitted by maps_service / carried in eval fixtures.
_MEASURED_SOURCE_TOKENS = ("tfl", "journey planner", "journeyplanner")

CALIBRATION_SAMPLED_ON = "2026-07-26"

# (label, straight_line_km, estimator_minutes, tfl_measured_minutes)
# Collected by scripts/sample_commute_calibration.py so these are reproducible rather
# than asserted. Sorted by distance.
CALIBRATION: tuple[tuple[str, float, int, int], ...] = (
    ("Tavistock Court WC1H -> UCL Gower Street", 0.47, 2, 12),
    ("Woburn Place WC1H -> UCL Gower Street", 0.50, 2, 11),
    ("Bloomsbury WC1H -> KCL Strand", 1.64, 9, 16),
    ("Camden NW1 -> UCL Gower Street", 1.71, 10, 18),
    ("Islington N1 0RW -> UCL Gower Street", 2.14, 12, 23),
    ("Shoreditch EC2A 3DU -> KCL Strand", 2.97, 17, 24),
    ("Hackney E8 -> KCL Strand", 5.61, 31, 40),
    ("Bow E3 2QB -> UCL Gower Street", 7.56, 39, 44),
    ("Peckham SE15 -> UCL Gower Street", 7.56, 39, 50),
    ("Canary Wharf E14 -> UCL Gower Street", 7.94, 40, 41),
    ("Stratford E15 -> UCL Gower Street", 9.22, 45, 39),
    ("Wembley HA9 -> UCL Gower Street", 10.68, 51, 46),
    ("Richmond TW9 -> UCL Gower Street", 13.70, 63, 58),
    ("Croydon CR0 -> UCL Gower Street", 16.71, 75, 56),
)

# --------------------------------------------------------------------------- #
# The UNCALIBRATED path: raw formula output, kept verbatim from the first pass. #
# Applies whenever the calibrated model's domain conditions are not met.        #
# --------------------------------------------------------------------------- #

# Below this many RAW (uncalibrated) estimated minutes the figure is withheld entirely.
# Justification, from CALIBRATION: every sampled pair whose RAW estimate came out under 15
# minutes was low by >= 1.78x, and the two sub-kilometre pairs were low by 5.5x and 6.0x.
# There is no band wide enough to make "2 minutes" an honest rendering of a 12-minute
# journey. This floor is NOT the floor on a calibrated figure — that one is
# MIN_CALIBRATED_ESTIMATE_MINUTES, and it is lower because the number it gates is better.
MIN_TRUSTWORTHY_ESTIMATE_MINUTES = 15

# Multiplicative band for RAW estimates at or above the floor, from the observed ratio range
# of tfl/estimate over the CALIBRATION rows with estimator_minutes >= 15
# (min 0.747 at Croydon, max 1.412 at Shoreditch). Rounded OUTWARD, so the band is never
# narrower than the evidence it is drawn from.
ESTIMATE_RATIO_LOW = 0.74
ESTIMATE_RATIO_HIGH = 1.45

# --------------------------------------------------------------------------- #
# The CALIBRATED path. Every constant below is a fitted or measured quantity;   #
# none of them is a chosen fudge factor. See the module docstring for the fit.  #
# --------------------------------------------------------------------------- #

CALIBRATED_MODEL_ID = "calibrated_overhead_plus_power_pace_v1"

# t(d) = CAL_OVERHEAD_MINUTES + CAL_PACE_COEFFICIENT * d ** CAL_DISTANCE_EXPONENT
# Least squares on log(t_model / t_tfl) over all 14 CALIBRATION pairs. Unrounded optimum
# (3.6941, 11.3847, 0.5809), SSE_log 0.15415; shipped rounded to two significant figures per
# parameter, SSE_log 0.15416 — i.e. the rounding costs 0.00001 of the objective.
CAL_OVERHEAD_MINUTES = 3.7        # fixed access cost: crossings, stairs, platform wait
CAL_PACE_COEFFICIENT = 11.4       # min/km at d = 1 km, before the exponent bends it
CAL_DISTANCE_EXPONENT = 0.58      # < 1 => effective pace FALLS with distance (mode mix)

# Domain of the fit == the range of distances anyone actually measured. Outside it the model
# is extrapolating and its residual band is not evidence, so it is not used at all.
CALIBRATED_MIN_KM = 0.47          # shortest sampled pair (Tavistock Court -> UCL)
CALIBRATED_MAX_KM = 16.71         # longest sampled pair (Croydon -> UCL)

# Multiplicative band for a CALIBRATED figure, from the fit residuals tfl/calibrated over all
# 14 pairs: min 0.8471 (Bloomsbury -> KCL), max 1.2330 (Peckham -> UCL). Rounded OUTWARD to
# 0.01, exactly as ESTIMATE_RATIO_* above, so the band is never narrower than its evidence.
# 1.78x narrower than the uncalibrated band it replaces: 1.24-0.84 = 0.40 wide against
# 1.45-0.74 = 0.71.
CALIBRATED_RATIO_LOW = 0.84
CALIBRATED_RATIO_HIGH = 1.24

# Every reference journey in CALIBRATION is TfL's fastest itinerary, i.e. public transport or
# walking. Nothing here measures driving or cycling, so no other mode gets the calibration.
CALIBRATED_MODES = ("transit",)

# A calibrated figure is only published for a pair whose residual would have passed this
# gate. All 14 sampled pairs do (worst 1.2361); the constant exists so that a future edit to
# CALIBRATION which breaks one cannot ship silently.
MAX_ACCEPTED_RESIDUAL_RATIO = 1.5

# The calibrated model is a function of DISTANCE, but the figure it replaces arrives as
# MINUTES from maps_service, and a caller could hand over minutes that came from some other
# formula (a different mode, a different version). Publishing a calibrated number for those
# would be calibrating something we never measured, so the input has to agree with the legacy
# transit formula at the stated distance. Tolerance is 1 minute and that is derived, not
# guessed: maps_service rounds distance_km to 2 dp AFTER computing minutes from the unrounded
# value, and the formula's steepest slope is 5.9 min/km, so 0.005 km of rounding moves it by
# at most 0.03 min — which a truncating int() can turn into exactly one whole minute.
LEGACY_MINUTES_TOLERANCE = 1

CAVEAT_EN = (
    "This is NOT a journey plan. No TfL itinerary was available for this pair, so the figure "
    "is derived from the straight-line distance (x1.3 route factor at 20 km/h) and models no "
    "walking access, waiting or interchange time. Measured against 14 real TfL journeys it "
    "ran between 0.74x and 1.45x the true time, and it is only calibrated inside London — "
    "the case where it actually fires is outside TfL coverage, where no reference exists. "
    "Quote it as an estimate with its range, never as a journey time."
)
CAVEAT_ZH = (
    "这不是行程规划结果。TfL 没有返回该起讫点的行程，因此该数字由直线距离推算"
    "（x1.3 路径系数、20 km/h），完全没有计入步行接驳、候车与换乘时间。与 14 条真实 TfL "
    "行程对比，它落在真实时间的 0.74~1.45 倍之间，而且只在伦敦范围内校准过——它真正被"
    "触发的场景恰恰在 TfL 覆盖范围之外，那里没有可对照的基准。请作为带区间的估算引用，"
    "不要当作行程时间。"
)

# The calibrated path needs its own caveat: the uncalibrated one above describes the x1.3 /
# 20 km/h formula, which is not the arithmetic that produced a calibrated figure. Saying
# "x1.3 route factor" about a number that did not use it would be a false basis, which is the
# same failure as no basis at all.
CALIBRATED_CAVEAT_EN = (
    "This is NOT a journey plan. No TfL itinerary was available for this pair, so the figure "
    "is a straight-line estimate: the distance is put through a model fitted to 14 real TfL "
    "journeys in London (a fixed 3.7-minute access overhead plus a pace of "
    "11.4 x km^-0.42 minutes per km, so short hops are charged the walking, waiting and "
    "interchange time the raw formula ignored). Across those 14 journeys the fitted figure ran "
    "between 0.84x and 1.24x the true time; quote it with that range, never as a journey time. "
    "It is fitted only over 0.47-16.71 km and only for public transport, and only inside "
    "London — the case where it actually fires is outside TfL coverage, where no reference "
    "exists to check it against."
)
CALIBRATED_CAVEAT_ZH = (
    "这不是行程规划结果。TfL 没有返回该起讫点的行程，因此该数字是直线距离估算：距离经过一个"
    "用伦敦 14 条真实 TfL 行程拟合出来的模型换算（固定 3.7 分钟接驳开销，加上 "
    "11.4 x 公里^-0.42 分钟/公里的配速，因此短途会计入原公式忽略的步行、候车与换乘时间）。"
    "在这 14 条行程上，拟合值落在真实时间的 0.84~1.24 倍之间，请连同该区间引用，不要当作行程"
    "时间。它只在 0.47~16.71 公里、只针对公共交通、且只在伦敦范围内拟合过——它真正被触发的"
    "场景恰恰在 TfL 覆盖范围之外，那里没有可对照的基准。"
)


def legacy_straight_line_minutes(distance_km: float | None, mode: str = "transit") -> int | None:
    """The RAW (pre-calibration) formula, mirrored from ``maps_service``.

    Deliberate duplication: this module has to be able to (a) recognise whether the minutes it
    was handed really are the transit formula's output for the stated distance, and (b) rebuild
    the uncalibrated figure for a mode the calibration does not cover, without importing the
    maps stack. ``test_the_legacy_mirror_matches_maps_service`` cross-checks every mode against
    ``maps_service.straight_line_travel_estimate`` over a distance sweep, so the two cannot
    drift apart unnoticed.

    Source of truth being mirrored: ``maps_service.straight_line_travel_estimate``.
    """
    if not isinstance(distance_km, (int, float)) or isinstance(distance_km, bool):
        return None
    km = float(distance_km)
    if km < 0 or math.isnan(km) or math.isinf(km):
        return None
    actual = km * 1.3
    if mode in ("transit", "driving"):
        return int((actual / 20.0) * 60 + min(10.0, km * 2))
    if mode in ("bicycling", "cycling-regular"):
        return int((actual / 15.0) * 60)
    if mode in ("walking", "foot-walking"):
        return int((actual / 5.0) * 60)
    return int((actual / 20.0) * 60 + 5)


def calibrated_minutes(distance_km: float | None) -> float | None:
    """Fitted door-to-door minutes for ``distance_km``, or None outside the fitted domain.

    ``t(d) = CAL_OVERHEAD_MINUTES + CAL_PACE_COEFFICIENT * d ** CAL_DISTANCE_EXPONENT``.
    Returns None below ``CALIBRATED_MIN_KM`` or above ``CALIBRATED_MAX_KM``: past the shortest
    and longest pairs anyone measured the curve is extrapolating and the residual band that
    would be quoted with it was never measured there.
    """
    if not isinstance(distance_km, (int, float)) or isinstance(distance_km, bool):
        return None
    km = float(distance_km)
    if math.isnan(km) or math.isinf(km):
        return None
    if km < CALIBRATED_MIN_KM or km > CALIBRATED_MAX_KM:
        return None
    return CAL_OVERHEAD_MINUTES + CAL_PACE_COEFFICIENT * km ** CAL_DISTANCE_EXPONENT


# The refusal floor in MINUTES is derived from the distance gate, never chosen independently:
# it is what the fitted model says at the shortest pair that was actually measured. 11 as of
# the 2026-07-26 sample, down from the first pass's 15.
MIN_CALIBRATED_ESTIMATE_MINUTES = int(round(calibrated_minutes(CALIBRATED_MIN_KM)))


def calibration_residuals() -> list[dict]:
    """Per-pair residuals of both estimators against TfL, computed rather than transcribed.

    Shared by ``scripts/sample_commute_calibration.py --refit`` and by the tests, so the table
    in the module docstring, the constants below it and the assertions cannot drift.
    """
    rows = []
    for label, km, legacy_min, tfl in CALIBRATION:
        cal = calibrated_minutes(km)
        cal_int = None if cal is None else int(round(cal))
        rows.append({
            "label": label,
            "km": km,
            "tfl_minutes": tfl,
            "legacy_minutes": legacy_min,
            "legacy_ratio": tfl / legacy_min if legacy_min else None,
            "calibrated_minutes": cal_int,
            "calibrated_ratio": None if not cal else tfl / cal,
            "calibrated_error": None if not cal else max(tfl / cal, cal / tfl),
        })
    return rows


# Computed once at import from the table above, not transcribed: the worst factor by which the
# shipped model misses a sampled pair. 1.2330 as of the 2026-07-26 sample.
CALIBRATION_WORST_ERROR = max(r["calibrated_error"] for r in calibration_residuals()
                              if r["calibrated_error"] is not None)

# A source guard, not a promise in a docstring. The licence to publish a corrected figure is
# that the correction demonstrably clears MAX_ACCEPTED_RESIDUAL_RATIO on the pairs it was
# fitted to. If an edit to CALIBRATION or to the constants breaks that, the calibration stops
# being applied at all and the module falls back to the first pass's suppression — rather than
# quietly shipping a figure whose own evidence says it is 1.5x+ out.
CALIBRATION_MEETS_GATE = CALIBRATION_WORST_ERROR <= MAX_ACCEPTED_RESIDUAL_RATIO


def is_measured(source: str | None) -> bool:
    """True when ``source`` denotes a real journey plan rather than a derived guess.

    Anything unrecognised is NOT measured. Defaulting the other way is how an unlabelled
    figure gets promoted to a fact.
    """
    if not source:
        return False
    s = str(source).strip().lower()
    if s in ("estimate", "estimated", "straight_line_estimate", BASIS_STRAIGHT_LINE):
        return False
    return any(tok in s for tok in _MEASURED_SOURCE_TOKENS)


def estimate_band(minutes: float | None) -> tuple[int, int] | None:
    """``(low, high)`` minutes for a straight-line estimate, or None when it is refused.

    Returns None for a missing figure or one below ``MIN_TRUSTWORTHY_ESTIMATE_MINUTES`` —
    those are not "a quick journey", they are the method failing (see module docstring).
    """
    if minutes is None:
        return None
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return None
    if m < MIN_TRUSTWORTHY_ESTIMATE_MINUTES:
        return None
    low = int(round(m * ESTIMATE_RATIO_LOW))
    high = int(round(m * ESTIMATE_RATIO_HIGH))
    return max(1, low), max(low + 1, high)


def calibrated_band(minutes: float | None) -> tuple[int, int] | None:
    """``(low, high)`` minutes around a CALIBRATED figure, from the fit residuals.

    No floor of its own: the gate on a calibrated figure is the distance domain in
    ``calibrated_minutes``, which is the only thing the residuals were measured over. A second
    independent floor here is exactly how two thresholds end up disagreeing.
    """
    if minutes is None:
        return None
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None
    low = int(round(m * CALIBRATED_RATIO_LOW))
    high = int(round(m * CALIBRATED_RATIO_HIGH))
    return max(1, low), max(low + 1, high)


def _calibration_applies(minutes: float | None, distance_km: float | None, mode: str) -> float | None:
    """The calibrated minutes when every domain condition holds, else None.

    A source guard, not a promise. All four conditions have to hold, and each one names
    something the 14 sampled pairs actually cover:
      1. ``mode`` is one the sample measured (``CALIBRATED_MODES``);
      2. the distance is known — the model is a function of distance and nothing else;
      3. the distance is inside the sampled range;
      4. the minutes handed over really are the legacy transit formula's output at that
         distance, so we are calibrating the estimator we measured and not some other one;
      5. and the fit still clears the 1.5x gate on the pairs it was fitted to.
    """
    if not CALIBRATION_MEETS_GATE:
        return None
    if mode not in CALIBRATED_MODES:
        return None
    cal = calibrated_minutes(distance_km)
    if cal is None:
        return None
    if minutes is None:
        return None
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return None
    legacy = legacy_straight_line_minutes(distance_km, "transit")
    if legacy is None or abs(m - legacy) > LEGACY_MINUTES_TOLERANCE:
        return None
    return cal


def _why_uncalibrated(minutes: float | None, distance_km: float | None, mode: str) -> str:
    """The reason THIS figure was not corrected, in the order ``_calibration_applies`` checks.

    A generic "outside the calibrated domain" would be wrong three times out of five here — the
    figure can be uncalibrated because of the mode, or because the minutes did not come from the
    formula the calibration was measured on. Saying which is the whole point of a basis note.
    """
    if not CALIBRATION_MEETS_GATE:
        return ("the shipped fit no longer clears the "
                f"{MAX_ACCEPTED_RESIDUAL_RATIO}x error gate on the pairs it was fitted to, so it "
                f"is not applied at all")
    if mode not in CALIBRATED_MODES:
        return (f"the correction is fitted only to public-transport journeys and this request is "
                f"'{mode}'")
    km_known = isinstance(distance_km, (int, float)) and not isinstance(distance_km, bool)
    if not km_known:
        return "no straight-line distance was available to put through the fitted model"
    km = float(distance_km)
    if km < CALIBRATED_MIN_KM or km > CALIBRATED_MAX_KM:
        return (f"the correction is fitted over {CALIBRATED_MIN_KM}-{CALIBRATED_MAX_KM} km and "
                f"this pair is {km:.2f} km apart")
    return ("this figure is not what the straight-line transit formula produces at that "
            "distance, leaving no measured error to correct it by")


def best_estimate_minutes(distance_km: float | None, mode: str = "transit") -> int | None:
    """Integer minutes for callers that can only carry a bare number (filters, sorts).

    Calibrated inside the fitted domain, raw formula outside it — the same split as
    ``describe_estimate``, so the figure a filter uses and the figure the answer quotes can
    never come from different models for the same pair. Monotone non-decreasing in distance
    across the join (the calibrated branch tops out at 62 minutes at 16.71 km, where the raw
    branch already reads 75).

    A bare number still carries no basis, so this is for internal thresholding only. Anything
    shown to a user goes through ``describe_estimate``.
    """
    if CALIBRATION_MEETS_GATE and mode in CALIBRATED_MODES:
        cal = calibrated_minutes(distance_km)
        if cal is not None:
            return int(round(cal))
    return legacy_straight_line_minutes(distance_km, mode)


def describe_estimate(minutes: float | None, distance_km: float | None = None,
                      mode: str = "transit") -> dict:
    """The full self-describing payload for a straight-line commute guess.

    Always returns ``basis``/``basis_note``/``caveat``/``estimate_model``.
    ``estimated_duration_minutes`` is ``None`` when the figure is refused, and there is never a
    ``duration_minutes`` key — that field is reserved for a measured journey plan.

    ``minutes`` is the RAW formula output. Where the calibration's domain conditions hold it is
    replaced by the fitted figure; where they do not it is used as-is under the first pass's
    15-minute floor. ``mode`` defaults to transit because ``maps_service`` does not pass one;
    see ``withdraw_uncalibrated_mode`` for how the tool layer corrects that.
    """
    km_known = isinstance(distance_km, (int, float)) and not isinstance(distance_km, bool)
    km_txt = f"{distance_km:.2f} km" if km_known else "the"
    cal = _calibration_applies(minutes, distance_km, mode)

    if cal is not None:
        # Band from the INTEGER figure that is actually published, not from the unrounded
        # model output: a reader who multiplies the quoted 11 minutes by 0.84 and 1.24 has to
        # land on the quoted 9-14, or the range is one more number nobody can check.
        est_minutes = int(round(cal))
        low, high = calibrated_band(est_minutes)
        raw = legacy_straight_line_minutes(distance_km, "transit")
        # State the correction's direction and size for THIS pair rather than a headline figure:
        # the raw formula is low at short range and HIGH past about 9 km, and "low by up to 6x"
        # would be simply false at the top of the range.
        factor = (cal / raw) if raw else None
        direction = (
            f"the raw straight-line formula reads {raw} minutes here, which the fit corrects "
            + (f"UP by {factor:.2f}x" if factor and factor >= 1
               else f"DOWN by {1 / factor:.2f}x" if factor else "")
            + ". " if raw else "")
        note = (
            f"Estimated from {km_txt} straight-line distance because TfL returned no journey for "
            f"this pair, then corrected by the model fitted to 14 measured TfL journeys "
            f"(t = {CAL_OVERHEAD_MINUTES} + {CAL_PACE_COEFFICIENT} x km^{CAL_DISTANCE_EXPONENT}); "
            f"{direction}Plausible range {low}-{high} minutes "
            f"({CALIBRATED_RATIO_LOW}x-{CALIBRATED_RATIO_HIGH}x, the fit's residual spread). Not a "
            f"measured journey time."
        )
        return {
            "basis": BASIS_STRAIGHT_LINE,
            "estimate_model": CALIBRATED_MODEL_ID,
            "estimated_duration_minutes": est_minutes,
            "estimate_low_minutes": low,
            "estimate_high_minutes": high,
            "straight_line_km": distance_km,
            "basis_note": note,
            "caveat": CALIBRATED_CAVEAT_EN,
        }

    band = estimate_band(minutes)
    if band is None:
        # Two distinct reasons to refuse, and they license different sentences. Reporting the
        # wrong one is how a reader ends up thinking a short hop is unanswerable in principle.
        if minutes is None:
            why = (
                "no straight-line figure could be produced for it either, so there is nothing "
                "to calibrate or to caveat"
            )
        elif km_known and float(distance_km) < CALIBRATED_MIN_KM:
            why = (
                f"this pair is {km_txt} apart, closer than the shortest pair anyone measured "
                f"({CALIBRATED_MIN_KM} km, which the fitted model puts at "
                f"{MIN_CALIBRATED_ESTIMATE_MINUTES} minutes). Below that the model is "
                f"extrapolating past its data, and nothing measured says what a shorter hop "
                f"really takes"
            )
        else:
            why = (
                f"the straight-line fallback produced an uncalibrated figure below "
                f"{MIN_TRUSTWORTHY_ESTIMATE_MINUTES} minutes, which measurement shows is wrong by "
                f"1.8x-6x for short hops (a 0.5 km central-London pair estimates at 2 minutes; the "
                f"real journey is 11-12)"
            )
        note = (
            "No commute time could be established. TfL returned no journey for this pair, and "
            f"{why}. No number is given rather than a wrong one."
        )
        return {
            "basis": BASIS_STRAIGHT_LINE,
            "estimate_model": None,
            "estimated_duration_minutes": None,
            "estimate_low_minutes": None,
            "estimate_high_minutes": None,
            "straight_line_km": distance_km,
            "basis_note": note,
            "caveat": CAVEAT_EN,
        }
    low, high = band
    note = (
        f"Estimated from {km_txt} straight-line distance because TfL returned no journey for "
        f"this pair. UNCALIBRATED — {_why_uncalibrated(minutes, distance_km, mode)}, so the raw "
        f"formula's figure stands with its wider measured band. Plausible range {low}-{high} "
        f"minutes. Not a measured journey time."
    )
    return {
        "basis": BASIS_STRAIGHT_LINE,
        "estimate_model": None,
        "estimated_duration_minutes": int(round(float(minutes))),
        "estimate_low_minutes": low,
        "estimate_high_minutes": high,
        "straight_line_km": distance_km,
        "basis_note": note,
        "caveat": CAVEAT_EN,
    }


def withdraw_uncalibrated_mode(payload: dict | None, mode: str) -> dict | None:
    """Undo a calibrated figure that was produced without knowing the travel mode.

    ``maps_service.calculate_travel_details`` calls ``describe_estimate`` without passing its
    ``mode``, so a cycling or driving request can be handed the public-transport calibration.
    Up to 2.71 km the raw cycling and transit formulas agree to within
    ``LEGACY_MINUTES_TOLERANCE``, so the guard inside ``_calibration_applies`` cannot separate
    them from the minutes alone — but the TOOL layer knows the mode, and this is where it says
    so. The calibrated figure is replaced by the first pass's uncalibrated treatment of the same
    distance, which for a short cycling trip means the 15-minute floor refuses it again.

    Returns ``payload`` unchanged when there is nothing to withdraw. Never raises.
    """
    if not isinstance(payload, dict):
        return payload
    if payload.get("estimate_model") != CALIBRATED_MODEL_ID:
        return payload
    if mode in CALIBRATED_MODES:
        return payload
    km = payload.get("straight_line_km")
    out = dict(payload)
    out.update(describe_estimate(legacy_straight_line_minutes(km, mode), km, mode=mode))
    return out


def describe_measured(minutes: int, source: str = "TfL Journey Planner") -> dict:
    """The payload for a real journey plan: the one case where ``duration_minutes`` is set."""
    return {
        "basis": BASIS_MEASURED,
        "duration_minutes": int(minutes),
        "basis_note": (
            f"Measured by the {source}: the fastest itinerary it returned for these two points, "
            "including walking legs and interchanges."
        ),
        "caveat": None,
    }
