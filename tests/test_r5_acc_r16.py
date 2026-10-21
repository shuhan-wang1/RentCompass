"""acc-r16 follow-up: a weekly total survives its monthly neighbour; header bullets and cap
subtractions are margins; journey-count multipliers; no dangling condition.

``tests/data/r5_r16_corpus.json`` is exported from the frozen r16 inst-D checkpoints (the
model's draft as ``raw_draft`` with the state the critic received; the critic's regeneration
as ``critic_text`` with its state):

* b5882620… "I go into the office 3 days a week …" — Stratford 19, Bow 28 (office days 3).
* 0a6c11d6… "My commute limit is 25 minutes …" — Stratford 19, Bow 29.
* 2e4cddc7… Kentish Town 20, Holloway 23, Camden 36.
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r5_r16_corpus.json")
                    .read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."


def _case(name):
    case = CORPUS[name]
    state = case["state"]
    return case, state, _collect_grounding_evidence(state, ""), state["extracted_context"]["current_message"]


def _grounded(text, evidence, message):
    return evaluate_grounding(text, evidence, retrieval_expected=True, tool_errored=False,
                              user_text=message).grounded


# ── 1. the weekly total stays when its monthly/yearly neighbour goes ──────────────────

def test_only_the_monthly_and_yearly_clauses_go():
    case, state, evidence, message = _case("r16_b5882620")
    text = case["raw_draft"]
    assert "9 × 2 × 3 = **54 minutes a week** — roughly 3.9 hours a month, or about 47 hours a year." in text
    out = validate_commute_response(text, state)
    assert "With three office days that's 9 × 2 × 3 = **54 minutes a week**." in out
    assert "3.9 hours" not in out and "47 hours" not in out
    assert out.count(NOTE_EN) == 1
    # the critic agrees: the out-of-rule monthly/yearly totals are unsupported
    assert unsupported_external_numbers(text, evidence, message)
    weekly_only = text.replace(" — roughly 3.9 hours a month, or about 47 hours a year", "")
    assert not unsupported_external_numbers(weekly_only, evidence, message)


# ── 2. bullets under a bold route header; cap subtractions ────────────────────────────

def test_a_margin_bullet_under_a_bold_header_is_kept():
    case, state, evidence, message = _case("r16_0a6c11d6")
    text = case["raw_draft"]
    assert "**Bow — no, it's over**\n- 29 minutes door to door, so 4 minutes over your limit" in text
    assert validate_commute_response(text, state) == text
    assert _grounded(text, evidence, message)


def test_the_regenerated_cap_subtractions_and_header_margin_are_kept():
    case = CORPUS["r16_0a6c11d6"]
    _, _, evidence, message = _case("r16_0a6c11d6")
    text, state = case["critic_text"], case["critic_state"]
    for piece in ("- Against your 25-minute limit: 25 − 19 = 6 minutes under",
                  "**Bow — outside your limit, by 4 minutes**",
                  "- Against your 25-minute limit: 29 − 25 = 4 minutes over",
                  "so if Bow is on your shortlist, you would need to either accept a slightly longer commute"):
        assert piece in text
    out = validate_commute_response(text, state)
    assert out == text and NOTE_EN not in out
    assert _grounded(text, evidence, message)


@pytest.mark.parametrize("old,new", [
    ("25 − 19 = 6 minutes under", "25 − 19 = 7 minutes under"),
    ("29 − 25 = 4 minutes over", "29 − 25 = 6 minutes over"),
    ("**Bow — outside your limit, by 4 minutes**", "**Bow — outside your limit, by 9 minutes**"),
])
def test_a_wrong_cap_margin_is_removed_and_flagged(old, new):
    case = CORPUS["r16_0a6c11d6"]
    _, _, evidence, message = _case("r16_0a6c11d6")
    mutated = case["critic_text"].replace(old, new)
    out = validate_commute_response(mutated, case["critic_state"])
    assert new.strip("*") not in out
    assert out.count(NOTE_EN) == 1
    assert unsupported_external_numbers(mutated, evidence, message), new


@pytest.mark.parametrize("line,true", [
    ("Bow is 29 minutes door to door, 4 minutes over.", True),
    ("Bow is 29 minutes door to door, 6 minutes over.", False),
    ("Stratford is 19 minutes, 6 minutes under your limit.", True),
    ("Stratford is 19 minutes, 4 minutes under your limit.", False),
])
def test_critic_and_redactor_agree_on_margins(line, true):
    case, state, evidence, message = _case("r16_0a6c11d6")
    text = f"{case['raw_draft']}\n\n{line}"
    assert (line in validate_commute_response(text, state)) is true
    assert bool(unsupported_external_numbers(text, evidence, message)) is (not true)


# ── 3. journeys per week ──────────────────────────────────────────────────────────────

def test_journey_count_arithmetic_is_kept():
    case = CORPUS["r16_2e4cddc7"]
    _, _, evidence, message = _case("r16_2e4cddc7")
    text, state = case["critic_text"], case["critic_state"]
    for piece in ("3 × 10 = 30 minutes per week", "16 × 10 = 160 minutes per week (2 hours 40 minutes)",
                  "three days a week = 6 journeys: 18 minutes vs Holloway, 96 minutes vs Camden"):
        assert piece in text
    out = validate_commute_response(text, state)
    assert out == text and NOTE_EN not in out
    assert _grounded(text, evidence, message)
    assert _grounded(case["raw_draft"], evidence, message)      # "around 2.5 hours a week" too


@pytest.mark.parametrize("old,new", [
    ("3 × 10 = 30 minutes per week", "3 × 12 = 36 minutes per week"),     # 12 journeys: no week
    ("6 journeys: 18 minutes", "8 journeys: 24 minutes"),                # 8 is not 2 x 3
    ("16 × 10 = 160 minutes", "16 × 10 = 180 minutes"),                  # wrong product
])
def test_a_wrong_journey_count_is_removed_and_flagged(old, new):
    case = CORPUS["r16_2e4cddc7"]
    _, _, evidence, message = _case("r16_2e4cddc7")
    mutated = case["critic_text"].replace(old, new)
    assert new not in validate_commute_response(mutated, case["critic_state"])
    assert unsupported_external_numbers(mutated, evidence, message), new


# ── 4. no dangling condition ──────────────────────────────────────────────────────────

def test_a_condition_goes_with_the_main_clause_it_led_into():
    """With an unsupported claim after "so if Bow is on your shortlist,", the condition goes
    too: "… Mile End — so if Bow is on your shortlist." shipped."""
    case = CORPUS["r16_0a6c11d6"]
    advice = ("you would need to either accept a slightly longer commute or look at addresses closer "
              "to Bow Road station than the one measured here")
    mutated = case["critic_text"].replace(advice, "the journey from there takes 40 minutes")
    out = validate_commute_response(mutated, case["critic_state"])
    assert "so if Bow is on your shortlist" not in out
    assert "40 minutes" not in out
    assert "plus a change at Mile End." in out
