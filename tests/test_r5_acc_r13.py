"""acc-r13: arithmetic on measured routes is ONE rule, in any notation, for the redactor and
the critic alike; removals leave whole sentences and no orphaned lead-in.

``tests/data/r5_r13_corpus.json`` is exported from the frozen r13 inst-D checkpoints, request
2264a2d5…: compare_or_rank_areas Holloway 25, Camden 33, Kentish Town 21 minutes.
``r13_2264a2d5_precritic`` is the state the critic received (``raw_draft`` = the model's own
text, ``draft`` = after the agent-node redaction); ``r13_2264a2d5_critic`` is the critic's
regeneration the format pass validated.
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent import commute_arithmetic as arithmetic
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r5_r13_corpus.json")
                    .read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."
HOLLOWAY = "- Versus Holloway: 4 minutes × 2 journeys × 5 days = **40 minutes a week**."
CAMDEN = "- Versus Camden: 12 minutes × 2 journeys × 5 days = **120 minutes (2 hours) a week**."
PROSE = ("Kentish Town is fastest at 21 minutes and would save you roughly 40 minutes a week over "
         "Holloway and 2 hours a week over Camden")
LEAD_IN = "the weekly saving from choosing Kentish Town over the others is:"


def _critic():
    return CORPUS["r13_2264a2d5_critic"]["text"], CORPUS["r13_2264a2d5_critic"]["state"]


# ── 1. the redactor: every notation ───────────────────────────────────────────────────

def test_the_recorded_critic_text_is_kept_whole():
    text, state = _critic()
    assert HOLLOWAY in text and CAMDEN in text and PROSE in text
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out


def test_the_models_own_draft_is_kept_whole():
    """"12 minutes each way, so 24 minutes a day, or about 2 hours a week over five return
    trips" lost its weekly total in the agent node."""
    case = CORPUS["r13_2264a2d5_precritic"]
    assert "or about 2 hours a week over five return trips" in case["raw_draft"]
    assert validate_commute_response(case["raw_draft"], case["state"]) == case["raw_draft"]


@pytest.mark.parametrize("wrong", [
    HOLLOWAY.replace("**40 minutes", "**45 minutes"),                       # wrong product
    HOLLOWAY.replace("4 minutes ×", "6 minutes ×").replace("**40", "**60"),  # wrong difference
    HOLLOWAY.replace("× 2 journeys", "× 3 journeys").replace("**40", "**60"),  # invented multiplier
    HOLLOWAY.replace("× 5 days", "× 4 days").replace("**40", "**32"),       # 4-day week nobody stated
])
def test_wrong_products_are_removed_whole(wrong):
    text, state = _critic()
    out = validate_commute_response(text.replace(HOLLOWAY, wrong), state)
    assert wrong not in out and "Versus Holloway" not in out
    assert CAMDEN in out and LEAD_IN in out           # the other bullet keeps its lead-in
    assert out.count(NOTE_EN) == 1


def test_a_lead_in_whose_whole_list_went_goes_too():
    text, state = _critic()
    both = text.replace(HOLLOWAY, HOLLOWAY.replace("**40", "**45")).replace(
        CAMDEN, CAMDEN.replace("**120 minutes (2 hours)", "**150 minutes"))
    out = validate_commute_response(both, state)
    assert "Versus Holloway" not in out and "Versus Camden" not in out
    assert LEAD_IN not in out
    assert out.count(NOTE_EN) == 1


def test_wrong_prose_arithmetic_removes_the_whole_sentence():
    """The removal must not leave "If commute is your priority,confirm rents there first"."""
    text, state = _critic()
    wrong = PROSE.replace("roughly 40 minutes", "roughly 70 minutes")
    out = validate_commute_response(text.replace(PROSE, wrong), state)
    assert "70 minutes" not in out
    assert "If commute is your priority" not in out
    assert "priority,confirm" not in out and "but confirm rents there first" not in out
    assert "If you want a fully evidenced option, Holloway is the strongest all-round pick" in out
    assert out.count(NOTE_EN) == 1


def test_a_correct_total_needs_no_base_figure_on_its_line():
    text, state = _critic()
    bare = "- Versus Holloway (25 min): roughly 40 minutes a week."
    assert bare in validate_commute_response(text.replace(HOLLOWAY, bare), state)
    untrue = "- Versus Holloway (25 min): roughly 3 hours a week."
    assert "3 hours" not in validate_commute_response(text.replace(HOLLOWAY, untrue), state)


def test_the_fastest_claim_is_checked():
    text, state = _critic()
    wrong = PROSE.replace("Kentish Town is fastest at 21 minutes", "Holloway is fastest at 25 minutes")
    assert "Holloway is fastest" not in validate_commute_response(text.replace(PROSE, wrong), state)


# ── 2. the critic uses the same definition ────────────────────────────────────────────

@pytest.mark.parametrize("name,key", [("r13_2264a2d5_precritic", "raw_draft"),
                                      ("r13_2264a2d5_precritic", "draft"),
                                      ("r13_2264a2d5_critic", "text")])
def test_the_critic_grades_derived_figures_as_grounded(name, key):
    """Initial and regenerated stages were grounded=False / unsupported_external_numbers,
    so the answer was regenerated and got the "Unverified" footer."""
    case = CORPUS[name]
    state = CORPUS["r13_2264a2d5_precritic"]["state"]
    evidence = _collect_grounding_evidence(state, "")
    message = state["extracted_context"]["current_message"]
    verdict = evaluate_grounding(case[key], evidence, retrieval_expected=True, tool_errored=False,
                                 user_text=message)
    assert verdict.grounded, verdict.issues


def test_the_critic_still_flags_invented_arithmetic():
    text, _ = _critic()
    state = CORPUS["r13_2264a2d5_precritic"]["state"]
    evidence = _collect_grounding_evidence(state, "")
    message = state["extracted_context"]["current_message"]
    for wrong in (HOLLOWAY.replace("**40 minutes", "**45 minutes"),
                  HOLLOWAY.replace("4 minutes ×", "7 minutes ×").replace("**40", "**70")):
        assert unsupported_external_numbers(text.replace(HOLLOWAY, wrong), evidence, message), wrong
    assert unsupported_external_numbers(text + "\n\nKentish Town takes 37 minutes.", evidence, message)


def test_one_definition_of_derived():
    measured = [25.0, 33.0, 21.0]
    assert arithmetic.is_difference(12, measured) and arithmetic.is_difference(4, measured)
    assert arithmetic.is_derived_total(120, measured, {5}) and arithmetic.is_derived_total(24, measured, {5})
    assert arithmetic.is_derived_total(118, measured, {5}, hedged=True)
    assert not arithmetic.is_derived_total(118, measured, {5})
    assert arithmetic.is_derived_total(72, measured, {3}) and not arithmetic.is_derived_total(72, measured, {5})
    (product,) = arithmetic.products("12 minutes × 2 journeys × 5 days = 120 minutes")
    assert arithmetic.product_is_correct(product, measured, {5})


# ── 4. a bullet with a real remainder is not a stub ───────────────────────────────────

def test_a_bullet_keeping_a_complete_sentence_is_not_dropped():
    """The r06 false-claim control (conversation 3709647b): with the false "still under 20
    minutes" withdrawn, "- If you want the better balance of rent and travel, Stratford wins."
    is a true sentence and stays; r13 dropped the whole bullet as a stub."""
    output_corpus = json.loads((pathlib.Path(__file__).parent / "data" / "r5_output_corpus.json")
                               .read_text(encoding="utf-8"))
    case = output_corpus["r06_099518b9_draft"]
    false = case["text"].replace("still under half an hour", "still under 20 minutes")
    out = validate_commute_response(false, case["state"])
    assert "- If you want the better balance of rent and travel, Stratford wins." in out
    assert "under 20 minutes" not in out
