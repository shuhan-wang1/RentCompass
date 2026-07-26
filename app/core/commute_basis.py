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

THE TWO RULES THAT FALL OUT
---------------------------
1. Below ``MIN_TRUSTWORTHY_ESTIMATE_MINUTES`` the estimator produces no number at all.
   A 2-minute answer for a 12-minute journey is not an imprecise measurement, it is a wrong
   one, and the same reasoning as ``safety_reference.MIN_PLAUSIBLE_MONTHLY`` applies: an
   implausible output means the method did not work here, not that the journey is quick.
2. Above it the number is returned in ``estimated_duration_minutes`` — NEVER in
   ``duration_minutes`` — accompanied by a range from the measured error and a caveat.
   ``duration_minutes`` means "a journey planner measured this" and nothing else.

THE LIMITATION THAT MUST TRAVEL WITH THE NUMBER
-----------------------------------------------
The estimator only ever runs when TfL returned no journey, which in practice means OUTSIDE
TfL coverage (Manchester, Leeds, ...) — and outside TfL coverage there is, by definition, no
TfL reference to calibrate against. So this calibration measures the estimator's *formula*
against real public-transport journeys in the one city where a reference exists. It is the
best available check, not a validation in the domain where the fallback actually fires. That
limitation is part of ``CAVEAT_EN`` / ``CAVEAT_ZH`` and must be stated with the number.
"""

from __future__ import annotations

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

# Below this many ESTIMATED minutes the straight-line figure is withheld entirely.
# Justification, from CALIBRATION: every sampled pair that estimated under 15 minutes was
# low by >= 1.78x, and the two sub-kilometre pairs were low by 5.5x and 6.0x. There is no
# band wide enough to make "2 minutes" an honest rendering of a 12-minute journey.
MIN_TRUSTWORTHY_ESTIMATE_MINUTES = 15

# Multiplicative band for estimates at or above the floor, from the observed ratio range
# of tfl/estimate over the CALIBRATION rows with estimator_minutes >= 15
# (min 0.747 at Croydon, max 1.412 at Shoreditch). Rounded OUTWARD, so the band is never
# narrower than the evidence it is drawn from.
ESTIMATE_RATIO_LOW = 0.74
ESTIMATE_RATIO_HIGH = 1.45

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


def describe_estimate(minutes: float | None, distance_km: float | None = None) -> dict:
    """The full self-describing payload for a straight-line commute guess.

    Always returns ``basis``/``basis_note``/``caveat``. ``estimated_duration_minutes`` is
    ``None`` when the figure is refused, and there is never a ``duration_minutes`` key —
    that field is reserved for a measured journey plan.
    """
    band = estimate_band(minutes)
    km_txt = f"{distance_km:.2f} km" if isinstance(distance_km, (int, float)) else "the"
    if band is None:
        note = (
            "No commute time could be established. TfL returned no journey for this pair, and "
            f"the straight-line fallback produced a figure below {MIN_TRUSTWORTHY_ESTIMATE_MINUTES} "
            "minutes, which measurement shows is wrong by 1.8x-6x for short hops (a 0.5 km "
            "central-London pair estimates at 2 minutes; the real journey is 11-12). No number "
            "is given rather than a wrong one."
        )
        return {
            "basis": BASIS_STRAIGHT_LINE,
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
        f"this pair. Plausible range {low}-{high} minutes. Not a measured journey time."
    )
    return {
        "basis": BASIS_STRAIGHT_LINE,
        "estimated_duration_minutes": int(round(float(minutes))),
        "estimate_low_minutes": low,
        "estimate_high_minutes": high,
        "straight_line_km": distance_km,
        "basis_note": note,
        "caveat": CAVEAT_EN,
    }


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
