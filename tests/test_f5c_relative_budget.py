"""P1-1: "raise the budget by £200" must not become an absolute budget of £200.

Live evidence this pins (fc pool, 2026-09-04): on `agenttest-t1-f4b-fc` the prior turn's
ceiling was £2,200; the follow-up "raise the budget by £200, search again" ran
`search_properties` once in 272 ms with `max_budget: 200` and returned zero results, and the
model told the user "the tool's internal criteria still show `max_budget: 200`". On
`-f4-fc` the same shape produced "I've raised the budget from £200 to £400".
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from core.agent_loop import _relative_budget_repair, build_fc_nodes
from core.search_readiness import detect_relative_budget_delta, resolve_relative_budget
from tests.test_agent_contract_guards import Provider, Result, Spec, _state, _tc


# ── the resolver ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # EN, the live phrasing and its neighbours
        ("raise the budget by £200, search again", 2400),
        ("Raise the budget by 200 and search again", 2400),
        ("can you bump the budget by £150?", 2350),
        ("lower the budget by £150", 2050),
        ("budget up by 200", 2400),
        ("I can stretch to another £200", 2400),
        ("£200 more a month is fine", 2400),
        ("£200 less please", 2000),
        # ZH
        ("把预算提高200镑", 2400),
        ("预算加200，再搜一次", 2400),
        ("预算多200镑", 2400),
        ("预算降低150", 2050),
        ("预算下调 200 镑", 2000),
    ],
)
def test_relative_phrasings_resolve_against_the_prior_ceiling(message, expected):
    assert resolve_relative_budget(message, 2200) == expected


@pytest.mark.parametrize(
    "message",
    [
        # An absolute restatement dressed in raise/lower language is NOT a delta.
        "raise the budget to £2400",
        "把预算提高到2400",
        "increase the budget up to 2400",
        # Ordinary criteria statements.
        "my budget is £2000",
        "2 bedroom flat in Camden under £2000 a month",
        "我想在Camden找一个两居室，预算每月2000镑以内",
        "tell me more about the second one",
        "search again please",
        # Numbers that count something other than money.
        "add 2 bedrooms",
        "加2个卧室",
        "把通勤时间提高10分钟",
        "raise the commute limit by 10 minutes",
    ],
)
def test_non_relative_messages_are_left_alone(message):
    assert detect_relative_budget_delta(message) is None
    assert resolve_relative_budget(message, 2200) is None


def test_a_weekly_delta_is_normalised_to_monthly():
    delta = detect_relative_budget_delta("increase the budget by £50 a week")
    assert delta is not None and delta > 200  # 50/week is ~217/month, not 50


def test_a_delta_with_no_prior_ceiling_resolves_to_nothing():
    assert resolve_relative_budget("raise the budget by £200", None) is None
    assert resolve_relative_budget("raise the budget by £200", 0) is None


def test_a_nonsense_reduction_floors_rather_than_going_negative():
    assert resolve_relative_budget("lower the budget by £3000", 2000) == 1


# ── the extract_preferences repair ────────────────────────────────────────────


def _clobbering_node(state):
    """Stand-in for the shared node: it folds the stated amount in as a CEILING, which is
    exactly the behaviour that produced max_budget=200 live."""
    accumulated = dict(state.get("accumulated_search_criteria") or {})
    accumulated["max_budget"] = 200
    return {"accumulated_search_criteria": accumulated, "user_preferences": {}}


def test_the_repair_restores_the_prior_ceiling_plus_the_delta():
    node = _relative_budget_repair(_clobbering_node)
    update = node({
        "accumulated_search_criteria": {"max_budget": 2200, "area": "Islington"},
        "extracted_context": {"current_message": "raise the budget by £200, search again"},
        "user_query": "raise the budget by £200, search again",
    })
    assert update["accumulated_search_criteria"]["max_budget"] == 2400
    # Nothing else in the node's own update is disturbed.
    assert update["accumulated_search_criteria"]["area"] == "Islington"
    assert "user_preferences" in update


def test_the_repair_clears_the_ceiling_when_there_is_no_prior_value():
    """A delta is not a ceiling. Keeping 200 is what guaranteed zero results."""
    node = _relative_budget_repair(_clobbering_node)
    update = node({
        "accumulated_search_criteria": {},
        "extracted_context": {"current_message": "raise the budget by £200"},
        "user_query": "raise the budget by £200",
    })
    assert update["accumulated_search_criteria"]["max_budget"] is None


def test_the_repair_is_a_no_op_for_an_ordinary_message():
    node = _relative_budget_repair(_clobbering_node)
    update = node({
        "accumulated_search_criteria": {"max_budget": 2200},
        "extracted_context": {"current_message": "find me a 2 bed in Camden"},
        "user_query": "find me a 2 bed in Camden",
    })
    assert update["accumulated_search_criteria"]["max_budget"] == 200  # node's own value


def test_the_repair_wraps_an_async_node():
    async def node(state):
        return {"accumulated_search_criteria": {"max_budget": 200}}

    wrapped = _relative_budget_repair(node)
    update = asyncio.run(wrapped({
        "accumulated_search_criteria": {"max_budget": 2200},
        "extracted_context": {"current_message": "raise the budget by £200"},
        "user_query": "raise the budget by £200",
    }))
    assert update["accumulated_search_criteria"]["max_budget"] == 2400


# ── the executor-side defence ─────────────────────────────────────────────────


def _run_one_search(state, call_args):
    provider = Provider(
        [Spec("search_properties")],
        {"search_properties": Result(True, {"status": "found", "recommendations": []})},
    )
    nodes = build_fc_nodes(provider)
    state["messages"] = [AIMessage(content="", tool_calls=[
        _tc("search_properties", call_args, "s1")])]

    async def run():
        return await nodes["execute_tools"](state)

    asyncio.run(run())
    return provider.calls[0][1]


def test_a_model_supplied_delta_never_becomes_the_search_ceiling():
    """The model itself emits `max_budget: 200` for "raise the budget by £200"."""
    state = _state("raise the budget by £200, search again")
    state["accumulated_search_criteria"] = {"max_budget": 2400, "area": "Islington"}
    params = _run_one_search(state, {"area": "Islington", "max_budget": 200})
    assert params["max_budget"] == 2400


def test_a_model_supplied_delta_with_no_ceiling_drops_the_filter():
    state = _state("raise the budget by £200, search again")
    state["accumulated_search_criteria"] = {"area": "Islington"}
    params = _run_one_search(state, {"area": "Islington", "max_budget": 200})
    assert not params.get("max_budget")


def test_a_genuine_ceiling_stated_this_turn_is_untouched():
    state = _state("actually make the budget £1800")
    state["accumulated_search_criteria"] = {"max_budget": 1800, "area": "Islington"}
    params = _run_one_search(state, {"area": "Islington", "max_budget": 1800})
    assert params["max_budget"] == 1800


# ── ceiling phrasings are NOT deltas (revision 2) ─────────────────────────────
# Found live by F4 on the legacy pool: the loose ZH pattern matched the bare 多 inside
# 最多, so 「预算每月最多2200英镑」 — a plain ceiling — was read as a +2200 DELTA. With no
# prior ceiling to add it to, the caller's "a delta is not a ceiling" rule then dropped the
# budget filter entirely and `fixtest-f4-rz5-legacy` recorded `max_budget: null` where the
# pre-change run had recorded 2000.


RZ5_SENTENCE = "预算每月最多2200英镑"


@pytest.mark.parametrize(
    "message",
    [
        # the exact live sentence, alone and in a fuller request
        RZ5_SENTENCE,
        "我想在Islington找两居室，" + RZ5_SENTENCE + "，不用考虑通勤。",
        # every ZH ceiling marker
        "预算至多2200镑",
        "预算顶多2200",
        "预算不超过2200镑",
        "预算不高于2200",
        "预算不多于2200镑",
        "预算最高2200镑",
        "预算上限2200镑",
        "预算2200镑以内",
        "预算2200以下",
        "预算2200之内",
        "每月2200英镑封顶",
        # every EN ceiling marker
        "budget of under £2200 a month",
        "keep it below £2200",
        "within £2200 a month",
        "no more than £2200",
        "not more than £2200",
        "at most £2200",
        "up to £2200",
        "a maximum of £2200",
        "max of £2200",
        "capped at £2200",
        "cap £2200",
    ],
)
def test_a_ceiling_phrasing_is_never_read_as_a_delta(message):
    assert detect_relative_budget_delta(message) is None
    assert resolve_relative_budget(message, 2000) is None


def test_a_ceiling_word_does_not_swallow_a_genuine_delta_beside_it():
    """「最多提高200镑」 bounds the RAISE; 最多 is followed by a verb, not by digits."""
    assert detect_relative_budget_delta("最多提高200镑") == 200
    # An EN ceiling marker on a DIFFERENT dimension must not disarm the budget delta.
    assert detect_relative_budget_delta(
        "raise the budget by £200 and keep the commute under 30 minutes") == 200


def test_the_repair_leaves_a_ceiling_sentence_to_the_node():
    """Both the graph-side repair and the executor-side defence route through
    `detect_relative_budget_delta`, so the live regression is closed in one place."""
    node = _relative_budget_repair(_clobbering_node)
    update = node({
        "accumulated_search_criteria": {},
        "extracted_context": {"current_message": RZ5_SENTENCE},
        "user_query": RZ5_SENTENCE,
    })
    # untouched: the node's own value stands, and max_budget is NOT cleared
    assert update["accumulated_search_criteria"]["max_budget"] == 200


def test_the_executor_defence_leaves_a_ceiling_sentence_alone():
    state = _state(RZ5_SENTENCE)
    state["accumulated_search_criteria"] = {"max_budget": 2200, "area": "Islington"}
    params = _run_one_search(state, {"area": "Islington", "max_budget": 2200})
    assert params["max_budget"] == 2200
