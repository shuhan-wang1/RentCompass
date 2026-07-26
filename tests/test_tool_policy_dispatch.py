"""Read-tool dispatch policy — regressions pinned to the 2026-07-25 fc_loop sweep.

That sweep (``arch=fc_loop``, 98 cases) recorded three ``forbidden_tool_executed``
violations and every one of them reached the executor unchallenged, because
``execute_tools_node`` gated WRITES and dispatched every READ:

    B8   web_search        "UK student accommodation deposit standard amount ..."   (x2)
    B12  search_properties max_budget=380 budget_period=week room_type=studio, no area
    B14  web_search        "UK maximum tenancy deposit limit England 5 weeks rent"  (x2)

``tools_denied`` was empty for all three; the only denials anywhere in the sweep were
``denied: turn time budget exhausted``. These tests fail on that behaviour: they assert
the call is refused BEFORE dispatch (the provider records no execution), that the refusal
is recorded as a denial rather than an execution, and that the model is handed the
authoritative figures instead.

The queries and arguments below are the recorded ones, not invented ones.
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import core.agent_loop as agent_loop
import core.tool_policy as tool_policy
from core.tenancy_reference import deposit_cap

from test_fc_loop import (  # the fc-loop fakes, reused verbatim
    FakeChat, FakeProvider, FakeResult, FakeSpec, _base_state, _drive, _run, _step, _tc,
)


# The three recorded user turns, verbatim from evaluation/benchmark/cases.jsonl.
B8_QUERY = ("What's the total move-in cost for a £1600 pcm place — first month plus the "
            "standard deposit?")
B12_QUERY = ("I'm looking at a £380/week studio. What'll it cost me all-in per month, "
             "including bills and council tax?")
B14_QUERY = "The rent is £1,000 a week. What deposit is the landlord legally allowed to take?"


def _money_state(query):
    return _base_state(
        user_query=query,
        extracted_context={"current_message": query, "reply_language": "en"},
    )


# ─── the predicate ──────────────────────────────────────────────────
@pytest.mark.parametrize("query,expected", [
    (B8_QUERY, (1600.0, "month")),
    (B12_QUERY, (380.0, "week")),
    (B14_QUERY, (1000.0, "week")),
])
def test_recorded_forbidden_turns_are_recognised_as_self_contained(query, expected):
    assert tool_policy.self_contained_money_question(query) == expected


def test_period_binds_to_the_figure_not_the_message():
    """B12 quotes the rent per WEEK and asks for the answer per MONTH. Reading the period
    off the whole message calls that ambiguous (and lets the doomed search through);
    reading it off the figure gets £380/week right."""
    assert tool_policy._amount_with_period("a £380/week studio ... per month") == (380.0, "week")
    assert tool_policy._amount_with_period("£1600 pcm") == (1600.0, "month")
    assert tool_policy._amount_with_period("£1,000 a week") == (1000.0, "week")
    # Unit nowhere near the figure and ambiguous in the message: refuse to guess.
    assert tool_policy._amount_with_period(
        "£1,000 for the flat, is that weekly or monthly?") is None
    # No unit at all: also refuse. The 5-vs-6-week cap turns on the annual rent, so a
    # figure whose period we invented would silently pick a cap.
    assert tool_policy._amount_with_period("the deposit on £1,000") is None


@pytest.mark.parametrize("query", [
    # Asks us to FIND something: retrieval is the point of the turn, budget or not.
    "Find me a 2-bed flat in Camden under £1500 a month. No commute to worry about.",
    "Find a studio in Stratford under £1300/month with a commute to Canary Wharf under 25 minutes.",
    "帮我找伦敦月租不超过1400镑的单间，通勤到帝国理工不超过35分钟。",
    # Names a place we could search in.
    "What's the average studio rent in Shoreditch?",
    "I'm about to rent a studio in Whitechapel (E1). What should I know before I sign?",
    # No rent figure of the user's own — the answer is not derivable from what they typed.
    "Search for the current Zone 1-2 monthly travelcard price.",
    "How much is a deposit usually?",
    # A rent with no period: the 5-vs-6 week cap turns on the ANNUAL rent, so a figure
    # whose period we would have to guess must not drive a statutory computation.
    "The rent is £1,000. What deposit can they take?",
    # Quotes a rent and a period but asks nothing about cost.
    "Actually make it £1500 a month instead.",
])
def test_predicate_does_not_fire_on_retrieval_turns(query):
    assert tool_policy.self_contained_money_question(query) is None


# ─── B8 / B14: web_search for a statutory constant ──────────────────
@pytest.mark.parametrize("case_id,query,search_query", [
    ("B8", B8_QUERY,
     "UK student accommodation deposit standard amount first month rent move-in cost"),
    ("B14", B14_QUERY, "UK maximum tenancy deposit limit England 5 weeks rent 2025"),
])
def test_web_search_for_statutory_money_rule_never_dispatches(case_id, query, search_query):
    specs = [FakeSpec("web_search")]
    provider = FakeProvider(specs, {"web_search": FakeResult(True, {"results": "..."})})
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("web_search", {"query": search_query}, "c1")]),
        AIMessage(content="answer"),
    ])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _money_state(query)))

    # Pre-dispatch: the tool never ran. This is the assertion the old code fails.
    assert provider.calls == [], f"{case_id}: web_search executed despite the read policy"

    # Recorded as a DENIAL, not an execution: denied artifacts are excluded from the
    # executed tool trace the eval's route/forbidden checkers judge.
    art = [a for a in state["tool_artifacts"] if a.get("tool") == "web_search"]
    assert len(art) == 1
    assert art[0]["denied"] is True
    assert not agent_loop._is_executed(art[0])
    assert "statutory money rule owned in-product" in art[0]["error"]


def test_b14_denial_hands_back_the_six_week_cap_the_web_snippet_omitted():
    """B14's second web_search DID succeed and returned Shelter's "a tenancy deposit cannot
    be more than 5 weeks' rent" — which omits the £50,000 threshold. The model led with
    £5,000. At £1,000/week the annual rent is £52,000, so the cap is six weeks: £6,000.
    The refusal must supply that, otherwise denying the tool just leaves the model guessing.
    """
    specs = [FakeSpec("web_search")]
    provider = FakeProvider(specs)
    chat = FakeChat([
        AIMessage(content="", tool_calls=[
            _tc("web_search", {"query": "England tenancy deposit cap 5 weeks rent"}, "c1")]),
        AIMessage(content="answer"),
    ])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _money_state(B14_QUERY)))

    msg = next(m for m in state["messages"]
               if isinstance(m, ToolMessage) and m.name == "web_search")
    ref = json.loads(msg.content)["reference"]
    assert ref["annual_rent_gbp"] == 52000.0
    assert ref["deposit_cap_weeks"] == 6
    assert ref["max_tenancy_deposit_gbp"] == 6000.0


def test_b8_denial_hands_back_the_move_in_total():
    specs = [FakeSpec("web_search")]
    provider = FakeProvider(specs)
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("web_search", {"query": "deposit"}, "c1")]),
        AIMessage(content="answer"),
    ])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _money_state(B8_QUERY)))

    msg = next(m for m in state["messages"]
               if isinstance(m, ToolMessage) and m.name == "web_search")
    ref = json.loads(msg.content)["reference"]
    # 1600*12/52 = 369.23/week; annual 19,200 < 50,000 -> 5 weeks = 1846.15;
    # first month + deposit = 3446.15 (cases.jsonl B8 reference_calculations).
    assert ref["deposit_cap_weeks"] == 5
    assert ref["max_tenancy_deposit_gbp"] == pytest.approx(1846.15, abs=0.01)
    assert ref["first_month_plus_deposit_gbp"] == pytest.approx(3446.15, abs=0.01)


# ─── B12: search_properties on a cost question ──────────────────────
def test_b12_search_properties_never_dispatches():
    """Recorded args: the model passed the £380 through as max_budget with no area at all.
    The tool spent 2.7s and came back need_clarification/missing_area — it never searched."""
    specs = [FakeSpec("search_properties")]
    provider = FakeProvider(specs, {"search_properties": FakeResult(
        False, {"success": False, "status": "need_clarification",
                "clarification_kind": "missing_area", "missing_fields": ["area"]})})
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {
            "max_budget": 380, "budget_period": "week", "room_type": "studio"}, "c1")]),
        AIMessage(content="answer"),
    ])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _run(_drive(nodes, _money_state(B12_QUERY)))

    assert provider.calls == [], "B12: search_properties executed despite the read policy"
    art = [a for a in state["tool_artifacts"] if a.get("tool") == "search_properties"]
    assert len(art) == 1 and art[0]["denied"] is True
    assert "no searchable area" in art[0]["error"]

    msg = next(m for m in state["messages"]
               if isinstance(m, ToolMessage) and m.name == "search_properties")
    body = json.loads(msg.content)
    assert body["success"] is False
    # 380/week -> 1646.67/month (cases.jsonl B12 reference_calculations).
    assert body["reference"]["monthly_rent_gbp"] == pytest.approx(1646.67, abs=0.01)


# ─── the policy must not touch legitimate retrieval ─────────────────
def test_ordinary_search_turn_is_untouched():
    specs = [FakeSpec("search_properties")]
    provider = FakeProvider(specs, {"search_properties": FakeResult(
        True, {"status": "found", "recommendations": [{"address": "1 Camden Rd"}]})})
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"area": "Camden"}, "c1")]),
        AIMessage(content="here you go"),
    ])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    query = "Find me a 2-bed flat in Camden under £1500 a month."
    _run(_drive(nodes, _money_state(query)))
    assert [c[0] for c in provider.calls] == ["search_properties"]


def test_non_retrieval_tools_are_never_gated():
    """The policy governs retrieval only. A calculator on the very same self-contained
    money turn must still run — refusing it would be routing, not enforcement."""
    specs = [FakeSpec("calculate_commute_cost")]
    provider = FakeProvider(specs, {"calculate_commute_cost": FakeResult(True, {"cost": 5})})
    chat = FakeChat([
        AIMessage(content="", tool_calls=[
            _tc("calculate_commute_cost", {"origin": "a", "destination": "b"}, "c1")]),
        AIMessage(content="ok"),
    ])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    _run(_drive(nodes, _money_state(B8_QUERY)))
    assert [c[0] for c in provider.calls] == ["calculate_commute_cost"]


def test_policy_failure_falls_open_to_dispatch(monkeypatch):
    """A policy that raises must not take the turn down, and must not silently become a
    deny-all. The pre-policy behaviour (dispatch) is the fallback."""
    class _Boom:
        @staticmethod
        def read_tool_denial(*a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_loop, "_load_tool_policy", lambda: _Boom)
    specs = [FakeSpec("web_search")]
    provider = FakeProvider(specs, {"web_search": FakeResult(True, {"results": "x"})})
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("web_search", {"query": "deposit"}, "c1")]),
        AIMessage(content="ok"),
    ])
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    _run(_drive(nodes, _money_state(B14_QUERY)))
    assert [c[0] for c in provider.calls] == ["web_search"]


# ─── the deposit reference itself ───────────────────────────────────
def test_deposit_cap_threshold_is_inclusive_at_50k():
    """Sch.1 para 2 is "£50,000 or more" -> six weeks. B14 sits one rung above the line
    and the 5-week figure (£5,000) is the trap the case is built around."""
    # £961.54/week is £50,000.08/year — just over.
    at_or_above = deposit_cap(weekly_rent=50_000 / 52)
    assert at_or_above["annual_rent_gbp"] == pytest.approx(50_000, abs=0.01)
    assert at_or_above["deposit_cap_weeks"] == 6
    just_below = deposit_cap(monthly_rent=4_000)  # 923.08/wk -> 48,000/yr
    assert just_below["deposit_cap_weeks"] == 5


def test_deposit_cap_requires_exactly_one_period():
    with pytest.raises(ValueError):
        deposit_cap()
    with pytest.raises(ValueError):
        deposit_cap(weekly_rent=100, monthly_rent=400)


# ─── B12 fallout: a clarification is not an empty search ────────────
def test_need_clarification_is_not_reported_as_a_completed_empty_search():
    """B12's final answer was "The property search completed: no studio listings matched
    your criteria (data from OnTheMarket)". No search ran — the tool returned
    need_clarification/missing_area. _completed_empty_search_raw counted it as a completed
    zero-match because the payload has no `recommendations` key."""
    clarify = {
        "success": False,
        "status": "need_clarification",
        "clarification_kind": "missing_area",
        "missing_fields": ["area"],
        "known_criteria": {"room_type": "studio", "max_budget": 380},
    }
    artifacts = [agent_loop._artifact(0, "search_properties", clarify, "d1", success=False)]
    assert agent_loop._completed_empty_search_raw(artifacts) is None

    state = _money_state(B12_QUERY)
    state["tool_artifacts"] = artifacts
    answer = agent_loop._artifact_grounded_fallback_answer(
        state, reason="no_reliable_numbers")
    assert "search completed" not in answer.lower()
    assert "OnTheMarket" not in answer


def test_genuine_zero_match_search_is_still_reported_honestly():
    """The honest complete-empty line must survive: a real no-results payload sets
    success=True, so the new guard costs that branch nothing."""
    empty = {
        "success": True,
        "status": "no_results",
        "recommendations": [],
        "search_criteria": {"room_type": "studio", "area": "camden"},
    }
    artifacts = [agent_loop._artifact(0, "search_properties", empty, "d1", success=True)]
    assert agent_loop._completed_empty_search_raw(artifacts) is empty

    state = _money_state("any")
    state["tool_artifacts"] = artifacts
    answer = agent_loop._artifact_grounded_fallback_answer(
        state, reason="no_reliable_numbers")
    assert "search completed" in answer.lower()
