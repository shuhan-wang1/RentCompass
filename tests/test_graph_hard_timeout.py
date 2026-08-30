from __future__ import annotations

import asyncio
import time

import pytest

import app as appmod


@pytest.fixture(autouse=True)
def _isolated_graph_runner_pool(monkeypatch):
    """Keep timeout rotation in this module isolated from the imported app runtime."""
    runner = appmod._GraphLoopRunner()
    monkeypatch.setattr(appmod, "_graph_loop_runner", runner)
    monkeypatch.setattr(appmod, "_graph_loop_runners", [runner])
    monkeypatch.setattr(appmod, "agent_graph", None)


class _HangingGraph:
    def __init__(self):
        self.cancelled = False

    async def ainvoke(self, graph_input, config=None):
        try:
            await asyncio.sleep(5)
        finally:
            self.cancelled = True


class _BlockingGraph:
    async def ainvoke(self, graph_input, config=None):
        time.sleep(0.2)
        return {"late": True}


class _FastGraph:
    async def ainvoke(self, graph_input, config=None):
        return {"value": graph_input, "config": config}


class _LoopIdentityGraph:
    def __init__(self):
        self.loop_ids = []

    async def ainvoke(self, graph_input, config=None):
        self.loop_ids.append(id(asyncio.get_running_loop()))
        await asyncio.sleep(0)
        return graph_input


def test_graph_boundary_timeout_cancels_without_waiting_for_hung_call():
    async def scenario():
        graph = _HangingGraph()
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await appmod._ainvoke_graph_with_timeout(graph, "input", {"x": 1}, 0.03)
        elapsed = time.monotonic() - started
        await asyncio.sleep(0)
        return graph, elapsed

    graph, elapsed = asyncio.run(scenario())
    assert elapsed < 0.3
    cancellation_deadline = time.monotonic() + 0.2
    while not graph.cancelled and time.monotonic() < cancellation_deadline:
        # Cancellation runs on the isolated graph loop, not this test's caller loop.
        time.sleep(0.005)
    assert graph.cancelled is True


def test_graph_boundary_timeout_survives_a_blocked_graph_event_loop():
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(appmod._ainvoke_graph_with_timeout(
            _BlockingGraph(), "input", {"x": 1}, 0.03))
    # A replacement generation serves immediately while the original loop is
    # still synchronously blocked.
    recovered = asyncio.run(appmod._ainvoke_graph_with_timeout(
        _FastGraph(), "next", {"x": 2}, 0.1))
    elapsed = time.monotonic() - started

    assert elapsed < 0.15
    assert recovered == {"value": "next", "config": {"x": 2}}
    # Let the deliberately non-cooperative fixture leave the bounded runner thread.
    time.sleep(0.25)


def test_graph_boundary_returns_completed_result_unchanged():
    result = asyncio.run(appmod._ainvoke_graph_with_timeout(
        _FastGraph(), "input", {"x": 1}, 0.5))
    assert result == {"value": "input", "config": {"x": 1}}


def test_graph_boundary_reuses_one_loop_for_sequential_and_concurrent_calls():
    graph = _LoopIdentityGraph()

    async def concurrent_calls():
        return await asyncio.gather(
            appmod._ainvoke_graph_with_timeout(graph, "third", None, 0.5),
            appmod._ainvoke_graph_with_timeout(graph, "fourth", None, 0.5),
        )

    first = asyncio.run(appmod._ainvoke_graph_with_timeout(graph, "first", None, 0.5))
    second = asyncio.run(appmod._ainvoke_graph_with_timeout(graph, "second", None, 0.5))
    rest = asyncio.run(concurrent_calls())

    assert [first, second, *rest] == ["first", "second", "third", "fourth"]
    assert len(graph.loop_ids) == 4
    assert len(set(graph.loop_ids)) == 1


def test_graph_runner_pool_is_bounded_and_recovers_after_capacity_returns():
    for _ in range(appmod._GRAPH_RUNNER_CAPACITY):
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(appmod._ainvoke_graph_with_timeout(
                _BlockingGraph(), "blocked", None, 0.03))

    assert len(appmod._graph_loop_runners) == appmod._GRAPH_RUNNER_CAPACITY
    with pytest.raises(RuntimeError, match="capacity"):
        asyncio.run(appmod._ainvoke_graph_with_timeout(
            _FastGraph(), "fail-closed", None, 0.05))

    # Both fixtures eventually yield; their queued heartbeats then return the
    # bounded workers to service without allocating a third thread.
    time.sleep(0.25)
    result = asyncio.run(appmod._ainvoke_graph_with_timeout(
        _FastGraph(), "recovered", None, 0.1))

    assert result["value"] == "recovered"
