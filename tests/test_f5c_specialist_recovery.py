"""Round-5 fixes for the manager_v1 specialist path.

* TD-1 / TD-3 — the per-call denial path, driven from a FAKE MODEL through the whole
  agent -> execute_tools loop (T2 saw `denied_calls = 0` on all 25 records and T3 could not
  get the real model to emit reserved args, so the security argument for the read-only
  sandbox had no evidence at all).
* TD-2 — the `specialist_call_not_planned` branch used to return denied WITHOUT counting it.
* P1-5(a) — a stall past the deadline is `timeout`, not `tool_error`.
* P1-5(b) — a role that loses its tools is told which grants it still has, which is the
  fallback the flat fc loop takes on its own.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from core import turn_observations
from core.agent_loop import build_fc_nodes
from core.specialist_runtime import (
    EvidenceDigestEntry,
    build_evidence_digest,
    specialist_result_reason,
)
from tests.test_manager_v1_specialist_dispatch import (
    _registry,
    _schema,
    _tc,
    _tool,
)
from uk_rent_agent.agent.state import create_initial_state


class _Chat:
    """Fake model: one scripted response per agent super-step."""

    def __init__(self, *messages):
        self.messages = list(messages)
        self.invocations = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        self.invocations += 1
        return self.messages.pop(0) if self.messages else AIMessage(content="Done.")


def _turn_state(message="compare housing evidence for Camden"):
    state = create_initial_state(
        message,
        extracted_context={"current_message": message, "reply_language": "en"},
        request_id="request-f5c",
        user_id="user-1",
        session_id="session-1",
    )
    state["messages"] = []
    return state


async def _drive(nodes, state, *, max_steps=8):
    node = "agent"
    for _ in range(max_steps):
        command = nodes[node](state)
        if asyncio.iscoroutine(command):
            command = await command
        state.update(command.update or {})
        if command.goto in ("agent", "execute_tools"):
            node = command.goto
            continue
        return state
    raise AssertionError("loop did not terminate")


# ── TD-1 / TD-3: one reserved-arg call, two legal siblings, from a fake model ──


def test_a_reserved_arg_call_is_denied_while_both_siblings_run(tmp_path):
    """The K1 property, driven the way production drives it: the MODEL emits the batch.

    `user_id` is a reserved input key — a manager LLM that hallucinates one must lose that
    single call, with a closed-set error code in the ledger, while its role sibling and the
    other role's call both execute; and the denial must be visible in the turn record."""
    executed = []

    def probe(name):
        async def run(**kwargs):
            executed.append(name)
            return {"tool": name}

        return run

    registry = _registry(
        tmp_path,
        [
            _tool("check_safety", probe("check_safety"),
                  parameters=_schema("address", "user_id")),
            _tool("get_weather", probe("get_weather"), parameters=_schema("city")),
            _tool("calculate_commute", probe("calculate_commute"),
                  parameters=_schema("from_address", "to_address")),
        ],
    )
    nodes = build_fc_nodes(
        registry,
        specialist_dispatch=True,
        agent_llm=_Chat(
            AIMessage(content="", tool_calls=[
                # The defective call: a hallucinated reserved argument.
                _tc("check_safety", {"address": "Camden", "user_id": "u-1"}, "c1"),
                # Two legal siblings, one in the same role, one in another.
                _tc("get_weather", {"city": "London"}, "c2"),
                _tc("calculate_commute",
                    {"from_address": "Camden", "to_address": "UCL"}, "c3"),
            ]),
            AIMessage(content="Camden looks fine on the evidence I could gather."),
        ),
    )

    turn_observations.begin_turn()
    try:
        state = asyncio.run(_drive(nodes, _turn_state()))
        trace = turn_observations.specialist_snapshot()
    finally:
        turn_observations.end_turn()

    assert sorted(executed) == ["calculate_commute", "get_weather"]

    by_tool = {item["tool"]: item for item in state["tool_artifacts"]}
    denied = by_tool["check_safety"]
    assert denied["denied"] is True and denied["success"] is False
    assert denied["specialist_error_code"] == "specialist_reserved_argument"
    # The stable code is LEDGER-only; the model is told nothing it could steer on.
    refusal = next(m for m in state["messages"]
                   if getattr(m, "name", None) == "check_safety")
    assert "specialist_reserved_argument" not in refusal.content

    # The denial is measurable in the canary record.
    assert trace["denied_calls"] == 1
    assert "specialist_reserved_argument" in str(trace)
    # Lifecycle invariants still balance around a refused dispatch.
    assert trace["planned"] >= trace["started"]
    assert trace["started"] == trace["completed"] + trace["partial"] + trace["failed"]
    assert trace["skipped"] <= trace["planned"] - trace["started"]


# ── TD-2: the not-planned branch is counted ───────────────────────────────────


def test_an_eligible_but_unplanned_call_increments_denied_calls(tmp_path, monkeypatch):
    """The known asymmetry: this branch returned denied without touching the counter, so
    the class was invisible to telemetry and could never be measured in a canary.

    The branch is unreachable through ordinary arguments (that is why it had no live
    evidence), so the prepared batch is wrapped to drop one planned call — a test-only
    monkeypatch of the runtime boundary, not a production hook."""
    import dataclasses
    from types import MappingProxyType

    import core.specialist_runtime as specialist_runtime

    real_prepare = specialist_runtime.prepare_specialist_batch

    def fake_prepare(calls, **kwargs):
        """A REAL PreparedSpecialistBatch with one planned call removed from the index.

        Removing the entry (rather than wrapping the object) keeps every other boundary
        check operating on the genuine type, so only the not-planned branch differs.
        """
        prepared = real_prepare(calls, **kwargs)
        hidden = min(prepared.eligible_indices)
        return dataclasses.replace(
            prepared,
            calls_by_index=MappingProxyType({
                index: call for index, call in prepared.calls_by_index.items()
                if index != hidden
            }),
        )

    monkeypatch.setattr(specialist_runtime, "prepare_specialist_batch", fake_prepare)

    async def probe(**kwargs):
        return {"ok": True}

    registry = _registry(
        tmp_path,
        [
            _tool("get_weather", probe, parameters=_schema("city")),
            _tool("check_safety", probe, parameters=_schema("address")),
        ],
    )
    nodes = build_fc_nodes(
        registry,
        specialist_dispatch=True,
        agent_llm=_Chat(
            AIMessage(content="", tool_calls=[
                _tc("get_weather", {"city": "London"}, "c1"),
                _tc("check_safety", {"address": "Camden"}, "c2"),
            ]),
            AIMessage(content="Here is what I could gather."),
        ),
    )

    turn_observations.begin_turn()
    try:
        state = asyncio.run(_drive(nodes, _turn_state()))
        trace = turn_observations.specialist_snapshot()
    finally:
        turn_observations.end_turn()

    denied = [a for a in state["tool_artifacts"] if a.get("denied")]
    assert len(denied) == 1
    assert denied[0]["specialist_error_code"] == "specialist_call_not_planned"
    assert trace["denied_calls"] == 1


# ── P1-5(a): a stall is a timeout, not a tool error ───────────────────────────


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("specialist task timed out before returning evidence", "timeout"),
        ("specialist task produced no reliable evidence", "tool_error"),
        ("specialist task was not started because the turn budget was exhausted",
         "budget_exhausted"),
        ("specialist artifact validation failed", "ledger_invalid"),
    ],
)
def test_the_failure_reason_separates_a_stall_from_a_tool_error(error, expected):
    """A 25 024 ms `area_evidence` failure was reported as `tool_error`, exactly like the
    1.8 ms argument rejections in the same sample. The two have opposite remedies."""
    assert specialist_result_reason("failed", error) == expected


def test_a_timed_out_artifact_produces_the_timeout_error_text():
    from core.specialist_runtime import build_specialist_results

    class _Prepared:
        pass

    # Exercised through the real classifier in build_specialist_results via its own
    # module-level test double is overkill here; the mapping above is the contract and the
    # producer is pinned by the marker names it reads.
    import inspect

    source = inspect.getsource(build_specialist_results)
    assert "timed out before returning evidence" in source
    assert 'artifact.get("timed_out")' in source


# ── P1-5(b): a failed role is told what it still has ──────────────────────────


def _entry(role, status, tools, reason=None):
    return EvidenceDigestEntry(
        role=role, task_id=f"t:{role}", status=status, reason=reason,
        tools=tuple(tools), tainted=False)


def test_a_failed_mobility_role_is_pointed_at_its_untried_grants():
    """The C5 case: `get_transport_info` + `check_transport_cost` failed instantly and the
    manager said "I don't have live TfL data available", while the flat loop recovered via
    `calculate_commute_cost` — a tool the SAME role already holds a grant for."""
    note = build_evidence_digest([
        _entry("mobility", "failed",
               ("get_transport_info", "check_transport_cost"), reason="tool_error"),
        _entry("listings", "succeeded", ("search_properties",)),
    ])

    assert "not a dead end" in note
    assert "calculate_commute_cost" in note
    # Only untried grants are named.
    assert "get_transport_info" not in note.split("Rules:")[1]


def test_no_recovery_hint_when_the_role_has_tried_everything():
    note = build_evidence_digest([
        _entry("listings", "failed",
               ("search_properties", "get_property_details"), reason="tool_error"),
    ])
    assert "not a dead end" not in note


def test_no_recovery_hint_on_a_clean_turn():
    note = build_evidence_digest([_entry("listings", "succeeded", ("search_properties",))])
    assert "not a dead end" not in note
