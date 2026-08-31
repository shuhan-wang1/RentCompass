"""Audit F5: the post-search commute fan-out is the untrusted-input path.

``candidate_validation.collect_commute_evidence`` takes ``from_address`` from a SCRAPED
listing — the only place in the system where untrusted third-party text becomes a tool
argument — and ``_OffloadedValidationProvider.execute_tool`` handed it straight to
``provider.execute_tool``.  The one call chain that most needed the capability boundary was
the one chain that bypassed it entirely.

These calls are deliberately not ``TaskPlan`` members: they are discovered from a search
RESULT, after the plan was sealed, so they carry a derived ``mobility`` task id and produce
no ``SpecialistResult`` — their evidence stays in the ordinary calculate_commute artifacts.
"""

from __future__ import annotations

import pytest

from core.agent_loop import build_fc_nodes
from core.specialist_runtime import validation_fanout_task_id
from tests.test_manager_v1_specialist_dispatch import (
    _execute,
    _registry,
    _schema,
    _state,
    _tc,
    _tool,
)
from uk_rent_agent.observability import current_agent_context


SCRAPED_ADDRESS = "Flat 4, 21 Hartland Road, London NW1 8DB"


def _search_payload():
    return {
        "status": "found",
        "search_criteria": {
            "max_commute_time": 30,
            "commute_destination": "Synthetic Campus",
        },
        "recommendations": [{"address": SCRAPED_ADDRESS, "price": 900}],
    }


def _build(tmp_path, commute_func, *, specialist_dispatch):
    from core.tools.search_properties import search_properties_tool

    async def search_properties(**_kwargs):
        return _search_payload()

    registry = _registry(
        tmp_path,
        [
            _tool("search_properties", search_properties,
                  parameters=search_properties_tool.parameters),
            _tool("calculate_commute", commute_func,
                  parameters=_schema("from_address", "to_address", "mode")),
        ],
    )
    direct_calls = []
    original_execute_tool = registry.execute_tool

    async def spying_execute_tool(name, **params):
        direct_calls.append(name)
        return await original_execute_tool(name, **params)

    registry.execute_tool = spying_execute_tool
    nodes = build_fc_nodes(registry, specialist_dispatch=specialist_dispatch)
    state = _execute(
        nodes,
        _state([_tc("search_properties", {"area": "Synthetic"}, "c1")],
               message="Find a home within 30 minutes of Synthetic Campus"),
    )
    return state, direct_calls


def test_commute_fanout_runs_under_the_mobility_capability_boundary(tmp_path):
    seen = []

    async def commute(**kwargs):
        seen.append((current_agent_context(), kwargs))
        return {"duration_minutes": 21}

    state, direct_calls = _build(tmp_path, commute, specialist_dispatch=True)

    assert len(seen) == 1
    context, kwargs = seen[0]
    assert context["agent_role"] == "mobility"
    assert context["task_id"].startswith("task:")
    # Parented by the PLAN's root, the same id every planned task carries, so one turn is
    # one joinable tree. The raw `turn:request-1` forked it in two (review R1/R5).
    plan_root = state["manager_task_plans"][0]["root_task_id"]
    assert plan_root.startswith("manager:")
    assert context["parent_task_id"] == plan_root
    # The untrusted argument reached the tool intact — the boundary constrains AUTHORITY,
    # it does not rewrite the manager's data.
    assert kwargs["from_address"] == SCRAPED_ADDRESS
    assert kwargs["to_address"] == "Synthetic Campus"

    # Nothing went through the unrestricted registry dispatch.
    assert direct_calls == []

    evidence = state["commute_evidence"]
    assert [item["evidence_status"] for item in evidence] == ["success"]
    assert evidence[0]["from_address"] == SCRAPED_ADDRESS

    # The fan-out is off-plan: it never appears as a specialist task or result.
    plan = state["manager_task_plans"][0]
    assert [task["role"] for task in plan["tasks"]] == ["listings"]
    assert {item["role"] for item in state["specialist_results"]} == {"listings"}
    expected_task_id = validation_fanout_task_id(
        plan_id=plan["plan_id"], root_task_id=plan["root_task_id"])
    assert context["task_id"] == expected_task_id

    # AUDITABLE: the ledger entry itself says the fan-out went through the boundary, so a
    # reader of a checkpoint does not have to trust the code (review R1/R5).
    fanout_artifact = next(
        item for item in state["tool_artifacts"]
        if item["tool"] == "calculate_commute"
    )
    assert fanout_artifact["agent_role"] == "mobility"
    assert fanout_artifact["plan_id"] == plan["plan_id"]
    assert fanout_artifact["task_id"] == expected_task_id
    assert fanout_artifact["parent_task_id"] == plan["root_task_id"]
    # Off-plan: no artifact_id is minted, so it stays out of build_specialist_results and
    # out of TaskPlan.tasks.
    assert "artifact_id" not in fanout_artifact
    assert all(task["role"] != "mobility" for task in plan["tasks"])


def test_fc_path_keeps_the_historical_unrestricted_fanout(tmp_path):
    seen = []

    async def commute(**kwargs):
        seen.append(current_agent_context())
        return {"duration_minutes": 21}

    state, direct_calls = _build(tmp_path, commute, specialist_dispatch=False)

    assert len(seen) == 1
    # Still the manager's own context — no specialist scope is entered on the fc path.
    assert seen[0]["agent_role"] == "manager"
    # Both the search and the fan-out go through unrestricted registry dispatch here.
    assert direct_calls == ["search_properties", "calculate_commute"]
    assert [item["evidence_status"] for item in state["commute_evidence"]] == ["success"]


def test_commute_fanout_fails_closed_when_the_capability_cannot_be_resolved(tmp_path):
    """A boundary that cannot validate must deny, never fall back to direct dispatch."""
    ran = []

    async def commute(**kwargs):
        ran.append(kwargs)
        return {"duration_minutes": 21}

    from core.tools.search_properties import search_properties_tool

    async def search_properties(**_kwargs):
        return _search_payload()

    registry = _registry(
        tmp_path,
        [
            _tool("search_properties", search_properties,
                  parameters=search_properties_tool.parameters),
            _tool("calculate_commute", commute,
                  parameters=_schema("from_address", "to_address", "mode")),
        ],
    )
    real_resolver = registry.resolve_specialist_capability

    def resolver(name, expected_spec_digest):
        if name == "calculate_commute":
            from core.specialist_runtime import SpecialistDispatchError

            raise SpecialistDispatchError("specialist_capability_metadata_drift")
        return real_resolver(name, expected_spec_digest)

    registry.resolve_specialist_capability = resolver
    state = _execute(
        build_fc_nodes(registry, specialist_dispatch=True),
        _state([_tc("search_properties", {"area": "Synthetic"}, "c1")],
               message="Find a home within 30 minutes of Synthetic Campus"),
    )

    assert ran == []
    evidence = state["commute_evidence"]
    assert [item["evidence_status"] for item in evidence] == ["failed"]


def test_off_role_tool_cannot_be_dispatched_through_the_validation_scope(tmp_path):
    """The scope is bound to `mobility`; it is not a general-purpose escape hatch."""
    import asyncio

    import core.agent_loop as agent_loop

    ran = []

    async def weather(**kwargs):
        ran.append(kwargs)
        return {"weather": "sunny"}

    registry = _registry(tmp_path, [_tool("get_weather", weather, parameters=_schema("city"))])
    scope = agent_loop._SpecialistCapabilityScope(
        registry,
        role="mobility",
        plan_id="plan:x",
        root_task_id="turn:request-1",
        task_id="task:abc",
    )
    result = asyncio.run(scope.execute("get_weather", {"city": "London"}))

    assert ran == []
    assert result.success is False
    assert "denied" in (result.error or "")


def test_validation_scope_still_forwards_the_one_injected_deadline(tmp_path):
    """`_deadline_monotonic` is the manager's post-seal override on BOTH paths."""
    import asyncio

    import core.agent_loop as agent_loop

    ran = []

    async def commute(**kwargs):
        ran.append(kwargs)
        return {"duration_minutes": 21}

    registry = _registry(
        tmp_path,
        [_tool("calculate_commute", commute,
               parameters=_schema("from_address", "to_address", "mode"))],
    )
    scope = agent_loop._SpecialistCapabilityScope(
        registry,
        role="mobility",
        plan_id="plan:x",
        root_task_id="manager:root",
        task_id="task:abc",
    )
    result = asyncio.run(scope.execute(
        "calculate_commute",
        {"from_address": "A", "to_address": "B", "_deadline_monotonic": 123.0},
    ))

    assert result.success is True
    assert agent_loop._SPECIALIST_INJECTED_KEYS == frozenset({"_deadline_monotonic"})
    # Forwarded to a callable that accepts it, and never part of the sealed identity.
    assert ran == [
        {"from_address": "A", "to_address": "B", "_deadline_monotonic": 123.0}
    ]


@pytest.mark.parametrize("bad_args", [{"from_address": ("a", "b")}, {"_hidden": 1}])
def test_validation_scope_seals_its_arguments(tmp_path, bad_args):
    import asyncio

    import core.agent_loop as agent_loop

    ran = []

    async def commute(**kwargs):
        ran.append(kwargs)
        return {"duration_minutes": 21}

    registry = _registry(
        tmp_path,
        [_tool("calculate_commute", commute,
               parameters=_schema("from_address", "to_address", "mode"))],
    )
    scope = agent_loop._SpecialistCapabilityScope(
        registry,
        role="mobility",
        plan_id="plan:x",
        root_task_id="turn:request-1",
        task_id="task:abc",
    )
    result = asyncio.run(scope.execute("calculate_commute", dict(bad_args)))

    # Both are refused before the tool is reached: a non-JSON-native argument, and a
    # reserved `_`-prefixed key. `_deadline_monotonic` is the ONE injected key the planned
    # path re-attaches after sealing, and the fan-out — whose arguments come from scraped
    # text — must not be the broader of the two paths (review R1/R6).
    assert ran == []
    assert result.success is False
