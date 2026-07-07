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
    )
