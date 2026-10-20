"""R4-01: ask_user after a tool round no longer crashes with UnboundLocalError.

r06 ac5da17d: search_properties -> remember (blocked) -> ask_user; ``current_message`` was
bound only on the first agent entry and the terminal ask_user branch read it.
"""
from __future__ import annotations

import asyncio
import json
import logging

from langchain_core.messages import AIMessage

from tests.test_fc_loop import FakeChat, FakeProvider, FakeResult, FakeSpec, _base_state, _drive, _tc
from core.agent_loop import build_fc_nodes


def test_ask_user_on_a_later_agent_entry_is_terminal_not_a_crash():
    specs = [FakeSpec("search_properties"), FakeSpec("ask_user", terminal=True)]
    provider = FakeProvider(specs, {"search_properties": FakeResult(True, {
        "status": "found", "recommendations": [], "search_criteria": {"area": "Camden"}})})
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"user_query": "flat in Camden"}, "c1")]),
        AIMessage(content="", tool_calls=[_tc("ask_user", {
            "question": "Which budget should I use?", "clarification_kind": "other",
            "missing_fields": ["max_budget"], "missing_optional_fields": []}, "c2")]),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = asyncio.run(_drive(nodes, _base_state()))
    assert state["response_type"] == "clarification"
    assert state["final_response"] == "Which budget should I use?"


def test_execution_failure_log_names_the_frame_not_the_content():
    import app as app_module
    try:
        def boom():
            raise UnboundLocalError("cannot access local variable 'current_message'")
        boom()
    except UnboundLocalError as exc:
        location = app_module._exception_location(exc)
    assert location.startswith("test_overnight_r04_ask_user_reentry.py:") and location.endswith(":boom")
    from uk_rent_agent.observability import JsonFormatter
    record = logging.LogRecord("app", logging.ERROR, __file__, 1, "agent.turn.execution_failed", (), None)
    record.error_type = "UnboundLocalError"
    record.error_location = location
    data = json.loads(JsonFormatter().format(record))
    assert data["error_location"] == location
