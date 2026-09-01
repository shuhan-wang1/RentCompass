"""Focused contract tests for the opt-in manager_v1 compatibility shell."""

from __future__ import annotations

import asyncio

from uk_rent_agent.agent.architecture import (
    FC_RUNTIME_ARCHES,
    MANAGER_V1_ARCH,
    SUPPORTED_AGENT_ARCHES,
    uses_fc_runtime,
)
from uk_rent_agent.observability import agent_execution_context, current_agent_context

from core import manager_v1, turn_observations


class _DummyRegistry:
    tools = {}

    def get(self, name):
        return None


def test_architecture_groups_keep_manager_opt_in_and_fc_compatible():
    assert SUPPORTED_AGENT_ARCHES == {"legacy", "fc_loop", MANAGER_V1_ARCH}
    assert FC_RUNTIME_ARCHES == {"fc_loop", MANAGER_V1_ARCH}
    assert uses_fc_runtime(" manager_V1 ") is True
    assert uses_fc_runtime("fc_loop") is True
    assert uses_fc_runtime("legacy") is False


def test_manager_builder_delegates_once_with_no_extra_graph_stage(monkeypatch):
    sentinel = object()
    calls = []

    def fake_build(tool_registry, **kwargs):
        calls.append((tool_registry, kwargs))
        return sentinel

    monkeypatch.setattr(manager_v1, "build_fc_graph", fake_build)
    registry = object()
    extract = object()
    critic = object()
    injected_llm = object()

    result = manager_v1.build_manager_v1_graph(
        registry,
        extract_preferences_node=extract,
        critic_node=critic,
        checkpointer="checkpoint",
        store="store",
        enable_hitl=True,
        hydrate_prefs_node="hydrate",
        persist_prefs_node="persist",
        agent_llm=injected_llm,
    )

    assert result is sentinel
    assert len(calls) == 1
    delegated_registry, delegated = calls[0]
    assert delegated_registry is registry
    assert delegated["extract_preferences_node"] is extract
    assert delegated["critic_node"] is critic
    assert delegated["agent_llm"] is injected_llm
    assert delegated["checkpointer"] == "checkpoint"
    assert delegated["store"] == "store"
    assert delegated["enable_hitl"] is True
    assert delegated["specialist_dispatch"] is False
    assert callable(delegated["instrument"])


def test_manager_builder_threads_enabled_specialists_to_fc_executor(monkeypatch):
    delegated = {}

    def fake_build(_tool_registry, **kwargs):
        delegated.update(kwargs)
        return object()

    class _CapabilityRegistry:
        def resolve_specialist_capability(self, *_args, **_kwargs):
            return object()

        async def execute_resolved_specialist_capability(self, *_args, **_kwargs):
            return object()

    monkeypatch.setattr(manager_v1, "build_fc_graph", fake_build)

    manager_v1.build_manager_v1_graph(
        _CapabilityRegistry(),
        extract_preferences_node=object(),
        critic_node=object(),
        specialists_enabled=True,
    )

    assert delegated["specialist_dispatch"] is True


def test_manager_builder_rejects_enabled_specialists_without_capability_api():
    try:
        manager_v1.build_manager_v1_graph(
            object(),
            extract_preferences_node=object(),
            critic_node=object(),
            specialists_enabled=True,
        )
    except RuntimeError as exc:
        assert "trusted in-process ToolRegistry" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract under test
        raise AssertionError("untrusted provider was accepted for specialist dispatch")


def test_manager_and_fc_compile_the_same_topology(monkeypatch):
    from core.langgraph_agent import build_agent_graph

    monkeypatch.setenv("AGENT_ARCH", "fc_loop")
    fc = build_agent_graph(_DummyRegistry())

    monkeypatch.setenv("AGENT_ARCH", MANAGER_V1_ARCH)
    manager = build_agent_graph(_DummyRegistry())

    assert set(manager.nodes) == set(fc.nodes)
    assert {"guard", "agent", "execute_tools", "critic", "format_output_fc"} <= set(
        manager.nodes
    )
    assert "decide_tool" not in manager.nodes


def test_sync_node_context_is_child_of_root_manager_task():
    captured = []

    def node(state):
        captured.append(current_agent_context())
        return state["value"]

    wrapped = manager_v1._manager_node_instrument(None)("guard", node)
    with agent_execution_context(
        agent_role="manager", task_id="turn:req-1", parent_task_id=None
    ):
        assert wrapped({"value": "ok", "loop_turn": 0}) == "ok"
        assert current_agent_context() == {
            "agent_role": "manager",
            "task_id": "turn:req-1",
        }

    assert captured == [{
        "agent_role": "manager",
        "task_id": "turn:req-1/node:guard:0",
        "parent_task_id": "turn:req-1",
    }]
    assert current_agent_context() == {}


def test_async_node_context_survives_await_and_tracks_loop_iteration():
    captured = []

    async def node(state):
        await asyncio.sleep(0)
        captured.append(current_agent_context())
        return "done"

    wrapped = manager_v1._manager_node_instrument(None)("agent", node)

    async def run():
        with agent_execution_context(
            agent_role="manager", task_id="turn:req-2", parent_task_id=None
        ):
            return await wrapped({"loop_turn": 3})

    assert asyncio.run(run()) == "done"
    assert captured == [{
        "agent_role": "manager",
        "task_id": "turn:req-2/node:agent:3",
        "parent_task_id": "turn:req-2",
    }]


def test_manager_write_audit_alias_is_instrumented_on_zero_write_turn():
    turn_observations.begin_turn()
    try:
        snapshot = turn_observations.write_audit_snapshot(MANAGER_V1_ARCH)
    finally:
        turn_observations.end_turn()

    assert snapshot["write_audit_status"] == "instrumented"
    assert snapshot["denied_write_count"] == 0
    assert snapshot["tainted_write_executed_count"] == 0
    assert snapshot["forbidden_write_executed_count"] == 0
    assert snapshot["write_audit"] == []
