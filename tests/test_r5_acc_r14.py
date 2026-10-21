"""acc-r14: derived figures come from the PAIR the text names; chained and subtraction steps
are arithmetic too; the critic and the redactor agree.

``tests/data/r5_r14_corpus.json`` is exported from the frozen r14 inst-D checkpoints, request
95f29d0b… (conversation 33baeb62…): compare_or_rank_areas Holloway 25, Camden 36, Kentish Town
22. ``r14_95f29d0b_precritic`` holds the model's table-form draft (``raw_draft``) and the
state the critic received; ``r14_95f29d0b_critic`` is the regenerated text.
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent import commute_arithmetic as arithmetic
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

DATA = pathlib.Path(__file__).parent / "data"
CORPUS = json.loads((DATA / "r5_r14_corpus.json").read_text(encoding="utf-8"))
R12 = json.loads((DATA / "r5_r12_corpus.json").read_text(encoding="utf-8"))
R13 = json.loads((DATA / "r5_r13_corpus.json").read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."
HOLLOWAY = ("- Versus Holloway: 25 − 22 = 3 minutes each way, so 3 × 2 = 6 minutes per day, and "
            "6 × 5 = 30 minutes per week on a five-day commute.")
CAMDEN = ("- Versus Camden: 36 − 22 = 14 minutes each way, so 14 × 2 = 28 minutes per day, and "
          "28 × 5 = 140 minutes per week (2 hours 20 minutes) on a five-day commute.")


def _critic():
    return CORPUS["r14_95f29d0b_critic"]["text"], CORPUS["r14_95f29d0b_critic"]["state"]


def _grade(text):
    state = CORPUS["r14_95f29d0b_precritic"]["state"]
    evidence = _collect_grounding_evidence(state, "")
    message = state["extracted_context"]["current_message"]
    return evidence, message, evaluate_grounding(text, evidence, retrieval_expected=True,
                                                 tool_errored=False, user_text=message)


# ── 1. the pair rule closes the r13 loophole ──────────────────────────────────────────

def test_a_total_that_contradicts_the_lines_base_is_removed():
    """53bfec0e (Holloway 24, Camden 35, Kentish Town 21): "3 minutes each way, about 20
    minutes a day" passed only as the 10% hedge of 2 x (35 - 24), another pair entirely."""
    case = R12["r12_53bfec0e_draft"]
    line = "- Versus Holloway (24 min): 3 minutes each way, about 6 minutes a day, roughly half an hour a week."
    mutated = line.replace("about 6 minutes a day", "about 20 minutes a day")
    out = validate_commute_response(case["text"].replace(line, mutated), case["state"])
    assert "20 minutes a day" not in out
    assert "- Versus Holloway (24 min): 3 minutes each way, roughly half an hour a week." in out
    assert out.count(NOTE_EN) == 1


def test_the_named_pair_decides_a_hedged_total():
    """With no base on the line, "over Holloway" and the clause's subject name the pair."""
    text, state = R13["r13_2264a2d5_critic"]["text"], R13["r13_2264a2d5_critic"]["state"]
    prose = "would save you roughly 40 minutes a week over Holloway"
    assert prose in text
    # 80 = 2 x 5 x (33 - 25) is derivable, but not from Kentish Town vs Holloway.
    out = validate_commute_response(text.replace(prose, prose.replace("40", "80")), state)
    assert "80 minutes a week over Holloway" not in out


# ── 2. chained and subtraction steps ──────────────────────────────────────────────────

def test_the_recorded_chained_derivation_is_kept():
    text, state = _critic()
    assert HOLLOWAY in text and CAMDEN in text
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out


def test_the_table_form_draft_with_three_office_days_is_kept():
    """"at three office days it's about 1 hour 25 minutes" (14 x 2 x 3 = 84) and "versus Camden
    at about 36 minutes" were removed from the model's own draft."""
    case = CORPUS["r14_95f29d0b_precritic"]
    assert "at three office days it's about 1 hour 25 minutes" in case["raw_draft"]
    assert validate_commute_response(case["raw_draft"], case["state"]) == case["raw_draft"]


@pytest.mark.parametrize("wrong", [
    HOLLOWAY.replace("25 − 22 = 3 minutes", "25 − 22 = 4 minutes"),       # wrong subtraction
    HOLLOWAY.replace("6 × 5 = 30 minutes", "6 × 5 = 35 minutes"),         # wrong chained step
    HOLLOWAY.replace("3 × 2 = 6 minutes", "3 × 2 = 8 minutes"),           # wrong first step
    HOLLOWAY.replace("6 × 5 = 30", "6 × 7 = 42"),                         # invented multiplier
])
def test_a_wrong_step_is_removed(wrong):
    text, state = _critic()
    out = validate_commute_response(text.replace(HOLLOWAY, wrong), state)
    assert wrong not in out
    assert CAMDEN in out
    assert out.count(NOTE_EN) == 1


def test_office_days_in_words_or_digits_are_a_stated_week():
    for phrase, days in (("at three office days", 3.0), ("3 office days", 3.0),
                         ("four days in the office", 4.0), ("2 days in the office", 2.0)):
        assert arithmetic.days_stated(phrase) == {days}, phrase


# ── the critic uses the same rule ─────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["raw_draft", "draft"])
def test_the_critic_grounds_both_draft_stages(key):
    assert _grade(CORPUS["r14_95f29d0b_precritic"][key])[2].grounded


def test_the_critic_grounds_the_regenerated_text():
    text, _ = _critic()
    verdict = _grade(text)[2]
    assert verdict.grounded, verdict.issues


def test_the_critic_flags_wrong_steps_and_wrong_prose_totals():
    text, _ = _critic()
    evidence, message, _ = _grade(text)
    for wrong in (HOLLOWAY.replace("6 × 5 = 30 minutes", "6 × 5 = 35 minutes"),
                  HOLLOWAY.replace("25 − 22 = 3 minutes", "25 − 22 = 4 minutes")):
        assert unsupported_external_numbers(text.replace(HOLLOWAY, wrong), evidence, message), wrong


def test_critic_and_redactor_agree_on_prose_weekly_totals():
    """r14 item 3: the critic passed "45 minutes a week over Holloway", "3 hours a week over
    Camden" and "180 minutes a week" (hypothetical "would save" prose, hours) that the
    redactor removes."""
    text, state = R13["r13_2264a2d5_critic"]["text"], R13["r13_2264a2d5_critic"]["state"]
    pre = R13["r13_2264a2d5_precritic"]["state"]
    evidence = _collect_grounding_evidence(pre, "")
    message = pre["extracted_context"]["current_message"]
    assert not unsupported_external_numbers(text, evidence, message)
    for old, new in (("roughly 40 minutes a week over Holloway", "roughly 45 minutes a week over Holloway"),
                     ("and 2 hours a week over Camden", "and 3 hours a week over Camden"),
                     ("and 2 hours a week over Camden", "and 180 minutes a week over Camden")):
        mutated = text.replace(old, new)
        assert unsupported_external_numbers(mutated, evidence, message), new
        assert new not in validate_commute_response(mutated, state), new
