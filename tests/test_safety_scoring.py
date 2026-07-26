"""The 2026-07-26 safety-score defect, pinned as regressions.

Live on the public site, the tool told a user Hackney Central had **9 crimes in six
months**, scored it **96/100 "Very Safe"** and added "better than the London average".
Measured against the same source the same day: **1,657 crimes in ONE month** at those
coordinates. Two independent causes, and either alone still produces a wrong answer:

  1. `crimes-at-location` returns crimes at a single pre-defined street anchor, not an area.
  2. `max(0, 100 - total // 2)` has no normalisation, so with correct radius data every
     London area collapses to 0 -- wrong in the opposite direction.

The tests below fail on the old behaviour in both directions.
"""
from __future__ import annotations

import inspect

from core.safety_reference import (
    CAVEAT_EN,
    REFERENCE_COUNTS,
    score_from_monthly_count,
)


# --------------------------------------------------------------------------- #
# 1. The endpoint. This is the root cause and it is a one-token regression.    #
# --------------------------------------------------------------------------- #

def test_crime_lookup_uses_the_radius_endpoint_not_the_single_anchor():
    from core import maps_service
    src = inspect.getsource(maps_service.get_crime_data_by_location)
    # Strip comments and docstring prose: both legitimately name the retired endpoint in
    # order to explain why it is retired. Only executable code is asserted on.
    code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert "crimes-street/all-crime" in code, (
        "crime lookup must use the ~1 mile radius endpoint")
    assert "crimes-at-location" not in code, (
        "crimes-at-location returns ONE street anchor; it under-reports an area by ~1000x "
        "and is what produced '9 crimes in six months' for Hackney Central")


# --------------------------------------------------------------------------- #
# 2. The incident itself, both directions.                                     #
# --------------------------------------------------------------------------- #

def test_hackney_real_volume_is_not_reported_as_very_safe():
    """1,657/month is what data.police.uk actually returns for Hackney Central."""
    score, band = score_from_monthly_count(1657)
    assert score is not None
    assert score < 80, f"1,657 crimes/month must not land in 'Very Safe' (got {score})"
    assert band is not None


def test_the_old_formula_would_have_failed_this_test():
    """Guard the guard: confirm the retired formula really does produce the bad answer."""
    old = lambda n: max(0, 100 - int(n) // 2)          # noqa: E731
    assert old(9) == 96                                # the number the user was shown
    assert old(1657 * 6) == 0                          # and what correct data would have given
    # i.e. neither the old endpoint nor the old scale can be fixed alone.


def test_correct_data_does_not_collapse_every_area_to_zero():
    """The second failure direction: fixing only the endpoint makes everywhere 'dangerous'."""
    scores = [score_from_monthly_count(v)[0] for v in REFERENCE_COUNTS.values()]
    assert all(s is not None and s > 0 for s in scores)
    assert max(scores) - min(scores) >= 40, "the scale must still separate areas"


# --------------------------------------------------------------------------- #
# 3. Absent data must never become a number.                                   #
# --------------------------------------------------------------------------- #

def test_no_data_produces_no_score():
    for absent in (None, 0, 0.0):
        score, band = score_from_monthly_count(absent)
        assert score is None, f"{absent!r} is an absent answer, not a quiet neighbourhood"
        assert band is None


def test_an_implausibly_small_count_is_refused_not_praised():
    """The incident value itself. ~9 crimes for the period is an incomplete fetch, and a
    naive percentile would rank it 99/100 'quieter than everywhere' -- the same wrong
    answer by a new route. Caught by an existing event-loop test that had hard-coded the
    old formula's output."""
    from core.safety_reference import MIN_PLAUSIBLE_MONTHLY
    for tiny in (9, 12, MIN_PLAUSIBLE_MONTHLY - 1):
        assert score_from_monthly_count(tiny) == (None, None), (
            f"{tiny}/month within a 1 mile radius is missing data, not a calm area")
    assert score_from_monthly_count(MIN_PLAUSIBLE_MONTHLY)[0] is not None


def test_zero_is_excluded_from_the_reference_set():
    """Fallowfield returned 0 rows in the same sweep; admitting it would skew the scale."""
    assert 0 not in REFERENCE_COUNTS.values()


# --------------------------------------------------------------------------- #
# 4. Ordering and the caveat that has to travel with the number.               #
# --------------------------------------------------------------------------- #

def test_quieter_areas_score_higher_than_busier_ones():
    richmond = score_from_monthly_count(REFERENCE_COUNTS["Richmond, London"])[0]
    hackney = score_from_monthly_count(REFERENCE_COUNTS["Hackney Central, London"])[0]
    bloomsbury = score_from_monthly_count(REFERENCE_COUNTS["Bloomsbury, London"])[0]
    assert richmond > hackney > bloomsbury


def test_the_footfall_caveat_names_the_confounder():
    """Bloomsbury (4,600) outscores Hackney (1,657) on raw counts because of visitor theft.
    A bare rank without that caveat replaces one misleading signal with another."""
    assert "footfall" in CAVEAT_EN.lower()
    assert "living" in CAVEAT_EN.lower()


def test_score_is_never_a_claim_of_certainty():
    """A 13-point sample cannot support 0 or 100, so the scale is clamped to 1..99.
    Uses the plausibility floor as the low end: anything under it is refused, not scored."""
    from core.safety_reference import MIN_PLAUSIBLE_MONTHLY
    assert score_from_monthly_count(MIN_PLAUSIBLE_MONTHLY)[0] <= 99
    assert score_from_monthly_count(10 ** 9)[0] >= 1
