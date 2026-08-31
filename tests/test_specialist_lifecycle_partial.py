"""Audit lifecycle/duration/cancellation: what the specialist trace actually reports.

Three separate mis-reports:

* a task with one successful and one abandoned call is ``partial``, but the terminal map
  had no ``partial`` and folded it into ``failed`` — so every downstream reader
  systematically overstated the specialist failure rate;
* ``duration_ms`` was derived from artifact ``elapsed_ms``, which is the batch-window
  CONSTANT for an abandoned call and 0 for a denied one — never a latency;
* the post-validation cancellation handler marked EVERY started task ``failed``, including
  tasks whose calls had all already produced successful artifacts.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import core.agent_loop as agent_loop
from core import turn_observations
from core.agent_loop import build_fc_nodes
from tests.test_manager_v1_specialist_dispatch import (
    _execute,
    _registry,
    _schema,
    _state,
    _tc,
    _tool,
)
from tests.test_parent_cancellation_cleanup import (
    _BlockingMarker,
    _assert_no_child_tasks,
    _wait_started,
)
from uk_rent_agent.observability import agent_execution_context


@pytest.fixture
def lifecycle_events(monkeypatch):
    """Capture every lifecycle transition without displacing the real telemetry.

    The spy DELEGATES, so a test can assert both the exact producer-side fields (which the
    bounded per-turn projection does not expose) and the counters the release gate reads.
    """
    events = []

    def recorder(status, upstream):
        def note(**fields):
            events.append({"status": status, **fields})
            return upstream(**fields) if callable(upstream) else True

        return note

    for status, attribute in agent_loop._SPECIALIST_LIFECYCLE_RECORDERS.items():
        upstream = getattr(turn_observations, attribute, None)
        monkeypatch.setattr(
            turn_observations, attribute, recorder(status, upstream), raising=False
        )
    return events


def _terminal(events):
    return [
        event for event in events
        if event["status"] in {"completed", "partial", "failed", "skipped"}
    ]


def test_partial_task_is_reported_partial_end_to_end(tmp_path, monkeypatch, lifecycle_events):
    """Audit missing-test #2: 1 success + 1 abandoned in ONE role."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.4")

    async def fast(**kwargs):
        return {"weather": kwargs.get("city")}

    slow = _BlockingMarker()

    registry = _registry(
        tmp_path,
        [
            _tool("get_weather", fast, parameters=_schema("city")),
            _tool("check_safety", slow.tool({"safety": "high"}),
                  parameters=_schema("address")),
        ],
    )
    state = _state(
        [
            _tc("get_weather", {"city": "London"}, "c1"),
            _tc("check_safety", {"address": "Camden"}, "c2"),
        ]
    )

    turn_observations.begin_turn()
    try:
        state = _execute(build_fc_nodes(registry, specialist_dispatch=True), state)
        trace = turn_observations.specialist_snapshot()
    finally:
        slow.release.set()
        turn_observations.end_turn()

    result = state["specialist_results"][0]
    assert result["role"] == "area_evidence"
    assert result["status"] == "partial"
    assert result["data"] == {
        "call_count": 2,
        "succeeded": 1,
        "artifact_ids": result["data"]["artifact_ids"],
    }
    assert len(result["evidence"]) == 1

    terminal = _terminal(lifecycle_events)
    assert [event["status"] for event in terminal] == ["partial"]
    # ``partial`` is NOT a failure and carries the reason in the closed code set.
    assert terminal[0]["error_code"] == "incomplete"
    assert [event["status"] for event in lifecycle_events][:2] == ["planned", "started"]

    # ...and the same transition really lands in the shared turn telemetry, where it used
    # to be counted as a failure.
    assert trace["partial"] == 1
    assert trace["failed"] == trace["completed"] == trace["skipped"] == 0
    assert trace["planned"] == trace["started"] == 1
    assert trace["started"] == trace["completed"] + trace["partial"] + trace["failed"]


def test_lifecycle_counts_satisfy_the_turn_end_invariants(tmp_path, monkeypatch,
                                                          lifecycle_events):
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.4")

    async def fast(**kwargs):
        return {"ok": True}

    slow = _BlockingMarker()
    registry = _registry(
        tmp_path,
        [
            _tool("get_weather", fast, parameters=_schema("city")),
            _tool("check_safety", slow.tool({"safety": "high"}),
                  parameters=_schema("address")),
            _tool("get_property_details", fast, parameters=_schema("url")),
        ],
    )
    state = _state(
        [
            _tc("get_weather", {"city": "London"}, "c1"),
            _tc("check_safety", {"address": "Camden"}, "c2"),
            _tc("get_property_details", {"url": "fixture://x"}, "c3"),
        ]
    )
    try:
        _execute(build_fc_nodes(registry, specialist_dispatch=True), state)
    finally:
        slow.release.set()

    counts = {}
    for event in lifecycle_events:
        counts[event["status"]] = counts.get(event["status"], 0) + 1
    counts.setdefault("partial", 0)
    counts.setdefault("failed", 0)
    counts.setdefault("skipped", 0)

    assert counts["planned"] >= counts["started"]
    assert counts["started"] == (
        counts["completed"] + counts["partial"] + counts["failed"]
    )
    assert counts["skipped"] <= counts["planned"] - counts["started"]


def test_duration_ms_is_measured_wall_clock_not_the_ledger_constant(
    tmp_path, monkeypatch, lifecycle_events
):
    """A fast task in a slow batch must not report its tool time as its task duration."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.5")

    async def instant(**_kwargs):
        return {"ok": True}

    slow = _BlockingMarker()
    registry = _registry(
        tmp_path,
        [
            _tool("get_property_details", instant, parameters=_schema("url")),
            _tool("check_safety", slow.tool({"safety": "high"}),
                  parameters=_schema("address")),
        ],
    )
    state = _state(
        [
            _tc("get_property_details", {"url": "fixture://x"}, "c1"),
            _tc("check_safety", {"address": "Camden"}, "c2"),
        ]
    )
    try:
        state = _execute(build_fc_nodes(registry, specialist_dispatch=True), state)
    finally:
        slow.release.set()

    listings = next(
        item for item in state["specialist_results"] if item["role"] == "listings"
    )
    artifact = next(
        item for item in state["tool_artifacts"] if item["tool"] == "get_property_details"
    )
    assert artifact["elapsed_ms"] < 200
    # The task was open for the whole batch window, and that is what a task duration means.
    assert listings["duration_ms"] > 300
    assert listings["duration_ms"] >= artifact["elapsed_ms"]

    terminal = _terminal(lifecycle_events)
    completed = next(event for event in terminal if event["status"] == "completed")
    assert completed["duration_ms"] == listings["duration_ms"]
    assert "error_code" not in completed


def test_skipped_task_carries_the_budget_error_code(tmp_path, lifecycle_events):
    async def weather(**_kwargs):
        raise AssertionError("a pre-exhausted turn must not dispatch")

    registry = _registry(tmp_path, [_tool("get_weather", weather, parameters=_schema("city"))])
    state = _state([_tc("get_weather", {"city": "London"}, "c1")])
    state["turn_tool_budget_used_s"] = 1_000_000.0
    state = _execute(build_fc_nodes(registry, specialist_dispatch=True), state)

    assert state["specialist_results"][0]["status"] == "skipped"
    terminal = _terminal(lifecycle_events)
    assert [event["status"] for event in terminal] == ["skipped"]
    assert terminal[0]["error_code"] == "budget_exhausted"


def test_cancellation_does_not_fail_a_task_whose_calls_already_succeeded(
    tmp_path, monkeypatch, lifecycle_events
):
    """The post-validation handler must attribute per task, not blanket-fail."""
    from core.tools.search_properties import search_properties_tool

    commute = _BlockingMarker()

    async def weather(**kwargs):
        return {"weather": kwargs.get("city")}

    async def search_properties(**_kwargs):
        return {
            "status": "found",
            "search_criteria": {
                "max_commute_time": 30,
                "commute_destination": "Synthetic Campus",
            },
            "recommendations": [{"address": "1 Validation Road", "price": 900}],
        }

    monkeypatch.setattr(
        agent_loop, "_record_budget_timeout_event", lambda **fields: None
    )
    registry = _registry(
        tmp_path,
        [
            _tool("get_weather", weather, parameters=_schema("city")),
            _tool("search_properties", search_properties,
                  parameters=search_properties_tool.parameters),
            _tool("calculate_commute", commute.tool({"duration_minutes": 20}),
                  parameters=_schema("from_address", "to_address", "mode")),
        ],
    )
    nodes = build_fc_nodes(registry, specialist_dispatch=True)
    state = _state(
        [
            _tc("get_weather", {"city": "London"}, "c1"),
            _tc("search_properties", {"area": "Synthetic"}, "c2"),
        ],
        message="Find a home within 30 minutes of Synthetic Campus",
    )

    async def run():
        with agent_execution_context(
            agent_role="manager",
            task_id="turn:request-1/node:execute_tools:0",
            parent_task_id="turn:request-1",
        ):
            node_task = asyncio.create_task(nodes["execute_tools"](state))
            await _wait_started(commute)
            node_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await node_task
            await _assert_no_child_tasks()

    try:
        asyncio.run(run())
    finally:
        commute.release.set()
        if commute.started.is_set():
            assert commute.finished.wait(timeout=2)

    terminal = {event["role"]: event for event in _terminal(lifecycle_events)}
    assert set(terminal) == {"area_evidence", "listings"}
    # get_weather already wrote a successful artifact before the cancellation.
    assert terminal["area_evidence"]["status"] == "completed"
    assert "error_code" not in terminal["area_evidence"]
    # search_properties' own artifact is written AFTER validation, so the LEDGER cannot see
    # it when the turn is cancelled inside the commute fan-out — but the call itself ran and
    # returned. The outcome is now read from the batch's own results first, so the write
    # ORDER of the artifact loop no longer decides whether a successful specialist call is
    # reported as a failure (review R1/R4).
    assert terminal["listings"]["status"] == "completed"
    assert "error_code" not in terminal["listings"]
    assert all(event["duration_ms"] >= 0 for event in terminal.values())


def test_gather_cancellation_reports_what_already_succeeded(
    tmp_path, monkeypatch, lifecycle_events
):
    """Review R1/R4: the GATHER handler is the common cancellation path, and had no test.

    It runs while ``asyncio.wait`` is still in flight — i.e. BEFORE the loop that appends
    artifacts for this super-step — so deriving the outcome from ``artifacts`` counted zero
    successes for every task and reported ``failed``/``cancelled`` unconditionally, even
    for calls that had returned seconds earlier.
    """
    monkeypatch.setattr(
        agent_loop, "_record_budget_timeout_event", lambda **fields: None
    )
    blocked = _BlockingMarker()
    returned = threading.Event()

    async def fast(**kwargs):
        return {"weather": kwargs.get("city")}

    async def fast_then_signal(**kwargs):
        result = {"ranked": kwargs.get("areas")}
        returned.set()
        return result

    registry = _registry(
        tmp_path,
        [
            _tool("get_weather", fast, parameters=_schema("city")),
            _tool("compare_or_rank_areas", fast_then_signal,
                  parameters=_schema("areas")),
            _tool("check_safety", blocked.tool({"safety": "high"}),
                  parameters=_schema("address")),
        ],
    )
    nodes = build_fc_nodes(registry, specialist_dispatch=True)
    state = _state(
        [
            _tc("get_weather", {"city": "London"}, "c1"),
            _tc("compare_or_rank_areas", {"areas": "a,b"}, "c2"),
            _tc("check_safety", {"address": "Camden"}, "c3"),
        ]
    )

    async def run():
        with agent_execution_context(
            agent_role="manager",
            task_id="turn:request-1/node:execute_tools:0",
            parent_task_id="turn:request-1",
        ):
            node_task = asyncio.create_task(nodes["execute_tools"](state))
            await _wait_started(blocked)
            deadline = time.monotonic() + 2
            while not returned.is_set():
                if time.monotonic() >= deadline:
                    raise AssertionError("fast fixtures did not finish")
                await asyncio.sleep(0.005)
            # Let the graph loop settle both finished futures, then cancel while
            # asyncio.wait is still pending on check_safety: two of the three
            # area_evidence calls have already returned successfully.
            await asyncio.sleep(0.05)
            node_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await node_task
            await _assert_no_child_tasks()

    turn_observations.begin_turn()
    try:
        asyncio.run(run())
        trace = turn_observations.specialist_snapshot()
    finally:
        blocked.release.set()
        if blocked.started.is_set():
            assert blocked.finished.wait(timeout=2)
        turn_observations.end_turn()

    terminal = _terminal(lifecycle_events)
    assert [event["status"] for event in terminal] == ["partial"]
    assert terminal[0]["role"] == "area_evidence"
    assert terminal[0]["error_code"] == "incomplete"
    assert trace["partial"] == 1
    assert trace["failed"] == 0
    # The turn-end invariant still holds on the cancellation path.
    assert trace["started"] == (
        trace["completed"] + trace["partial"] + trace["failed"]
    )
    assert trace["skipped"] <= trace["planned"] - trace["started"]


def test_unknown_error_codes_are_dropped_before_telemetry(monkeypatch):
    captured = []
    monkeypatch.setattr(
        turn_observations, "note_specialist_fail",
        lambda **fields: captured.append(fields), raising=False)

    class _Task:
        task_id = "plan:x/listings"
        parent_task_id = "manager:x"
        role = "listings"

    agent_loop._note_specialist_lifecycle(
        "failed", plan_id="plan:x", task=_Task(), call_count=1,
        duration_ms=1.0, error_code="not_in_the_closed_set")
    agent_loop._note_specialist_lifecycle(
        "failed", plan_id="plan:x", task=_Task(), call_count=1,
        duration_ms=1.0, error_code="tool_error")

    assert "error_code" not in captured[0]
    assert captured[1]["error_code"] == "tool_error"


def test_lifecycle_tolerates_a_missing_partial_recorder(monkeypatch):
    """``note_specialist_partial`` may not exist yet; the generic event is the fallback."""
    monkeypatch.delattr(turn_observations, "note_specialist_partial", raising=False)
    seen = []
    monkeypatch.setattr(
        turn_observations, "note_specialist_event",
        lambda status, **fields: seen.append((status, fields)), raising=False)

    class _Task:
        task_id = "plan:x/area_evidence"
        parent_task_id = "manager:x"
        role = "area_evidence"

    agent_loop._note_specialist_lifecycle(
        "partial", plan_id="plan:x", task=_Task(), call_count=2,
        duration_ms=12.0, error_code="incomplete")

    assert seen and seen[0][0] == "partial"
    assert seen[0][1]["error_code"] == "incomplete"
