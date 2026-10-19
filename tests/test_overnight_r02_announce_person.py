"""A recital of the USER's search is not an announced lookup (R2-02).

Live rid 3458558b ("Do you store anything about me? What exactly have you kept about me so
far?"): the answer said "Right now you are looking for a studio in Hackney with a £1,500
budget." The subject-less gerund pattern ``(searching|checking|looking) for \\w+`` matched it,
the loop logged ``fc_loop.announced_tool_without_execution`` and nudged, the retry opened with
"I did not announce a lookup in my last message…", and the turn shipped with "I was not able
to run that lookup this round, so nothing above is a fresh result."

Only the assistant can announce its own next action: a first-person subject ("I'm searching
for…", "我正在查询") or none at all ("Searching for listings now…") announces; "you are looking
for…", "they are checking…", "你正在查找…" describe someone else.
"""
from __future__ import annotations

import asyncio

import pytest

from core import agent_loop
from core.agent_loop import (
    ANNOUNCED_TOOL_STUB_MARKER_EN,
    announces_unexecuted_tool,
)

LIVE_ANSWER = (
    "Here is what I currently have about you.\n\n"
    "**Search criteria for this conversation**\n"
    "Right now you are looking for a studio in Hackney with a £1,500 budget. That lives in "
    "this conversation only.\n\n"
    "**Long-term memory**\n"
    "Nothing is saved yet. I only store something when you ask me to remember it.")


@pytest.mark.parametrize("text", [
    LIVE_ANSWER,
    "Right now you are looking for a studio in Hackney with a £1,500 budget.",
    "You're searching for a 1-bed near UCL, and that is all I have.",
    "So far you've been looking for flats in Camden.",
    "They are checking for damp before you move in, the agent says.",
    "The landlord is looking for a tenant from November.",
    "你正在查找哈克尼的单间，预算 £1,500。",
    "目前您正在搜索伦敦的一居室。",
])
def test_r2_02_someone_elses_search_is_not_an_announcement(text):
    assert announces_unexecuted_tool(text, []) is False


@pytest.mark.parametrize("text", [
    "I'm searching for one-bedroom listings in Shoreditch now.",
    "I am now looking for cheaper options in Hackney.",
    "Searching for listings in Hackney now…",
    "OK, searching for flats near UCL.",
    "Looking that up for you.",
    "我正在查询天气。",
    "正在为您搜索房源。",
    "Let me search for one-bedroom listings in Shoreditch.",
    "我来查一下。",
])
def test_r2_02_the_assistants_own_announcements_are_still_detected(text):
    assert announces_unexecuted_tool(text, []) is True


def test_r2_02_the_recital_ships_without_nudge_or_stub_marker():
    """Through the agent node: one model call, no corrective nudge, no stub footer."""
    import uuid

    from langchain_core.messages import AIMessage

    from tests.test_agent_contract_guards import Chat, Provider, Spec, _drive
    from tests.test_agent_contract_guards import _state as _shared_state

    provider = Provider([Spec("search_properties")])
    chat = Chat(AIMessage(content=LIVE_ANSWER))
    nodes = agent_loop.build_fc_nodes(provider, agent_llm=chat)
    state = _shared_state("Do you store anything about me? What exactly have you kept about me "
                          "so far?")
    tag = uuid.uuid4().hex[:12]
    state["session_id"] = f"r2-02-session-{tag}"
    state["user_id"] = f"r2-02-user-{tag}"
    asyncio.run(_drive(nodes, state))

    assert provider.calls == []
    assert ANNOUNCED_TOOL_STUB_MARKER_EN not in state["final_response"]
    assert "you are looking for a studio in Hackney" in state["final_response"]
    assert not any(agent_loop._ANNOUNCED_TOOL_NUDGE_MARKER in (getattr(m, "additional_kwargs", None) or {})
                   for m in state.get("messages") or [])
