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

2026-07-26: WHY THE FIGURES ARE NOW ALSO THE ANSWER
---------------------------------------------------
Supplying the figures was not enough. This module existed, and was correct, and the loop
still shipped the wrong number — because it was only ever consulted when the model happened
to reach for a retrieval tool and got denied (``tool_policy.read_tool_denial``). A turn that
called no tool at all never saw it, and B7 / B4 / B14 are exactly those turns:

* **B7** — "For a £4,500 per month flat, how much is the deposit?" The answer *stated the
  £50,000 rule* and then applied the five-week cap anyway: £5,192.31 instead of £6,230.77.
  That is the case's own ``failure_conditions[0]``, verbatim.
* **B14** — same boundary, weekly input. Headline £5,000; the correct £6,000 appeared only
  in a trailing hedge, which is how the case's ``must_mention_value`` passed on a wrong
  headline.
* **B4** — "total move-in cost for a £1500/month place". It printed the correct £1,730.77
  deposit, said the holding deposit is *deducted* from the first month's rent, and then
  **added** it anyway: £3,230.77 + £346.15 = £3,576.92, quoted as "£3,500 - £3,600" against
  a reference of £3,230.77.

In all three the critic returned ``grounded=True, issues=[]``. Nothing downstream can catch
this class: the eval's own fabrication grader treats *both* the five- and the six-week
reading as "derivable" (``graders._derivable``), so a wrong cap is indistinguishable from a
right one to every check in the pipeline.

The conclusion the B7 answer forces is that a rule the model is *told* is a rule the model
can get wrong in the same breath as reciting it. So the arithmetic here is no longer offered
as reference material for a model to apply — for the narrow class of turns that reduce
entirely to it, ``statutory_answer()`` below **is** the answer, emitted deterministically by
``agent_loop.guard_node`` before any LLM call, alongside the fair-housing refusal and the
refinement-in-place short-circuit that already live there for the same reason.
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


def annual_from(*, weekly_rent: Optional[float] = None,
                monthly_rent: Optional[float] = None) -> float:
    """Annual rent, computed from the period the user ACTUALLY stated.

    Not via the weekly equivalent. ``monthly * 12 / 52 * 52`` is not ``monthly * 12`` in
    binary floating point — for B7 it is ``54000.00000000001`` — and the whole cap turns on
    a comparison against 50,000. Deriving the annual figure the way the statute and
    ``benchmark/README.md`` both state it (``annual_rent = monthly_rent * 12``) keeps the
    threshold test, the reported annual rent, and the marking scheme arithmetically
    identical instead of merely close.
    """
    if (weekly_rent is None) == (monthly_rent is None):
        raise ValueError("annual_from() needs exactly one of weekly_rent / monthly_rent")
    if monthly_rent is not None:
        return float(monthly_rent) * MONTHS_PER_YEAR
    return float(weekly_rent) * WEEKS_PER_YEAR


def deposit_cap_weeks(annual_rent: float) -> int:
    """Weeks of rent the statutory cap allows for ``annual_rent``.

    The threshold is INCLUSIVE at £50,000 (Sch.1 para 2: "£50,000 or more" takes the
    six-week cap). B10 sits £400 above the line (£4,200 pcm -> £50,400) and B14 £2,000
    above it (£1,000/wk -> £52,000); both are six-week cases, and the five-week figure is
    the trap each is built around.

    Compared to the penny, not to the float. An annual rent is a money amount, so
    ``49_999.999999`` is not a value the comparison should ever have to adjudicate; letting
    a representation artefact decide a statutory cap is how a threshold ends up
    disagreeing with itself between two call sites.
    """
    return (DEPOSIT_CAP_WEEKS_AT_OR_ABOVE_THRESHOLD
            if round(float(annual_rent), 2) >= DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP
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
        annual = annual_from(monthly_rent=monthly)
    else:
        weekly = float(weekly_rent)
        monthly = monthly_from_weekly(float(weekly_rent))
        stated = "weekly"
        annual = annual_from(weekly_rent=weekly)
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


# ─── move-in cost, and the holding-deposit double-count ──────────────
# A holding deposit is NOT a move-in fee. Tenant Fees Act 2019 Sch.2 para 3-8: once the
# tenancy is granted the holding deposit must be applied toward the first rent payment or
# the tenancy deposit (or repaid). It is therefore a PREPAYMENT of money already inside
# ``first month + deposit`` — it moves when you pay, never how much.
#
# B4 is what happens when that distinction is left to prose. The answer said the holding
# deposit "is deducted from the first month's rent" and then added it to the total anyway:
#   1500.00 + 1730.77            = 3230.77   <- correct
#   1500.00 + 1730.77 + 346.15   = 3576.92   <- shipped, as "£3,500 - £3,600"
# The overcount is exactly one week's rent, i.e. exactly the holding-deposit cap, which is
# the signature of a credit counted twice. So the total here is computed from two components
# and the holding deposit is structurally incapable of entering it: it only ever produces
# ``balance_due_at_move_in_gbp``, a SPLIT of the same total.

MOVE_IN_COMPONENTS = ("first_month_rent", "tenancy_deposit")


def move_in_cost(*, weekly_rent: Optional[float] = None,
                 monthly_rent: Optional[float] = None,
                 holding_deposit_gbp: Optional[float] = None) -> dict:
    """Total move-in cost = first month's rent + the capped tenancy deposit. Nothing else.

    ``holding_deposit_gbp`` is a holding deposit the user says they have ALREADY paid. It
    does not change ``total_move_in_gbp`` — see the note above — it is reported as a credit
    and produces ``balance_due_at_move_in_gbp`` (total minus the credit) so an answer can
    state "of which £X is already paid" without ever adding it on. Passing more than the
    one-week statutory cap is not rejected (the user may have been overcharged, which is
    itself worth telling them) but it IS flagged via ``holding_deposit_over_cap``.

    No admin / referencing / inventory / renewal fee appears here, because under the Tenant
    Fees Act 2019 a landlord or agent in England and Wales may not charge one at all. B4's
    other failure condition is "adds fabricated fees"; there is no field to put one in.
    """
    cap = deposit_cap(weekly_rent=weekly_rent, monthly_rent=monthly_rent)

    def _p(v: float) -> float:
        return round(v + 0.0, 2)

    first_month = cap["monthly_rent_gbp"]
    deposit = cap["max_tenancy_deposit_gbp"]
    total = _p(first_month + deposit)

    held = None if holding_deposit_gbp is None else _p(float(holding_deposit_gbp))
    credit = 0.0 if held is None else held

    out = dict(cap)
    out.update({
        "first_month_rent_gbp": first_month,
        "tenancy_deposit_gbp": deposit,
        "total_move_in_components": list(MOVE_IN_COMPONENTS),
        "total_move_in_gbp": total,
        "holding_deposit_paid_gbp": held,
        "holding_deposit_over_cap": (
            held is not None and held > cap["max_holding_deposit_gbp"] + 0.005),
        "balance_due_at_move_in_gbp": _p(total - credit),
        "holding_deposit_note": (
            "A holding deposit is credited against the first rent payment or the tenancy "
            "deposit once the tenancy is granted, so it is already inside the total above. "
            "It reduces what is left to pay; it is never an extra line added to the total."
        ),
    })
    return out


# ─── the deterministic answer ────────────────────────────────────────
def _money(v: float) -> str:
    return f"£{v:,.2f}"


def _cap_sentence_en(cap: dict) -> str:
    at_or_above = cap["deposit_cap_weeks"] == DEPOSIT_CAP_WEEKS_AT_OR_ABOVE_THRESHOLD
    return (
        f"Annual rent is {_money(cap['annual_rent_gbp'])}"
        + (f" ({_money(cap['monthly_rent_gbp'])} x 12)"
           if cap["stated_rent_period"] == "monthly"
           else f" ({_money(cap['weekly_rent_gbp'])} x 52)")
        + (f", which is {_money(DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP)} or more, so the cap is "
           f"{cap['deposit_cap_weeks']} weeks' rent"
           if at_or_above else
           f", which is under {_money(DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP)}, so the cap is "
           f"{cap['deposit_cap_weeks']} weeks' rent")
        + f" ({STATUTE}, Sch.1 para 2, {JURISDICTION})."
    )


def _cap_sentence_zh(cap: dict) -> str:
    at_or_above = cap["deposit_cap_weeks"] == DEPOSIT_CAP_WEEKS_AT_OR_ABOVE_THRESHOLD
    basis = (f"（{_money(cap['monthly_rent_gbp'])} x 12）"
             if cap["stated_rent_period"] == "monthly"
             else f"（{_money(cap['weekly_rent_gbp'])} x 52）")
    if at_or_above:
        rel = f"达到或超过 {_money(DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP)}"
    else:
        rel = f"低于 {_money(DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP)}"
    return (f"年租金为 {_money(cap['annual_rent_gbp'])}{basis}，{rel}，"
            f"因此押金上限为 {cap['deposit_cap_weeks']} 周租金"
            f"（{STATUTE} Sch.1 para 2，{JURISDICTION}）。")


def deposit_answer(cap: dict, *, language: str = "en") -> str:
    """The full deposit answer for a stated rent, arithmetic shown, cap already applied."""
    weekly = _money(cap["weekly_rent_gbp"])
    weeks = cap["deposit_cap_weeks"]
    total = _money(cap["max_tenancy_deposit_gbp"])
    if language == "zh":
        lines = [
            f"按你说的租金，房东最多可以收 {total} 的租赁押金。",
            "",
            f"- {_cap_sentence_zh(cap)}",
            f"- 周租金：{weekly}"
            + (f"（{_money(cap['monthly_rent_gbp'])} x 12 / 52）"
               if cap["stated_rent_period"] == "monthly" else "（你已说明）"),
            f"- 押金上限：{weekly} x {weeks} = {total}",
            "",
            f"这是法定上限，不是预测——房东可以少收。押金必须在 30 天内存入政府认可的押金保护"
            f"计划。另外可能会有一笔最多 1 周租金（{_money(cap['max_holding_deposit_gbp'])}）"
            f"的意向金（holding deposit），签约后会抵扣首月租金或押金，不是额外费用。"
            "苏格兰和北爱尔兰规则不同。",
        ]
        return "\n".join(lines)
    lines = [
        f"For the rent you've given, the most the landlord can legally take as a tenancy "
        f"deposit is {total}.",
        "",
        f"- {_cap_sentence_en(cap)}",
        f"- Weekly rent: {weekly}"
        + (f" ({_money(cap['monthly_rent_gbp'])} x 12 / 52)"
           if cap["stated_rent_period"] == "monthly" else " (as you stated)"),
        f"- Deposit cap: {weekly} x {weeks} = {total}",
        "",
        f"That is the statutory maximum, not a prediction — a landlord may ask for less. "
        f"The deposit must be protected in a government-approved scheme within 30 days. A "
        f"holding deposit, if one is asked for, is capped separately at one week's rent "
        f"({_money(cap['max_holding_deposit_gbp'])}) and is credited against your first "
        f"rent payment or the deposit once the tenancy is granted, so it is not an extra "
        f"cost. Scotland and Northern Ireland have separate rules.",
    ]
    return "\n".join(lines)


def move_in_answer(mi: dict, *, language: str = "en") -> str:
    """The full move-in answer: first month's rent + capped deposit, components stated."""
    first = _money(mi["first_month_rent_gbp"])
    dep = _money(mi["tenancy_deposit_gbp"])
    total = _money(mi["total_move_in_gbp"])
    weeks = mi["deposit_cap_weeks"]
    weekly = _money(mi["weekly_rent_gbp"])
    held = mi.get("holding_deposit_paid_gbp")
    if language == "zh":
        lines = [
            f"入住前需要一次性支付的总额是 {total}，由两部分组成：",
            "",
            f"- 首月租金：{first}",
            f"- 租赁押金：{weekly} x {weeks} 周 = {dep}",
            f"- 合计：{first} + {dep} = {total}",
            "",
            f"- {_cap_sentence_zh(mi)}",
        ]
        if held is not None:
            lines += [
                f"- 你已支付的意向金 {_money(held)} 会抵扣上述款项，不另计："
                f"入住时还需支付 {_money(mi['balance_due_at_move_in_gbp'])}。",
            ]
        lines += [
            "",
            "这里只包含租金和法定押金。英格兰和威尔士自 2019 年起禁止收取中介费、手续费等费用。"
            "账单、市政税和押金保护计划的费用不在其中——如果你需要全包总额，请把账单和市政税的"
            "金额告诉我，我不会代你估算。",
        ]
        return "\n".join(lines)
    lines = [
        f"Your total move-in cost is {total}, made up of two things:",
        "",
        f"- First month's rent: {first}",
        f"- Tenancy deposit: {weekly} x {weeks} weeks = {dep}",
        f"- Total: {first} + {dep} = {total}",
        "",
        f"- {_cap_sentence_en(mi)}",
    ]
    if held is not None:
        lines += [
            f"- The {_money(held)} holding deposit you've already paid comes off that total "
            f"rather than being added to it, so {_money(mi['balance_due_at_move_in_gbp'])} "
            f"is left to pay at move-in.",
        ]
    lines += [
        "",
        "That is rent and the statutory deposit only. Letting agents and landlords in "
        "England and Wales have not been allowed to charge admin, referencing or "
        "inventory fees since the Tenant Fees Act 2019, so there is nothing else to add. "
        "Bills and council tax are separate and are not included above — tell me those "
        "figures if you want an all-in monthly number; I won't estimate them for you.",
    ]
    return "\n".join(lines)


#: The kinds ``statutory_answer`` can produce. ``tool_policy`` classifies a turn into one
#: of these; anything it cannot place goes to the model as before.
ANSWER_KINDS = ("deposit", "move_in")


def statutory_answer(kind: str, amount: float, period: str, *,
                     language: str = "en",
                     holding_deposit_gbp: Optional[float] = None) -> str:
    """Deterministic answer text for a turn that reduces entirely to this module.

    ``period`` is "week" or "month" — the period the user stated, never inferred.
    Raises ValueError on an unknown ``kind`` or ``period`` rather than guessing, because
    every wrong guess available here is a wrong money figure.
    """
    if period not in ("week", "month"):
        raise ValueError(f"statutory_answer(): period must be week|month, got {period!r}")
    rent_kw = {"weekly_rent": amount} if period == "week" else {"monthly_rent": amount}
    lang = "zh" if language == "zh" else "en"
    if kind == "deposit":
        return deposit_answer(deposit_cap(**rent_kw), language=lang)
    if kind == "move_in":
        return move_in_answer(
            move_in_cost(holding_deposit_gbp=holding_deposit_gbp, **rent_kw), language=lang)
    raise ValueError(f"statutory_answer(): unknown kind {kind!r}")
