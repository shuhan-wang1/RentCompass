from __future__ import annotations

import asyncio
import threading
import time

import pytest
from langchain_core.messages import AIMessage

import core.agent_loop as agent_loop
from core import turn_observations
from core.agent_loop import build_fc_nodes
from core.tool_system import Tool
from tests.test_manager_v1_specialist_dispatch import (
    _registry,
    _schema,
    _state,
    _tc,
    _tool,
)
from uk_rent_agent.observability import agent_execution_context


class _AllowMemoryGate:
    @staticmethod
    def write_authorization(_message, _content):
        return True

    @staticmethod
    def is_pure_recall_question(_message):
        return False

    @staticmethod
    def memory_write_allowed(*, context_tainted, user_authorized):
        return bool(user_authorized)


class _BlockingMarker:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def tool(self, value):
        async def run(**_kwargs):
            self.started.set()
            # Deliberately non-yielding: this is the unkillable private-worker shape
            # the cancellation path must abandon without awaiting.
            self.release.wait(timeout=5)
            self.finished.set()
            return value

        return run


def _remember_tool(func):
    return Tool(
        name="remember",
        description="blocking write fixture",
        func=func,
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "kind": {"type": "string"},
                "user_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["content"],
            "additionalProperties": False,
        },
        max_retries=0,
        retry_on_error=False,
        side_effect="write",
        retry_safe=False,
    )


async def _wait_started(*markers: _BlockingMarker) -> None:
    deadline = time.monotonic() + 2
    while not all(marker.started.is_set() for marker in markers):
        if time.monotonic() >= deadline:
            raise AssertionError("blocking fixture did not start")
        await asyncio.sleep(0.005)


async def _assert_no_child_tasks() -> None:
    # Cancellation callbacks run on the next loop turn; give both the Task and its
    # done callback a chance to settle, without touching the private worker thread.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    current = asyncio.current_task()
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]
    assert pending == []


def test_parent_cancel_cleans_read_and_write_children_without_waiting_for_workers(
    tmp_path, monkeypatch
):
    read = _BlockingMarker()
    write = _BlockingMarker()
    events = []
    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: _AllowMemoryGate)
    monkeypatch.setattr(
        agent_loop, "_record_budget_timeout_event", lambda **fields: events.append(fields)
    )
    registry = _registry(
        tmp_path,
        [
            _tool("get_weather", read.tool({"weather": "sunny"}), parameters=_schema("city")),
            _remember_tool(write.tool({"saved": True})),
        ],
    )
    nodes = build_fc_nodes(registry, specialist_dispatch=True)
    state = _state(
        [
            _tc("get_weather", {"city": "London"}, "c1"),
            _tc("remember", {"content": "remember my budget", "kind": "semantic"}, "c2"),
        ],
        message="Remember my budget and check London weather",
    )

    async def run():
        with agent_execution_context(
            agent_role="manager",
            task_id="turn:request-1/node:execute_tools:0",
            parent_task_id="turn:request-1",
        ):
            node_task = asyncio.create_task(nodes["execute_tools"](state))
            await _wait_started(read, write)
            cancelled_at = time.monotonic()
            node_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await node_task
            cancellation_latency = time.monotonic() - cancelled_at
            still_running = (not read.finished.is_set(), not write.finished.is_set())
            await _assert_no_child_tasks()
            return cancellation_latency, still_running

    turn_observations.begin_turn()
    try:
        latency, still_running = asyncio.run(run())
        trace = turn_observations.specialist_snapshot()
    finally:
        read.release.set()
        write.release.set()
        assert read.finished.wait(timeout=2)
        assert write.finished.wait(timeout=2)
        turn_observations.end_turn()

    assert latency < 0.25
    assert still_running == (True, True), "execute_tools waited for an unkillable worker"
    by_tool = {event["tool"]: event for event in events}
    assert by_tool["get_weather"]["phase"] == "turn"
    assert by_tool["get_weather"]["outcome"] == "abandoned"
    assert by_tool["remember"]["phase"] == "turn"
    assert by_tool["remember"]["outcome"] == "outcome_unknown"
    assert trace["planned"] == trace["started"] == trace["failed"] == 1
    assert trace["completed"] == trace["skipped"] == 0


def test_cancel_while_directly_awaiting_write_still_marks_write_outcome_unknown(
    tmp_path, monkeypatch
):
    read_finished = threading.Event()
    write = _BlockingMarker()
    events = []

    async def fast_read(**_kwargs):
        read_finished.set()
        return {"weather": "sunny"}

    monkeypatch.setattr(agent_loop, "_load_memory_gate", lambda: _AllowMemoryGate)
    monkeypatch.setattr(
        agent_loop, "_record_budget_timeout_event", lambda **fields: events.append(fields)
    )
    registry = _registry(
        tmp_path,
        [
            _tool("get_weather", fast_read, parameters=_schema("city")),
            _remember_tool(write.tool({"saved": True})),
        ],
    )
    nodes = build_fc_nodes(registry, specialist_dispatch=True)
    state = _state(
        [
            _tc("get_weather", {"city": "London"}, "c1"),
            _tc("remember", {"content": "remember my budget"}, "c2"),
        ],
        message="Remember my budget and check London weather",
    )

    async def run():
        with agent_execution_context(
            agent_role="manager",
            task_id="turn:request-1/node:execute_tools:0",
            parent_task_id="turn:request-1",
        ):
            node_task = asyncio.create_task(nodes["execute_tools"](state))
            await _wait_started(write)
            deadline = time.monotonic() + 2
            while not read_finished.is_set():
                if time.monotonic() >= deadline:
                    raise AssertionError("read fixture did not finish")
                await asyncio.sleep(0.005)
            # Let execute_tools consume the completed read and enter `await write_task`.
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
        write.release.set()
        assert write.finished.wait(timeout=2)
        turn_observations.end_turn()

    assert [(event["tool"], event["phase"], event["outcome"]) for event in events] == [
        ("remember", "turn", "outcome_unknown")
    ]
    # The specialist produced a tool value, but the parent turn was cancelled before
    # accepting/building its result contract, so its lifecycle is failed, not completed.
    assert trace["planned"] == trace["started"] == trace["failed"] == 1
    assert trace["completed"] == 0


def test_arbitrary_base_exception_cleans_started_specialist_child(
    tmp_path, monkeypatch
):
    read = _BlockingMarker()
    events = []

    class ParentAbort(BaseException):
        pass

    async def aborting_wait(_tasks, timeout=None):
        await _wait_started(read)
        raise ParentAbort("parent scope aborted")

    monkeypatch.setattr(agent_loop.asyncio, "wait", aborting_wait)
    monkeypatch.setattr(
        agent_loop, "_record_budget_timeout_event", lambda **fields: events.append(fields)
    )
    registry = _registry(
        tmp_path,
        [_tool("get_weather", read.tool({"weather": "sunny"}), parameters=_schema("city"))],
    )
    nodes = build_fc_nodes(registry, specialist_dispatch=True)
    state = _state([_tc("get_weather", {"city": "London"}, "c1")])

    async def run():
        with agent_execution_context(
            agent_role="manager",
            task_id="turn:request-1/node:execute_tools:0",
            parent_task_id="turn:request-1",
        ):
            with pytest.raises(ParentAbort):
                await nodes["execute_tools"](state)
            await _assert_no_child_tasks()

    turn_observations.begin_turn()
    try:
        asyncio.run(run())
        trace = turn_observations.specialist_snapshot()
    finally:
        read.release.set()
        assert read.finished.wait(timeout=2)
        turn_observations.end_turn()

    assert [(event["tool"], event["phase"], event["outcome"]) for event in events] == [
        ("get_weather", "turn", "abandoned")
    ]
    assert trace["planned"] == trace["started"] == trace["failed"] == 1
    assert trace["completed"] == 0


def test_parent_cancel_during_post_search_validation_accounts_worker_and_terminal_once(
    tmp_path, monkeypatch
):
    from core.tools.search_properties import search_properties_tool

    commute = _BlockingMarker()
    events = []

    async def search_properties(**_kwargs):
        return {
            "status": "found",
            "search_criteria": {
                "max_commute_time": 30,
                "commute_destination": "Synthetic Campus",
            },
            "recommendations": [
                {"address": "1 Validation Road", "price": 900}
            ],
        }

    monkeypatch.setattr(
        agent_loop, "_record_budget_timeout_event", lambda **fields: events.append(fields)
    )
    registry = _registry(
        tmp_path,
        [
            _tool(
                "search_properties",
                search_properties,
                parameters=search_properties_tool.parameters,
            ),
            _tool(
                "calculate_commute",
                commute.tool({"duration_minutes": 20}),
                parameters=_schema("from_address", "to_address", "mode"),
            ),
        ],
    )
    nodes = build_fc_nodes(registry, specialist_dispatch=True)
    state = _state(
        [_tc("search_properties", {"area": "Synthetic"}, "c1")],
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
            cancelled_at = time.monotonic()
            node_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await node_task
            cancellation_latency = time.monotonic() - cancelled_at
            worker_still_running = not commute.finished.is_set()
            await _assert_no_child_tasks()
            return cancellation_latency, worker_still_running

    turn_observations.begin_turn()
    try:
        latency, worker_still_running = asyncio.run(run())
        trace = turn_observations.specialist_snapshot()
    finally:
        commute.release.set()
        if commute.started.is_set():
            assert commute.finished.wait(timeout=2)
        turn_observations.end_turn()

    assert latency < 0.25
    assert worker_still_running is True, "execute_tools waited for validation worker"
    assert [
        (event["tool"], event["phase"], event["outcome"])
        for event in events
    ] == [("calculate_commute", "turn", "abandoned")]
    assert trace["planned"] == trace["started"] == trace["failed"] == 1
    assert trace["completed"] == trace["skipped"] == 0
    assert trace["failed"] + trace["completed"] + trace["skipped"] == 1
