"""Reference distribution for turning a police.uk crime count into a defensible band.

WHY THIS FILE EXISTS
--------------------
The previous scoring was ``safety_score = max(0, 100 - total_crimes // 2)``. It had no
normalisation of any kind, so the number it produced was not a property of the area — it
was a property of how many rows the API happened to return. Combined with the wrong
endpoint (``crimes-at-location``, a single street anchor, instead of ``crimes-street``,
a ~1 mile radius) it reported Hackney Central as **9 crimes in six months, 96/100, "very
safe, better than the London average"** when the real figure for one month at that point is
**1,657**. A student could have chosen an area on an inverted signal.

Fixing the endpoint alone is not enough: with correct radius data every London area lands
at ``100 - 9942//2`` = 0, i.e. "dangerous" everywhere, which is wrong in the other
direction. A count only means something against a reference, so here is one.

WHAT THIS REFERENCE IS, AND IS NOT
----------------------------------
Fourteen points sampled from ``crimes-street/all-crime`` for a single month, covering
central London, inner London, outer London and four non-London student areas. Collected by
``scripts/sample_safety_reference.py`` so the numbers are reproducible rather than asserted.

It is a **small, fixed, dated sample**, not a national distribution. It is good enough to
say "busier than most places we sampled" and NOT good enough to say "the 87th percentile of
UK neighbourhoods". The band names are worded accordingly.

THE LIMITATION THAT MUST TRAVEL WITH THE NUMBER
-----------------------------------------------
Counts are footfall-sensitive. Bloomsbury (4,600) scores far worse than Hackney (1,657)
because central districts accumulate theft and shoplifting from visitors, which is not the
same as residential risk for someone living there. Any answer built on this must carry that
caveat — see ``CAVEAT_EN`` / ``CAVEAT_ZH``. Reporting a bare rank without it would replace
one misleading signal with another.
"""

from __future__ import annotations

# Monthly crime counts within the ~1 mile radius that crimes-street/all-crime covers.
# Sampled 2026-07-26 for month 2026-05 (the latest the API had).
REFERENCE_MONTH = "2026-05"
REFERENCE_SAMPLED_ON = "2026-07-26"
REFERENCE_RADIUS_MILES = 1.0

REFERENCE_COUNTS: dict[str, int] = {
    "Richmond, London": 258,
    "Wimbledon, London": 332,
    "Headingley, Leeds": 403,
    "Selly Oak, Birmingham": 469,
    "Clifton, Bristol": 763,
    "South Kensington, London": 1185,
    "Peckham, London": 1275,
    "Stratford, London": 1497,
    "Camden Town, London": 1570,
    "Hackney Central, London": 1657,
    "Brixton, London": 1688,
    "Shoreditch, London": 2581,
    "Bloomsbury, London": 4600,
}
# Fallowfield, Manchester returned 0 rows in the same sweep. It is DELIBERATELY excluded:
# a zero from this API means "no rows published for that point/month", not "no crime", and
# admitting it to the reference would drag the whole scale toward a value that is an
# artefact. The same reasoning is why score_from_monthly_count() refuses to score a zero.

_SORTED = sorted(REFERENCE_COUNTS.values())

# Defence in depth. Fixing the endpoint stops the incident at its source, but the scoring
# function must not be able to reproduce it on its own: the value the user was shown was
# ~9 crimes for the period, and a naive percentile would rank that as "quieter than
# everywhere we sampled" -- 99/100 -- which is the same wrong answer by a new route.
#
# The quietest area in the reference is Richmond at 258/month within a ~1 mile radius. A
# populated UK area cannot plausibly return single or low double digits; such a value means
# the fetch was incomplete, not that the neighbourhood is calm. Below this floor we refuse
# to score at all. Refusing on a genuinely quiet rural point is the safe direction to err.
MIN_PLAUSIBLE_MONTHLY = 30

CAVEAT_EN = (
    "Counts come from data.police.uk within about a 1 mile radius and are footfall-sensitive: "
    "busy central and nightlife districts record a lot of theft from visitors, which is not the "
    "same as risk to someone living there. Use this as one input, not a verdict."
)
CAVEAT_ZH = (
    "数据来自 data.police.uk 约 1 英里半径内的犯罪记录，且受人流量影响：市中心和夜生活密集的区域"
    "会记录大量针对访客的盗窃，这与居住在当地的风险不是一回事。请把它当作参考之一，而不是结论。"
)


def score_from_monthly_count(monthly_count: float | None) -> tuple[int | None, str | None]:
    """Map an average monthly crime count to ``(score, band)`` against the reference.

    Returns ``(None, None)`` when there is nothing trustworthy to score — missing, zero, or
    implausibly small (see ``MIN_PLAUSIBLE_MONTHLY``). A tiny count is NOT low crime; it is
    an incomplete answer from the API, and the caller must say so rather than present a
    number. This is the specific failure the old code made: it turned "we got almost no
    rows" into "96/100, very safe".

    The score is the reference ECDF inverted, clamped to 1..99 so it never claims a
    certainty this sample cannot support.
    """
    if monthly_count is None or monthly_count < MIN_PLAUSIBLE_MONTHLY:
        return None, None

    below = sum(1 for v in _SORTED if v < monthly_count)
    pct_rank = below / len(_SORTED)                 # 0.0 = quietest, 1.0 = busiest
    score = int(round(100 * (1.0 - pct_rank)))
    score = max(1, min(99, score))

    if pct_rank <= 0.25:
        band = "quieter than most areas we sampled"
    elif pct_rank <= 0.50:
        band = "around the middle of the areas we sampled"
    elif pct_rank <= 0.75:
        band = "busier than most areas we sampled"
    else:
        band = "among the busiest areas we sampled"
    return score, band


def reference_note() -> str:
    """One line naming the basis, so an answer can cite what it compared against."""
    return (f"compared against {len(REFERENCE_COUNTS)} sampled UK areas "
            f"({REFERENCE_MONTH} data.police.uk, ~{REFERENCE_RADIUS_MILES:g} mile radius)")
