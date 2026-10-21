"""acc-r15: totals are checked for the period they name over the user's own office days; header
margins, spreads and per-listing estimates are evidence; removals leave whole sentences.

``tests/data/r5_r15_corpus.json`` is exported from the frozen r15 inst-D checkpoints (the
model's draft as ``raw_draft``, the state the critic received):

* efaa330f… "I go into the office 3 days a week. Compare Stratford and Bow …" — Stratford 19,
  Bow 28 (difference 9); criteria office_days_per_week 3.
* 452df7d4… "My commute limit is 25 minutes …" — Stratford 20, Bow 28.
* 700d77a8… Camden 25, Holloway 24, Kentish Town 21.
* 40a2522f… "Find me a 1-bed in Islington under £2,200 with commute times to King's Cross" —
  search listings with travel_time estimates, an Islington → King's Cross TfL journey of 15.
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import unsupported_external_numbers

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r5_r15_corpus.json")
                    .read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."


def _case(name):
    case = CORPUS[name]
    state = case["state"]
    evidence = _collect_grounding_evidence(state, "")
    return case["raw_draft"], state, evidence, state["extracted_context"]["current_message"]


# ── 1. BLOCKER: unit- and day-aware weekly totals ─────────────────────────────────────

BLOCKER = "that's about 18 minutes a week saved — around 54 minutes over a typical month"


def test_a_daily_figure_called_weekly_and_a_monthly_label_are_removed():
    """9 x 2 x 3 = 54 is the WEEKLY saving; "18 minutes a week" is the daily figure and "54
    minutes over a typical month" a weekly figure called monthly. Both shipped, 0 Notes."""
    text, state, evidence, message = _case("r15_efaa330f")
    assert BLOCKER in text
    out = validate_commute_response(text, state)
    assert "18 minutes a week" not in out and "54 minutes over a typical month" not in out
    assert "With 3 office days a week (return trips)." not in out        # no fragment left
    assert "Stratford is roughly 9 minutes faster each way." in out
    assert out.count(NOTE_EN) == 1
    assert unsupported_external_numbers(text, evidence, message)


@pytest.mark.parametrize("total", ["about 20 minutes a week", "about 90 minutes a week",
                                   "about 30 minutes a week", "about 36 minutes a week",
                                   "about 54 minutes a month"])
def test_totals_that_ignore_the_users_three_days_are_removed(total):
    text, state, evidence, message = _case("r15_efaa330f")
    mutated = text.replace(BLOCKER, f"that's {total} saved")
    assert total not in validate_commute_response(mutated, state)
    assert unsupported_external_numbers(mutated, evidence, message), total


@pytest.mark.parametrize("total", [
    "that's about 54 minutes a week saved",
    "that's about 18 minutes a day saved — around 54 minutes a week",
    "that's about 54 minutes a week, or 90 minutes on a five-day week",   # the answer states 5 for it
    "that's about 27 minutes a week one way",
])
def test_correct_unit_and_day_totals_are_kept(total):
    text, state, evidence, message = _case("r15_efaa330f")
    mutated = text.replace(BLOCKER, total)
    out = validate_commute_response(mutated, state)
    assert total in out and NOTE_EN not in out
    assert not unsupported_external_numbers(mutated, evidence, message)


# ── 2. header margin and figure-free advice ───────────────────────────────────────────

def test_a_header_margin_and_speculative_advice_are_kept():
    text, state, evidence, message = _case("r15_452df7d4")
    assert "comfortably inside your 25-minute limit (5 minutes to spare)" in text
    assert "a particular listing in Bow could be closer to the station" in text
    out = validate_commute_response(text, state)
    assert out == text
    wrong = text.replace("(5 minutes to spare)", "(9 minutes to spare)")
    assert "(9 minutes to spare)" not in validate_commute_response(wrong, state)
    assert unsupported_external_numbers(wrong, evidence, message)


# ── 3. a spread of the measured routes ────────────────────────────────────────────────

@pytest.mark.parametrize("claim,kept", [
    ("All three are within 4 minutes of each other", True),       # 25 - 21
    ("All three are within 5 minutes of each other", True),       # a true bound
    ("All three are within 2 minutes of each other", False),
    ("All three are 3 minutes apart", False),
])
def test_a_spread_is_max_minus_min(claim, kept):
    text, state, evidence, message = _case("r15_700d77a8")
    original = "All three are within 4 minutes of each other"
    assert original in text
    mutated = text.replace(original, claim)
    assert (claim in validate_commute_response(mutated, state)) is kept
    assert bool(unsupported_external_numbers(mutated, evidence, message)) is (not kept)


def test_the_recorded_spread_answer_is_kept_whole():
    text, state, _, _ = _case("r15_700d77a8")
    assert validate_commute_response(text, state) == text


# ── 4. the search answer ──────────────────────────────────────────────────────────────

def test_the_search_answer_keeps_the_listing_estimate_and_the_area_journey():
    """"- Commute to King's Cross: roughly 20 minutes (an estimate …)" under the King Square
    header is that listing's own ~20; "the TfL Journey Planner gives a measured journey of about
    15 minutes" is the Islington route, though the line also names the King Square flat."""
    text, state, _, _ = _case("r15_40a2522f")
    assert "- Commute to King's Cross: roughly 20 minutes (an estimate" in text
    assert "a measured journey of about 15 minutes to King's Cross" in text
    assert validate_commute_response(text, state) == text


def test_a_wrong_listing_estimate_is_still_removed():
    text, state, _, _ = _case("r15_40a2522f")
    out = validate_commute_response(text.replace("roughly 20 minutes (an estimate", "roughly 30 minutes (an estimate"), state)
    assert "roughly 30 minutes" not in out


# ── 5. no subordinate-clause remnant ──────────────────────────────────────────────────

def test_a_subordinate_clause_whose_main_clause_went_goes_too():
    """r10 bb5cdb25 state: with the false "about 9 minutes to spare" withdrawn, "While Bow comes
    in roughly 4 minutes over." was left as a capitalised fragment."""
    r10 = json.loads((pathlib.Path(__file__).parent / "data" / "r5_r10_corpus.json").read_text(encoding="utf-8"))
    case = r10["r10_bb5cdb25_draft"]
    spare = "So Stratford meets your 25-minute limit with about 5 minutes to spare"
    out = validate_commute_response(case["text"].replace(spare, spare.replace("about 5", "about 9")),
                                    case["state"])
    assert "While Bow comes in" not in out and "while Bow comes in" not in out
    assert "The main difference is the walk to the station" in out
