"""acc-r20 (round 6d, critic): the critic follows the shared commute rules the redactor uses.

``tests/data/r6_r20_corpus.json`` is exported from the frozen r20 inst-D checkpoints:

* r20_f510d8f3 "I go into the office 3 days a week. Compare Stratford and Bow … per day, week and
  month …" — calculate_commute_cost Stratford 20, Bow 28 (the journey nested under "commute");
  3 office days. Both the draft and the critic's regeneration were correct, the redactor kept
  them, yet the critic flagged both and the answer shipped with the "Unverified:" footer.

r18_1787af1e (tests/data/r6_r18_corpus.json): the regeneration's "roughly 12 minutes of headroom
against your 30-minute cap" (one measured route, 18) was flagged though it is 30 - 18.
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

DATA = pathlib.Path(__file__).parent / "data"
CORPUS = json.loads((DATA / "r6_r20_corpus.json").read_text(encoding="utf-8"))
R18 = json.loads((DATA / "r6_r18_corpus.json").read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."


def _ctx(case):
    state = case["state"]
    return state, _collect_grounding_evidence(state, ""), case["message"]


def _grounded(text, evidence, message):
    return evaluate_grounding(text, evidence, retrieval_expected=True, tool_errored=False,
                              user_text=message).grounded


@pytest.mark.parametrize("field", ["raw_draft", "critic_text"])
def test_the_correct_answer_and_its_regeneration_are_grounded_and_kept(field):
    case = CORPUS["r20_f510d8f3"]
    state, evidence, message = _ctx(case)
    text = case[field]
    assert validate_commute_response(text, state) == text
    assert not unsupported_external_numbers(text, evidence, message)
    assert _grounded(text, evidence, message)


@pytest.mark.parametrize("sentence,ok", [
    ("Stratford wins clearly on time: 20 minutes versus 28 minutes each way.", True),
    ("Stratford saves you 8 minutes each way, about 3.5 hours a month.", True),
    ("Stratford is faster by 8 minutes each way, so Bow costs you about 3.5 extra hours a month.", True),
    ("Stratford wins clearly on time: 20 minutes versus 30 minutes each way.", False),
    ("Stratford saves you 8 minutes each way, about 5 hours a month.", False),
    ("Stratford saves you 10 minutes each way, about 3.5 hours a month.", False),
    ("Stratford is faster by 8 minutes each way, so Bow costs you about 5 extra hours a month.", False),
])
def test_critic_and_redactor_agree_on_side_by_side_times_and_monthly_savings(sentence, ok):
    case = CORPUS["r20_f510d8f3"]
    state, evidence, message = _ctx(case)
    text = case["raw_draft"].replace(
        "- Time: Stratford is faster by 8 minutes each way, so Bow costs you about 3.5 extra hours a month",
        f"- Time: {sentence}")
    assert sentence in text
    assert bool(unsupported_external_numbers(text, evidence, message)) is (not ok)
    out = validate_commute_response(text, state)
    assert (sentence in out) is ok
    if not ok:
        assert out.count(NOTE_EN) == 1


# 15 is in no evidence; a wrong margin that happens to equal an evidence number (20, 9) is left
# to the redactor by the critic's standing number rule.
@pytest.mark.parametrize("headroom,ok", [("roughly 12 minutes", True), ("roughly 15 minutes", False)])
def test_a_single_routes_headroom_against_the_cap_is_checked_not_flagged(headroom, ok):
    case = R18["r18_1787af1e"]
    state, evidence, message = _ctx(case)
    text = case["critic_text"]
    assert "with roughly 12 minutes of headroom against your 30-minute cap" in text
    mutated = text.replace("roughly 12 minutes of headroom", f"{headroom} of headroom")
    assert bool(unsupported_external_numbers(mutated, evidence, message)) is (not ok)
    assert (f"{headroom} of headroom" in validate_commute_response(mutated, state)) is ok
