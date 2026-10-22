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


# ── money and distance: what the critic may count as evidence (r6d) ────────────────────
# r18_adce1322 "What do you remember about me?" (no tools; memory block "[FACT] Budget £2,000").
# r18_640bdda0 a Camden search with no match inside £1,800 and one over-budget alternative
#   (Highgate West Hill £2,000/month, distance_miles 1.6); the persona's unrelated sensitive
#   details are scrubbed from the fixture.
# r20_1419b01e "Tell me more about Arbutus Street in Haggerston." (no tools); its £2,200 was only
#   ever in an earlier answer's text, never in a server-held row.


def _flags(case, text=None, state=None):
    state = state or case["state"]
    return bool(unsupported_external_numbers(text or case["raw_draft"], _collect_grounding_evidence(state, ""),
                                             case["message"]))


def test_a_recalled_budget_is_evidence_for_restating_it():
    case = R18["r18_adce1322"]
    assert "- Your budget is £2,000" in case["raw_draft"]
    assert not _flags(case)
    assert validate_commute_response(case["raw_draft"], case["state"]) == case["raw_draft"]
    assert _flags(case, case["raw_draft"].replace("- Your budget is £2,000", "- Your budget is £2,500"))


def test_an_inference_is_no_evidence_for_a_budget():
    import copy
    case = R18["r18_adce1322"]
    state = copy.deepcopy(case["state"])
    state["memory_context"] = "\n".join(line for line in state["memory_context"].splitlines()
                                        if "[FACT] Budget" not in line)
    assert "£2,000" in state["memory_context"]                     # only in an [INFERENCE] line now
    assert _flags(case, state=state)


HIGHGATE = ("- **Highgate West Hill, London N6** — 1-bed flat, **£2,000/month**. That's £200 over your budget, "
            "so it's not within what you can afford. It's also about 1.6 miles from central Camden.")


@pytest.mark.parametrize("old,new,ok", [
    ("£200 over your budget", "£200 over your budget", True),
    # wrong over-budget figure (not £300: the price pool's standing x1-36 fan-out derives it
    # from a £15 fee, 15 x 20 — a documented trade-off of that pool, not of this rule)
    ("£200 over your budget", "£260 over your budget", False),
    ("**£2,000/month**", "**£2,100/month**", False),                  # wrong price
    ("about 1.6 miles", "about 2.4 miles", False),                    # a distance no tool gave
])
def test_an_over_budget_alternatives_own_row_is_evidence(old, new, ok):
    case = R18["r18_640bdda0"]
    assert HIGHGATE in case["raw_draft"]
    text = case["raw_draft"].replace(old, new)
    assert _flags(case, text) is (not ok)


def test_an_earlier_answers_price_is_no_evidence_but_a_held_row_is():
    import copy
    case = CORPUS["r20_1419b01e"]
    assert "its monthly price of £2,200" in case["raw_draft"]
    assert _flags(case)                         # the £2,200 is in no server-held row this turn
    state = copy.deepcopy(case["state"])
    state["extracted_context"]["view_results"] = list(state["extracted_context"].get("view_results") or []) + [
        {"address": "Arbutus Street, Haggerston, London, E8", "price": "£2200/month",
         "budget_status": "⚠️ Over budget by £200"}]
    assert not _flags(case, state=state)
    assert _flags(case, case["raw_draft"].replace("£2,200", "£2,450"), state=state)   # wrong price
