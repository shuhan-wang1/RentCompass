"""The price-grounding pool must not be seeded by unrelated quantities (2026-07-26).

THE DEFECT. ``unsupported_reply_prices`` built its "supported" pool from *every bare
number in the serialized evidence*, then multiplied each by 1‑36 plus week/month
conversions and 1‑8-week deposit multiples, and matched with a purely RELATIVE 1 %
tolerance. On a nearest-station turn the evidence carries distances in metres, so the
three numbers {214, 635, 665} produced a 129-value pool in which these fabricated rents
all read as "supported" — none of them in the data, none of them a rent at all::

    £9999 = 665 x 15 = 9975      £4321 = 214 x 20 = 4280
    £3999 = 665 x  6 = 3990      £7777 = 214 x 36 = 7704
    £2345 = 214 x 11 = 2354      £5050 = 635 x  8 = 5080
    £1500 = 214 x  7 = 1498      £2000 = 665 x  3 = 1995

Two independent failures compose here, and both are pinned below:

1. a metre seeded a rent derivation at all (the pool was not unit- or key-aware);
2. a 1 % RELATIVE tolerance on a pool that grows multiplicatively covers over a THIRD of
   every value below the top multiple, so large figures are almost always "supported".

THE ASYMMETRY. The critic gates real answers in production, so the second half of this
file measures the other direction: hedged estimates and statutory thresholds must stay
grounded. Both directions were measured against the 196 retained (answer, evidence)
pairs of the 2026-07-25 internal round before this change shipped; the cases named
``sweep/...`` / ``sweep-legacy/...`` below are verbatim from that corpus.
"""

from __future__ import annotations

import pytest

from uk_rent_agent.agent.critic import (
    _MAX_ABS_TOL,
    _asserts_price,
    _derivations,
    _evidence_money_mentions,
    _is_money_key,
    evaluate_grounding,
    unsupported_reply_prices,
)

# The literal turn from the report: TfL nearest-station distances, in metres.
NEAREST_STATION_EVIDENCE = {
    "nearest_stations": [
        {"name": "Russell Square", "distance_m": 214},
        {"name": "Holborn", "distance_m": 635},
        {"name": "Goodge Street", "distance_m": 665},
    ]
}

# value -> the metre arithmetic that used to ground it.
DEFEATING_ARITHMETIC = {
    9999: "665 x 15 = 9975",
    4321: "214 x 20 = 4280",
    3999: "665 x  6 = 3990",
    7777: "214 x 36 = 7704",
    2345: "214 x 11 = 2354",
    5050: "635 x  8 = 5080",
    1500: "214 x  7 = 1498",
    2000: "665 x  3 = 1995",
}


# ── direction 1: the fabrications must be caught ───────────────────────────────

def test_metres_do_not_ground_a_rent_the_pinned_9999_case():
    """THE regression pin. £9,999 vs 665 m x 15 = 9,975 — within the old 1 % tolerance."""
    reply = "The nearest station is Russell Square. The rent is £9,999 pcm."
    assert unsupported_reply_prices(reply, NEAREST_STATION_EVIDENCE) == [9999.0]
    assert evaluate_grounding(reply, NEAREST_STATION_EVIDENCE).grounded is False


@pytest.mark.parametrize("value,arithmetic", sorted(DEFEATING_ARITHMETIC.items()))
def test_every_documented_metre_derived_fabrication_is_caught(value, arithmetic):
    reply = f"The rent is £{value:,} pcm."
    assert unsupported_reply_prices(reply, NEAREST_STATION_EVIDENCE) == [float(value)], (
        f"£{value} was grounded by {arithmetic} — a distance in metres is not a rent")


def test_a_distance_key_contributes_nothing_to_the_money_pool():
    """Source guard, not a promise: the pool is empty for a distances-only payload, so no
    multiplier range and no tolerance can conjure a supported rent out of it."""
    assert _evidence_money_mentions(NEAREST_STATION_EVIDENCE) == []


def test_the_leak_rate_on_a_metre_only_turn_is_zero():
    """Measures the defect instead of sampling it. Against the metre-only evidence of a
    real nearest-station turn, the old rule certified 5,007 of the 9,000 integer rents from
    £1,000 to £9,999 — 55.6 % — as "supported"; the eight figures in the report were not
    special, they were a coin flip. ``test_critic_grounding``'s
    ``test_the_issue_is_surfaced_the_same_way_unsupported_prices_is`` asserts the right
    thing against this same fixture but happened to pick £1,234, one of the 44.4 % that did
    not leak, so the suite documented the contract without ever exercising the hole.
    A rate, not a list, so no future widening can pass by dodging specific literals."""
    leaked = [value for value in range(1000, 10000)
              if not unsupported_reply_prices(f"The rent is £{value:,} pcm.",
                                              NEAREST_STATION_EVIDENCE)]
    assert leaked == [], f"{len(leaked)} fabricated rents still ground on metres"


@pytest.mark.parametrize("key", [
    "distance_m", "distance_miles", "radius_m", "duration_minutes", "commute_minutes",
    "max_travel_time", "safety_score", "score", "similarity_score", "total_crimes_6m",
    "most_recent_month_count", "bedrooms", "rank", "count", "perfect_count",
    "soft_count", "total_found", "total_matches", "hits", "misses", "lat", "lon",
    "from_zone", "to_zone", "severity", "freshness", "importance", "current_page",
])
def test_non_money_keys_are_not_money(key):
    """Every one of these is a real numeric-leaf key in the retained round's evidence.
    ``current_page`` is the segment-vs-substring trap: it CONTAINS "rent"."""
    assert _is_money_key(key) is False, key


@pytest.mark.parametrize("key", [
    "price", "price_raw", "monthly_price", "monthly_cost", "monthly_gbp", "weekly_gbp",
    "max_budget", "suggested_budget", "budget_increase_needed", "daily_cap",
    "daily_cap_gbp", "daily_off_peak_cap", "fare_gbp", "fare_pence", "monthly_rent",
    "weekly_rent", "rent", "deposit", "total_move_in_cost", "maxBudget", "rent_pcm",
])
def test_money_keys_still_seed_the_pool(key):
    """The legitimate reason the bare-number path exists: numeric JSON price fields."""
    assert _is_money_key(key) is True, key
    assert _evidence_money_mentions({key: 1500}) == [(1500.0, "unknown")]


def test_the_tolerance_cannot_widen_with_magnitude():
    """1 % of £9,090 is £90.90 — so the OLD purely-relative rule read a £90 miss on a
    £9,000 rent as a match. The cap makes the tolerance magnitude-independent."""
    evidence = {"monthly_price": 9000}
    assert 9090 - 9000 <= 0.01 * 9090, "the old relative rule really did ground £9,090"
    assert 90.0 > _MAX_ABS_TOL
    assert unsupported_reply_prices("The rent is £9,090 pcm.", evidence) == [9090.0]
    # ...while a figure inside the absolute cap is still grounded.
    assert unsupported_reply_prices("The rent is £9,009 pcm.", evidence) == []


@pytest.mark.parametrize("reply", [
    "The rent is £9,999 pcm.",
    "Price: £9,999 pcm",
    "- **Price:** GBP 9,999/month",
    "This flat costs £9,999 per month.",
    # A hedge word that does NOT touch the figure must not launder it. Each of these
    # would be spared by a bare-word context window like the grader's.
    "Here's some information about the flat: the rent is £9,999 pcm.",
    "The rent is £9,999 pcm, which is above the average for the area.",
    "The monthly rent is £9,999 and the range of amenities is excellent.",
    "Rent: £9,999 pcm. Tell me if you'd like to know about nearby stations.",
    "The rent is £9,999 pcm (a typical Zone 1 flat).",
    # ...and a hedge ADVERB that does touch it is still not an exemption: an adverb
    # changes how confidently the sentence claims, not what it claims. This is the
    # shape of tests/test_fc_critic.py::test_fc_artifacts_make_retrieval_expected.
    "Zone-2 rents are around £9,999 pcm.",
    "The rent is about £9,999 pcm.",
    "Rents there are typically £9,999 pcm.",
    "The rent is roughly £9,999 pcm.",
    "Rents run at approximately £9,999 pcm.",
    "The rent is ~£9,999 pcm.",
])
def test_an_asserted_fabricated_price_is_never_laundered_by_a_hedge(reply):
    assert unsupported_reply_prices(reply, NEAREST_STATION_EVIDENCE) == [9999.0], reply
    assert _asserts_price(reply, 9999.0) is True


def test_with_no_money_in_the_evidence_nothing_is_exempt():
    """Fail-closed. The bound/interval exemption presupposes retrieved data for the figure
    to be context alongside; on zero usable evidence there is none, and the critic node's
    deterministic no-reliable-data 兜底 only fires on a NOT-grounded verdict. Pinned
    because that 兜底 is reached through this function's verdict, not independently."""
    for evidence in (None, "", "No search results found for this query.",
                     {"nearest_stations": [{"distance_m": 214}]}):
        assert unsupported_reply_prices(
            "Rents are typically around £1,400-£1,500/month.", evidence) == [1400.0, 1500.0]
        assert unsupported_reply_prices(
            "The maximum deposit applies for annual rent under £50,000.",
            evidence) == [50000.0]


# ── direction 2: honest answers must stay grounded ─────────────────────────────
# Every case below is a real answer from the 196 retained pairs. A newly-flagged real
# answer is a regression, so these are the ones the tightening is not allowed to break.

def test_statutory_deposit_threshold_is_not_a_price_claim():
    """sweep/B7, sweep/B10, sweep/B14, sweep-legacy/B10, sweep-legacy/B15 — £50,000 is the
    Tenant Fees Act 2019 deposit-cap boundary. It appears in 5 of the 196 retained answers
    and no listing ever supplies it. Caveating it with "double-check the exact prices
    against the source listing" points the reader at the one thing never in doubt."""
    evidence = {"recommendations": [{"monthly_price": 4200}]}
    for reply in (
        "The maximum deposit is 5 weeks' rent for annual rent under £50,000.",
        "Under the Tenant Fees Act 2019 the cap is five weeks' rent, provided the "
        "annual rent is less than £50,000.",
        "If the annual rent is £50,000 or more, the cap is 6 weeks' rent instead.",
        "The annual rent here is £50,400, which is above £50,000.",
        "Since this exceeds £50,000, the deposit cap increases to six weeks' rent.",
    ):
        assert 50000.0 not in unsupported_reply_prices(reply, evidence), reply


@pytest.mark.parametrize("reply", [
    # sweep/E11
    "Studios in Stratford typically start around £1,400-£1,500/month.",
    # sweep/F1
    "A holding deposit is often around £250-£500.",
    # sweep/B13
    "If bills are included, that adds roughly £150-£250/month in value.",
    # sweep/F9 — and the "75/100" in the same answer must not stand in for the "£100".
    "Safety Score: 75/100. Zone 2, so a monthly Travelcard is roughly £100-110.",
    # sweep/A12
    "Camden or Euston can bring prices down to the **GBP 1,300 - 1,600/month** range.",
    # sweep/B3
    "So you should expect to pay around **£1,730 - £1,750** as a deposit.",
    # sweep/B4
    "The total move-in cost is typically around **£3,500 - £3,600**.",
])
def test_explicit_intervals_are_not_gated(reply):
    """An interval is a market estimate, not a claim that the retrieved listing costs this.
    Structural, not vocabulary: it is the "£A - £B" shape that exempts, so ordinary prose
    cannot satisfy it (see test_an_asserted_fabricated_price_is_never_laundered_by_a_hedge)."""
    evidence = {"recommendations": [{"monthly_price": 1500}]}
    assert unsupported_reply_prices(reply, evidence) == [], reply


def test_annual_rent_from_a_weekly_rent_is_a_derivation_not_a_fabrication():
    """sweep-legacy/B1 and sweep-legacy/B9: "£350 per week x 52 = £18,200 a year" and
    "£475 x 52 = £24,700". x 52 is a first-class UK rent figure that the old x 1-36 range
    could not express, so both answers were flagged as unsupported — they only escaped
    because the relative tolerance happened to be hundreds of pounds wide at that
    magnitude. Pinned so a future narrowing of the multiplier set cannot silently
    reintroduce the false positive."""
    assert 18200.0 in _derivations(350.0, "weekly")
    assert 24700.0 in _derivations(475.0, "unknown")
    reply = "£350 per week multiplied by 52 weeks in a year gives an annual rent of £18,200."
    assert unsupported_reply_prices(reply, {"weekly_gbp": 350}) == []


def test_a_deposit_derived_with_the_4_35_week_convention_stays_grounded():
    """sweep-legacy/B3 — the widest legitimate rounding drift in the retained corpus, and
    the case that sets _MAX_ABS_TOL. The answer shows its work using 4.35 weeks/month
    instead of 52/12: £1,500 / 4.35 x 5 = £1,724.15 against the sanctioned £1,730.77, a
    £6.63 gap. A £5 cap would flag it; £10 keeps it."""
    reply = ("The weekly rent would be approximately £1,500 / 4.35 = £344.83. The maximum "
             "deposit would then be 5 weeks x £344.83 = £1,724.15.")
    assert unsupported_reply_prices(reply, {"max_budget": 1500}) == []
    assert abs(1724.15 - 1500 * 12 / 52 * 5) <= _MAX_ABS_TOL


def test_currency_marked_evidence_grounds_regardless_of_its_key():
    """Key-scoping must not break the path that grounds free-text evidence: a "£"/period
    marker IS the unit evidence, so it counts from any key — including none at all."""
    assert unsupported_reply_prices("The rent is £2,678 pcm.", "2678 pcm") == []
    assert unsupported_reply_prices(
        "The rent is £1,500 pcm.",
        {"explanation": "One-bed flat. Headline rent £1500 pcm."}) == []
    assert unsupported_reply_prices(
        "The rent is £2,900 pcm.",
        ["=== PREVIOUSLY SHOWN PROPERTIES ===\nPrice: £2,900 pcm"]) == []


def test_a_bare_number_on_a_money_labelled_prose_line_still_grounds():
    """The assembled context is a rendered STRING, so a price there has no dict key to
    read — "Price: 2,900" must still ground, while "Base Score: 100" must not."""
    context = "=== Current Property Context ===\nPrice: 2,900\nSafety Score: 100"
    assert unsupported_reply_prices("The rent is £2,900 pcm.", context) == []
    assert unsupported_reply_prices("A monthly Travelcard is £100.", context) == [100.0]
