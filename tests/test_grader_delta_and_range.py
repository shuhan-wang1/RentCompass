"""Three taxonomy/semantics rules added after re-scoring retained evidence (2026-07-23).

Each one exists because the offline contract-delta over the six retained idp98 rounds
produced a verdict nobody could defend:

  * C12 flipped PASS->FAIL on a DERIVED DIFFERENCE (「每天多花约 40-50 分钟」) being read
    as a third journey time. It only passed before because CJK minutes were never read at
    all — the defect predates the CJK fix, which merely exposed it.
  * E2 flipped PASS->FAIL for honestly reporting the nearest alternative and labelling it
    "slightly over your 30-minute limit". Its old pass was itself accidental: "within your
    budget" from the PREVIOUS bullet leaked into the 24-char window before "37 minutes"
    and suppressed the claim.
  * Ranges anchored on the unit, so 「15-26 分钟」 yielded only 26 — a fabricated lower
    bound was unreachable.
"""
from __future__ import annotations

import pytest

from evaluation.metrics import graders


def _ctx(answer: str, evidence=None, tools=("calculate_commute",)):
    return graders.GradeContext(
        final_answer=answer,
        tools_called=list(tools),
        tool_call_events=[],
        evidence=evidence or [],
        route="compare_commute",
        user_texts=[],
        reference_calculations=None,
        error=None,
        reconstructed_context=None,
        history_texts=[],
    )


def _commute_evidence(*durations):
    return [{"tool": "calculate_commute", "data": {"duration_minutes": d}} for d in durations]


def _minutes(answer, evidence=None):
    g = graders.grade_grounding(_ctx(answer, evidence))
    return {c.value: c.status for c in g.claims if c.kind == "commute_minutes"}


# ── a difference is not a journey time ─────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    "两者月票相近，但每天多花约 40-50 分钟在路上。",
    "Camberwell 比 Sinclair Road 慢了 38 分钟。",
    "That route saves 12 minutes each way.",
    "The difference of 20 minutes adds up over a month.",
])
def test_difference_figures_are_not_commute_claims(answer):
    assert _minutes(answer) == {}


def test_the_c12_shape_no_longer_reports_a_fabricated_duration():
    """The real C12 sentence, against the real C12 evidence pool {15, 26, 41, 53}."""
    answer = ("**88 Camberwell Road** 通勤时间较长（约 41-53 分钟），且需换乘两次，"
              "但每天多花约 40-50 分钟在路上。")
    claims = _minutes(answer, _commute_evidence(15, 26, 41, 53))
    assert 50.0 not in claims, "the derived daily difference must not be a journey time"
    assert claims.get(41.0) == "grounded"
    assert claims.get(53.0) == "grounded"


def test_a_measured_duration_next_to_a_difference_is_still_graded():
    """The delta cue excuses its own clause, not the whole answer."""
    answer = "通勤时间约 41 分钟。相比之下每天多花约 50 分钟。"
    claims = _minutes(answer, _commute_evidence(41))
    assert claims.get(41.0) == "grounded"
    assert 50.0 not in claims


def test_delta_cue_overrides_the_approximation_hedge():
    """约 is a measurement cue for the hypothetical classifier; a difference outranks it."""
    assert _minutes("每天多花约 45 分钟。") == {}


def test_bare_more_does_not_excuse_a_measurement():
    """The cue list is deliberately comparative-only; a loose cue would swallow claims."""
    claims = _minutes("通勤时间约 41 分钟，比我想的更多。", _commute_evidence(41))
    assert claims.get(41.0) == "grounded"


# ── ranges yield both endpoints ────────────────────────────────────────────────

@pytest.mark.parametrize("answer,expected", [
    ("通勤时间约 15-26 分钟。", {15.0, 26.0}),
    ("The journey takes 15-26 minutes.", {15.0, 26.0}),
    ("The journey takes 15 to 26 minutes.", {15.0, 26.0}),
    ("通勤时间 15 至 26 分钟。", {15.0, 26.0}),
])
def test_both_endpoints_of_a_range_become_claims(answer, expected):
    assert set(_minutes(answer, _commute_evidence(15, 26))) == expected


def test_a_fabricated_lower_bound_is_now_catchable():
    """Only the upper bound is tool-backed; the invented lower bound must surface."""
    claims = _minutes("通勤时间约 5-26 分钟。", _commute_evidence(26))
    assert claims.get(5.0) == "unsupported"
    assert claims.get(26.0) == "grounded"


def test_a_single_figure_is_unaffected_by_the_range_rule():
    assert set(_minutes("通勤时间约 26 分钟。", _commute_evidence(26))) == {26.0}


def test_range_rule_does_not_fire_across_a_clause_break():
    """「…26。 30 分钟」 is two statements, not a 26-30 range."""
    assert 26.0 not in _minutes("上一段是 26。 通勤 30 分钟。", _commute_evidence(30))


# ── an explicitly-labelled non-match does not violate commute_leq_minutes ──────

CON = {"type": "commute_leq_minutes", "dest": "King's College London", "value": 30}


def _commute_leq(answer, evidence):
    return graders.CONSTRAINT_CHECKERS["commute_leq_minutes"](CON, _ctx(answer, evidence))


def test_the_e2_shape_passes_when_the_overage_is_labelled():
    answer = ("Unfortunately there are no studios within your criteria.\n"
              "- **37 minutes** to King's College London by TfL transit -- slightly over "
              "your 30-minute limit")
    assert _commute_leq(answer, _commute_evidence(37)).passed


def test_an_unlabelled_overage_still_fails():
    answer = "This studio is a great fit — 37 minutes to King's College London."
    assert not _commute_leq(answer, _commute_evidence(37)).passed


def test_one_loose_cue_is_not_enough_to_excuse():
    """'over' alone, with no noun naming what was exceeded, must not excuse."""
    answer = "The commute is 37 minutes, over at the other end of the line."
    assert not _commute_leq(answer, _commute_evidence(37)).passed


def test_an_ungrounded_figure_is_never_excused_by_labelling():
    """The escape hatch covers the OVER check only — it cannot launder a fabrication."""
    answer = "- **37 minutes** to King's College London -- slightly over your 30-minute limit"
    assert not _commute_leq(answer, _commute_evidence(12)).passed


def test_a_within_limit_commute_still_passes():
    assert _commute_leq("The commute is 22 minutes.", _commute_evidence(22)).passed
