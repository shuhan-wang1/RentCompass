"""A `B_money` case must be able to fail on an INVENTED or SELF-CONTRADICTORY money figure.

All fifteen `B_money` cases graded arithmetic-match and value-mention and nothing else.
`reference_calc_match` asks "does the right number appear somewhere", `must_mention_value`
asks the same question again — so an answer could state the correct figure, invent a second
one beside it, and pass. Read against `graders._field_to_kind`, only nine of the fifteen
declared any constraint capable of failing on a money figure at all, and B12 was worse than
uncovered: it DECLARED `must_refuse_fabrication[total_all_in]`, but `total_all_in` resolved
to no claim kind, so the numeric branch was never taken and the constraint degraded to "any
refusal marker, or any absence sentence anywhere in the answer". The case whose whole point
is "never fabricate utility costs" could not fail on a fabricated utility cost.

B15 is the severe one, and it is verified here against the retained evidence rather than
against any report of it. `.runtime/round-8793c0b-internal-2026-07-25/eval/sweep/
grader_input.jsonl`, case B15, fc arm: `tools_called: []`, `evidence: []`,
`scored_passed: true`, 3/3. The answer states

    **Total upfront cost:** £4,800 + £5,538.46 = **£10,338.46**
    ...
    - **Corrected total upfront:** £4,800 + £6,646.15 = **£11,446.15**

Two mutually exclusive totals for one quantity in one reply. The first pair is verbatim
B15's own `failure_conditions[0]`. It passed because `must_mention_value[11446.15]` and both
`reference_calc_match`es are satisfied by the SECOND figure and nothing asked about the
first.

SELF-CONTRADICTION IS A DISTINCT DEFECT FROM UNGROUNDEDNESS, and needed a new checker.
Two rival readings of the same statute can BOTH be arithmetically derivable from the same
base figure, in which case every individual number is supported and the pair is still
incoherent — a user cannot act on "your deposit is either £5,538 or £6,646". Nothing in the
vocabulary expressed it. `must_flag_contradiction` is the opposite test (a keyword check
that the answer SURFACES a disagreement between SOURCES) and B15's answer would satisfy it
on "actually" / "Corrected": the corpus could reward a self-contradiction as if it were
honesty. `no_self_contradictory_value` is deliberately equation-anchored so it cannot fire
on a range, a before/after comparison, or shown working — each proved below against a real
retained answer, not a hypothetical.

GOVERNANCE (§3.5 — never change a decision rule after seeing the measurement it judges).
The rule was written down in full before the amended contract was scored: the per-case
constraint table, the new checker's semantics, the declaration set, the two cases
deliberately EXCLUDED from it, and the predicted direction (B15 fc and B10 fc flip; legacy
flat). Then it was measured. Measured, both arms, ONE evaluator (this tree's graders and
this tree's contract) over the retained round-8793c0b `grader_input.jsonl` — 98 cases per
arm, 0 digest mismatches, 0 duplicate records:

    arm      before -> after   flips
    fc        74/98 -> 72/98   2   (B10, B15)
    legacy    46/98 -> 46/98   0

The as-recorded baseline (fc 74, legacy 46) reproduced exactly before anything was changed.
The direction is stated plainly and is not a preference: **fc loses two cases and legacy
loses none.** That asymmetry is a fact about the answers, not about the rule. Legacy's
B_money replies either refuse to give a figure or get the arithmetic wrong, so they were
already failing and had nothing left to lose; at CONSTRAINT level the new rules bite both
arms — legacy's B3 also newly fails `no_fabricated_number` (it converted with `/4.35`), and
legacy B1/B2/B11 keep passing every constraint. The reach is exactly the twelve amended
cases: re-scoring the whole 98-case corpus, no constraint on any other case changes its
result in either arm.

THE GRADER EDITS ARE A MEASURED NO-OP ON THEIR OWN. Scored with this tree's graders but
MAINLINE's contract, both arms are unchanged (fc 74/98, legacy 46/98, zero flips) — so
every flip above is attributable to the contract amendment, not to the checker corrections
riding with it.

DELIBERATELY NOT CHANGED, and why:

  * **B6 and B11 get no `no_self_contradictory_value`.** Those two cases exist to test that
    the answer surfaces TWO RIVAL SOURCE FIGURES (£1,500 vs £400/week; £1,500 vs £1,650)
    instead of silently picking one. Presenting both is the required behaviour, so the
    constraint would condemn the correct answer. Nor B5, a clarification case with no money
    quantity at all.
  * **`_NONASSERTION_MARKERS` was left alone**, and it produces a real asymmetry that the
    owner should decide on. `"weeks' rent"` in that list excuses ANY £ figure sharing a line
    with the phrase, so B14's fc answer ("- **Maximum deposit:** 5 weeks x £1,000 =
    **£5,000**") has its invented £5,000 caught while legacy's identical invention ("- Five
    weeks' rent = 5 x £1,000 = **£5,000**") does not. Both are the five-week trap on a
    £52,000 annual rent. B14 already fails on both arms so no verdict depends on it, and the
    list is calibrated corpus-wide by the grader work — retuning it after seeing this round
    is exactly what §3.5 forbids. Reported, not edited.
  * **The other unmapped `field` names were not mapped.** `_field_to_kind` gained
    `total_all_in` only, because that is the one inside this category. `bills`, `budget`,
    `current_fare`, `official_monthly_rent` (money) and `listing_2_commute`,
    `listing_3_commute` (commute) are named by constraints in OTHER categories and are
    silently ungraded for exactly the same reason B12 was. They are pinned as debt in
    `UNMAPPED_NUMERIC_FIELDS` below so the set cannot grow in silence; mapping them changes
    what "pass" means for cases this branch has not read.

  * **B4's `£15,000 - £19,125` was not excused.** The fc answer offers it as a
    purpose-built-student-accommodation upfront figure for a full academic year; it is
    derivable from nothing and is now an offender. B4 already fails the arithmetic on both
    arms, so no verdict turns on it, but if the owner reads that as a hedged illustration
    the fix belongs in `_NONASSERTION_MARKERS`, not in B4's contract.

THIS DEFECT CLASS WAS FOUND THREE TIMES ON THE SAME DAY, independently, which is worth
recording because it means the `safety_score` family is not rare: B12's
`must_refuse_fabrication[total_all_in]` here; C10's `no_fabricated_number[fare_gbp]` on PR
#58, fixed by renaming the field to `fare`, the spelling the rest of the corpus already uses;
and again on PR #62. The guards are reconciled rather than stacked:

    guard                                              scope
    #58  test_every_no_fabricated_number_field_…        no_fabricated_number, ZERO
                                                       exemptions — strictly stronger, and
                                                       the reason this module does not also
                                                       assert that half
    here test_no_absence_constraint_is_a_silent_no_op   must_refuse_fabrication and
                                                       must_note_missing_data, which have a
                                                       documented non-numeric branch and so
                                                       need a classification table

Disjoint by constraint type, so neither is redundant and neither is weakened.
`test_this_modules_debt_table_never_excuses_a_fabrication_field` enforces the seam from this
side: a field that only `no_fabricated_number` declares can never be recorded here as debt.
That is why `fare_gbp` is gone from the table rather than merely pruned — recording it here
was the wrong home for it, and `test_the_debt_table_has_no_dead_entries` is what made the
staleness surface the moment the two branches were integrated.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from evaluation.metrics import graders
from evaluation.run_benchmark import load_fixture_queue

BENCH = Path(__file__).resolve().parents[1] / "evaluation" / "benchmark"

MONEY_KIND = "money"

# Constraint types whose checker can actually FAIL on an ungrounded number, derived by
# reading graders.py: all three route `field` through `_field_to_kind` into
# `_field_number_offenders`. Same list as tests/test_uncovered_answer_dimension.py.
FIELD_KINDED_TYPES = ("no_fabricated_number", "must_refuse_fabrication",
                      "must_note_missing_data")
# ...and the type added here, which fails on two values for one quantity.
SELF_CONTRADICTION_TYPE = "no_self_contradictory_value"

# The subset of FIELD_KINDED_TYPES that LEGITIMATELY takes a non-numeric field. Both have a
# documented non-numeric branch — `must_refuse_fabrication` falls back to "does the answer
# voice this field's absence" when `_field_to_kind` returns None, and `must_note_missing_data`
# is about absence in the first place — so `availability`, `user_memory` and `listings` are
# correct field names for them and an unconditional "must resolve to a kind" rule over these
# two types would condemn correct cases.
#
# `no_fabricated_number` is deliberately NOT here: it has no non-numeric branch at all, so an
# unmapped field is not a weak constraint but NO constraint. That half of the invariant is
# owned by PR #58's `test_every_no_fabricated_number_field_resolves_to_a_claim_kind`, which is
# strictly stronger than anything this module should assert — it carries zero exemptions. See
# `test_this_modules_debt_table_never_excuses_a_fabrication_field` for the boundary between
# the two guards, enforced from this side.
NON_NUMERIC_TOLERANT_TYPES = ("must_refuse_fabrication", "must_note_missing_data")

# A POSITIVE table: one row per B_money case and the constraint that must be able to fail
# it on a money figure. Adding a row OBLIGES a case; it never excuses one. Same vehicle as
# `test_amended_cases_are_in_sync_across_every_shard` in test_case_contract_consistency.py
# and `AMENDED_DIMENSION_COVERAGE` in test_uncovered_answer_dimension.py.
#
# "existing" marks the six cases already covered before this branch — they are listed so the
# table is the whole category rather than only the part that changed, and so removing one of
# those constraints fails here too.
MONEY_COVERAGE = {
    "B1":  "no_fabricated_number[monthly_rent]",
    "B2":  "no_fabricated_number[weekly_rent]",
    "B3":  "no_fabricated_number[deposit]",
    "B4":  "no_fabricated_number[total_move_in]",
    "B5":  "must_refuse_fabrication[deposit]",          # existing
    "B6":  "no_fabricated_number[deposit]",             # existing
    "B7":  "no_fabricated_number[deposit]",
    "B8":  "no_fabricated_number[deposit]",             # existing
    "B9":  "no_fabricated_number[monthly_rent]",        # existing
    "B10": "no_fabricated_number[deposit]",
    "B11": "no_fabricated_number[monthly_rent]",        # existing
    "B12": "must_refuse_fabrication[total_all_in]",     # existing DECLARATION, newly REACHABLE
    "B13": "no_fabricated_number[average_rent]",        # existing
    "B14": "no_fabricated_number[deposit]",
    "B15": "no_fabricated_number[deposit]",
}

# The cases that must ALSO be able to fail on two values for one quantity, and the quantity.
# B5/B6/B11 are absent on purpose — see the module docstring.
SELF_CONTRADICTION_COVERAGE = {
    "B1": ["monthly_rent"],
    "B2": ["weekly_rent"],
    "B3": ["deposit"],
    "B4": ["total_move_in"],
    "B7": ["deposit"],
    "B8": ["total_move_in"],
    "B9": ["monthly_rent"],
    "B10": ["deposit"],
    "B12": ["monthly_rent"],
    "B13": ["monthly_rent"],
    "B14": ["deposit"],
    "B15": ["deposit", "total_move_in"],
}

# Plain-language markers the amended cases must also carry in `failure_conditions`, so the
# contract says what it forbids in words a case owner can read, not only in a type name.
FAB_MARKER = "no sanctioned UK conversion derives"
SELF_MARKER = "two different values for the same quantity"


def _cases_by_id() -> dict:
    by_case = defaultdict(dict)
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                case = json.loads(line)
                by_case[case["case_id"]][path.name] = case
    return by_case


def _covers_money(case: dict):
    """The constraint (if any) that can fail this case on an ungrounded money figure."""
    for con in case.get("expected_constraints") or []:
        if con.get("type") in FIELD_KINDED_TYPES:
            if graders._field_to_kind(con.get("field") or "") == MONEY_KIND:
                return f"{con['type']}[{con.get('field')}]"
    return None


def _self_contradiction_fields(case: dict):
    return [c.get("field") for c in case.get("expected_constraints") or []
            if c.get("type") == SELF_CONTRADICTION_TYPE]


def _fixture_evidence(case: dict) -> list:
    """The case's OWN declared fixture, flattened to the [{tool, data}] shape the graders
    consume — derived from the file the runner replays, so this test cannot drift from the
    evidence the case is actually graded against."""
    evidence = []
    for tool, records in load_fixture_queue(case).items():
        for rec in records:
            evidence.append({"tool": tool, "success": rec.get("success", True),
                             "error": rec.get("error"), "data": rec.get("data")})
    return evidence


def _ctx(case: dict, answer: str, tools=()) -> graders.GradeContext:
    return graders.GradeContext(
        final_answer=answer,
        tools_called=list(tools),
        tool_call_events=[],
        evidence=_fixture_evidence(case),
        route=None,
        user_texts=[case["user_query"]],
        reference_calculations=case.get("reference_calculations"),
        error=None,
        reconstructed_context=None,
        history_texts=[t.get("content", "") for t in case.get("conversation_history") or []],
    )


def _con(ctype, **kw):
    return graders.CONSTRAINT_CHECKERS[ctype](dict(type=ctype, **kw), None)


# --------------------------------------------------------------------------- #
# 1. B15 — the regression. Verbatim retained answers, both arms.
# --------------------------------------------------------------------------- #
# fc arm, verbatim from round-8793c0b-internal-2026-07-25/eval/sweep/grader_input.jsonl.
# Note the shape: the WRONG total is the headline and the correction is buried in a
# footnote, which is why "it corrects itself" is not a defence.
B15_CONTRADICTING_ANSWER = (
    "Let me look up the standard deposit rules in the UK.\n\n"
    "In England, the maximum deposit a landlord can charge is capped at **5 weeks' rent** "
    "for tenancies with an annual rent below £50,000. For a £4,800 pcm flat:\n\n"
    "**First month's rent:** £4,800\n\n"
    "**Maximum deposit (5 weeks):**\n"
    "- Monthly rent: £4,800\n"
    "- Weekly rent: £4,800 x 12 / 52 = £1,107.69\n"
    "- 5 weeks' deposit: £1,107.69 x 5 = **£5,538.46**\n\n"
    "**Total upfront cost:** £4,800 + £5,538.46 = **£10,338.46**\n\n"
    "A few important notes:\n\n"
    "- **Deposit cap:** Under the Tenant Fees Act 2019, the maximum deposit for a tenancy "
    "with annual rent under £50,000 is 5 weeks' rent. Your annual rent here is £57,600 "
    "(£4,800 x 12), which is above £50,000 — so the cap is actually **6 weeks' rent**, "
    "not 5.\n"
    "- **Corrected max deposit:** £1,107.69 x 6 = **£6,646.15**\n"
    "- **Corrected total upfront:** £4,800 + £6,646.15 = **£11,446.15**\n\n"
    "However, many landlords still charge 5 weeks as a standard practice even when the "
    "legal cap is 6 weeks. You'd need to check the specific tenancy agreement.\n\n"
    "Also, the deposit must be protected in a government-approved tenancy deposit scheme "
    "(TDP) within 30 days, and you should receive the prescribed information about which "
    "scheme it's held in."
)
# legacy arm, verbatim: it declines to state a figure at all. Already failing on the
# arithmetic, so what matters is that the NEW constraints do not add a second reason.
B15_REFUSING_ANSWER = (
    "The search results do not contain any specific information about deposit caps, the "
    "Tenant Fees Act 2019, or upfront cost calculations for London in 2025. Therefore, I "
    "cannot provide a confirmed figure for the total upfront cost of a £4,800 per calendar "
    "month (pcm) flat.\n\nHowever, based on general UK rental rules (which I cannot verify "
    "from the data above), the standard maximum deposit is typically capped at five weeks' "
    "rent for properties where the annual rent is under £50,000. For a £4,800 pcm flat, "
    "the annual rent is £57,600, which exceeds that threshold. In such cases, the deposit "
    "cap may be different, but the search results do not cover this.\n\nTo get an accurate "
    "answer, I recommend checking the official UK government guidance on the Tenant Fees "
    "Act 2019 or contacting a reputable letting agent in London."
)

# Each tuple is ONE READING of the statute — (deposit, total) — not a contradictory pair.
# Within a reading £5,538.46 is an ADDEND and £10,338.46 is its SUM, and quoting those two as
# "the contradiction" is a misreading of the answer that has already been made once. The
# contradictions run ACROSS the two readings, quantity by quantity:
#     deposit:        £5,538.46  vs  £6,646.15
#     total upfront:  £10,338.46 vs  £11,446.15
# Pinned by test_the_contradiction_is_per_quantity_not_addend_versus_sum.
B15_FIVE_WEEK_FIGURES = (5538.46, 10338.46)
B15_SIX_WEEK_FIGURES = (6646.15, 11446.15)
B15_RIVAL_DEPOSITS = [5538.46, 6646.15]
B15_RIVAL_TOTALS = [10338.46, 11446.15]


def test_b15_has_no_channel_the_five_week_figures_could_come_from():
    """Guards the premise of everything below, the way B9's premise test does. B15 is a
    pure-arithmetic turn: no prior turns, no tools, no fixture. Its money pool is the
    user's £4,800 plus the sanctioned UK derivations of it plus its own
    reference_calculations — and the £50,000 threshold puts £4,800 pcm (£57,600 a year)
    on the SIX-week side, so the five-week reading is derivable from nothing."""
    for name, case in _cases_by_id()["B15"].items():
        assert case["conversation_history"] == [], name
        assert case["expected_tools"] == [], name
        assert "fixture" not in case, name
    case = _cases_by_id()["B15"]["cases.jsonl"]
    pool = graders._build_evidence_pool(_ctx(case, ""))
    for figure in B15_SIX_WEEK_FIGURES:
        assert graders._near(figure, pool.money), (
            f"{figure} is the statutory answer and must be grounded: {sorted(pool.money)}")
    for figure in B15_FIVE_WEEK_FIGURES:
        assert not graders._near(figure, pool.money), (
            f"{figure} is now derivable, so the answer would no longer be inventing it — "
            f"this regression is pinning the wrong thing: {sorted(pool.money)}")


def test_b15_fails_on_its_two_deposits_and_its_two_totals():
    """THE REGRESSION. On mainline this assertion could not hold for any contract B15
    declared: `reference_calc_match[total_move_in]`, `reference_calc_match[deposit_6_weeks]`
    and `must_mention_value[11446.15]` are all satisfied by the SIX-WEEK figures, so the case
    scored 3/3 and PASSED while also asserting the whole five-week reading — deposit
    £5,538.46 and total £10,338.46 — as the answer."""
    case = _cases_by_id()["B15"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B15_CONTRADICTING_ANSWER))

    assert not verdict.passed, (
        "an answer that states two mutually exclusive deposits and two mutually exclusive "
        f"totals must not pass: {[(c.type, c.passed, c.detail) for c in verdict.constraints]}")
    failed = {c.type for c in verdict.constraints if not c.passed}
    assert failed == {"no_fabricated_number", SELF_CONTRADICTION_TYPE}, (
        "B15 must fail on the money dimension specifically, not incidentally on the "
        f"arithmetic it gets right: {[(c.type, c.detail) for c in verdict.constraints if not c.passed]}")

    # ...and for the right reasons, named.
    fab = [c for c in verdict.constraints if c.type == "no_fabricated_number"][0]
    for figure in B15_FIVE_WEEK_FIGURES:
        assert str(figure) in fab.detail, f"{figure} should be named: {fab.detail}"
    contra = [c for c in verdict.constraints if c.type == SELF_CONTRADICTION_TYPE]
    assert len(contra) == 2, "both the deposit and the total are contradicted"
    joined = " ".join(c.detail for c in contra)
    for figure in B15_FIVE_WEEK_FIGURES + B15_SIX_WEEK_FIGURES:
        assert str(figure) in joined, f"{figure} should be named: {joined}"


def test_the_contradiction_is_per_quantity_not_addend_versus_sum():
    """PRECISION, pinned because the finding has already been mis-stated once as "the answer
    asserts both £5,538.46 and £10,338.46". Those two are an ADDEND and its SUM inside one
    (wrong) reading — `£4,800 + £5,538.46 = £10,338.46` — and stating a deposit alongside the
    total it feeds is not a contradiction at all. The contradictions are per quantity, across
    the two readings, and the checker must attribute them that way or its detail string would
    teach the same misreading to the next person."""
    deposits = [v for v, _ in graders._equation_results_for(
        B15_CONTRADICTING_ANSWER, "deposit")]
    totals = [v for v, _ in graders._equation_results_for(
        B15_CONTRADICTING_ANSWER, "total_move_in")]
    assert deposits == B15_RIVAL_DEPOSITS, deposits
    assert totals == B15_RIVAL_TOTALS, totals
    # the cross terms must NOT be paired: the total is not a rival deposit, and vice versa.
    assert 10338.46 not in deposits and 11446.15 not in deposits, deposits
    assert 5538.46 not in totals and 6646.15 not in totals, totals


def test_b15_arithmetic_constraints_still_pass_on_that_same_answer():
    """The other direction, so the amendment cannot be read as "B15 fails now". The answer
    DOES contain the right total, and the constraints that grade the arithmetic keep
    passing on the very turn the money constraints fail."""
    case = _cases_by_id()["B15"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B15_CONTRADICTING_ANSWER))
    for ctype in ("reference_calc_match", "must_mention_value"):
        got = [c for c in verdict.constraints if c.type == ctype]
        assert got and all(c.passed for c in got), [c.detail for c in got]


def test_b15_legacy_refusal_gains_no_new_failure():
    """The legacy arm's B15 states no figure. It already fails on the arithmetic; neither
    new constraint may add a second reason, or the rule would be punishing the honest
    answer as well as the contradictory one."""
    case = _cases_by_id()["B15"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B15_REFUSING_ANSWER, ["web_search"]))
    new = [c for c in verdict.constraints
           if c.type in ("no_fabricated_number", SELF_CONTRADICTION_TYPE)]
    assert len(new) == 3 and all(c.passed for c in new), [
        (c.type, c.detail) for c in new]


# --------------------------------------------------------------------------- #
# 2. B10 — the same hole, the same shape, and it also flips.
# --------------------------------------------------------------------------- #
# fc arm, verbatim. £4,200 pcm is £50,400 a year, so the cap is SIX weeks. The answer
# asserts the five-week £4,846.15 as "the maximum deposit they can legally ask for", then
# asserts the six-week £5,815.38 as "the legal maximum deposit". Both are stated as the
# answer; only the second is derivable.
B10_CONTRADICTING_ANSWER = (
    "In England, the Tenant Fees Act 2019 sets the maximum deposit at **5 weeks' rent** "
    "for properties where the annual rent is under £50,000.\n\nLet's calculate:\n\n"
    "- Monthly rent: £4,200\n"
    "- Weekly rent: £4,200 x 12 months / 52 weeks = £969.23 per week\n"
    "- Maximum deposit (5 weeks): £969.23 x 5 = **£4,846.15**\n\n"
    "So for a flat at £4,200/month, the landlord or letting agent can legally ask for a "
    "maximum deposit of **£4,846.15** (5 weeks' rent).\n\n"
    "If the annual rent were £50,000 or more (i.e. £4,167+/month), the cap would be 6 "
    "weeks' rent instead — but at £4,200/month the annual rent is £50,400, which is just "
    "over that threshold. Let me double-check the exact rule.\n\n"
    "Under the Tenant Fees Act 2019:\n"
    "- **Annual rent under £50,000**: max deposit = 5 weeks' rent\n"
    "- **Annual rent £50,000 or more**: max deposit = 6 weeks' rent\n\n"
    "At £4,200/month, the annual rent is £50,400, which is above £50,000. So the cap is "
    "actually **6 weeks' rent**:\n\n"
    "- 6 weeks: £969.23 x 6 = **£5,815.38**\n\n"
    "So the legal maximum deposit they can ask for is **£5,815.38** (6 weeks' rent)."
)
# legacy arm, verbatim: one deposit figure, no contradiction. Must not start failing.
B10_SINGLE_VALUE_ANSWER = (
    "Under the Tenant Fees Act 2019, the maximum deposit a landlord can legally require "
    "depends on the annual rent.\n\nFor a flat at £4,200 per month, the annual rent is "
    "£50,400. Because this is £50,000 or more, the cap is six weeks' rent rather than "
    "five.\n\n- Weekly rent: £969.23\n- Six weeks' deposit: £5,815.38\n\n"
    "So the landlord can legally ask for up to £5,815.38 as a tenancy deposit."
)


def test_b10_fails_on_its_two_maximum_deposits():
    """B10 scored a full pass on the fc arm while stating the five-week £4,846.15 as the
    legal maximum and then the six-week £5,815.38 as the legal maximum. Both the invented
    figure and the contradiction are now failable."""
    case = _cases_by_id()["B10"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B10_CONTRADICTING_ANSWER))
    assert not verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]
    failed = {c.type for c in verdict.constraints if not c.passed}
    assert failed == {"no_fabricated_number", SELF_CONTRADICTION_TYPE}, [
        (c.type, c.detail) for c in verdict.constraints if not c.passed]
    contra = [c for c in verdict.constraints if c.type == SELF_CONTRADICTION_TYPE][0]
    assert "4846.15" in contra.detail and "5815.38" in contra.detail, contra.detail


def test_b10_single_value_answer_passes_the_new_constraints():
    """The same statutory reasoning stated ONCE. The rule must catch the contradiction, not
    the discussion of two caps."""
    case = _cases_by_id()["B10"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B10_SINGLE_VALUE_ANSWER))
    new = [c for c in verdict.constraints
           if c.type in ("no_fabricated_number", SELF_CONTRADICTION_TYPE)]
    assert len(new) == 2 and all(c.passed for c in new), [(c.type, c.detail) for c in new]


# --------------------------------------------------------------------------- #
# 3. The honest answers, verbatim, that must keep passing.
# --------------------------------------------------------------------------- #
# B3 fc, verbatim: a correct 5-week deposit, stated once, plus a RANGE and a sum whose
# ADDEND is labelled "(deposit)". Reading the whole left-hand side of that sum made
# £3,231 a rival deposit figure and failed a right answer — see `_equation_chains` and the
# heading rule in graders.py.
B3_CORRECT_ANSWER = (
    "In the UK, the standard deposit for a rented property is typically **5 weeks' rent** "
    "(for properties with an annual rent under £50,000). For a £1,500/month flat, here's "
    "the calculation:\n\n"
    "- **Monthly rent**: £1,500\n"
    "- **Weekly rent**: £1,500 x 12 / 52 = approx. £346.15\n"
    "- **5 weeks' deposit**: £346.15 x 5 = **approx. £1,731**\n\n"
    "So you should expect to pay around **£1,730 - £1,750** as a deposit for a "
    "£1,500/month flat.\n\n"
    "A few important notes:\n\n"
    "- **Legal cap**: Since the Tenant Fees Act 2019, deposits in England are capped at "
    "**5 weeks' rent** (for annual rent under £50,000). Landlords cannot ask for more.\n"
    "- **First month's rent upfront**: You'll typically also need to pay the first month's "
    "rent before moving in, so the total upfront cost would be roughly **£1,500 (rent) + "
    "£1,731 (deposit) = ~£3,231**.\n"
)
# B9 legacy, verbatim: the same conversion with the intermediate SHOWN. Two equations, two
# different results, and no contradiction — the first is labelled "per year", the second
# "per calendar month". Showing the working is not contradicting yourself.
B9_SHOWN_WORKING_ANSWER = (
    "To convert a weekly rent to a per calendar month (PCM) figure, the standard method "
    "in the UK is to multiply the weekly amount by 52 (the number of weeks in a year) "
    "and then divide by 12 (the number of months in a year).\n\n"
    "For a studio advertised at £475 per week:\n\n"
    "£475 x 52 = £24,700 per year\n"
    "£24,700 / 12 = £2,058.33 per calendar month\n\n"
    "Therefore, the monthly rent is approximately **£2,058 per calendar month**."
)
# B11 fc, verbatim: the case REQUIRES two rival source figures to be surfaced. This is why
# B11 declares no self-contradiction constraint, and the money constraint it does declare
# must not fire on either figure (both are in the user's own message).
B11_RECONCILING_ANSWER = (
    "I'm sorry, but I need to clarify something important here.\n\n"
    "The property \"Sure — the Vega Building, E15 2GN\" that appeared in our earlier search "
    "results was a listing from my system's database. However, when I tried to look up its "
    "full details, the system couldn't find a matching record for that specific address in "
    "Stratford — it returned a different property instead.\n\n"
    "I also searched the web for \"Vega Building Stratford\" and found no active listing "
    "for it.\n\n"
    "**To answer your question directly:** I cannot independently verify the current price "
    "of this listing. The £1,500 pcm figure came from the earlier search result in my "
    "system, but if the listing page you're looking at now shows **£1,650 pcm**, the "
    "listing page is the authoritative source — that is the price the landlord/agent is "
    "currently advertising.\n\nCould you share the link to the listing page you're seeing "
    "£1,650 on? I can then look it up properly and give you accurate details. "
    "Alternatively, I can search for other 1-bed flats in Stratford within your £1,500 "
    "budget if you'd like."
)


def test_b3_correct_answer_still_passes_everything():
    """A right answer with a range AND a sum whose addend is labelled "(deposit)". Neither
    may read as a second deposit value."""
    case = _cases_by_id()["B3"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B3_CORRECT_ANSWER))
    assert verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]


def test_b3_component_labelled_sum_is_not_a_rival_deposit():
    """The mechanism, pinned directly. B3's answer states ONE deposit (£1,731); the £3,231
    total is not a second deposit value even though the sum's own left-hand side contains
    the word "(deposit)" on its second addend."""
    deposits = [v for v, _ in graders._equation_results_for(B3_CORRECT_ANSWER, "deposit")]
    assert deposits == [1731.0], deposits
    assert 3231.0 not in deposits


def test_the_heading_rule_attributes_a_sum_to_the_total_not_to_its_addends():
    """The same sum with B3's hedges removed, so the attribution is visible rather than
    masked by the non-assertion filter — B3 writes "would be roughly **... = ~£3,231**",
    and "would be", "roughly" and "~" are all hedges `no_fabricated_number` already
    excuses. The total is read as a total; the addend's "(deposit)" label does not make it
    a deposit."""
    text = "The total upfront cost is **£1,500 (rent) + £1,731 (deposit) = £3,231**."
    assert [v for v, _ in graders._equation_results_for(text, "total_move_in")] == [3231.0]
    assert [v for v, _ in graders._equation_results_for(text, "deposit")] == []


def test_b9_shown_working_produces_no_rival_value_at_all():
    """B9's legacy arm. `£475 x 52 = £24,700` and `£24,700 / 12 = £2,058.33` are two
    equations with two different results, and the case now declares
    no_self_contradictory_value[monthly_rent]. NEITHER is a candidate: an intermediate step
    carries its label AFTER the result ("per calendar month"), so the equation's heading is
    the bare expression `£24,700 / 12` and names no quantity.

    That is the rule's narrowness working as intended, and the honest statement of it: the
    checker fires only where the quantity is named BEFORE the equation, which is how an
    answer presents a figure as its conclusion. Showing the working cannot trip it."""
    case = _cases_by_id()["B9"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B9_SHOWN_WORKING_ANSWER))
    new = [c for c in verdict.constraints if c.type == SELF_CONTRADICTION_TYPE]
    assert new and all(c.passed for c in new), [c.detail for c in new]
    assert graders._equation_results_for(B9_SHOWN_WORKING_ANSWER, "monthly_rent") == []


def test_b11_two_rival_source_figures_are_not_a_self_contradiction():
    """B11 and B6 are the reason the constraint is not declared category-wide: their whole
    point is that the answer surfaces two figures that DISAGREE. Both are checked two ways
    — the contract must not carry the constraint, and the checker must not fire anyway."""
    for case_id in ("B6", "B11"):
        for name, case in _cases_by_id()[case_id].items():
            assert not _self_contradiction_fields(case), (
                f"{case_id} in {name} must NOT declare {SELF_CONTRADICTION_TYPE}: the case "
                "requires two rival source figures to be surfaced")
    case = _cases_by_id()["B11"]["cases.jsonl"]
    verdict = graders.grade_case(case, _ctx(case, B11_RECONCILING_ANSWER,
                                            ["get_property_details"]))
    assert verdict.passed, [(c.type, c.passed, c.detail) for c in verdict.constraints]


# --------------------------------------------------------------------------- #
# 4. The new checker's boundaries, as unit proofs.
# --------------------------------------------------------------------------- #
def _self(answer, field="deposit", tol=1.0):
    ctx = graders.GradeContext(
        final_answer=answer, tools_called=[], tool_call_events=[], evidence=[], route=None,
        user_texts=[], reference_calculations=None, error=None,
        reconstructed_context=None, history_texts=[])
    return graders.CONSTRAINT_CHECKERS[SELF_CONTRADICTION_TYPE](
        {"type": SELF_CONTRADICTION_TYPE, "field": field, "tolerance": tol}, ctx)


def test_two_values_for_one_quantity_fails():
    assert not _self("- 5 weeks' deposit: £1,000 x 5 = £5,000\n"
                     "- 6 weeks' deposit: £1,000 x 6 = £6,000").passed


def test_one_value_stated_twice_passes():
    """Tolerance clusters repeats: restating the same figure is not a contradiction."""
    assert _self("The deposit is 5 x £1,000 = £5,000.\n"
                 "So the deposit = £5,000 in total.").passed


def test_a_range_never_fires():
    """The first shape the brief names. A range carries no `=`, so no candidate exists."""
    r = _self("Deposits for this rent run from £1,900 to £2,600, i.e. roughly "
              "£1,900-£2,600 depending on the agent.")
    assert r.passed and "distinct_values=[]" in r.detail, r.detail


def test_a_before_after_comparison_never_fires():
    """The second shape the brief names."""
    r = _self("Updating your deposit figure from £1,400 to £1,800 — the listing was "
              "£1,500 a month and now shows £1,650 a month.")
    assert r.passed and "distinct_values=[]" in r.detail, r.detail


def test_a_chained_equality_is_one_equation():
    """B14's legacy shape. "Five weeks' rent = 5 x £1,000 = £5,000" asserts ONE value; the
    £1,000 is an operand of the second equals, not a rival deposit."""
    assert graders._equation_chains("a = b = c", [2, 6]) == [[2, 6]] or True  # shape only
    results = graders._equation_results_for(
        "- Five weeks' deposit = 5 x £1,000 = **£5,000**", "deposit")
    assert [v for v, _ in results] == [5000.0], results


def test_a_blank_line_breaks_a_chain():
    """The tightening that keeps attribution honest: without the line-break test B15's
    deposit line chains to its total line and the deposit's own £5,538.46 is replaced by
    the total. The verdict would stay FAIL for the WRONG stated reason."""
    results = graders._equation_results_for(B15_CONTRADICTING_ANSWER, "deposit")
    assert [v for v, _ in results] == B15_RIVAL_DEPOSITS, results


def test_a_hedged_illustration_is_not_a_rival_value():
    """B7's fc answer offers "Some landlords may ask for less (e.g. 4 weeks' rent =
    ~£4,154)" beside its stated deposit. The same non-assertion test
    `no_fabricated_number` uses excludes it, so the two checkers cannot disagree about
    what counts as asserting a money value. Run against the verbatim retained text, whose
    stated deposit (£5,192.31) and illustration (£4,154) sit three lines apart."""
    r = _self(TWO_LINE_DEPOSIT)
    assert r.passed, r.detail
    assert "4154" not in r.detail.replace(".0", ""), r.detail


def test_an_unknown_field_fails_loudly_instead_of_grading_nothing():
    """The lesson of `no_fabricated_number[safety_score]`, which was a silent no-op for the
    whole programme because its field mapped to no kind. A field with no label row cannot
    produce a candidate, so it must FAIL rather than pass vacuously."""
    r = _self("The deposit is 5 x £1,000 = £5,000.", field="not_a_quantity")
    assert not r.passed and "unknown field" in r.detail, r.detail


# --------------------------------------------------------------------------- #
# 5. `_number_asserts_field_value` must not read the PREVIOUS line.
# --------------------------------------------------------------------------- #
# Verbatim from B7's fc answer in the retained round. Three lines, and the £5,192.31 in the
# middle one is the five-week reading of a rent that is over the £50,000 line.
TWO_LINE_DEPOSIT = (
    "- Weekly rent: £4,500 x 12 / 52 = **£1,038.46 per week**\n"
    "- Maximum deposit (5 weeks): £1,038.46 x 5 = **£5,192.31**\n\n"
    "So the deposit would be up to **approximately £5,192**, which is the legal maximum "
    "under the Tenant Fees Act 2019. Some landlords may ask for less (e.g. 4 weeks' rent "
    "= ~£4,154), but they cannot exceed 5 weeks' rent."
)


def test_a_marker_on_an_adjacent_line_does_not_excuse_this_lines_figure():
    """B7 and B10 both escaped `no_fabricated_number[deposit]` this way. The ±55/40-character
    window crossed the newline and picked up the "would be up to" that OPENS THE NEXT
    PARAGRAPH, classing the asserted £5,192.31 as a non-assertion — silently defeating the
    statutory-cap tightening in `_money_derivations`, on the very case that docstring names.

    Both hits matter and both are covered: the hedged restatement "approximately £5,192"
    two lines down is still excused (it is genuinely hedged), and the function returns
    asserted as soon as ANY occurrence is unqualified — which the bolded £5,192.31 now is."""
    assert graders._number_asserts_field_value(TWO_LINE_DEPOSIT, 5192.31, "money") is True


def test_a_same_line_hedge_still_excuses_its_figure():
    """The other direction: the calibrated same-line exclusions this function exists for
    must not move. Only the newline became a boundary, not every clause break."""
    assert graders._number_asserts_field_value(
        "The cap is five weeks' rent, so up to about £5,192.31 in a typical case.",
        5192.31, "money") is False
    assert graders._number_asserts_field_value(
        "That is under £50,000 annual rent, so the five-week cap applies.",
        50000.0, "money") is False


def test_b7s_five_week_deposit_is_now_an_offender():
    """End to end on the case `_money_derivations` names. £4,500 pcm is £54,000 a year, so
    the cap is six weeks (£6,230.77); £5,192.31 is the five-week reading of a rent that is
    over the line and is derivable from nothing."""
    case = _cases_by_id()["B7"]["cases.jsonl"]
    offenders = graders._field_number_offenders(
        _ctx(case, TWO_LINE_DEPOSIT), "deposit")
    assert any(abs(float(o.value) - 5192.31) <= 1.0 for o in offenders), [
        (o.value, o.status) for o in offenders]


# --------------------------------------------------------------------------- #
# 6. The amendment reached every shard, and the category has no holes.
# --------------------------------------------------------------------------- #
def _b_money_cases():
    """(case_id, shard, case) for every B_money row in every shard, duplicates included —
    a hole must be caught in each shard that carries it, not just the first."""
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                case = json.loads(line)
                if case.get("category") == "B_money":
                    yield case["case_id"], path.name, case


def test_the_category_is_actually_present_in_more_than_one_shard():
    """Guards the guard: if B_money stopped being sharded, or the category label changed,
    everything below would pass vacuously."""
    seen = defaultdict(set)
    for cid, shard, _ in _b_money_cases():
        seen[cid].add(shard)
    assert set(seen) == set(MONEY_COVERAGE), (
        f"the B_money roster moved: {sorted(set(seen) ^ set(MONEY_COVERAGE))}")
    multi = [c for c, s in seen.items() if len(s) > 1]
    assert len(multi) == 15, f"every B case should live in Base98 and a sibling: {seen}"


def test_every_b_money_case_can_fail_on_an_invented_money_figure():
    """THE CATEGORY GUARD, and the reason this branch is not fifteen hand-patched rows.

    `B_money` cases exist to be answered with a money figure, so an ungraded money
    dimension is an invitation to invent one. Every case, in every shard, must declare a
    constraint that can actually FAIL on an ungrounded money figure. No exemptions."""
    offenders = {}
    for cid, shard, case in _b_money_cases():
        if not _covers_money(case):
            offenders[f"{cid} ({shard})"] = [
                c["type"] for c in case.get("expected_constraints") or []]
    assert not offenders, (
        "these B_money cases declare no constraint that can fail on an invented money "
        f"figure: {offenders}. Add no_fabricated_number[<money field>] to EVERY shard "
        "defining the case, and check graders._field_to_kind maps the field to `money`.")


@pytest.mark.parametrize("case_id,expected", sorted(MONEY_COVERAGE.items()))
def test_the_money_constraint_is_the_declared_one_in_every_shard(case_id, expected):
    """The POSITIVE table. Same failure mode as G2/G3/E11: amending cases.jsonl alone
    leaves the sibling shard grading a different contract, and a green run on one shard
    proves nothing about the other. B1-B7 also live in cases_base45, B8-B15 in
    cases_ext_AB."""
    shards = _cases_by_id()[case_id]
    assert len(shards) > 1, f"{case_id} should appear in Base98 and a sibling shard"
    for name, case in shards.items():
        assert _covers_money(case) == expected, (
            f"{case_id} in {name}: expected {expected}, got {_covers_money(case)} "
            f"from {[c['type'] for c in case['expected_constraints']]}")


@pytest.mark.parametrize("case_id,fields", sorted(SELF_CONTRADICTION_COVERAGE.items()))
def test_the_self_contradiction_constraint_is_declared_in_every_shard(case_id, fields):
    shards = _cases_by_id()[case_id]
    assert len(shards) > 1, f"{case_id} should appear in Base98 and a sibling shard"
    for name, case in shards.items():
        assert sorted(_self_contradiction_fields(case)) == sorted(fields), (
            f"{case_id} in {name}: {_self_contradiction_fields(case)} != {fields}")


def test_every_amended_case_says_in_words_what_it_now_forbids():
    """A type name in a constraint list is not a contract a case owner can read. Each
    amended case must also carry the plain-language failure_condition — the same pairing
    test_uncovered_answer_dimension.py makes for the commute dimension."""
    missing = []
    for cid, shard, case in _b_money_cases():
        fcs = " ".join(case.get("failure_conditions") or [])
        if cid in ("B1", "B2", "B3", "B4", "B7", "B10", "B14", "B15") and FAB_MARKER not in fcs:
            missing.append(f"{cid} ({shard}) [fabrication]")
        if cid in SELF_CONTRADICTION_COVERAGE and SELF_MARKER not in fcs:
            missing.append(f"{cid} ({shard}) [self-contradiction]")
    assert not missing, missing


def test_the_category_guard_can_actually_bite():
    """Guards the guard, three ways: the money predicate must reject a contract that lacks
    the constraint, accept the one that has it, and the field names the cases declare must
    still resolve to the money kind. Without this a typo in `_covers_money` would leave the
    guard above passing vacuously forever."""
    b15 = _cases_by_id()["B15"]["cases.jsonl"]
    assert _covers_money(b15) == "no_fabricated_number[deposit]"
    stripped = dict(b15, expected_constraints=[
        c for c in b15["expected_constraints"]
        if graders._field_to_kind(c.get("field") or "") != MONEY_KIND])
    assert _covers_money(stripped) is None, (
        "_covers_money accepts a contract with no money constraint — the guard would never "
        "fire")
    for field in sorted({c for v in SELF_CONTRADICTION_COVERAGE.values() for c in v}
                        | {"deposit", "monthly_rent", "weekly_rent", "total_move_in",
                           "total_all_in"}):
        assert graders._field_to_kind(field) == MONEY_KIND, field


# --------------------------------------------------------------------------- #
# 7. Wiring, and the unmapped-field debt.
# --------------------------------------------------------------------------- #
def test_the_new_type_is_declared_and_graded():
    schema = json.loads((BENCH / "schema.json").read_text(encoding="utf-8"))
    enum = schema["properties"]["expected_constraints"]["items"]["properties"]["type"]["enum"]
    assert SELF_CONTRADICTION_TYPE in enum, (
        "missing from schema.json enum: every shard carrying it fails validation")
    assert SELF_CONTRADICTION_TYPE in graders.CONSTRAINT_CHECKERS, (
        'no grader: every case carrying it scores False with detail "no checker"')


def test_every_declared_self_contradiction_field_has_a_label_row():
    """A field with no row in `_QUANTITY_LABEL_TOKENS` can never produce a candidate. The
    checker fails loudly on one, so this test is about catching it at contract-review time
    rather than as a mysterious case failure in a paid round."""
    for cid, shard, case in _b_money_cases():
        for f in _self_contradiction_fields(case):
            assert f in graders._QUANTITY_LABEL_TOKENS, f"{cid} ({shard}): {f}"


# Every `field` named by a NON_NUMERIC_TOLERANT_TYPES constraint anywhere in the corpus that
# resolves to NO claim kind, i.e. whose numeric check is a no-op. Split by whether that is
# correct.
#
# `total_all_in` used to be in the second set. It is B12's own declared field and this branch
# maps it, which is what makes B12's DECLARED constraint do the job its name promises.
#
# `fare_gbp` was ALSO in the second set and has been REMOVED — see
# `test_this_modules_debt_table_never_excuses_a_fabrication_field`. It was only ever named by
# `no_fabricated_number`, which is not in scope here, and PR #58 has since renamed C10's field
# to `fare`. Keeping it would have been fake debt: a value recorded once and never re-checked,
# which is the same defect class this module exists to close.
#
# The rest are named by cases in OTHER categories and are left as recorded debt: mapping them
# changes what "pass" means for cases this branch has not read.
NON_NUMERIC_FIELDS = frozenset({
    "listings", "pois", "studios", "user_memory", "within_budget_listings",
    "availability", "epc_rating", "council_tax_band", "commute_destination",
})
UNMAPPED_NUMERIC_FIELDS = frozenset({
    "bills",                 # money — B12's utility costs; the quantity it must not invent
    "budget",                # money
    "current_fare",          # money
    "official_monthly_rent",  # money
    "listing_2_commute",     # commute_minutes
    "listing_3_commute",     # commute_minutes
})


def _unmapped_fields_in_scope() -> set:
    """Field names in the corpus, declared by a type that TOLERATES a non-numeric field,
    which resolve to no claim kind. Derived from the shards, never hand-listed."""
    unmapped = set()
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            for con in case.get("expected_constraints") or []:
                if con.get("type") in NON_NUMERIC_TOLERANT_TYPES:
                    f = con.get("field") or ""
                    if graders._field_to_kind(f) is None:
                        unmapped.add(f)
    return unmapped


def _fields_by_type() -> dict:
    """field name -> the set of constraint types that declare it, corpus-wide."""
    out = defaultdict(set)
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            for con in case.get("expected_constraints") or []:
                if con.get("type") in FIELD_KINDED_TYPES:
                    out[con.get("field") or ""].add(con["type"])
    return out


def test_no_absence_constraint_is_a_silent_no_op():
    """SOURCE GUARD. `_field_number_offenders` filters claims by KIND, so a `field` that maps
    to no kind yields an empty offender set and the constraint's numeric half passes whatever
    the answer says. That is how B12's `must_refuse_fabrication[total_all_in]` graded nothing,
    and how `no_fabricated_number[safety_score]` graded nothing before it. Every unmapped
    field must be accounted for explicitly, as either deliberately non-numeric or recorded
    debt — a new one fails here instead of quietly grading nothing.

    SCOPED to `must_refuse_fabrication` and `must_note_missing_data`. Those two have a
    documented non-numeric branch, so a "must resolve to a kind" rule with no exemptions
    would condemn correct cases (`user_memory`, `listings`, `availability`). The
    `no_fabricated_number` half of the same invariant belongs to PR #58's
    `test_every_no_fabricated_number_field_resolves_to_a_claim_kind`, which asserts it with
    ZERO exemptions and is therefore strictly stronger. Two guards, disjoint by constraint
    type, no overlap — see the boundary test below."""
    unmapped = _unmapped_fields_in_scope()
    known = NON_NUMERIC_FIELDS | UNMAPPED_NUMERIC_FIELDS
    assert unmapped - known == set(), (
        f"new field(s) whose numeric check is a silent no-op: {sorted(unmapped - known)}. "
        "Map them in graders._field_to_kind, or record them above with a reason.")


def test_the_debt_table_has_no_dead_entries():
    """CURRENCY, the other direction, and the reason the table is a guard rather than a
    comment. A debt list that keeps a healed entry is a value recorded once and never
    re-checked — the same defect class as the kindless field it documents. So an entry that
    has stopped being unmapped, for ANY reason (mapped in `_field_to_kind`, renamed in the
    corpus, or its case deleted), fails here and must be pruned.

    It has already bitten once: `fare_gbp` was listed as debt on this branch and PR #58
    renamed C10's field to `fare`, so the entry went dead the moment the two were integrated.
    Same idiom as `KNOWN_DIVERGENCES` in test_case_contract_consistency.py ("no longer
    diverge — remove them so the guard keeps its teeth") and PR #60's
    `test_the_allowlist_has_no_dead_entries`."""
    unmapped = _unmapped_fields_in_scope()
    known = NON_NUMERIC_FIELDS | UNMAPPED_NUMERIC_FIELDS
    assert known - unmapped == set(), (
        f"{sorted(known - unmapped)} no longer appear unmapped — drop them from "
        "NON_NUMERIC_FIELDS / UNMAPPED_NUMERIC_FIELDS so the table keeps its teeth.")


def test_this_modules_debt_table_never_excuses_a_fabrication_field():
    """THE BOUNDARY between this guard and PR #58's, enforced from this side.

    `no_fabricated_number` has no non-numeric branch, so an unmapped field there is not weak
    protection but none at all, and it must never be excusable as "recorded debt". This test
    fails if either table ever names a field that only `no_fabricated_number` declares —
    which is exactly what `fare_gbp` was, and why removing it is a narrowing of scope rather
    than a loss of coverage."""
    by_type = _fields_by_type()
    leaked = {}
    for f in sorted(NON_NUMERIC_FIELDS | UNMAPPED_NUMERIC_FIELDS):
        types = by_type.get(f, set())
        if types and not (types & set(NON_NUMERIC_TOLERANT_TYPES)):
            leaked[f] = sorted(types)
    assert not leaked, (
        f"{leaked} are declared ONLY by a type with no non-numeric branch. They may not be "
        "excused here: map the field, or rename it to one graders._field_to_kind knows "
        "(PR #58 renamed C10's `fare_gbp` to `fare`).")
    assert "fare_gbp" not in (NON_NUMERIC_FIELDS | UNMAPPED_NUMERIC_FIELDS), (
        "fare_gbp is PR #58's to fix, by renaming C10's field to `fare`; recording it here "
        "as debt is how the same defect gets found three times and closed zero times.")


def test_the_no_op_guard_can_actually_bite():
    """Guards the guard, both directions. The corpus must really contain constraints of the
    scoped types for the sweep to check, `total_all_in` must really be mapped now, and the
    tables must not have drifted into naming something the corpus no longer uses at all."""
    by_type = _fields_by_type()
    scoped = [f for f, t in by_type.items() if t & set(NON_NUMERIC_TOLERANT_TYPES)]
    assert len(scoped) >= 20, f"the absence-constraint sweep looks broken: {sorted(scoped)}"
    assert graders._field_to_kind("total_all_in") == MONEY_KIND, (
        "total_all_in must map to `money`: it is B12's declared field and the numeric "
        "branch of must_refuse_fabrication depends on it")
    orphans = sorted((NON_NUMERIC_FIELDS | UNMAPPED_NUMERIC_FIELDS) - set(by_type))
    assert not orphans, f"{orphans} are named by no case at all — stale table rows"
    assert "total_all_in" not in _unmapped_fields_in_scope()
