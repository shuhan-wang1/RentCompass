"""The two acc-r12 redactor false positives, on the recorded r12 agent drafts and states.

``tests/data/r5_r12_corpus.json`` is exported from the frozen r12 inst-D checkpoints:

* request 8573f129… (conversation d9676906…): compare_or_rank_areas Holloway score 80.0 /
  9 min, Camden 79.2 / 27 min.
* request 53bfec0e… (conversation d254be91…): Holloway 24, Camden 35, Kentish Town 21 min.
"""
import json
import pathlib
import re

from core.candidate_validation import validate_commute_response

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r5_r12_corpus.json")
                    .read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."
PICK = ("Holloway is the strongest practical pick — near-identical overall score to Camden but a "
        "9-minute commute instead of 27, for only £150 more in median rent.")
CAMDEN = ("- Versus Camden (35 min): 14 minutes each way, so about 28 minutes a day, roughly 2 hours "
          "20 minutes a week on a five-day commute.")
HOLLOWAY = "- Versus Holloway (24 min): 3 minutes each way, about 6 minutes a day, roughly half an hour a week."


def _case(name):
    return CORPUS[name]["text"], CORPUS[name]["state"]


def _no_fragments(out):
    assert not re.search(r"\S {2,}\S", out)      # no double space inside a line
    assert "— for" not in out and "—  " not in out
    for line in out.splitlines():
        assert line.strip() not in ("- Versus Camden (35 min).", "- Versus Holloway (24 min)."), line


# ── 1. "<near-identical X to Camden> but a <N>-minute commute instead of <M>" ─────────

def test_combined_comparison_with_camden_is_kept():
    text, state = _case("r12_8573f129_draft")
    assert PICK in text
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out


def test_a_wrong_figure_in_the_combination_is_removed_cleanly():
    text, state = _case("r12_8573f129_draft")
    wrong = PICK.replace("a 9-minute commute", "a 19-minute commute")
    out = validate_commute_response(text.replace(PICK, wrong), state)
    assert "19-minute" not in out
    assert "Holloway is the strongest practical pick for only £150 more in median rent." in out
    assert out.count(NOTE_EN) == 1
    _no_fragments(out)


# ── 2. weekly arithmetic derived from a measured difference ───────────────────────────

def test_tool_derived_daily_and_weekly_savings_are_kept():
    text, state = _case("r12_53bfec0e_draft")
    assert CAMDEN in text and HOLLOWAY in text
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out


def test_arithmetic_that_does_not_follow_is_removed():
    text, state = _case("r12_53bfec0e_draft")
    for line, wrong, gone in (
            (CAMDEN, CAMDEN.replace("roughly 2 hours 20 minutes", "roughly 3 hours"), "3 hours"),
            (CAMDEN, CAMDEN.replace("about 28 minutes a day", "about 40 minutes a day"), "40 minutes"),
            # 60 would follow from Holloway-Camden (11 min x 5 = 55, "roughly"); 3 hours follows
            # from no pair of measured routes.
            (HOLLOWAY, HOLLOWAY.replace("roughly half an hour", "roughly 3 hours"), "3 hours a week")):
        out = validate_commute_response(text.replace(line, wrong), state)
        assert gone not in out, wrong
        assert out.count(NOTE_EN) == 1
        _no_fragments(out)


def test_a_total_never_stands_without_the_difference_it_comes_from():
    """A wrong base difference goes, and so does everything derived from it; a bullet left
    with only its label is dropped, never shipped as a stub ("- Versus Camden (35 min)."). A
    correct total with no base on its line stays."""
    text, state = _case("r12_53bfec0e_draft")
    wrong_base = CAMDEN.replace("14 minutes each way, so about 28 minutes a day, roughly 2 hours 20",
                                "12 minutes each way, so about 24 minutes a day, roughly 2 hours")
    out = validate_commute_response(text.replace(CAMDEN, wrong_base), state)
    assert "Versus Camden" not in out
    assert HOLLOWAY in out
    _no_fragments(out)
    # acc-r13 owner rule: a CORRECT total stands without its base figure on the line, as long
    # as both routes' measured minutes are in this turn's evidence (Holloway 24, Kentish Town 21).
    no_base = HOLLOWAY.replace("3 minutes each way, about 6 minutes a day, ", "")
    out = validate_commute_response(text.replace(HOLLOWAY, no_base), state)
    assert no_base in out
    _no_fragments(out)


def test_the_users_office_days_are_a_valid_multiplier():
    text, state = _case("r12_53bfec0e_draft")
    three_days = CAMDEN.replace("roughly 2 hours 20 minutes a week on a five-day commute",
                                "roughly 1 hour 24 minutes a week on a three-day commute")
    assert three_days in validate_commute_response(text.replace(CAMDEN, three_days), state)


def test_a_bare_journey_time_equal_to_a_difference_is_still_a_journey_claim():
    """"Camden takes 14 minutes each way" asserts Camden's own time (35), not a difference."""
    text, state = _case("r12_53bfec0e_draft")
    out = validate_commute_response(text.replace(CAMDEN, "- Camden takes 14 minutes each way."), state)
    assert "Camden takes 14 minutes" not in out
