"""Round-2 cheap fixes: R2-11, R2-24, R2-29, R2-30, R2-32."""
from __future__ import annotations

import pytest


# ── R2-29: "favourite" as an adjective is not saved-listing talk ──────────────────────
@pytest.mark.parametrize("message,expect_hint", [
    ("My favourite area is Angel and my favourite fruit is mango.", False),
    ("Please save the Gloucester Avenue one to my favourites", True),
    ("Show me my favourites", True),
    ("帮我收藏这套", True),
])
def test_r2_29_save_button_hint_only_for_saved_listing_talk(message, expect_hint):
    from core.constraint_updates import validate_current_criteria_recap
    out = validate_current_criteria_recap("Noted.", {}, message=message, language="en")
    assert ("heart/Save button" in out) is expect_hint


# ── R2-11: Chinese answers get Chinese guard footers ──────────────────────────────────
def test_r2_11_caveat_follows_the_answer_language():
    from uk_rent_agent.agent.critic import CAVEAT, CAVEAT_ZH, append_caveat
    zh_answer = "关于您问的第二个房源的通勤时间，需要先说明一点：系统把起点解析成了另一个车站，因此这组数据不能代表真实通勤情况。"
    out = append_caveat(zh_answer)
    assert out.endswith(CAVEAT_ZH) and CAVEAT not in out
    assert append_caveat("The rent is £1,500 a month.").endswith(CAVEAT)


# ── R2-30: a policy refusal is not a tool failure ─────────────────────────────────────
def test_r2_30_policy_refusal_is_worded_as_a_refusal():
    from core.memory_contract import memory_contract_from_artifact, memory_notice
    artifact = {"tool": "remember", "success": True, "raw_data": {
        "success": False, "id": None, "stored": None, "mtype": None,
        "security_decision": "instruction_not_fact",
        "error": "I can only save durable facts about you (preferences, budget, constraints) "
                 "— not instructions, so I did not store that."}}
    contract = memory_contract_from_artifact(artifact)
    notice = memory_notice(contract, "en")
    assert "memory tool failed" not in notice and "not instructions" in notice
    assert "不保存指令" in memory_notice(contract, "zh")


# ── R2-24: "no commute" opts out of the commute dimension ─────────────────────────────
@pytest.mark.parametrize("message", [
    "Find a 1-bed in Camden under £2,000, no commute",
    "Studio in Hackney, I don't need a commute",
])
def test_r2_24_no_commute_messages_get_no_commute_apology(message):
    from core.agent_loop import _missing_requested_dimension_lines
    lines = _missing_requested_dimension_lines(message, set(), "en")
    assert not any("Commute time" in line for line in lines)
    asked = _missing_requested_dimension_lines("Find a 1-bed in Camden, how long is the commute to UCL?",
                                               set(), "en")
    assert any("Commute time" in line for line in asked)


# ── R2-32: a police API outage is not an empty result ─────────────────────────────────
def test_r2_32_all_months_failed_is_reported_as_an_outage(monkeypatch):
    from core.tools import check_safety
    monkeypatch.setattr(check_safety, "get_crime_data_by_location", lambda *a, **k: {
        "total_crimes_6m": 0, "months_covered": 0, "crimes_per_month": None,
        "queried_months": ["2026-07", "2026-08"], "successful_months": [],
        "failed_months": ["2026-07", "2026-08"], "source": "data.police.uk"})
    result = check_safety.check_safety_impl(address="Hackney, London E8")
    assert result["service_status"] == "unavailable"
    assert result["safety_score"] is None
    assert "did not respond" in result["safety_analysis"]
    assert "too few records" not in result["safety_analysis"]
    assert "did not respond" in result["recommendation"]


# ── R2-31: every remember call of the turn is acknowledged ────────────────────────────
def test_r2_31_two_saves_are_both_quoted():
    from core.memory_contract import compose_memory_contract_response, memory_contract_from_artifacts
    arts = [{"tool": "remember", "success": True, "raw_data": {"success": True, "stored": "I have a cat"}},
            {"tool": "remember", "success": True, "raw_data": {"success": True, "stored": "I am allergic to pollen"}}]
    contract = memory_contract_from_artifacts(arts)
    out = compose_memory_contract_response("Done.", contract, language="zh")
    assert "I have a cat" in out and "I am allergic to pollen" in out
    assert "已保存的原文" not in out and "已保存的内容" in out
    zh = compose_memory_contract_response(
        "好的。", memory_contract_from_artifacts([{"tool": "remember", "success": True,
                                                  "raw_data": {"success": True, "stored": "我养了一只猫"}}]),
        language="zh")
    assert "已保存的原文" in zh


def test_r2_31_a_partial_failure_says_so():
    from core.memory_contract import memory_contract_from_artifacts, memory_notice
    arts = [{"tool": "remember", "success": True, "raw_data": {"success": True, "stored": "I have a cat"}},
            {"tool": "remember", "success": False, "raw_data": None, "error": "boom"}]
    assert "part of that" in memory_notice(memory_contract_from_artifacts(arts), "en")


# ── R2-27: a failed web lookup is reported as one ─────────────────────────────────────
def test_r2_27_failed_web_search_fallback_names_the_failure():
    from core.agent_loop import _artifact_grounded_fallback_answer
    state = {"extracted_context": {"current_message": "What is the council tax band rule for students?",
                                   "reply_language": "en"},
             "user_query": "What is the council tax band rule for students?",
             "tool_artifacts": [{"turn": 1, "tool": "web_search", "raw_data": None,
                                 "success": False, "error": "unavailable", "params_digest": "w"}]}
    out = _artifact_grounded_fallback_answer(state, reason="time_budget")
    assert "web lookup failed" in out
    assert "listing results" not in out


# ── R2-26: a composite question about the listings keeps its answer ───────────────────
def test_r2_26_composite_answer_leads_and_the_ledger_follows():
    from core import agent_loop
    from core.agent_loop import build_fc_nodes
    from core.candidate_validation import validate_candidates
    from tests.test_agent_contract_guards import Chat, Provider, _state

    rows = [{"address": "Canning Road, Stratford, E15", "price": 1699, "area": "Stratford"},
            {"address": "Legacy Tower, London, E15", "price": 2395, "area": "Stratford"}]
    criteria = {"area": "Stratford", "areas": ["Stratford"], "max_budget": 2200}
    validation = validate_candidates(rows, criteria, [])
    message = ("Check safety for the first listing, weather there on Friday afternoon at 3pm, "
               "and the TfL fare to Bank, all at once")
    answer = ("For Canning Road, Stratford: police data shows 4,211 recorded incidents over three "
              "months; Friday 15:00 looks dry; the single fare to Bank is £3.10.")
    nodes = build_fc_nodes(Provider([]), agent_llm=Chat())
    state = _state(message)
    state["final_response"] = answer
    state["tool_artifacts"] = [
        agent_loop._artifact(1, "search_properties", {
            "status": "found", "recommendations": rows[:1], "over_budget_alternatives": rows[1:],
            "candidate_validation": validation, "search_criteria": criteria}, success=True),
        agent_loop._artifact(1, "check_safety", {"address": "Stratford", "safety_score": None,
                                                 "source": "data.police.uk"}, success=True)]
    assert validation.get("excluded")          # the ledger path (requires_status) is taken
    out = nodes["format_output_fc"](state)["final_response"]
    assert out.startswith("For Canning Road, Stratford")
    assert "Legacy Tower" in out
