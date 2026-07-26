"""Authoritative UK tenancy money rules the product OWNS rather than looks up.

WHY THIS FILE EXISTS
--------------------
Until now the statutory deposit cap lived nowhere in the product. It is written down
in ``evaluation/benchmark/README.md`` — i.e. in the *marking scheme*, not in the thing
being marked. So when a user asked a deposit question the model had exactly two ways to
answer: recall it from pretraining, or buy it off the web. Both were observed to fail in
the 2026-07-25 ``fc_loop`` sweep:

* **B14** — "The rent is £1,000 a week. What deposit is the landlord legally allowed to
  take?" The loop ran ``web_search("England tenancy deposit cap 5 weeks rent Housing Act
  2004")``, got Shelter's summary line *"A tenancy deposit cannot be more than 5 weeks'
  rent"*, and led with **£5,000**. That summary omits the threshold. At £1,000/week the
  annual rent is £52,000, which is over the £50,000 line, so the correct cap is six
  weeks — **£6,000**. The model had already done that arithmetic correctly further down
  its own answer and still deferred to the retrieved snippet. A web lookup did not merely
  cost time here; it actively produced the wrong headline number.
* **B8** — "total move-in cost for a £1600 pcm place". Two ``web_search`` calls for the
  "standard deposit", both returning zero results, and a turn that degraded into "Sorry —
  I couldn't retrieve reliable specific figures". The answer is arithmetic on a figure the
  user had already typed.

A statutory constant is not information to be retrieved. It is a rule with a citation and
an effective date, and it belongs in the codebase next to ``safety_reference.py`` — for
the same reason: a number is only defensible when the product can say where it came from.

WHAT THIS IS, AND IS NOT
------------------------
England and Wales only, under the **Tenant Fees Act 2019** (in force 1 June 2019 for new
tenancies, 1 June 2020 for all). Scotland and Northern Ireland have their OWN regimes and
are deliberately NOT modelled — ``deposit_cap`` describes itself as England/Wales in its
own payload so a caller can never quietly generalise it.

It is a statutory cap, i.e. a MAXIMUM. It is not a prediction of what a landlord will
actually ask for, and nothing here should be phrased to a user as "the deposit will be".
"""

from __future__ import annotations

from typing import Optional

# ─── Tenant Fees Act 2019, Schedule 1 ────────────────────────────────
# para 2: tenancy deposit capped at 5 weeks' rent, or 6 weeks' rent where the ANNUAL
#         rent for the tenancy is £50,000 or more.
# para 3: holding deposit capped at 1 week's rent.
JURISDICTION = "England and Wales"
STATUTE = "Tenant Fees Act 2019"
STATUTE_IN_FORCE = "2019-06-01"

DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP = 50_000
DEPOSIT_CAP_WEEKS_BELOW_THRESHOLD = 5
DEPOSIT_CAP_WEEKS_AT_OR_ABOVE_THRESHOLD = 6
HOLDING_DEPOSIT_CAP_WEEKS = 1

WEEKS_PER_YEAR = 52
MONTHS_PER_YEAR = 12


def weekly_from_monthly(monthly_rent: float) -> float:
    """UK convention: a monthly rent is an annual rent in twelfths, so the weekly
    equivalent is ``monthly * 12 / 52`` — NOT ``monthly / 4``."""
    return monthly_rent * MONTHS_PER_YEAR / WEEKS_PER_YEAR


def monthly_from_weekly(weekly_rent: float) -> float:
    return weekly_rent * WEEKS_PER_YEAR / MONTHS_PER_YEAR


def deposit_cap_weeks(annual_rent: float) -> int:
    """Weeks of rent the statutory cap allows for ``annual_rent``.

    The threshold is inclusive at £50,000 (Sch.1 para 2: "£50,000 or more" takes the
    six-week cap), which is exactly the edge B14 sits on — £1,000/week is £52,000/year.
    """
    return (DEPOSIT_CAP_WEEKS_AT_OR_ABOVE_THRESHOLD
            if annual_rent >= DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP
            else DEPOSIT_CAP_WEEKS_BELOW_THRESHOLD)


def deposit_cap(*, weekly_rent: Optional[float] = None,
                monthly_rent: Optional[float] = None) -> dict:
    """Every figure a deposit / move-in answer needs, derived from ONE stated rent.

    Exactly one of ``weekly_rent`` / ``monthly_rent`` is required (the caller knows which
    period the user typed; guessing is how the 5-vs-6 week trap gets sprung). Returns a
    flat dict of named, rounded-to-the-penny figures plus the rule that produced them, so
    the consumer states components rather than a bare total.
    """
    if (weekly_rent is None) == (monthly_rent is None):
        raise ValueError("deposit_cap() needs exactly one of weekly_rent / monthly_rent")
    if weekly_rent is None:
        weekly = weekly_from_monthly(float(monthly_rent))
        monthly = float(monthly_rent)
        stated = "monthly"
    else:
        weekly = float(weekly_rent)
        monthly = monthly_from_weekly(float(weekly_rent))
        stated = "weekly"
    annual = weekly * WEEKS_PER_YEAR
    weeks = deposit_cap_weeks(annual)
    deposit = weekly * weeks

    def _p(v: float) -> float:
        return round(v + 0.0, 2)

    return {
        "jurisdiction": JURISDICTION,
        "statute": STATUTE,
        "stated_rent_period": stated,
        "weekly_rent_gbp": _p(weekly),
        "monthly_rent_gbp": _p(monthly),
        "annual_rent_gbp": _p(annual),
        "deposit_cap_weeks": weeks,
        "deposit_cap_threshold_annual_gbp": DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP,
        "max_tenancy_deposit_gbp": _p(deposit),
        "max_holding_deposit_gbp": _p(weekly * HOLDING_DEPOSIT_CAP_WEEKS),
        "first_month_plus_deposit_gbp": _p(monthly + deposit),
        "rule": (
            f"{STATUTE} (Sch.1 para 2), {JURISDICTION}: the tenancy deposit is capped at "
            f"{DEPOSIT_CAP_WEEKS_BELOW_THRESHOLD} weeks' rent when the ANNUAL rent is under "
            f"£{DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP:,}, and "
            f"{DEPOSIT_CAP_WEEKS_AT_OR_ABOVE_THRESHOLD} weeks' rent when the annual rent is "
            f"£{DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP:,} or more. Weekly rent = monthly x 12 / 52. "
            f"A holding deposit is capped separately at "
            f"{HOLDING_DEPOSIT_CAP_WEEKS} week's rent."
        ),
        "caveat": (
            "These are statutory MAXIMA, not a prediction of what this landlord will ask "
            "for. Scotland and Northern Ireland have separate rules."
        ),
    }
