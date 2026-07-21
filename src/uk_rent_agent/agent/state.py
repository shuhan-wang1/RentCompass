from __future__ import annotations

import uuid
from typing import Annotated, Any, Dict, List, Optional, TypedDict


def bounded_add(left: list, right: list, *, limit: int = 100) -> list:
    """Reducer guard: checkpoints must not grow without bound across turns."""
    merged = list(left or []) + list(right or [])
    return merged[-limit:]


class AgentState(TypedDict, total=False):
    user_query: str
    user_id: str
    session_id: str
    extracted_context: Dict[str, Any]
    user_preferences: Dict[str, List[str]]
    accumulated_search_criteria: Dict[str, Any]
    tool_decision: Dict[str, Any]
    tool_observation: Optional[str]
    tool_raw_data: Optional[Any]
    search_results: Annotated[list, bounded_add]
    final_response: str
    response_type: str
    tool_data: Dict[str, Any]
    run_id: str
    request_id: str
    context_tainted: bool
    critic_attempts: int
    verdict: Dict[str, Any]
    memory_context: str
    plan: list
    # Bounded agent loop (decide -> tool -> reflect -> decide ...). loop_turn counts the
    # loopable-tool executions completed THIS turn; observations records each one so the
    # final synthesis can reason over every tool's output. Both are PER-TURN: they are
    # plain (non-reducer) channels so the create_initial_state(loop_turn=0, observations=[])
    # input cleanly RESETS them at the start of every turn even under the checkpointer.
    # (A bounded_add reducer — as search_results uses — would instead MERGE across turns,
    # because a []-input is a no-op for that reducer; reflect and gather_wave are the only,
    # strictly-sequential writers, so last-write-wins is safe and each returns the full
    # per-turn list.)
    loop_turn: int
    observations: list
    # Native function-calling loop (AGENT_ARCH=fc_loop; app/core/agent_loop.py). messages is
    # the langchain_core BaseMessage list the `agent` node binds tools onto and the
    # `execute_tools` node appends ToolMessages to; tool_artifacts is the raw-data ledger
    # [{turn, tool, raw_data, params_digest}] format_output_fc aggregates over (design §2.8b).
    # Both are PER-TURN plain (non-reducer) channels — exactly like loop_turn/observations —
    # so the create_initial_state(messages=[], tool_artifacts=[]) input cleanly RESETS them at
    # the start of every turn under the checkpointer, and each node returns the FULL updated
    # list (last-write-wins; agent and execute_tools alternate as sole sequential writers).
    messages: list
    tool_artifacts: list
    # Canary telemetry (2026-07-20): set True by the fc_loop turn-wide soft-wrap path
    # (agent_loop._wrap_up) when the WHOLE-turn wall-clock crossed the soft-wrap edge and the
    # answer was generated tools-disabled / from gathered artifacts. A PLAIN per-turn channel
    # reset to False by create_initial_state; _wrap_up is the sole writer. app/app.py surfaces
    # it on the per-turn canary.turn record. Purely observational — never gates routing.
    soft_wrapped: bool
    # Cumulative wall-clock (seconds) the fc_loop execute_tools node has spent running tool
    # batches THIS user turn. A PLAIN per-turn channel (reset by create_initial_state) that
    # accumulates across the turn's batches so FC_TURN_TOOL_BUDGET_S can be enforced turn-wide
    # (execute_tools is the sole writer; last-write-wins is safe). See app/core/agent_loop.py.
    turn_tool_budget_used_s: float
    # Monotonic timestamp (time.monotonic()) captured at the fc_loop guard/entry node marking
    # the TURN START, so the agent + execute_tools nodes can measure whole-turn elapsed (LLM +
    # tools) and enforce the turn-wide soft wrap (FC_TURN_SOFT_WRAP_S) + search deadline. A PLAIN
    # per-turn channel reset by create_initial_state (0.0 = not yet captured; guard is the sole
    # writer). Process-local: never compare across a cross-process checkpoint resume.
    turn_start_monotonic: float
    # Multi-intent execution plan (build_execution_plan -> dispatch_tasks -> task_worker x N
    # -> gather_wave). task_plan is the resolved task list [{id,index,tool,params,depends_on}];
    # plan_origin is "multi_search" (degenerate single-intent web fan-out, ends at
    # generate_response) or "plan" (multi-intent, ends at reflect as ONE loop step);
    # plan_notes carries synthetic observations for tasks dropped at build time (missing
    # info) so generate_response can surface them inline; plan_just_completed tells reflect a
    # WHOLE plan just finished (its per-task observations + loop_turn are already recorded, so
    # reflect must NOT append/increment again). All are PLAIN per-turn channels reset by
    # create_initial_state, mirroring loop_turn/observations. task_results uses the SAME
    # bounded_add reducer + run_id filtering as search_results so wave workers merge safely.
    task_plan: list
    plan_origin: str
    plan_notes: list
    plan_just_completed: bool
    task_results: Annotated[list, bounded_add]


def create_initial_state(
    user_query: str,
    extracted_context: dict | None = None,
    user_preferences: dict | None = None,
    accumulated_search_criteria: dict | None = None,
    user_id: str = "default",
    session_id: str = "default",
    request_id: str | None = None,
) -> AgentState:
    return AgentState(
        user_query=user_query,
        user_id=user_id,
        session_id=session_id,
        extracted_context=extracted_context or {},
        user_preferences=user_preferences or {
            "hard_preferences": [], "soft_preferences": [],
            "excluded_areas": [], "required_amenities": [], "safety_concerns": [],
        },
        accumulated_search_criteria=accumulated_search_criteria or {
            "destination": None, "max_budget": None, "max_travel_time": None,
            "property_features": [], "soft_preferences": [], "amenities_of_interest": [],
        },
        tool_decision={},
        tool_observation=None,
        tool_raw_data=None,
        search_results=[],
        final_response="",
        response_type="answer",
        tool_data={},
        run_id=uuid.uuid4().hex,
        request_id=request_id or uuid.uuid4().hex,
        context_tainted=False,
        critic_attempts=0,
        verdict={},
        memory_context="",
        plan=[],
        loop_turn=0,
        observations=[],
        messages=[],
        tool_artifacts=[],
        soft_wrapped=False,
        turn_tool_budget_used_s=0.0,
        turn_start_monotonic=0.0,
        task_plan=[],
        plan_origin="",
        plan_notes=[],
        plan_just_completed=False,
        task_results=[],
    )
