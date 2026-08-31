"""Experimental manager architecture with an opt-in Phase-2 specialist runtime.

``manager_v1`` delegates graph topology and loop mechanics to the proven ``fc_loop``
runtime.  Its separate rollout flag may add deterministic read-only specialist scopes
around manager-approved tool calls; it never adds an LLM call or exposes manager state,
memory, checkpoints, or user interaction to a specialist.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from uk_rent_agent.agent.architecture import MANAGER_V1_ARCH
from uk_rent_agent.observability import agent_execution_context, current_agent_context

from core.agent_loop import build_fc_graph


# The delegated FC executor records the same write decisions for manager_v1.  Register
# the alias at import time so a zero-write turn is correctly observed as an instrumented
# zero instead of an uninstrumented/null security signal.
try:
    from core.turn_observations import register_write_auditor

    register_write_auditor(MANAGER_V1_ARCH)
except Exception:  # pragma: no cover - telemetry must never prevent graph construction
    pass


def _state_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    candidate = args[0] if args else kwargs.get("state")
    return candidate if isinstance(candidate, dict) else {}


def _node_task_id(
    node_name: str,
    state: dict[str, Any],
    parent_task_id: str | None,
) -> str:
    """Build a stable per-node task id within one root turn.

    ``loop_turn`` distinguishes repeated agent/executor super-steps without adding
    mutable global counters.  The request/run fallback keeps direct graph tests and
    offline evaluation attributable even when no HTTP root context is installed.
    """
    root = parent_task_id
    if not root:
        request_id = state.get("request_id") or state.get("run_id")
        root = f"turn:{request_id}" if request_id else "manager"
    loop_turn = state.get("loop_turn")
    iteration = loop_turn if isinstance(loop_turn, int) and not isinstance(loop_turn, bool) else 0
    return f"{root}/node:{node_name}:{iteration}"


def _manager_node_instrument(
    upstream: Callable[[str, Callable[..., Any]], Callable[..., Any]] | None,
) -> Callable[[str, Callable[..., Any]], Callable[..., Any]]:
    """Compose optional eval instrumentation with manager execution context."""

    def instrument(node_name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        observed = upstream(node_name, fn) if upstream is not None else fn

        if inspect.iscoroutinefunction(observed):

            @functools.wraps(observed)
            async def async_node(*args: Any, **kwargs: Any) -> Any:
                outer = current_agent_context()
                parent_task_id = outer.get("task_id")
                task_id = _node_task_id(
                    node_name, _state_from_call(args, kwargs), parent_task_id
                )
                with agent_execution_context(
                    agent_role="manager",
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                ):
                    return await observed(*args, **kwargs)

            return async_node

        @functools.wraps(observed)
        def sync_node(*args: Any, **kwargs: Any) -> Any:
            outer = current_agent_context()
            parent_task_id = outer.get("task_id")
            task_id = _node_task_id(
                node_name, _state_from_call(args, kwargs), parent_task_id
            )
            with agent_execution_context(
                agent_role="manager",
                task_id=task_id,
                parent_task_id=parent_task_id,
            ):
                return observed(*args, **kwargs)

        return sync_node

    return instrument


def build_manager_v1_graph(
    tool_registry,
    *,
    extract_preferences_node,
    critic_node,
    checkpointer=None,
    store=None,
    enable_hitl=False,
    hydrate_prefs_node=None,
    persist_prefs_node=None,
    instrument=None,
    agent_llm=None,
    specialists_enabled=False,
):
    """Build manager_v1 with the exact FC topology and model-call budget."""
    if specialists_enabled:
        required = (
            "resolve_specialist_capability",
            "execute_resolved_specialist_capability",
        )
        missing = [name for name in required if not callable(getattr(tool_registry, name, None))]
        if missing:
            raise RuntimeError(
                "manager_v1 specialists require a trusted in-process ToolRegistry "
                f"capability API (missing: {', '.join(missing)})"
            )
    return build_fc_graph(
        tool_registry,
        extract_preferences_node=extract_preferences_node,
        critic_node=critic_node,
        checkpointer=checkpointer,
        store=store,
        enable_hitl=enable_hitl,
        hydrate_prefs_node=hydrate_prefs_node,
        persist_prefs_node=persist_prefs_node,
        instrument=_manager_node_instrument(instrument),
        agent_llm=agent_llm,
        specialist_dispatch=bool(specialists_enabled),
    )
