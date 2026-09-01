"""Audit K8: the identifiers that end up in a checkpoint.

Two separate defects lived here:

* ``safe_root_task_id`` seeded itself with ``turn``, so one request's eight super-steps
  minted eight different "root" task ids and nothing downstream could join them back to
  the turn.
* the persisted ``params_digest`` copies were the manager's raw, unsalted, truncated hash
  of the tool arguments — an offline oracle over the SQLite checkpoint (the audit's PoC
  recovered a full street address from one).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from langchain_core.messages import AIMessage

from core.agent_loop import build_fc_nodes
from core.manager_v1 import _node_task_id
from core.specialist_runtime import (
    ReadCall,
    prepare_specialist_batch,
    safe_turn_root_id,
    validation_fanout_task_id,
)
from tests.test_manager_v1_specialist_dispatch import _registry, _schema, _state, _tc, _tool
from tests.test_specialist_failure_radius import _Spec


def _prepare(*, turn, root_task_id="turn:request-1", run_id="run-1", args=None):
    return prepare_specialist_batch(
        [
            ReadCall(
                index=0,
                tool_name="get_weather",
                args=args or {"city": "London"},
                params_digest="0123456789abcdef",
                tool_call_id="c1",
            )
        ],
        live_specs=[_Spec("get_weather")],
        root_task_id=root_task_id,
        run_id=run_id,
        turn=turn,
    )


# ── (a) one root per turn ─────────────────────────────────────────────────────


def test_root_task_id_is_stable_across_super_steps_but_plan_id_is_not():
    batches = [_prepare(turn=turn) for turn in range(8)]

    assert len({batch.plan.root_task_id for batch in batches}) == 1
    assert len({batch.plan.plan_id for batch in batches}) == 8
    assert len({batch.call(0).artifact_id for batch in batches}) == 8


def _execute_bare(nodes, state):
    """Drive execute_tools with NO ambient agent context, i.e. the direct-graph shape."""
    command = asyncio.run(nodes["execute_tools"](state))
    state.update(command.update or {})
    return state


def test_three_super_steps_of_one_request_share_one_root_task_id(tmp_path):
    """Audit missing-test #3, through the real node."""

    async def weather(**kwargs):
        return {"weather": kwargs.get("city")}

    registry = _registry(tmp_path, [_tool("get_weather", weather, parameters=_schema("city"))])
    nodes = build_fc_nodes(registry, specialist_dispatch=True)
    state = _state([_tc("get_weather", {"city": "London"}, "c1")])

    for step, city in enumerate(("London", "Leeds", "Bristol")):
        state["loop_turn"] = step
        state["messages"] = list(state.get("messages") or []) + [
            AIMessage(
                content="",
                tool_calls=[_tc("get_weather", {"city": city}, f"c{step}")],
            )
        ]
        state = _execute_bare(nodes, state)

    plans = state["manager_task_plans"]
    assert len(plans) == 3
    assert len({plan["root_task_id"] for plan in plans}) == 1
    assert len({plan["plan_id"] for plan in plans}) == 3
    # And the artifacts agree with the plan they belong to.
    roots = {
        artifact["parent_task_id"]
        for artifact in state["tool_artifacts"]
        if "parent_task_id" in artifact
    }
    assert roots == {plans[0]["root_task_id"]}


# ── (b) a client-supplied request id never lands in an id verbatim ────────────


@pytest.mark.parametrize(
    "request_id",
    ["req-1", "REQ.1:2/3", "a" * 96],
)
def test_well_formed_request_ids_keep_their_readable_root(request_id):
    assert safe_turn_root_id(request_id) == f"turn:{request_id}"


@pytest.mark.parametrize(
    "request_id",
    ["", "   ", "-leading-dash", "with space", "line\nbreak", "b" * 200, "../../etc"],
)
def test_hostile_request_ids_are_hashed_or_dropped(request_id):
    root = safe_turn_root_id(request_id)
    if not request_id.strip():
        assert root is None
        return
    assert root.startswith("turn:h:")
    assert len(root) == len("turn:h:") + 16
    assert request_id.strip() not in root


def test_node_task_id_never_interpolates_a_hostile_request_id():
    task_id = _node_task_id("execute_tools", {"request_id": "x y\nz"}, None)
    assert task_id.startswith("turn:h:")
    assert "\n" not in task_id and " " not in task_id
    assert task_id.endswith("/node:execute_tools:0")
    # An explicit parent still wins, unchanged.
    assert _node_task_id("agent", {"request_id": "r"}, "turn:root") == (
        "turn:root/node:agent:0"
    )


def test_validation_fanout_task_id_is_deterministic_and_opaque():
    first = validation_fanout_task_id(plan_id="plan:abc", root_task_id="turn:request-1")
    again = validation_fanout_task_id(plan_id="plan:abc", root_task_id="turn:request-1")
    other = validation_fanout_task_id(plan_id="plan:def", root_task_id="turn:request-1")

    assert first == again != other
    assert first.startswith("task:")
    assert "request-1" not in first


# ── (c) the persisted params digest is keyed per run ──────────────────────────


def test_checkpointed_params_digest_is_a_per_run_keyed_value():
    raw = "0123456789abcdef"
    batch = _prepare(turn=0, run_id="run-1")
    other_run = _prepare(turn=0, run_id="run-2")

    persisted = batch.plan.tasks[0].inputs["calls"][0]["params_digest"]
    key = hashlib.sha256(b"rentcompass-specialist:" + b"run-1").digest()
    expected = hmac.new(key, raw.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    assert persisted == expected
    assert persisted != raw
    # Per-run key: the same arguments in another run are not linkable by this value.
    assert other_run.plan.tasks[0].inputs["calls"][0]["params_digest"] != persisted
    # Nothing else in the checkpoint carries the raw digest either.
    assert raw not in batch.plan.model_dump_json()
    assert raw not in json.dumps(batch.plan.model_dump(mode="json"), sort_keys=True)
    assert raw not in batch.call(0).artifact_id
    assert raw not in batch.call(0).tool_call_id
    # ...while the in-memory call keeps the raw value so it can still bind the ledger.
    assert batch.call(0).params_digest == raw


def test_ledger_binding_still_works_with_the_masked_checkpoint_digest(tmp_path):
    """The artifact writer stamps the RAW digest; results must still be derived."""

    async def weather(**kwargs):
        return {"weather": kwargs.get("city")}

    registry = _registry(tmp_path, [_tool("get_weather", weather, parameters=_schema("city"))])
    state = _execute_bare(
        build_fc_nodes(registry, specialist_dispatch=True),
        _state([_tc("get_weather", {"city": "London"}, "c1")]),
    )

    artifact = state["tool_artifacts"][0]
    plan = state["manager_task_plans"][0]
    assert artifact["params_digest"] != plan["tasks"][0]["inputs"]["calls"][0]["params_digest"]
    assert state["specialist_results"][0]["status"] == "succeeded"
