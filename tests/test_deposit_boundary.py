"""The 5-vs-6-week statutory deposit cap, and the holding-deposit double-count.

Pinned to three answers the 2026-07-25 round actually shipped. Every "wrong number" below
is the number production emitted, asserted as such — the precedent is
``tests/test_safety_scoring.py``, which keeps the retired safety formula alive in a test so
that it can be shown to produce the bad answer (96 from 9 crimes) and can therefore never
quietly come back.

    B7   "For a £4,500 per month flat, how much is the deposit?"
         annual 4500*12 = £54,000, which is >= £50,000, so the cap is SIX weeks.
         Shipped: £5,192.31  = (4500*12/52)*5   <- the five-week cap
         Correct: £6,230.77  = (4500*12/52)*6
         The answer STATED the £50,000 rule and then applied five weeks anyway. That is
         cases.jsonl B7 failure_conditions[0] verbatim.

    B14  "The rent is £1,000 a week. What deposit is the landlord legally allowed to take?"
         annual 1000*52 = £52,000 >= £50,000 -> six weeks.
         Shipped headline: £5,000 = 1000*5. £6,000 appeared only in a trailing hedge, which
         is how the case's must_mention_value passed on a wrong headline.

    B4   "What's the total move-in cost for a £1500/month place?"
         Correct: £1,500.00 + £1,730.77 = £3,230.77
         Shipped: "£3,500 - £3,600", i.e. £3,230.77 + £346.15 = £3,576.92 — the model
         printed the right deposit, said the holding deposit is DEDUCTED from the first
         month's rent, and then added it. The overcount is exactly one week's rent, which is
         exactly the holding-deposit cap: the signature of a credit counted twice.

The critic returned ``grounded=True, issues=[]`` for all three, and the eval's own
fabrication grader treats the five- and six-week readings as equally "derivable"
(``graders._derivable``), so no check anywhere in the pipeline could have caught these. The
fix is therefore not a better instruction: it is that ``tenancy_reference`` writes these
answers itself and ``guard_node`` returns them before any LLM call.
"""
from __future__ import annotations

import glob
import json
import os

import pytest
from langchain_core.messages import AIMessage

import core.agent_loop as agent_loop
import core.tool_policy as tool_policy
from core.tenancy_reference import (
    DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP,
    HOLDING_DEPOSIT_CAP_WEEKS,
    annual_from,
    deposit_cap,
    deposit_cap_weeks,
    move_in_cost,
    statutory_answer,
)

from test_fc_loop import FakeChat, FakeProvider, FakeSpec, _base_state, _drive, _run


B3_QUERY = "For a £1500/month flat, how much deposit should I expect?"
B4_QUERY = "What's the total move-in cost for a £1500/month place?"
B7_QUERY = "For a £4,500 per month flat, how much is the deposit?"
B10_QUERY = "For a flat at £4,200 per month, how much deposit can they legally ask for?"
B14_QUERY = "The rent is £1,000 a week. What deposit is the landlord legally allowed to take?"
B15_QUERY = ("For a £4,800 pcm flat, what's my total upfront cost — first month plus the "
             "deposit they're allowed to charge?")
B12_QUERY = ("I'm looking at a £380/week studio. What'll it cost me all-in per month, "
             "including bills and council tax?")


def _money_state(query):
    return _base_state(
        user_query=query,
        extracted_context={"current_message": query, "reply_language": "en"},
    )


# ─── the wrong numbers, pinned ───────────────────────────────────────
def test_the_shipped_wrong_figures_really_are_what_the_wrong_rule_produces():
    """The regression anchor. If a future change reverts to the five-week cap, or re-adds a
    holding deposit to a move-in total, it lands on one of these numbers — so they are
    written down, derived, and asserted to be what the module does NOT return."""
    # B7: the five-week reading of a rent that is over the threshold.
    assert round(4500 * 12 / 52 * 5, 2) == 5192.31
    # B14: the five-week reading of a weekly rent that is over the threshold.
    assert 1000 * 5 == 5000
    # B10: same trap, £400 over the line.
    assert round(4200 * 12 / 52 * 5, 2) == 4846.15
    # B4: the double count. First month + deposit + a holding deposit that is a CREDIT.
    assert round(1500 + 1500 * 12 / 52 * 5 + 1500 * 12 / 52, 2) == 3576.92
    # ...and 3576.92 is inside the "£3,500 - £3,600" range the answer actually quoted,
    # which is how we know the overcount was the holding deposit and not a rounding slip.
    assert 3500 <= 3576.92 <= 3600


@pytest.mark.parametrize("kw,annual,weeks,deposit,shipped_wrong", [
    # B7 — the case this defect was measured on.
    (dict(monthly_rent=4500), 54000.0, 6, 6230.77, 5192.31),
    # B14 — the same boundary reached from a weekly rent.
    (dict(weekly_rent=1000), 52000.0, 6, 6000.00, 5000.00),
    # B10 — £50,400/yr, only £400 over the line.
    (dict(monthly_rent=4200), 50400.0, 6, 5815.38, 4846.15),
    # B15 — comfortably over.
    (dict(monthly_rent=4800), 57600.0, 6, 6646.15, 5538.46),
    # B3 / B4 — under the line, five weeks is CORRECT here. The fix must not overshoot into
    # six weeks for everything; that would be the same bug with the sign flipped.
    (dict(monthly_rent=1500), 18000.0, 5, 1730.77, 2076.92),
    # B8 — under the line.
    (dict(monthly_rent=1600), 19200.0, 5, 1846.15, 2215.38),
])
def test_cap_is_the_statutory_one_and_never_the_shipped_figure(
        kw, annual, weeks, deposit, shipped_wrong):
    cap = deposit_cap(**kw)
    assert cap["annual_rent_gbp"] == pytest.approx(annual, abs=0.005)
    assert cap["deposit_cap_weeks"] == weeks
    assert cap["max_tenancy_deposit_gbp"] == pytest.approx(deposit, abs=0.005)
    assert cap["max_tenancy_deposit_gbp"] != pytest.approx(shipped_wrong, abs=0.005)


# ─── the boundary itself ─────────────────────────────────────────────
def test_threshold_is_inclusive_at_exactly_50000():
    """Sch.1 para 2 is "£50,000 or more": at £50,000 the cap is SIX weeks, not five."""
    assert deposit_cap_weeks(DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP) == 6
    assert deposit_cap_weeks(DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP - 0.01) == 5
    assert deposit_cap_weeks(DEPOSIT_CAP_THRESHOLD_ANNUAL_GBP + 0.01) == 6
    # A penny either side of the line, expressed as a real monthly rent.
    assert deposit_cap(monthly_rent=4166.66)["deposit_cap_weeks"] == 5   # annual 49,999.92
    assert deposit_cap(monthly_rent=4166.67)["deposit_cap_weeks"] == 6   # annual 50,000.04
    assert deposit_cap(weekly_rent=961.53)["deposit_cap_weeks"] == 5     # annual 49,999.56
    assert deposit_cap(weekly_rent=961.54)["deposit_cap_weeks"] == 6     # annual 50,000.08


def test_annual_rent_comes_from_the_stated_period_not_a_round_trip():
    """``monthly * 12 / 52 * 52`` is not ``monthly * 12`` in binary floating point — for
    B7's £4,500 it is 54000.00000000001 — and the cap is decided by a comparison against
    50,000. The threshold must not be adjudicating representation noise, and the annual
    figure we report must equal the one benchmark/README.md computes."""
    assert annual_from(monthly_rent=4500) == 54000.0
    assert 4500 * 12 / 52 * 52 != 54000  # the round-trip that is NOT used
    assert annual_from(weekly_rent=1000) == 52000.0
    assert annual_from(monthly_rent=4200) == 50400.0
    with pytest.raises(ValueError):
        annual_from()
    with pytest.raises(ValueError):
        annual_from(monthly_rent=1, weekly_rent=1)


def test_the_two_periods_agree_about_the_same_tenancy():
    """A £4,333.33 pcm rent and a £1,000/wk rent are the same tenancy from either side of
    the conversion; a threshold that answered differently depending on which the user typed
    would be a threshold disagreeing with itself."""
    for weekly in (500.0, 900.0, 961.54, 1000.0, 1200.0):
        via_week = deposit_cap(weekly_rent=weekly)
        via_month = deposit_cap(monthly_rent=via_week["monthly_rent_gbp"])
        assert via_week["deposit_cap_weeks"] == via_month["deposit_cap_weeks"], weekly


# ─── the holding-deposit double count ────────────────────────────────
def test_b4_total_is_first_month_plus_deposit_and_nothing_else():
    mi = move_in_cost(monthly_rent=1500)
    assert mi["first_month_rent_gbp"] == 1500.00
    assert mi["deposit_cap_weeks"] == 5
    assert mi["tenancy_deposit_gbp"] == pytest.approx(1730.77, abs=0.005)
    assert mi["total_move_in_gbp"] == pytest.approx(3230.77, abs=0.005)
    # The shipped double count, and the fabricated-fee failure condition next to it.
    assert mi["total_move_in_gbp"] != pytest.approx(3576.92, abs=0.005)
    assert mi["total_move_in_components"] == ["first_month_rent", "tenancy_deposit"]


def test_a_stated_holding_deposit_is_a_credit_and_never_an_addition():
    """The B4 defect, reproduced as an input: the user has paid a holding deposit. It must
    change what is LEFT to pay and not the total."""
    plain = move_in_cost(monthly_rent=1500)
    held = move_in_cost(monthly_rent=1500, holding_deposit_gbp=346.15)
    assert held["total_move_in_gbp"] == plain["total_move_in_gbp"] == pytest.approx(3230.77, abs=0.005)
    assert held["balance_due_at_move_in_gbp"] == pytest.approx(2884.62, abs=0.005)
    assert held["holding_deposit_paid_gbp"] == 346.15
    assert held["holding_deposit_over_cap"] is False
    # 3230.77 + 346.15 = 3576.92 is the number that must be unreachable.
    assert held["total_move_in_gbp"] + held["holding_deposit_paid_gbp"] == pytest.approx(
        3576.92, abs=0.005)


@pytest.mark.parametrize("holding", [None, 0.0, 50.0, 346.15, 1000.0])
def test_the_total_is_structurally_independent_of_the_holding_deposit(holding):
    base = move_in_cost(monthly_rent=1500)["total_move_in_gbp"]
    assert move_in_cost(monthly_rent=1500,
                        holding_deposit_gbp=holding)["total_move_in_gbp"] == base


def test_holding_deposit_over_the_one_week_cap_is_flagged_not_silently_accepted():
    mi = move_in_cost(monthly_rent=1500, holding_deposit_gbp=800.0)
    assert mi["max_holding_deposit_gbp"] == pytest.approx(346.15, abs=0.005)
    assert HOLDING_DEPOSIT_CAP_WEEKS == 1
    assert mi["holding_deposit_over_cap"] is True
    assert mi["total_move_in_gbp"] == pytest.approx(3230.77, abs=0.005)


@pytest.mark.parametrize("kw,total", [
    (dict(monthly_rent=1500), 3230.77),   # B4
    (dict(monthly_rent=1600), 3446.15),   # B8
    (dict(monthly_rent=4800), 11446.15),  # B15 — six-week cap inside a move-in total
])
def test_move_in_totals_match_the_benchmark_references(kw, total):
    assert move_in_cost(**kw)["total_move_in_gbp"] == pytest.approx(total, abs=0.005)


# ─── the answer text ─────────────────────────────────────────────────
def test_b7_answer_leads_with_the_six_week_figure():
    text = statutory_answer("deposit", 4500, "month")
    assert "£6,230.77" in text
    assert "5,192.31" not in text
    # The rule is stated AND applied — B7's answer did the first without the second.
    assert "6 weeks" in text and "£50,000.00 or more" in text
    assert "£54,000.00" in text
    # Headline, not a hedge: the correct figure is in the first line (B14's failure was a
    # right number buried under a wrong headline).
    assert "£6,230.77" in text.splitlines()[0]


def test_b14_answer_leads_with_6000_not_5000():
    text = statutory_answer("deposit", 1000, "week")
    assert "£6,000.00" in text.splitlines()[0]
    assert "£5,000" not in text
    assert "£52,000.00" in text


def test_b4_answer_states_components_and_never_adds_a_holding_deposit():
    text = statutory_answer("move_in", 1500, "month")
    assert "£3,230.77" in text.splitlines()[0]
    assert "£1,500.00" in text and "£1,730.77" in text
    for wrong in ("£3,576.92", "£3,500", "£3,600"):
        assert wrong not in text
    # No fabricated fee (B4 failure_conditions[1]).
    low = text.lower()
    assert "admin fee" not in low and "agency fee" not in low


def test_move_in_answer_with_a_stated_holding_deposit_says_off_not_added():
    text = statutory_answer("move_in", 1500, "month", holding_deposit_gbp=346.15)
    assert "£3,230.77" in text.splitlines()[0]
    assert "£2,884.62" in text
    assert "£3,576.92" not in text


def test_answer_kind_and_period_are_never_guessed():
    with pytest.raises(ValueError):
        statutory_answer("deposit", 1500, "fortnight")
    with pytest.raises(ValueError):
        statutory_answer("rent_review", 1500, "month")


def test_zh_answer_carries_the_same_figures():
    text = statutory_answer("deposit", 4500, "month", language="zh")
    assert "£6,230.77" in text and "£54,000.00" in text
    assert "5,192.31" not in text


# ─── classification: narrow, and validated on the corpus ─────────────
@pytest.mark.parametrize("query,expected", [
    (B3_QUERY, ("deposit", 1500.0, "month", None)),
    (B4_QUERY, ("move_in", 1500.0, "month", None)),
    (B7_QUERY, ("deposit", 4500.0, "month", None)),
    (B10_QUERY, ("deposit", 4200.0, "month", None)),
    (B14_QUERY, ("deposit", 1000.0, "week", None)),
    (B15_QUERY, ("move_in", 4800.0, "month", None)),
])
def test_the_measured_cases_are_classified_as_pure_arithmetic(query, expected):
    assert tool_policy.statutory_money_answer(query) == expected


@pytest.mark.parametrize("query", [
    # B12: only PARTLY derivable. Its contract requires refusing to fabricate the bills, so
    # a deterministic total here would be confidently wrong.
    B12_QUERY,
    "For a £1500/month flat, what's the deposit including council tax?",
    "£1500 a month — what's the deposit and the agency fee?",
    # Not arithmetic: process questions the template does not answer.
    "For a £1500/month flat, how do I get my deposit back?",
    "The rent is £1500 a month. What if they keep the deposit?",
    "£1500 a month. Is my deposit protected in a scheme?",
    "For a £1500/month flat, when do I pay the deposit?",
    "£1500 a month — can the landlord deduct cleaning from the deposit?",
    # Two rent figures: pricing the wrong one is a silent wrong answer.
    "Deposit for a £1500/month flat or a £1800/month one?",
    # A holding deposit named but unpriced: ask, don't assume.
    "Total move-in cost for a £1500/month place if I paid a holding deposit?",
    # Everything self_contained_money_question already refuses stays refused.
    "Find me a 2-bed in Camden under £1500 a month.",
    "How much is a deposit usually?",
    "The rent is £1,000. What deposit can they take?",
    "Actually make it £1500 a month instead.",
])
def test_classifier_refuses_anything_it_cannot_answer_completely(query):
    assert tool_policy.statutory_money_answer(query) is None


def test_a_priced_holding_deposit_on_a_move_in_turn_is_read_as_a_credit():
    q = ("What's the total move-in cost for a £1500/month place? I've already paid a £346.15 "
         "holding deposit.")
    assert tool_policy.statutory_money_answer(q) == ("move_in", 1500.0, "month", 346.15)


_CASE_FILES = sorted(glob.glob(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "evaluation", "benchmark", "cases*.jsonl")))


@pytest.mark.skipif(not _CASE_FILES, reason="benchmark cases not present")
def test_classifier_fires_on_exactly_the_seven_pure_arithmetic_cases():
    """The gate skips the model, so its blast radius is a fact worth pinning rather than a
    claim in a docstring. Same discipline as tool_policy's own corpus validation."""
    seen = {}
    for path in _CASE_FILES:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                case = json.loads(line)
                cid = case.get("case_id") or case.get("id")
                seen.setdefault(cid, case.get("user_query") or "")
    fires = sorted(cid for cid, q in seen.items()
                   if tool_policy.statutory_money_answer(q) is not None)
    assert fires == ["B10", "B14", "B15", "B3", "B4", "B7", "B8"], fires
    # B12 is admitted by the tool gate and refused by this one: the second predicate is
    # strictly narrower, which is what licenses it to skip the model.
    assert tool_policy.self_contained_money_question(B12_QUERY) is not None
    assert tool_policy.statutory_money_answer(B12_QUERY) is None


# ─── end to end: the model cannot get this wrong any more ────────────
@pytest.mark.parametrize("case_id,query,right,wrong", [
    ("B7", B7_QUERY, "£6,230.77",
     "The annual rent is £54,000, which is over £50,000. The deposit is £5,192.31."),
    ("B14", B14_QUERY, "£6,000.00",
     "A tenancy deposit cannot be more than 5 weeks' rent, so £5,000. (Six weeks would "
     "be £6,000.)"),
    ("B4", B4_QUERY, "£3,230.77",
     "First month £1,500 plus a £1,730.77 deposit, plus a holding deposit of £346.15 "
     "deducted from the first month — around £3,500 to £3,600."),
])
def test_the_shipped_wrong_answer_can_no_longer_reach_the_user(case_id, query, right, wrong):
    """The LLM is scripted to return the exact text production shipped. The guard answers
    first, so the model is never invoked at all and that text cannot be emitted.

    This is the test that fails on the old behaviour: before the fix the guard routed to
    ``agent``, the scripted wrong answer became ``final_response``, and the user saw it."""
    chat = FakeChat([AIMessage(content=wrong)])
    provider = FakeProvider([FakeSpec("web_search")])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _money_state(query)))

    answer = state["final_response"]
    assert right in answer, f"{case_id}: {answer!r}"
    assert answer != wrong
    assert "5,192.31" not in answer and "£5,000" not in answer
    assert "3,576.92" not in answer and "£3,600" not in answer
    # Zero LLM calls and zero tool calls: nothing was consulted that could be wrong.
    assert chat._scripted == [AIMessage(content=wrong)]
    assert provider.calls == []
    assert state["response_type"] == "answer"


def test_b12_still_reaches_the_model():
    """The guard must not swallow a partially-derivable turn. B12's answer needs the model
    (and a refusal to fabricate bills), so the model's reply is what ships."""
    chat = FakeChat([AIMessage(content="The rent is £1,646.67/month; I don't have your bills.")])
    provider = FakeProvider([FakeSpec("web_search")])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _money_state(B12_QUERY)))
    assert "£1,646.67" in state["final_response"]
    assert chat._scripted == []  # the model DID run


def test_an_ordinary_search_turn_is_untouched_by_the_guard():
    chat = FakeChat([AIMessage(content="Sure, which area?")])
    provider = FakeProvider([FakeSpec("search_properties")])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _money_state("Find me a 2-bed in Camden under £1500 a month.")))
    assert state["final_response"] == "Sure, which area?"


def test_guard_falls_through_to_the_model_when_the_reference_breaks(monkeypatch):
    """A raising policy must not take the turn down and must not become an answer-all. The
    pre-fix behaviour (hand it to the model) is the fallback."""
    class _Boom:
        @staticmethod
        def statutory_money_answer(*a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_loop, "_load_tool_policy", lambda: _Boom)
    chat = FakeChat([AIMessage(content="model answer")])
    provider = FakeProvider([FakeSpec("web_search")])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _money_state(B7_QUERY)))
    assert state["final_response"] == "model answer"


def test_fair_housing_refusal_still_wins_over_the_arithmetic_guard():
    """Ordering matters: a discriminatory turn that also quotes a rent must be refused, not
    answered with a deposit figure."""
    query = "For a £4,500 per month flat, how much is the deposit? I want to avoid immigrants."
    chat = FakeChat([AIMessage(content="should never run")])
    nodes = agent_loop.build_fc_nodes(FakeProvider([]), agent_llm=chat)
    state = _run(_drive(nodes, _money_state(query)))
    assert "£6,230.77" not in state["final_response"]
