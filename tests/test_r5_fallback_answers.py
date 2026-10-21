"""R4-10: fallback answers that list nothing, on recorded states (round 5).

``tests/data/r5_fallback_corpus.json`` holds the recorded state of each turn, exported from
the frozen checkpoints (r06 e373e422: the shipped state; r07 inst-B B-F12: the state the
critic received, with the model's own answer as ``draft``).
"""
import asyncio
import json
import pathlib
import re

import pytest

from core.agent_loop import _artifact_grounded_fallback_answer
from core.langgraph_agent import _collect_grounding_evidence, _make_critic_node
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" / "r5_fallback_corpus.json")
                    .read_text(encoding="utf-8"))
B_F12 = ["r07_0823359a", "r07_90312245", "r07_810d5bc9"]


def _no_empty_lead_in(text):
    """Every line that ends with a colon is followed by a non-empty line."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.rstrip().endswith((":", "：")):
            assert index + 1 < len(lines) and lines[index + 1].strip(), text


def test_tool_call_error_with_nothing_obtained_has_no_results_preamble():
    """Acceptance NEW-7 (r06 e373e422): "These are the results already obtained this turn:"
    was followed by no results."""
    case = CORPUS["r06_e373e422"]
    assert "These are the results already obtained this turn:" in case["shipped"]
    out = _artifact_grounded_fallback_answer(case["state"], reason="tool_call_error",
                                             failed_tools=["get_property_details"])
    assert "results already obtained" not in out
    assert out.startswith("I couldn't run one of the lookups this turn (its request was malformed).")
    assert "The details lookup for that listing failed this turn" in out
    _no_empty_lead_in(out)


@pytest.mark.parametrize("name", B_F12)
def test_failed_lookup_fallback_names_the_lookups_and_no_listing_results(name):
    """B-F12: every POI lookup of the turn failed; the fallback said "I do not yet have listing
    results ready to show" to a gym/restaurant question and "Nearby amenities have not been
    looked up yet" after the lookups had run and failed."""
    case = CORPUS[name]
    assert "I do not yet have listing results ready to show." in case["shipped"]
    out = _artifact_grounded_fallback_answer(case["state"], reason="no_reliable_numbers")
    assert "listing results" not in out
    assert "have not been looked up yet" not in out
    assert "here is what I have verified:" not in out
    assert "nearby-places lookup" in out and "failed this turn" in out
    for artifact in case["state"]["tool_artifacts"]:
        assert artifact["raw_data"]["address"] in out
    _no_empty_lead_in(out)


def test_failed_lookup_fallback_in_chinese():
    case = CORPUS["r07_0823359a"]
    state = dict(case["state"], extracted_context=dict(case["state"]["extracted_context"],
                                                       reply_language="zh"))
    out = _artifact_grounded_fallback_answer(state, reason="no_reliable_numbers")
    assert "房源结果" not in out
    assert "以下" not in out and "先按已核实的信息回答" not in out
    assert "周边设施查询（gym near Camden Road, Islington, London, N7）失败" in out
    _no_empty_lead_in(out)


def test_a_result_still_gets_its_preamble():
    case = CORPUS["r07_0823359a"]
    state = dict(case["state"], tool_artifacts=case["state"]["tool_artifacts"] + [{
        "turn": 2, "tool": "check_safety", "success": True,
        "raw_data": {"address": "Camden Road, N7", "safety_score": 61, "safety_level": "Safe"}}])
    out = _artifact_grounded_fallback_answer(state, reason="no_reliable_numbers")
    assert out.startswith("Sorry — I couldn't retrieve reliable specific figures right now, so "
                          "here is what I have verified:")
    assert "Camden Road, N7 scored 61/100" in out


# ── the hard replace that produced B-F12 ─────────────────────────────────────────────

@pytest.mark.parametrize("name", B_F12)
def test_the_failed_lookups_radius_is_not_an_external_number(name):
    """Each B-F12 answer was honest ("I tried twice to look up gyms within 800m …, and both
    lookups failed") and answered the rest ("Best value: Camden Road, N7 …"). The 800 m it
    quoted came from the FAILED lookup, so it counted as unsupported and the critic
    hard-replaced the answer with the fallback."""
    case = CORPUS[name]
    state = case["state"]
    evidence = _collect_grounding_evidence(state, "")
    message = state["extracted_context"]["current_message"]
    assert re.search(r"800\s?m\b", case["draft"])
    assert not unsupported_external_numbers(case["draft"], evidence, message)
    assert evaluate_grounding(case["draft"], evidence, retrieval_expected=True,
                              tool_errored=False, user_text=message).grounded
    # a radius the lookup did not use is still unsupported
    assert unsupported_external_numbers(re.sub(r"800\s?m\b", "1500 m", case["draft"]), evidence, message)


def test_the_critic_keeps_the_best_value_answer(monkeypatch):
    """The real critic node, offline (no regeneration is possible here), keeps the model's
    answer for r07 0823359a: the "best value" part and the named gym-lookup failure."""
    import core.llm_config as llm_config

    def _no_llm(*_a, **_k):
        raise AssertionError("the critic must not regenerate a grounded answer")

    monkeypatch.setattr(llm_config, "get_react_llm", _no_llm)
    case = CORPUS["r07_0823359a"]
    update = asyncio.run(_make_critic_node()(dict(case["state"], final_response=case["draft"])))
    final = update.get("final_response", case["draft"])
    assert final == case["draft"]
    assert final.startswith("Best value: Camden Road, N7 (1 bed flat) at £1800/month.")
    assert "I do not yet have listing results" not in final
