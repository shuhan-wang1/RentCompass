"""Small synthetic regressions for the v2 agent contracts.

These tests deliberately exercise product contracts rather than benchmark cases.  They use
made-up listing identities, prices, and commute results so the tests cannot pass by matching
held-out fixture text.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage

import core.agent_loop as agent_loop
from core.agent_loop import build_fc_nodes
from core.candidate_validation import (
    collect_commute_evidence,
    validate_candidates,
    render_candidate_status,
)
from core.tenancy_reference import monthly_from_weekly
from uk_rent_agent.agent.critic import (
    enforce_no_evidence_numeric_contract,
)


@dataclass
class _Spec:
    name: str
    description: str = "test"
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    side_effect: str = "none"
    retry_safe: bool = True
    version: str = "1"
    terminal: bool = False


@dataclass
class _Result:
    success: bool
    data: dict | None = None
    error: str | None = None


class _Provider:
    def __init__(self, results=None, *, delay=0):
        self._results = results or {}
        self.delay = delay
        self.calls = []

    def list_specs(self):
        return [_Spec("remember", side_effect="write", retry_safe=False), _Spec("web_search")]

    def get(self, name):
        return next((s for s in self.list_specs() if s.name == name), None)

    async def execute_tool(self, name, **params):
        self.calls.append((name, params))
        if self.delay:
            await asyncio.sleep(self.delay)
        result = self._results.get(name)
        if callable(result):
            return result(**params)
        return result or _Result(True, {"stored": params.get("content")})


def _candidate(address, price):
    return {"address": address, "price": price, "url": f"https://list.test/{address}"}


def test_explicit_commute_limit_without_evidence_cannot_be_claimed():
    result = validate_candidates(
        [_candidate("1 Cedar Lane", "£900/month")],
        {"max_budget": 1000, "max_travel_time": 30, "commute_destination": "North Campus"},
        commute_evidence=[],
    )

    assert result["unknown"][0]["status"] == "unknown"
    answer = render_candidate_status(result, language="en")
    assert "cannot verify" in answer.lower()
    assert "meets" not in answer.lower()


def test_partial_multi_listing_commute_evidence_is_bound_per_listing():
    rows = [_candidate("2 Cedar Lane", "£900/month"), _candidate("3 Cedar Lane", "£920/month")]
    evidence = [{
        "candidate_key": "url:https://list.test/2 cedar lane",
        "from_address": "2 Cedar Lane",
        "to_address": "North Campus",
        "success": True,
        "duration_minutes": 18,
        "raw_data": {"success": True, "duration_minutes": 18},
    }]
    result = validate_candidates(
        rows,
        {"max_budget": 1000, "max_travel_time": 30, "commute_destination": "North Campus"},
        commute_evidence=evidence,
    )

    assert [r["candidate"]["address"] for r in result["eligible"]] == ["2 Cedar Lane"]
    assert [r["candidate"]["address"] for r in result["unknown"]] == ["3 Cedar Lane"]
    assert "3 Cedar Lane" in render_candidate_status(result, language="en")
    assert "18 min" in render_candidate_status(result, language="en")


def test_excluded_listing_never_enters_meets_all_or_recommended():
    result = validate_candidates(
        [_candidate("4 Cedar Lane", "£1,250/month"), _candidate("5 Cedar Lane", "£950/month")],
        {"max_budget": 1000},
        commute_evidence=[],
    )
    assert [r["candidate"]["address"] for r in result["eligible"]] == ["5 Cedar Lane"]
    assert [r["candidate"]["address"] for r in result["excluded"]] == ["4 Cedar Lane"]


def test_failed_and_missing_commute_evidence_are_distinct_unknown_reasons():
    rows = [_candidate("6 Cedar Lane", "£900/month"), _candidate("7 Cedar Lane", "£900/month")]
    result = validate_candidates(
        rows,
        {"max_travel_time": 30, "commute_destination": "North Campus"},
        commute_evidence=[{
            "candidate_key": "url:https://list.test/6 cedar lane",
            "from_address": "6 Cedar Lane",
            "to_address": "North Campus",
            "success": False,
            "evidence_status": "failed",
            "error": "provider error",
        }],
    )
    by_address = {x["candidate"]["address"]: x for x in result["unknown"]}
    assert by_address["6 Cedar Lane"]["evidence_status"] == "failed"
    assert by_address["7 Cedar Lane"]["evidence_status"] == "missing"


def test_commute_collector_calls_each_listing_and_preserves_failure_timeout():
    class Provider:
        def __init__(self):
            self.calls = []

        async def execute_tool(self, name, **params):
            self.calls.append(params["from_address"])
            if params["from_address"] == "9 Cedar Lane":
                raise RuntimeError("temporary failure")
            if params["from_address"] == "10 Cedar Lane":
                await asyncio.sleep(0.05)
            return _Result(True, {"success": True, "duration_minutes": 22,
                                  "from_address": params["from_address"],
                                  "to_address": params["to_address"]})

    provider = Provider()
    evidence = asyncio.run(collect_commute_evidence(
        provider, [_candidate("8 Cedar Lane", "£900"), _candidate("9 Cedar Lane", "£900"),
                   _candidate("10 Cedar Lane", "£900")],
        "North Campus", timeout_s=0.01,
    ))
    assert provider.calls == ["8 Cedar Lane", "9 Cedar Lane", "10 Cedar Lane"]
    assert {x["evidence_status"] for x in evidence} == {"success", "failed", "timeout"}


def test_commute_collector_enforces_one_shared_deadline_and_concurrency_cap():
    class Provider:
        def __init__(self):
            self.calls = []
            self.active = 0
            self.peak_active = 0

        async def execute_tool(self, name, **params):
            self.calls.append(params["from_address"])
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            try:
                await asyncio.sleep(1)
                return _Result(True, {"duration_minutes": 20})
            finally:
                self.active -= 1

    provider = Provider()
    rows = [_candidate(f"{index} Ash Lane", "£900") for index in range(6)]
    started = time.monotonic()
    evidence = asyncio.run(collect_commute_evidence(
        provider, rows, "North Campus", timeout_s=1,
        deadline_monotonic=time.monotonic() + 0.08, concurrency=2,
    ))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert provider.peak_active <= 2
    assert len(provider.calls) == 2
    assert [row["evidence_status"] for row in evidence[:2]] == ["timeout", "timeout"]
    assert all(row["evidence_status"] in {"budget_exhausted", "skipped"}
               for row in evidence[2:])


def test_commute_timeout_does_not_start_a_second_background_wave():
    class Provider:
        def __init__(self):
            self.calls = 0

        async def execute_tool(self, name, **params):
            self.calls += 1
            await asyncio.sleep(0.2)
            return _Result(True, {"duration_minutes": 20})

    provider = Provider()
    evidence = asyncio.run(collect_commute_evidence(
        provider,
        [_candidate(f"{index} Elm Lane", "£900") for index in range(6)],
        "North Campus", timeout_s=0.02,
        deadline_monotonic=time.monotonic() + 1.0, concurrency=2,
    ))

    assert provider.calls == 2


def test_commute_collector_marks_candidates_beyond_fanout_cap_without_dispatch():
    class Provider:
        def __init__(self):
            self.calls = []

        async def execute_tool(self, name, **params):
            self.calls.append(params["from_address"])
            return _Result(True, {"duration_minutes": 20})

    provider = Provider()
    rows = [_candidate(f"{index} Birch Lane", "£900") for index in range(7)]
    evidence = asyncio.run(collect_commute_evidence(
        provider, rows, "South Campus", max_candidates=3, concurrency=2,
    ))

    assert len(provider.calls) == 3
    assert len(evidence) == len(rows)
    assert all(row["evidence_status"] == "success" for row in evidence[:3])
    assert all(row["evidence_status"] == "skipped" for row in evidence[3:])


def _memory_state(message):
    return {
        "user_query": message,
        "extracted_context": {"current_message": message, "reply_language": "en"},
        "accumulated_search_criteria": {},
        "user_preferences": {"hard_preferences": [], "soft_preferences": [],
                              "excluded_areas": [], "required_amenities": [],
                              "safety_concerns": []},
        "user_id": "synthetic-user",
        "session_id": "synthetic-session",
        "run_id": "synthetic-run",
        "loop_turn": 0,
        "messages": [],
        "tool_artifacts": [],
        "context_tainted": False,
        "final_response": "",
        "response_type": "answer",
    }


def test_explicit_memory_request_forces_remember_and_success_is_claimable():
    provider = _Provider({"remember": _Result(True, {"success": True, "stored": "budget"})})
    nodes = build_fc_nodes(provider, agent_llm=type("Chat", (), {
        "bind_tools": lambda self, tools: self,
        "ainvoke": lambda self, messages: asyncio.sleep(0, result=AIMessage(content="I saved it")),
    })())
    state = _memory_state("Please remember that my rent budget is £900 per month")
    cmd = asyncio.run(nodes["agent"](state))
    state.update(cmd.update or {})
    assert cmd.goto == "execute_tools"
    asyncio.run(nodes["execute_tools"](state))


def test_memory_failure_is_not_rendered_as_success_and_plain_statement_does_not_write():
    provider = _Provider({"remember": _Result(False, None, "disk unavailable")})
    nodes = build_fc_nodes(provider, agent_llm=type("Chat", (), {
        "bind_tools": lambda self, tools: self,
        "ainvoke": lambda self, messages: asyncio.sleep(0, result=AIMessage(content="I saved it")),
    })())
    state = _memory_state("Please remember that my rent budget is £900 per month")
    cmd = asyncio.run(nodes["agent"](state))
    state.update(cmd.update or {})
    command = asyncio.run(nodes["execute_tools"](state))
    state.update(command.update or {})
    state["final_response"] = "I have remembered this."
    rendered = nodes["format_output_fc"](state)
    assert "failed" in rendered["final_response"].lower()

    plain = _memory_state("I remember my old budget was £900")
    plain_cmd = asyncio.run(nodes["agent"](plain))
    assert not any(name == "remember" for name, _ in provider.calls[1:])
    assert plain_cmd.goto == "critic"


def test_empty_retrieval_cannot_supply_market_amount_but_user_amount_can_be_repeated():
    assert "£1,234" not in enforce_no_evidence_numeric_contract(
        "Typical local rent is £1,234/month.", retrieved_evidence={"success": True, "results": []},
        user_message="What is the usual rent here?", reply_language="en")
    assert "£900" in enforce_no_evidence_numeric_contract(
        "You said £900/month.", retrieved_evidence={"success": True, "results": []},
        user_message="My budget is £900/month.", reply_language="en")


def test_weekly_to_monthly_uses_52_over_12_and_money_rounding_is_stable():
    assert monthly_from_weekly(100) == pytest.approx(433.3333333333)
    assert round(monthly_from_weekly(100), 2) == 433.33
    assert round(monthly_from_weekly(350), 2) == 1516.67
