"""acc-r14 follow-up: a margin against the user's LIMIT is measured against the cap, and the
any-pair fallback never takes a hedged near-miss.

Recorded text and state: r10 request bb5cdb25 (conversation ebea7b9f…), "Which is the better
commute to Canary Wharf, Stratford or Bow, and is either under 25 minutes?" — calculate_commute
Stratford 20 min, Bow 29 min; the user's cap is 25 (``tests/data/r5_r10_corpus.json``). The
fallback case uses the r13 state (Holloway 25, Camden 33, Kentish Town 21).
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import unsupported_external_numbers

DATA = pathlib.Path(__file__).parent / "data"
R10 = json.loads((DATA / "r5_r10_corpus.json").read_text(encoding="utf-8"))
R13 = json.loads((DATA / "r5_r13_corpus.json").read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."
SPARE = "So Stratford meets your 25-minute limit with about 5 minutes to spare"
OVER = "while Bow comes in roughly 4 minutes over"


def _r10():
    case = R10["r10_bb5cdb25_draft"]
    state = case["state"]
    evidence = _collect_grounding_evidence(state, "")
    return case["text"], state, evidence, state["extracted_context"]["current_message"]


def test_the_correct_margin_against_the_cap_is_kept():
    text, state, evidence, message = _r10()
    assert SPARE in text and OVER in text
    out = validate_commute_response(text, state)
    assert out == text and NOTE_EN not in out
    assert not unsupported_external_numbers(text, evidence, message)


@pytest.mark.parametrize("old,new", [
    (SPARE, SPARE.replace("about 5", "about 9")),          # 9 = Bow - Stratford, not 25 - 20
    (SPARE, SPARE.replace("about 5 minutes to spare", "about 9 minutes inside your cap")),
    (OVER, OVER.replace("roughly 4", "roughly 7")),        # 29 - 25 = 4
])
def test_a_wrong_margin_is_removed_and_flagged(old, new):
    text, state, evidence, message = _r10()
    mutated = text.replace(old, new)
    out = validate_commute_response(mutated, state)
    assert new not in out
    assert out.count(NOTE_EN) == 1
    if "spare" in new or "cap" in new:
        assert unsupported_external_numbers(mutated, evidence, message), new


@pytest.mark.parametrize("old,new", [
    (SPARE, SPARE.replace("about 5 minutes to spare", "5 minutes inside your cap")),
    (OVER, "while Bow is 4 minutes over your limit"),
])
def test_other_margin_wordings_against_the_cap_are_kept(old, new):
    text, state, evidence, message = _r10()
    mutated = text.replace(old, new)
    assert new in validate_commute_response(mutated, state)
    assert not unsupported_external_numbers(mutated, evidence, message)


def test_a_hedged_near_miss_never_passes_through_the_any_pair_fallback():
    """"roughly 85 minutes" names no pair and no base; it is within 10% of 8 x 2 x 5 = 80
    (Camden - Holloway) and passed that way. Through the fallback only an exact figure stands."""
    text, state = R13["r13_2264a2d5_critic"]["text"], R13["r13_2264a2d5_critic"]["state"]
    pre = R13["r13_2264a2d5_precritic"]["state"]
    evidence = _collect_grounding_evidence(pre, "")
    message = pre["extracted_context"]["current_message"]
    anchor = "If you only travel in one direction on some days, halve those figures accordingly."
    assert anchor in text
    near = text.replace(anchor, f"{anchor} Across the week that is roughly 85 minutes saved.")
    out = validate_commute_response(near, state)
    assert "85 minutes" not in out and out.count(NOTE_EN) == 1
    assert unsupported_external_numbers(near, evidence, message)
    exact = text.replace(anchor, f"{anchor} Across the week that is 80 minutes saved.")
    assert "80 minutes saved" in validate_commute_response(exact, state)
