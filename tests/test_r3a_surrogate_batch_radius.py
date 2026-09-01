"""Review3 R1-H1: a malformed model-authored argument must cost ONE call, not the turn.

``_params_digest`` was hardened with ``encode("utf-8", "surrogatepass")``, but the SAME
model-authored dict then hit a strict UTF-8 encode in ``ToolInvocation.create`` — which
``agent_loop.build_fc_nodes._run`` called ABOVE its own ``try:``. On fc_loop with
specialists OFF (i.e. production) that moved the crash from "before anything ran" to
"after the whole batch was dispatched": ``UnicodeEncodeError`` escaped the node, so
``execute_tools`` returned no ``Command`` and every sibling that had already completed
lost its artifact and its ToolMessage.

Two independent guarantees are pinned here:
  1. a lone surrogate is now encodable, so the call itself runs (parity with the digest);
  2. even when identity construction DOES fail, it fails inside ``_run``'s handler, so the
     failure degrades to that call's own ``ToolResult(False, ...)`` and its siblings land.
"""

from __future__ import annotations

import core.agent_loop as agent_loop
from core.langgraph_agent import _params_digest
from tests.test_manager_v1_specialist_dispatch import (
    _execute,
    _registry,
    _schema,
    _state,
    _tc,
    _tool,
)
from uk_rent_agent.agent.contracts import ToolInvocation

SURROGATE = "Lon\ud800don"


def _batch(tmp_path, *, specialist_dispatch=False):
    ran = []

    def worker(name):
        async def run(**kwargs):
            ran.append(name)
            return {"source": name, "value": kwargs.get("value")}

        return run

    registry = _registry(
        tmp_path,
        [
            _tool("check_safety", worker("check_safety"), parameters=_schema("value")),
            _tool("get_weather", worker("get_weather"), parameters=_schema("value")),
        ],
    )
    nodes = agent_loop.build_fc_nodes(
        registry, specialist_dispatch=specialist_dispatch
    )
    return nodes, ran


def _artifacts_by_tool(state):
    return {item["tool"]: item for item in state["tool_artifacts"]}


def test_tool_invocation_identity_survives_a_lone_surrogate():
    """The idempotency key and the params digest agree on what is encodable."""
    params = {"value": SURROGATE}
    inv = ToolInvocation.create(
        run_id="run", node_id="execute_tools", tool="get_weather", params=params
    )
    assert len(inv.idempotency_key) == 64
    # Deterministic, and distinct from the same tool with a well-formed argument.
    assert inv.idempotency_key == ToolInvocation.create(
        run_id="run", node_id="execute_tools", tool="get_weather", params=params
    ).idempotency_key
    assert inv.idempotency_key != ToolInvocation.create(
        run_id="run", node_id="execute_tools", tool="get_weather",
        params={"value": "London"},
    ).idempotency_key
    # The other identity of the same call no longer disagrees about encodability.
    assert len(_params_digest("get_weather", params)) == 16


def test_a_surrogate_bearing_call_does_not_kill_its_siblings(tmp_path):
    """fc_loop, specialists OFF — the production shape the crash was reachable from."""
    nodes, ran = _batch(tmp_path)
    state = _execute(
        nodes,
        _state([
            _tc("check_safety", {"value": "Camden"}, "c1"),
            _tc("get_weather", {"value": SURROGATE}, "c2"),
        ]),
    )

    artifacts = _artifacts_by_tool(state)
    # The well-formed sibling's RESULT is in the ledger — the whole point: it used to run
    # to completion and then be thrown away with the node.
    assert artifacts["check_safety"]["success"] is True
    assert artifacts["check_safety"]["raw_data"]["value"] == "Camden"
    # And the malformed one is no longer fatal: it runs like any other call.
    assert artifacts["get_weather"]["success"] is True
    assert ran == ["check_safety", "get_weather"] or ran == [
        "get_weather", "check_safety"
    ]
    # Every call still produced a ToolMessage, so the model sees a complete batch.
    assert {message.tool_call_id for message in state["messages"]
            if getattr(message, "tool_call_id", None)} == {"c1", "c2"}


def test_identity_construction_failure_is_contained_to_its_own_call(
    tmp_path, monkeypatch
):
    """The blast radius is the call, not the node — whatever makes identity fail.

    ``surrogatepass`` closes the one input we know about; moving the construction inside
    ``_run``'s handler is what makes the NEXT such input a one-call failure instead of a
    lost turn.
    """
    nodes, ran = _batch(tmp_path)
    original = ToolInvocation.create

    def exploding_create(*, run_id, node_id, tool, params=None, version="1"):
        if tool == "get_weather":
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
        return original(
            run_id=run_id, node_id=node_id, tool=tool, params=params, version=version
        )

    monkeypatch.setattr(agent_loop.ToolInvocation, "create", exploding_create)

    state = _execute(
        nodes,
        _state([
            _tc("check_safety", {"value": "Camden"}, "c1"),
            _tc("get_weather", {"value": "London"}, "c2"),
        ]),
    )

    artifacts = _artifacts_by_tool(state)
    assert artifacts["check_safety"]["success"] is True
    assert artifacts["check_safety"]["raw_data"]["value"] == "Camden"
    # The defective call degraded to the ordinary per-call failure and never ran.
    assert artifacts["get_weather"]["success"] is False
    assert artifacts["get_weather"]["error"] == "Tool execution failed"
    assert ran == ["check_safety"]
    assert {message.tool_call_id for message in state["messages"]
            if getattr(message, "tool_call_id", None)} == {"c1", "c2"}
