from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


class AgentState(TypedDict):
    user_query: str
    user_id: str
    session_id: str
    extracted_context: Dict[str, Any]
    user_preferences: Dict[str, List[str]]
    accumulated_search_criteria: Dict[str, Any]
    tool_decision: Dict[str, Any]
    tool_observation: Optional[str]
    tool_raw_data: Optional[Any]
    search_results: Annotated[list, operator.add]
    final_response: str
    response_type: str
    tool_data: Dict[str, Any]


def create_initial_state(
    user_query: str,
    extracted_context: dict | None = None,
    user_preferences: dict | None = None,
    accumulated_search_criteria: dict | None = None,
    user_id: str = "default",
    session_id: str = "default",
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
    )
