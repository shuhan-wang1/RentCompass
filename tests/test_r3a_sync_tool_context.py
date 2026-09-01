"""Review3 R1-M2: the specialist boundary must not depend on how a tool is DECLARED.

``Tool._execute_with_callable`` runs a sync callable on a bare pool thread via
``run_in_executor(None, ...)``, which does not propagate contextvars. A sync tool
therefore observed ``current_agent_context() == {}``, and
``web_search._current_specialist_role()`` maps "no role" to MANAGER authority —
unrestricted nested dispatch. The whole cross-role containment held only because
``web_search_func`` happens to be ``async def``; nothing enforced or tested it.
"""

from __future__ import annotations

import asyncio

from core.tool_system import Tool, ToolRegistry
from core.tools import web_search as ws
from tests.test_manager_v1_specialist_dispatch import _schema
from uk_rent_agent.observability import agent_execution_context, current_agent_context
from uk_rent_agent.tools.idempotency import IdempotencyStore

_SPECIALIST_CTX = {
    "agent_role": "area_evidence",
    "task_id": "plan:abc/area_evidence",
    "parent_task_id": "manager:root",
}


def _registry_with(tmp_path, name, func):
    tool = Tool(
        name=name,
        description=f"fixture {name}",
        func=func,
        parameters=_schema("city"),
        max_retries=1,
        retry_on_error=False,
        version="1",
        side_effect="none",
        retry_safe=True,
    )
    registry = ToolRegistry(IdempotencyStore(tmp_path / f"{name}.sqlite3"))
    registry.register(tool)
    return tool, registry


def _run_pinned(registry, tool, name, **kwargs):
    """Dispatch through the pinned capability API, inside a specialist context."""
    from core.specialist_runtime import tool_spec_security_digest

    digest = tool_spec_security_digest(tool.to_spec())

    async def run():
        with agent_execution_context(**_SPECIALIST_CTX):
            capability = registry.resolve_specialist_capability(name, digest)
            return await registry.execute_resolved_specialist_capability(
                capability, args=dict(kwargs), expected_spec_digest=digest
            )

    return asyncio.run(run())


def test_a_sync_pinned_callable_observes_the_specialist_context(tmp_path):
    seen = {}

    def sync_tool(**kwargs):
        seen.update(current_agent_context())
        return {"city": kwargs.get("city")}

    tool, registry = _registry_with(tmp_path, "get_weather", sync_tool)
    result = _run_pinned(registry, tool, "get_weather", city="London")

    assert result.success is True
    assert seen == _SPECIALIST_CTX


def test_sync_and_async_callables_see_the_same_authority(tmp_path):
    """The invariant, stated as an equality: declaration style is not authority."""
    sync_seen = {}
    async_seen = {}

    def sync_tool(**_kwargs):
        sync_seen.update(current_agent_context())
        return {"ok": True}

    async def async_tool(**_kwargs):
        async_seen.update(current_agent_context())
        return {"ok": True}

    sync_t, sync_registry = _registry_with(tmp_path, "get_weather", sync_tool)
    async_t, async_registry = _registry_with(tmp_path, "check_safety", async_tool)

    _run_pinned(sync_registry, sync_t, "get_weather", city="London")
    _run_pinned(async_registry, async_t, "check_safety", city="London")

    assert sync_seen == async_seen == _SPECIALIST_CTX


def test_a_sync_tool_cannot_read_itself_back_as_manager_authority(tmp_path):
    """The exact escalation this closes: `None` role == unrestricted nested dispatch."""
    roles = []

    def sync_tool(**_kwargs):
        roles.append(ws._current_specialist_role())
        return {"ok": True}

    tool, registry = _registry_with(tmp_path, "get_weather", sync_tool)
    _run_pinned(registry, tool, "get_weather", city="London")

    # `None` here would mean "no grant in force" -> the historic unrestricted path.
    assert roles == ["area_evidence"]


def test_the_plain_execute_path_propagates_the_context_too(tmp_path):
    """Not only the pinned path: ``Tool.execute`` shares the same sync branch."""
    seen = {}

    def sync_tool(**_kwargs):
        seen.update(current_agent_context())
        return {"ok": True}

    tool, _registry = _registry_with(tmp_path, "get_weather", sync_tool)

    async def run():
        with agent_execution_context(**_SPECIALIST_CTX):
            return await tool.execute(city="London")

    assert asyncio.run(run()).success is True
    assert seen == _SPECIALIST_CTX
