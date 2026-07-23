"""Commute-minute claim extraction — thresholds vs measurements (2026-07-23 ruling).

Two registered grader defects, fixed and locked here:

1. **English thresholds were one-sided.** `_MIN_BOUNDARY` was only consulted in a
   24-character window BEFORE the number, so a correct answer failed on phrasing distance
   alone: "under your 25-minute limit" was spared, "meets your 25-minute limit" was not —
   the identical user threshold, graded differently. Observed live on E11
   (`ungrounded=[25.0]`).
2. **CJK minute claims were never extracted from the answer at all.** `_CJK_MINUTES_RE`
   only built the evidence POOL from tool data, so a zh reply produced zero
   `commute_minutes` claims and `commute_leq_minutes` passed vacuously — a fabricated zh
   journey time could not be caught. That is a blind spot, not a safety margin.

The anti-washout half matters as much as the fix: widening threshold suppression must NOT
turn a real fabrication green. The money tests below pin E11's genuinely unsupported
market figures — they must stay unsupported.
"""
from __future__ import annotations

import pytest

from evaluation.metrics.graders import GradeContext, grade_grounding


def _minute_claims(answer, evidence=None):
    ctx = GradeContext(final_answer=answer, tools_called=[], tool_call_events=[],
                       evidence=evidence or [])
    return [c for c in grade_grounding(ctx).claims if c.kind == "commute_minutes"]


def _money_claims(answer, evidence=None):
    ctx = GradeContext(final_answer=answer, tools_called=[], tool_call_events=[],
                       evidence=evidence or [])
    return [c for c in grade_grounding(ctx).claims if c.kind == "money"]


COMMUTE_EVIDENCE = [{"tool": "calculate_commute",
                     "data": {"duration_minutes": 24, "route_source": "TfL"}}]


# ── 1. English thresholds: marker on EITHER side is not a measurement ──
@pytest.mark.parametrize("answer", [
    "The journey is well under your 25-minute limit.",            # marker BEFORE
    "Your limit was 25 minutes, and this beats it.",              # marker BEFORE
    "This easily meets your 25-minute limit.",                    # marker AFTER (E11 r2)
    "It would meet your 25-minute commute requirement.",          # marker AFTER (E11 r3)
    "That is 25 minutes or less door to door.",                   # marker AFTER
    "Anything within 30 minutes works for you.",                  # marker BEFORE
    "We kept it below your 25 minute ceiling.",                   # marker AFTER
])
def test_threshold_restatement_is_not_a_commute_claim(answer):
    assert _minute_claims(answer) == [], f"threshold wrongly extracted from: {answer!r}"


@pytest.mark.parametrize("answer,value", [
    ("The commute is 24 minutes by tube.", 24.0),
    ("It takes approximately 20 minutes via the Jubilee line.", 20.0),   # hedge still asserts
    ("Journey time: 24 minutes.", 24.0),
])
def test_measured_duration_is_still_a_claim(answer, value):
    claims = _minute_claims(answer, COMMUTE_EVIDENCE)
    assert [c.value for c in claims] == [value]


def test_threshold_and_measurement_together_keeps_only_the_measurement():
    """The realistic shape: state the real duration AND restate the user's bound."""
    claims = _minute_claims(
        "The commute is 24 minutes, comfortably inside your 25-minute limit.",
        COMMUTE_EVIDENCE)
    assert [c.value for c in claims] == [24.0]
    assert claims[0].status == "grounded"


# ── 2. CJK: symmetric extraction, previously a total blind spot ──
@pytest.mark.parametrize("answer,value", [
    ("通勤约30分钟。", 30.0),
    ("通勤时间是24分钟。", 24.0),
    ("从这里到国王学院大约需要 24 分钟。", 24.0),
])
def test_cjk_measured_duration_is_extracted(answer, value):
    claims = _minute_claims(answer)
    assert [c.value for c in claims] == [value], f"zh claim missed in {answer!r}"


@pytest.mark.parametrize("answer", [
    "你的通勤上限是30分钟。",
    "不超过30分钟。",
    "通勤时间在30分钟以内。",
    "我们会把通勤控制在30分钟之内。",
    "少于30分钟即可。",
])
def test_cjk_threshold_restatement_is_not_a_claim(answer):
    assert _minute_claims(answer) == [], f"zh threshold wrongly extracted from: {answer!r}"


def test_cjk_fabricated_duration_is_now_catchable():
    """The point of closing the blind spot: a zh duration with no evidence must be
    classified, not silently ignored."""
    claims = _minute_claims("通勤时间是45分钟。", COMMUTE_EVIDENCE)
    assert [c.value for c in claims] == [45.0]
    assert claims[0].status != "grounded"


def test_cjk_measurement_and_threshold_together():
    claims = _minute_claims("通勤约24分钟，在你要求的30分钟以内。", COMMUTE_EVIDENCE)
    assert [c.value for c in claims] == [24.0]


# ── 3. ANTI-WASHOUT: the threshold fix must not green a real fabrication ──
def test_e11_r1_market_estimate_range_stays_unsupported():
    """E11 r1 verbatim shape. These are market figures with no tool evidence; widening
    threshold suppression is about MINUTES and must not touch money classification."""
    answer = ("An ensuite in a shared flat (private room + bathroom, shared kitchen) is "
              "often available at £900-£1,200 in Stratford and would easily meet your budget.")
    claims = _money_claims(answer)
    vals = {c.value for c in claims}
    assert 900.0 in vals and 1200.0 in vals
    assert all(c.status != "grounded" for c in claims if c.value in (900.0, 1200.0))


def test_e11_r1_higher_budget_suggestion_stays_unsupported():
    answer = ("Studios in Stratford and nearby East London areas often start around "
              "£1,400-£1,500/month. Raising the cap a little could open up options.")
    claims = _money_claims(answer)
    vals = {c.value for c in claims}
    assert 1400.0 in vals and 1500.0 in vals
    assert all(c.status != "grounded" for c in claims if c.value in (1400.0, 1500.0))


def test_money_thresholds_are_unaffected_by_the_minute_fix():
    """A budget bound and a commute bound must not bleed into each other: the minute
    markers are consulted only for commute_minutes."""
    answer = "The commute is 24 minutes, and rents here run to £1,300 per month."
    minutes = _minute_claims(answer, COMMUTE_EVIDENCE)
    money = _money_claims(answer)
    assert [c.value for c in minutes] == [24.0]
    assert 1300.0 in {c.value for c in money}


# ── clause-bounded windows (the two-sided rule must not over-suppress) ──
def test_threshold_after_a_measurement_does_not_suppress_it_cjk():
    """A bound binds inside its own clause. Without clause truncation a two-sided window
    swallows a real measurement whenever the user's limit is restated right after it."""
    claims = _minute_claims("通勤约24分钟，在你要求的30分钟以内。", COMMUTE_EVIDENCE)
    assert [c.value for c in claims] == [24.0]


def test_threshold_before_a_measurement_in_a_prior_clause_does_not_suppress_it():
    claims = _minute_claims("你的上限是30分钟，实际通勤是24分钟。", COMMUTE_EVIDENCE)
    assert [c.value for c in claims] == [24.0]


def test_english_threshold_in_a_prior_clause_does_not_suppress_the_measurement():
    claims = _minute_claims(
        "Your limit was 25 minutes. The actual journey is 24 minutes.", COMMUTE_EVIDENCE)
    assert [c.value for c in claims] == [24.0]


# ── must_complete_requested_dimensions ──
from evaluation.metrics.graders import _c_must_complete_requested_dimensions  # noqa: E402


def _dim_check(dimensions, tools_called):
    ctx = GradeContext(final_answer="", tools_called=list(tools_called),
                       tool_call_events=[], evidence=[])
    return _c_must_complete_requested_dimensions({"dimensions": dimensions}, ctx)


def test_dimension_check_passes_when_every_family_executed():
    r = _dim_check(["commute", "nearby", "safety"],
                   ["search_properties", "calculate_commute", "search_nearby_pois",
                    "check_safety"])
    assert r.passed is True


def test_dimension_check_fails_the_e11_baseline_truncated_shape():
    """The two baseline soft-wrap answers ran search+safety(+compare) and openly said the
    commute and pharmacy checks were not done — they must now FAIL, not pass by saying
    less."""
    r = _dim_check(["commute", "nearby", "safety"],
                   ["search_properties", "check_safety", "compare_or_rank_areas"])
    assert r.passed is False
    assert "commute" in r.detail and "nearby" in r.detail


def test_dimension_check_ignores_prose_and_judges_executed_tools():
    """A reply may claim the commute was checked; only an executed tool completes it."""
    ctx = GradeContext(
        final_answer="Commute to Canary Wharf: about 20 minutes. Pharmacies: 5 nearby.",
        tools_called=["search_properties", "check_safety"], tool_call_events=[], evidence=[])
    r = _c_must_complete_requested_dimensions(
        {"dimensions": ["commute", "nearby", "safety"]}, ctx)
    assert r.passed is False


def test_dimension_check_accepts_any_satisfying_tool_in_the_family():
    assert _dim_check(["commute"], ["check_transport_cost"]).passed is True
    assert _dim_check(["commute"], ["calculate_commute_cost"]).passed is True


def test_unknown_dimension_fails_closed():
    r = _dim_check(["teleportation"], ["search_properties"])
    assert r.passed is False and "UNKNOWN_DIMENSION" in r.detail


# ── 4. hypothetical CONSTRAINT vs measured value (2026-07-23 ruling) ──
# A number is excluded only when ALL THREE hold in the SAME clause: a proposal cue, a
# constraint-adjustment cue, and NO measurement cue. `e.g.` alone is deliberately NOT a
# free pass — a fabricated duration must not be able to hide behind it.
@pytest.mark.parametrize("answer", [
    "Consider a slightly longer commute (e.g. 30 minutes) which opens up more areas.",
    "Would you accept up to 30 minutes?",
    "Would you be open to a longer commute of for example 30 minutes?",
    "可以考虑把通勤放宽到30分钟。",
    "比如把上限调整到30分钟，选择会更多。",
])
def test_hypothetical_constraint_is_not_a_commute_claim(answer):
    assert _minute_claims(answer) == [], f"hypothetical wrongly extracted from: {answer!r}"


def test_cues_split_across_clauses_do_not_exclude():
    """All three conditions must hold in the SAME clause. When the constraint cue sits in
    a different clause from the number, the number stays a claim — the fail-safe
    direction, so a fabrication cannot be laundered by a nearby proposal."""
    answer = "Would you be open to a longer commute, for example 30 minutes?"
    assert [c.value for c in _minute_claims(answer)] == [30.0]


@pytest.mark.parametrize("answer,value", [
    # proposal cue present, but it STATES a measurement -> still a claim
    ("For example, the commute takes 30 minutes.", 30.0),
    ("The journey duration is e.g. 30 minutes.", 30.0),
    ("比如说，通勤耗时30分钟。", 30.0),
    # proposal cue but NO constraint-adjustment cue -> still a claim
    ("For example, 30 minutes on the Jubilee line.", 30.0),
])
def test_proposal_cue_alone_never_excludes_a_measurement(answer, value):
    claims = _minute_claims(answer, COMMUTE_EVIDENCE)
    assert [c.value for c in claims] == [value], f"measurement wrongly excluded from: {answer!r}"


def test_e11_r3_suggestion_excluded_but_the_real_answer_still_graded():
    """E11 r3 verbatim: the 30 is a proposed looser bound, the 24 is the measured journey."""
    answer = ("The commute is 24 minutes. Consider a slightly longer commute "
              "(e.g. 30 minutes) which opens up more affordable areas.")
    assert [c.value for c in _minute_claims(answer, COMMUTE_EVIDENCE)] == [24.0]


def test_hypothetical_classifier_does_not_touch_money():
    """E11 r1's rent fabrication must keep failing — this taxonomy fix is minutes-only."""
    answer = ("Consider raising your limit — an ensuite is often available at "
              "£900-£1,200 in Stratford.")
    vals = {c.value for c in _money_claims(answer)}
    assert 900.0 in vals and 1200.0 in vals
    assert all(c.status != "grounded" for c in _money_claims(answer)
               if c.value in (900.0, 1200.0))


def test_dimension_check_is_independent_of_the_claim_taxonomy():
    """must_complete_requested_dimensions judges EXECUTED tools, so no amount of
    reclassifying numbers can make an E11 answer that never ran commute/nearby pass."""
    r = _dim_check(["commute", "nearby", "safety"],
                   ["search_properties", "check_safety"])
    assert r.passed is False
    assert "commute" in r.detail and "nearby" in r.detail
