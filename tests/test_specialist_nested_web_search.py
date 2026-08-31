"""Review R1/R1: `web_search.sub_queries` must stay inside the capability boundary.

`web_search` is the only granted tool of the `area_evidence` role that is itself an
orchestrator: its `sub_queries` argument is written by the model and used to dispatch OTHER
tools.  It did that through the module-global registry, so one `area_evidence` grant drove
`calculate_commute` (mobility) and `get_property_details` (listings) with model-authored
arguments — no grant, no digest, no pinning, no sealing, no artifact, and an `agent_role`
label that actively mis-attributed the calls to `area_evidence`.
"""

from __future__ import annotations

import asyncio

import pytest

import core.tools.web_search as ws
from core.agent_loop import build_fc_nodes
from core.tools.web_search import web_search_func, web_search_tool
from tests.test_manager_v1_specialist_dispatch import (
    _execute,
    _registry,
    _schema,
    _state,
    _tc,
    _tool,
)
from uk_rent_agent.observability import (
    agent_execution_context,
    current_agent_context,
)


def _web_search_tool_fixture():
    return _tool(
        "web_search",
        web_search_func,
        parameters=web_search_tool.parameters,
    )


def _sub_query(tool, params):
    return {"tool": tool, "params": params}


def _build(tmp_path, monkeypatch, extra_tools, *, specialist_dispatch=True):
    observed = []

    def watcher(name):
        async def run(**kwargs):
            observed.append((name, dict(current_agent_context()), kwargs))
            return {"tool": name}

        return run

    tools = [_web_search_tool_fixture()]
    for name, parameters in extra_tools:
        tools.append(_tool(name, watcher(name), parameters=parameters))
    registry = _registry(tmp_path, tools)

    direct_calls = []
    original_execute_tool = registry.execute_tool

    async def spying_execute_tool(name, **params):
        direct_calls.append(name)
        return await original_execute_tool(name, **params)

    registry.execute_tool = spying_execute_tool

    resolved = []
    original_resolver = registry.resolve_specialist_capability

    def spying_resolver(name, expected_spec_digest):
        resolved.append(name)
        return original_resolver(name, expected_spec_digest)

    registry.resolve_specialist_capability = spying_resolver
    monkeypatch.setattr(ws, "_tool_registry", registry)
    nodes = build_fc_nodes(registry, specialist_dispatch=specialist_dispatch)
    return nodes, observed, direct_calls, resolved


def _tool_payload(state, tool_name):
    return next(
        item for item in state["tool_artifacts"] if item["tool"] == tool_name
    )


def test_cross_role_nested_calls_are_denied_under_a_specialist_grant(
    tmp_path, monkeypatch
):
    """One `area_evidence` grant must not drive mobility and listings tools."""
    nodes, observed, direct_calls, _resolved = _build(
        tmp_path,
        monkeypatch,
        [
            ("calculate_commute", _schema("from_address", "to_address", "mode")),
            ("get_property_details", _schema("property_id")),
        ],
    )
    state = _execute(
        nodes,
        _state([
            _tc(
                "web_search",
                {
                    "query": "camden safety",
                    "sub_queries": [
                        _sub_query(
                            "calculate_commute",
                            {"from_address": "ATTACKER", "to_address": "B",
                             "mode": "transit"},
                        ),
                        _sub_query("get_property_details", {"property_id": "P1"}),
                    ],
                },
                "c1",
            )
        ]),
    )

    # Neither off-role tool ran, by either dispatch path.
    assert observed == []
    assert direct_calls == []
    plan = state["manager_task_plans"][0]
    assert [grant["name"] for task in plan["tasks"] for grant in task["tools"]] == [
        "web_search"
    ]
    # The denial is VISIBLE in the tool's own payload rather than a silent skip.
    artifact = _tool_payload(state, "web_search")
    assert artifact["raw_data"]["success"] is False
    assert artifact["raw_data"]["error_code"] == "nested_tool_role_forbidden"


def test_same_role_nested_call_runs_through_the_capability_path(tmp_path, monkeypatch):
    """A nested tool the grant DOES cover still works — via resolve+execute, not by name."""
    nodes, observed, direct_calls, resolved = _build(
        tmp_path, monkeypatch, [("get_weather", _schema("city"))]
    )
    state = _execute(
        nodes,
        _state([
            _tc(
                "web_search",
                {
                    "query": "camden weather",
                    "sub_queries": [_sub_query("get_weather", {"city": "London"})],
                },
                "c1",
            )
        ]),
    )

    assert [name for name, _ctx, _kwargs in observed] == ["get_weather"]
    _name, context, kwargs = observed[0]
    # Still inside the SAME specialist context the outer web_search was granted.
    assert context["agent_role"] == "area_evidence"
    assert kwargs["city"] == "London"
    # The nested call went through the pinned capability API, never the name lookup.
    assert resolved == ["web_search", "get_weather"]
    assert direct_calls == []
    artifact = _tool_payload(state, "web_search")
    assert "get_weather_1" in artifact["raw_data"]["detailed_data"]


def test_fc_path_keeps_the_historical_unrestricted_nested_dispatch(
    tmp_path, monkeypatch
):
    """No specialist grant is in force on the fc path: behaviour is unchanged."""
    nodes, observed, direct_calls, resolved = _build(
        tmp_path,
        monkeypatch,
        [("calculate_commute", _schema("from_address", "to_address", "mode"))],
        specialist_dispatch=False,
    )
    state = _execute(
        nodes,
        _state([
            _tc(
                "web_search",
                {
                    "query": "camden commute",
                    "sub_queries": [
                        _sub_query(
                            "calculate_commute",
                            {"from_address": "A", "to_address": "B", "mode": "transit"},
                        )
                    ],
                },
                "c1",
            )
        ]),
    )

    assert [name for name, _ctx, _kwargs in observed] == ["calculate_commute"]
    assert direct_calls == ["web_search", "calculate_commute"]
    assert resolved == []
    artifact = _tool_payload(state, "web_search")
    assert artifact["raw_data"]["success"] is True


@pytest.mark.parametrize(
    ("role", "expected_code"),
    [
        # A role this module does not know is not a licence to dispatch anything.
        ("critic", "nested_tool_role_forbidden"),
        # In-role by catalog, but the arguments are not sealable: still refused.
        ("area_evidence", "nested_specialist_dispatch_denied"),
    ],
)
def test_unknown_role_and_unsealable_arguments_are_refused(
    tmp_path, monkeypatch, role, expected_code
):
    registry = _registry(
        tmp_path,
        [
            _web_search_tool_fixture(),
            _tool("get_weather", _unreachable, parameters=_schema("city")),
        ],
    )
    monkeypatch.setattr(ws, "_tool_registry", registry)
    params = {"city": "London"}
    if expected_code == "nested_specialist_dispatch_denied":
        # A reserved (`_`-prefixed) key is a harness injection channel; a model-authored
        # nested call may never open it.
        params = {"city": "London", "_deadline_monotonic": 1.0}

    async def run():
        with agent_execution_context(
            agent_role=role, task_id="task:x", parent_task_id="manager:root"
        ):
            return await web_search_func(
                query="camden", sub_queries=[_sub_query("get_weather", params)]
            )

    result = asyncio.run(run())

    assert result["success"] is False
    assert result["error_code"] == expected_code


async def _unreachable(**_kwargs):
    raise AssertionError("a denied nested call must never reach the tool")


def test_role_allowlist_can_only_narrow_the_static_nested_allowlist():
    from uk_rent_agent.agent.specialist_contracts import SPECIALIST_TOOL_ALLOWLISTS

    for role in SPECIALIST_TOOL_ALLOWLISTS:
        allowed = ws._nested_allowlist_for_role(role)
        assert allowed <= (ws._ALLOWED_NESTED_TOOLS | {"web_search_only"})
        # `web_search_only` is web_search's own capability, so it rides on the same grant.
        assert ("web_search_only" in allowed) is (
            "web_search" in SPECIALIST_TOOL_ALLOWLISTS[role]
        )
    assert ws._nested_allowlist_for_role("no_such_role") == frozenset()
    # The manager keeps the whole static allowlist: `None` means "no grant in force".
    assert ws._current_specialist_role() is None
