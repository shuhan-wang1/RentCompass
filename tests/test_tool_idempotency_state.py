import asyncio
import threading
import time

import pytest
from pydantic import BaseModel

from core.tool_system import Tool
from uk_rent_agent.tools.idempotency import IdempotencyStore


PARAMS = {
    "type": "object",
    "properties": {"content": {"type": "string"}},
    "required": ["content"],
}


def _write_tool(func, *, name="remember", output_model=None):
    return Tool(
        name=name,
        description="test write",
        func=func,
        parameters=PARAMS,
        side_effect="write",
        retry_safe=False,
        output_model=output_model,
    )


def test_known_failure_is_durable_and_replayed_without_second_write(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.sqlite3")
    calls = []

    def fail_once(content):
        calls.append(content)
        return {"success": False, "error": "provider rejected write"}

    tool = _write_tool(fail_once)

    async def run():
        first = await tool.execute(
            content="x", idempotency_key="k", _idempotency_store=store
        )
        second = await tool.execute(
            content="x", idempotency_key="k", _idempotency_store=store
        )
        return first, second

    first, second = asyncio.run(run())
    assert first.success is False and first.outcome == "failed"
    assert second.success is False and second.outcome == "failed"
    assert calls == ["x"]
    assert store.get_record("k").status == "failed"


def test_cancelled_inflight_write_becomes_unknown_and_is_not_retried(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.sqlite3")
    started = threading.Event()
    calls = []

    def slow_write(content):
        calls.append(content)
        started.set()
        time.sleep(0.15)
        return {"success": True, "id": "late"}

    tool = _write_tool(slow_write)

    async def run():
        task = asyncio.create_task(tool.execute(
            content="x", idempotency_key="k", _idempotency_store=store
        ))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.002)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.18)  # the executor thread may finish, but no ack was observed
        return await tool.execute(
            content="x", idempotency_key="k", _idempotency_store=store
        )

    replay = asyncio.run(run())
    assert replay.success is False
    assert replay.outcome == "unknown"
    assert "unknown" in replay.error
    assert calls == ["x"]
    assert store.get_record("k").status == "unknown"


def test_unknown_remote_write_can_be_recorded_before_local_claim(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.sqlite3")
    store.mark_unknown("remote-k", "transport lost", tool="remember")

    record = store.get_record("remote-k")
    assert record.status == "unknown"
    assert record.tool == "remember"
    assert store.claim("remote-k", "remember") is False


def test_same_key_cannot_cross_tool_boundary(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.sqlite3")
    first = _write_tool(lambda content: {"success": True}, name="remember")
    other = _write_tool(lambda content: {"success": True}, name="save_favourite")

    async def run():
        await first.execute(content="x", idempotency_key="shared", _idempotency_store=store)
        return await other.execute(
            content="x", idempotency_key="shared", _idempotency_store=store
        )

    result = asyncio.run(run())
    assert result.success is False
    assert result.outcome == "conflict"


class _StrictOutput(BaseModel):
    saved: bool


def test_output_contract_failure_is_terminal_not_left_running(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.sqlite3")
    calls = []

    def malformed(content):
        calls.append(content)
        return {"success": True, "unexpected": 1}

    tool = _write_tool(malformed, output_model=_StrictOutput)

    async def run():
        first = await tool.execute(
            content="x", idempotency_key="k", _idempotency_store=store
        )
        second = await tool.execute(
            content="x", idempotency_key="k", _idempotency_store=store
        )
        return first, second

    first, second = asyncio.run(run())
    assert first.success is False and first.outcome == "failed"
    assert second.success is False and second.outcome == "failed"
    assert calls == ["x"]
    assert store.get_record("k").status == "failed"
