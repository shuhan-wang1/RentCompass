"""A tool call with malformed JSON arguments must never produce a blank answer (2026-10-19 B-05).

DeepSeek occasionally returns a tool call whose ``arguments`` are not valid JSON. LangChain puts
it in ``AIMessage.invalid_tool_calls``; nothing read that field, so the turn was treated as a
plain-text answer with empty content: HTTP 200 and a blank message (~1 in 75 tool turns live).
"""
from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

import core.agent_loop as agent_loop
from tests.test_agent_contract_guards import Chat, Provider, Result, Spec, _drive, _state

_BROKEN = AIMessage(content="", invalid_tool_calls=[{
    "name": "search_properties", "args": '{"area": "Camden", "max_budget": 2000',
    "id": "call_bad", "error": "Expecting ',' delimiter", "type": "invalid_tool_call"}])


def test_a_malformed_call_gets_one_corrective_iteration():
    chat = Chat(_BROKEN, AIMessage(content="Here are flats in Camden under £2,000."))
    nodes = agent_loop.build_fc_nodes(Provider([Spec("search_properties")]), agent_llm=chat)
    state = _state("Find a 1-bed in Camden under £2000")
    command = asyncio.run(nodes["agent"](state))
    assert command.goto == "agent"
    nudge = command.update["messages"][-1]
    assert isinstance(nudge, HumanMessage) and "not valid JSON" in nudge.content
    assert "search_properties" in nudge.content
    # The malformed call is never echoed back to the provider.
    assert not any(getattr(m, "invalid_tool_calls", None) for m in command.update["messages"])


def test_a_second_malformed_reply_ships_a_non_blank_answer():
    chat = Chat(_BROKEN, _BROKEN)
    nodes = agent_loop.build_fc_nodes(Provider([Spec("search_properties")]), agent_llm=chat)
    state = asyncio.run(_drive(nodes, _state("Find a 1-bed in Camden under £2000")))
    final = state.get("final_response") or ""
    assert final.strip()
    assert "malformed" in final


def test_a_valid_call_alongside_an_invalid_one_still_runs():
    mixed = AIMessage(content="", tool_calls=[{"name": "search_properties",
                                               "args": {"area": "Camden"}, "id": "ok",
                                               "type": "tool_call"}],
                      invalid_tool_calls=_BROKEN.invalid_tool_calls)
    provider = Provider([Spec("search_properties")],
                        {"search_properties": Result(True, {"status": "found",
                                                            "recommendations": []})})
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=Chat(mixed))
    command = asyncio.run(nodes["agent"](_state("Find a 1-bed in Camden")))
    assert command.goto == "execute_tools"
