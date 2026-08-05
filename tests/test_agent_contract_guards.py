"""Minimal synthetic regressions for the agent's evidence and side-effect contracts.

These cases deliberately use small in-memory providers and made-up listings.  They do
not exercise the held-out data or make network calls.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage

from core import agent_loop
from core.agent_loop import build_fc_nodes
from core.candidate_state import (
    build_candidate_state,
    validate_commute_response,
)
from core.tenancy_reference import monthly_from_weekly


@dataclass
class Spec:
    name: str
    side_effect: str = "none"
    retry_safe: bool = True
    version: str = "1"
    terminal: bool = False
    description: str = "test"
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


class Result:
    def __init__(self, success=True, data=None, error=None):
        self.success = success
        self.data = data
        self.error = error


class Provider:
    def __init__(self, specs, results=None):
        self.specs = specs
        self.results = results or {}
        self.calls = []

    def list_specs(self):
        return list(self.specs)

    def get(self, name):
        return next((s for s in self.specs if s.name == name), None)

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        result = self.results.get(name)
        if callable(result):
            result = result(**params)
        return result or Result(True, {"ok": True})


class Chat:
    def __init__(self, *messages):
        self.messages = list(messages)

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        return self.messages.pop(0)


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _state(message="Find homes with a commute under 30 minutes"):
    return {
        "user_query": message,
        "extracted_context": {"current_message": message, "reply_language": "en"},
        "accumulated_search_criteria": {},
        "user_preferences": {},
        "session_id": "synthetic-session",
        "user_id": "synthetic-user",
        "run_id": "synthetic-run",
        "loop_turn": 0,
        "messages": [],
        "tool_artifacts": [],
        "context_tainted": False,
        "final_response": "",
        "response_type": "answer",
    }


async def _drive(nodes, state):
    node = "agent"
    while True:
        command = nodes[node](state)
        if asyncio.iscoroutine(command):
            command = await command
        state.update(command.update or {})
        if command.goto == "execute_tools":
            node = "execute_tools"
        elif command.goto == "agent":
            node = "agent"
        else:
            return state


def test_commute_claim_without_evidence_is_replaced_by_unverified_text():
    state = _state()
    state["tool_artifacts"] = []
    safe = validate_commute_response(
        "Oak Row meets the 30 minute commute limit.", state,
    )
    assert "could not be verified" in safe.lower()
    assert "meets" not in safe.lower()


def test_each_listing_needs_its_own_commute_evidence():
    raw = {
        "status": "found",
        "recommendations": [
            {"address": "Oak Row, Synthetic Town", "price": "£900/month"},
            {"address": "Pine Walk, Synthetic Town", "price": "£950/month"},
        ],
        "search_criteria": {
            "area": "synthetic-town",
            "commute_destination": "Synthetic Campus",
            "max_travel_time": 30,
        },
    }
    artifacts = [agent_loop._artifact(
        1, "calculate_commute",
        {"success": True, "from_address": "Oak Row, Synthetic Town",
         "to_address": "Synthetic Campus", "duration_minutes": 20},
        success=True,
    )]
    result = build_candidate_state(raw["recommendations"], raw["search_criteria"], artifacts)
    assert [item["address"] for item in result["eligible"]] == ["Oak Row, Synthetic Town"]
    assert [item["address"] for item in result["unknown"]] == ["Pine Walk, Synthetic Town"]


def test_excluded_listings_never_enter_eligible_or_meets_all():
    candidates = [{"address": "Oak Row, Synthetic Town", "price": "£1400/month"}]
    result = build_candidate_state(
        candidates, {"max_budget": 1000}, [],
    )
    assert not result["eligible"]
    assert result["excluded"][0]["reason"] == "over_budget"


def test_unknown_and_failed_commute_evidence_are_distinct():
    candidates = [
        {"address": "Oak Row, Synthetic Town", "price": "£900/month"},
        {"address": "Pine Walk, Synthetic Town", "price": "£900/month"},
    ]
    criteria = {"commute_destination": "Synthetic Campus", "max_travel_time": 30}
    artifacts = [
        agent_loop._artifact(1, "calculate_commute", None, success=False,
                              error="provider failure"),
        agent_loop._artifact(1, "calculate_commute", None, success=False,
                              timed_out=True, abandoned=True, outcome_unknown=True,
                              error="timed out"),
    ]
    result = build_candidate_state(candidates, criteria, artifacts)
    assert {item["commute_evidence_status"] for item in result["unknown"]} == {
        "failed", "timeout"
    }


def test_explicit_memory_request_forces_remember_even_if_model_omits_tool(monkeypatch):
    provider = Provider([Spec("remember", side_effect="write", retry_safe=False)],
                        {"remember": Result(True, {"stored": "budget £900/month"})})
    nodes = build_fc_nodes(
        provider,
        agent_llm=Chat(AIMessage(content="I have remembered that."), AIMessage(content="Done.")),
    )
    state = _state("Please remember that my budget is £900/month")
    asyncio.run(_drive(nodes, state))
    assert [name for name, _params in provider.calls] == ["remember"]
    assert state["memory_write_contract"]["success"] is True


def test_memory_failure_cannot_be_reported_as_saved(monkeypatch):
    provider = Provider([Spec("remember", side_effect="write", retry_safe=False)],
                        {"remember": Result(False, None, "disk unavailable")})
    nodes = build_fc_nodes(
        provider,
        agent_llm=Chat(AIMessage(content="It has been saved."), AIMessage(content="Done.")),
    )
    state = _state("Please remember that my budget is £900/month")
    asyncio.run(_drive(nodes, state))
    assert state["memory_write_contract"]["success"] is False
    assert "could not save" in state["final_response"].lower()


def test_ordinary_statement_does_not_force_memory_write():
    provider = Provider([Spec("remember", side_effect="write", retry_safe=False)])
    nodes = build_fc_nodes(provider, agent_llm=Chat(AIMessage(content="Noted.")))
    state = _state("My budget is £900/month")
    asyncio.run(_drive(nodes, state))
    assert provider.calls == []


def test_empty_retrieval_cannot_license_market_amount_but_user_amount_is_allowed():
    from uk_rent_agent.agent.critic import unsupported_external_numbers

    assert unsupported_external_numbers(
        "Typical local rents are £1200-£1500/month.",
        [{"tool": "web_search", "success": False, "raw_data": None}],
        "What is the market rent here?",
    )
    assert not unsupported_external_numbers(
        "Your stated budget is £900/month.", [], "My budget is £900/month",
    )


def test_weekly_to_monthly_uses_52_over_12_and_penny_rounding():
    assert round(monthly_from_weekly(350), 2) == pytest.approx(1516.67)
    assert round(monthly_from_weekly(1), 2) == pytest.approx(4.33)
    assert round(monthly_from_weekly(0.01), 2) == pytest.approx(0.04)


def test_multiple_commute_calls_keep_failure_and_timeout_per_listing():
    candidates = [
        {"address": "Oak Row, Synthetic Town", "price": "£900/month"},
        {"address": "Pine Walk, Synthetic Town", "price": "£900/month"},
    ]
    criteria = {"commute_destination": "Synthetic Campus", "max_travel_time": 30}
    artifacts = [
        agent_loop._artifact(1, "calculate_commute",
                              {"success": True, "from_address": candidates[0]["address"],
                               "to_address": "Synthetic Campus", "duration_minutes": 25},
                              success=True),
        agent_loop._artifact(1, "calculate_commute", None, success=False,
                              timed_out=True, outcome_unknown=True, error="timed out"),
    ]
    result = build_candidate_state(candidates, criteria, artifacts)
    assert len(result["eligible"]) == 1
    assert result["unknown"][0]["commute_evidence_status"] == "timeout"
