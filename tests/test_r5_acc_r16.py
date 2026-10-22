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
# Owner decision (r6, acc-r17): a monthly total of the evidenced weekly one (x 52/12) and a
# yearly one (x 52) are evidenced, so this draft now ships whole: 54 x 52/12 = 234 minutes =
# 3.9 hours, 54 x 52 = 46.8 hours. A wrong neighbour still goes and the weekly total stays.

@pytest.mark.parametrize("clause,kept", [
    ("roughly 3.9 hours a month, or about 47 hours a year", True),
    ("roughly 2.5 hours a month, or about 47 hours a year", False),
    ("roughly 3.9 hours a month, or about 30 hours a year", False),
])
def test_only_a_wrong_monthly_or_yearly_clause_goes(clause, kept):
    case, state, evidence, message = _case("r16_b5882620")
    original = "roughly 3.9 hours a month, or about 47 hours a year"
    assert f"9 × 2 × 3 = **54 minutes a week** — {original}." in case["raw_draft"]
    text = case["raw_draft"].replace(original, clause)
    out = validate_commute_response(text, state)
    assert "With three office days that's 9 × 2 × 3 = **54 minutes a week**" in out
    assert (clause in out) is kept
    assert out.count(NOTE_EN) == (0 if kept else 1)
    # the critic agrees
    assert bool(unsupported_external_numbers(text, evidence, message)) is (not kept)
    weekly_only = text.replace(f" — {clause}", "")
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


# ── r17: a stated journey count must match the week ───────────────────────────────────

BLOCK = ("(10 journeys):\n- vs Holloway: 3 × 10 = 30 minutes per week\n"
         "- vs Camden: 16 × 10 = 160 minutes per week (2 hours 40 minutes)")


def _r17(old, new):
    case = CORPUS["r16_2e4cddc7"]
    _, _, evidence, message = _case("r16_2e4cddc7")
    text = case["critic_text"]
    assert old in text
    mutated = text.replace(old, new)
    return mutated, validate_commute_response(mutated, case["critic_state"]), evidence, message


@pytest.mark.parametrize("old,new", [
    ("(10 journeys)", "(8 journeys)"),                                     # 5 days x 2 = 10
    ("three days a week = 6 journeys", "three days a week = 8 journeys"),  # 3 days x 2 = 6
    ("(10 journeys)", "(10 return trips a week)"),                         # 5 return trips
])
def test_a_wrong_journey_count_label_is_removed_and_flagged(old, new):
    mutated, out, evidence, message = _r17(old, new)
    assert new not in out
    assert out.count(NOTE_EN) == 1
    assert unsupported_external_numbers(mutated, evidence, message), new


@pytest.mark.parametrize("old,new", [
    ("(10 journeys)", "(10 journeys)"),
    ("three days a week = 6 journeys", "three days a week = 6 journeys"),
    ("(10 journeys)", "(5 one-way trips)"),                                # explicit one-way
])
def test_a_correct_journey_count_is_kept(old, new):
    mutated, out, evidence, message = _r17(old, new)
    assert out == mutated and NOTE_EN not in out
    assert not unsupported_external_numbers(mutated, evidence, message)


def test_no_critic_flagged_leftover_after_a_wrong_block_goes():
    """The wrong "(8 journeys)" block goes whole; what is left must not be flagged by the
    critic, or the Unverified footer would follow (the 5-day week stated in the removed
    lead-in is still the standard week for "half an hour" / "2 hours 40 minutes")."""
    wrong = ("(8 journeys):\n- vs Holloway: 3 × 8 = 24 minutes per week\n"
             "- vs Camden: 16 × 8 = 128 minutes per week")
    mutated, out, evidence, message = _r17(BLOCK, wrong)
    assert "8 journeys" not in out and "3 × 8" not in out and "16 × 8" not in out
    assert unsupported_external_numbers(mutated, evidence, message)
    assert not unsupported_external_numbers(out, evidence, message)
    assert "saves roughly half an hour a week over Holloway, and about 2 hours 40 minutes a week" in out
