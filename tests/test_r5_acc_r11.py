"""The two acc-r11 defects (and the low-priority collateral), on the recorded r11 texts.

``tests/data/r5_r11_corpus.json`` is exported from the frozen r11 inst-D checkpoints:

* request 97bdde65… (conversation 8ab78d41…): "Which gyms are near Camden Road, Islington N7,
  and which nearby area is best value for a 1-bed?" compare_or_rank_areas: Camden 2100 /
  27 min, Holloway 2250 / 10 min, Islington 2400 / 28 min, Kentish Town 7, Tufnell Park 12,
  Finsbury Park and Archway 16, Highbury 22 min; both search_nearby_pois (gym) FAILED.
  ``r11_97bdde65_precritic`` is the state the critic received, with the agent's answer as
  ``draft``; ``r11_97bdde65_critic`` is the critic's regeneration the format pass validated.
* request d36a82c2…: Stratford 20 min vs Bow 29 min to Canary Wharf, cap 25.
"""
import asyncio
import json
import pathlib

from core.candidate_validation import validate_commute_response
from core.langgraph_agent import (
    _build_generation_prompt,
    _collect_grounding_evidence,
    _fc_artifact_observation,
    _make_critic_node,
)
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r5_r11_corpus.json")
                    .read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."
CLAUSE = "Slightly dearer median than Camden but a much shorter commute"


def _case(name):
    return CORPUS[name]["text"], CORPUS[name]["state"]


def _holloway_line(text):
    return next(line for line in text.splitlines() if line.startswith("2. **Holloway**"))


# ── defect 1: a comparative against a named, measured area ───────────────────────────

def test_comparative_against_a_named_measured_area_is_kept():
    text, state = _case("r11_97bdde65_critic")
    assert CLAUSE in text
    out = validate_commute_response(text, state)
    assert out == text
    assert NOTE_EN not in out


def test_wrong_direction_or_unmeasured_target_is_removed_and_the_sentence_stays_whole():
    text, state = _case("r11_97bdde65_critic")
    for false in ("Slightly dearer median than Camden but a much longer commute",
                  "Slightly dearer median than Camden but about the same commute",
                  "Slightly dearer median than Notting Hill but a much shorter commute",
                  "A much shorter commute than Kentish Town"):
        out = validate_commute_response(text.replace(CLAUSE, false), state)
        assert false not in out, false
        line = _holloway_line(out)
        assert line.endswith("Value score 68.8, total 75.9. The data is older here (91 days)."), line
        assert ". the data" not in out
        assert out.count(NOTE_EN) == 1


def test_other_comparatives_the_measurements_support_are_kept():
    text, state = _case("r11_97bdde65_critic")
    for true in ("Slightly dearer median than Islington but a much shorter commute",
                 "Slightly dearer median than Tufnell Park but about the same commute"):
        assert true in validate_commute_response(text.replace(CLAUSE, true), state), true


def test_the_agent_draft_passes_the_redactor_unchanged():
    """"Finsbury Park and Archway (16 min)" and "aren't comparable with the three above" are
    measured / not a commute claim; the split "and" must not come back as ";"."""
    case = CORPUS["r11_97bdde65_precritic"]
    assert validate_commute_response(case["draft"], case["state"]) == case["draft"]


# ── defect 2: the critic rewrote an honest failed lookup as a coverage gap ────────────

def test_the_draft_is_grounded_in_the_structured_area_figures():
    """The draft's minutes (27, 10, 7, 12, 16, 22, 28) are compare_or_rank_areas'
    ``commute_minutes`` integers; numeric leaves were invisible to the critic's unit check,
    so the honest draft was graded ungrounded and regenerated."""
    case = CORPUS["r11_97bdde65_precritic"]
    state = case["state"]
    evidence = _collect_grounding_evidence(state, "")
    message = state["extracted_context"]["current_message"]
    assert evaluate_grounding(case["draft"], evidence, retrieval_expected=True,
                              tool_errored=False, user_text=message).grounded
    assert unsupported_external_numbers(case["draft"].replace("27 min", "35 min"), evidence, message)


def test_the_critic_keeps_the_honest_failed_lookup_statement(monkeypatch):
    import core.llm_config as llm_config

    def _no_llm(*_a, **_k):
        raise AssertionError("the critic must not regenerate a grounded answer")

    monkeypatch.setattr(llm_config, "get_react_llm", _no_llm)
    case = CORPUS["r11_97bdde65_precritic"]
    update = asyncio.run(_make_critic_node()(dict(case["state"], final_response=case["draft"])))
    final = update.get("final_response", case["draft"])
    assert final == case["draft"]
    assert "this is a failed lookup, not a finding that there are no gyms there" in final
    assert "don't cover gyms" not in final


def test_a_rewrite_is_told_which_lookups_failed():
    """When the critic does rewrite, its prompt names each failed lookup as a failure."""
    state = CORPUS["r11_97bdde65_precritic"]["state"]
    prompt = _build_generation_prompt(state)
    assert "FAILED LOOKUPS THIS TURN" in prompt
    assert prompt.count("[search_nearby_pois#") >= 2 and "not a gap in coverage" in prompt
    assert "Camden Road" in prompt.split("FAILED LOOKUPS THIS TURN", 1)[1]
    # the evidence block itself still carries only usable artifacts
    assert "FAILED" not in (_fc_artifact_observation(state) or "")


# ── collateral: advice beside an invented figure ──────────────────────────────────────

def test_advice_clause_survives_and_the_invented_arithmetic_goes():
    text, state = _case("r11_d36a82c2_critic")
    out = validate_commute_response(text, state)
    assert ("and for Bow you would want to look at addresses closer to Bow Church DLR Station "
            "than the one measured here") in out
    assert "90 minutes over a five-day week" not in out
    assert "18 minutes a day" not in out
    assert out.count(NOTE_EN) == 1


def test_measured_legs_on_the_routes_own_line_are_kept():
    """"Stratford puts you about 4 minutes from the Jubilee line" (the walk leg) and "(9
    minutes each)" (both routes' in-vehicle legs)."""
    text, state = _case("r11_d36a82c2_draft")
    assert validate_commute_response(text, state) == text
