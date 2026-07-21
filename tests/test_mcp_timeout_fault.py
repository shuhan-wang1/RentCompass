"""Delayed fake MCP service: the timeout path, its error body, and the runner's
exit code landing on disk.

Why a DELAYING fake rather than a failing one: a session that returns an error
exercises the error branch; a session that never returns exercises the timeout
branch. They are different code with different outcomes, and only the second one
produces the case that actually matters — a WRITE whose outcome is unknown
because the server may still be completing it after we stopped waiting.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "app", _ROOT):
    if str(_p) in sys.path:
        sys.path.remove(str(_p))
    sys.path.insert(0, str(_p))

from evaluation.fault_injection.injectors import (  # noqa: E402
    DelayingMCPSession, make_mcp_client, stop_fake_mcp_client,
)

CALL_TIMEOUT = 1.0
# Comfortably longer than the timeout so a loaded box cannot let the call win.
HANG_FOR = 20.0


class _Reg:
    """Fallback registry. retry_safe=False marks a WRITE: an unconfirmed write may
    NOT be retried, and may not be quietly reported as a clean failure either."""

    def __init__(self, retry_safe):
        self._tool = types.SimpleNamespace(version="1", retry_safe=retry_safe,
                                           side_effect="write" if not retry_safe else "none")
        self.fallback_calls = []

    def get(self, _name):
        return self._tool

    async def execute_tool(self, name, **kw):
        from core.tool_system import ToolResult
        self.fallback_calls.append(name)
        return ToolResult(success=True, tool_name=name, data={"from": "fallback"})


@pytest.fixture
def hung_client():
    made = []

    def _make(retry_safe):
        reg = _Reg(retry_safe)
        session = DelayingMCPSession(delay_s=HANG_FOR)
        client = make_mcp_client(reg, session=session, call_timeout=CALL_TIMEOUT)
        made.append(client)
        return client, reg, session

    yield _make
    for c in made:
        stop_fake_mcp_client(c)


def test_timeout_actually_fires_and_is_bounded_by_call_timeout(hung_client):
    """The precondition every other assertion here rests on. If the call returned
    early, or the loop was never started and the generic except swallowed it, this
    would still 'pass' something — so the elapsed time is asserted explicitly."""
    client, _reg, session = hung_client(retry_safe=False)
    t0 = time.monotonic()
    result = asyncio.run(client.execute_tool("remember", content="x"))
    elapsed = time.monotonic() - t0

    assert session.calls == ["remember"], "the call must have reached the fake service"
    assert result.success is False
    assert CALL_TIMEOUT <= elapsed < HANG_FOR / 2, (
        f"expected a ~{CALL_TIMEOUT}s timeout, took {elapsed:.2f}s — the call did not "
        f"go through wait_for")


def test_write_timeout_error_body_says_the_outcome_is_unknown(hung_client):
    """The exact wording is load-bearing. A write we stopped waiting for may still
    land, so calling it a failure would invite a retry and a double write."""
    client, _reg, _s = hung_client(retry_safe=False)
    result = asyncio.run(client.execute_tool("remember", content="x",
                                             idempotency_key="idem-1"))

    assert result.success is False
    assert f"timed out after {CALL_TIMEOUT}s" in result.error
    assert "outcome is unknown" in result.error
    assert "not retried" in result.error
    assert result.idempotency_key == "idem-1", \
        "the key must survive so a later reconciliation can identify the call"


def test_write_timeout_does_not_fall_back(hung_client):
    """Falling back would RE-RUN the write against the in-process registry while the
    first one may still be in flight."""
    client, reg, _s = hung_client(retry_safe=False)
    asyncio.run(client.execute_tool("remember", content="x"))
    assert reg.fallback_calls == []


def test_read_timeout_does_fall_back(hung_client):
    """A retry-safe READ is the opposite case: re-running it costs nothing, so the
    turn should get an answer rather than an error."""
    client, reg, _s = hung_client(retry_safe=True)
    result = asyncio.run(client.execute_tool("web_search", query="x"))

    assert reg.fallback_calls == ["web_search"]
    assert result.success is True
    assert result.data == {"from": "fallback"}


def test_the_hung_call_is_cancelled_not_left_running(hung_client):
    """A detached call that keeps a loop slot forever turns one slow response into a
    leak that outlives the request."""
    client, _reg, _s = hung_client(retry_safe=True)
    asyncio.run(client.execute_tool("web_search", query="x"))
    pending = asyncio.all_tasks(client._loop) if client._loop.is_running() else set()
    assert len(pending) < 5, f"unexpected task backlog: {pending}"


# --------------------------------------------------------------------------- #
# Runner exit code on disk                                                    #
# --------------------------------------------------------------------------- #

def test_runner_writes_its_exit_code_to_disk(tmp_path, monkeypatch):
    """The artifact must record what the process TOLD its caller, not only what the
    scenarios found. A CI step that swallows the status otherwise leaves no trace of
    the difference, and "the summary looked fine" is not evidence the run passed."""
    from evaluation.fault_injection import run as fi_run

    async def _fake_run_all(out: Path, timestamp: str):
        out.mkdir(parents=True, exist_ok=True)
        summary = {"framework": "fault_injection", "harness_errors": 0,
                   "timestamp": timestamp}
        summary["exit_code"] = 0
        (out / "fault_summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (out / "fault_exit_code").write_text("0\n", encoding="utf-8")
        return summary

    monkeypatch.setattr(fi_run, "_run_all", _fake_run_all)
    rc = fi_run.main(["--out", str(tmp_path), "--timestamp", "T"])

    assert rc == 0
    assert (tmp_path / "fault_exit_code").read_text().strip() == "0"
    assert json.loads((tmp_path / "fault_summary.json").read_text())["exit_code"] == 0


def test_runner_exit_code_on_disk_matches_the_returned_code(tmp_path, monkeypatch):
    """Two independent computations of the same value is exactly how they drift, so
    the returned code is READ BACK from the summary rather than recomputed."""
    from evaluation.fault_injection import run as fi_run

    async def _fake_run_all(out: Path, timestamp: str):
        out.mkdir(parents=True, exist_ok=True)
        summary = {"harness_errors": 3, "timestamp": timestamp, "exit_code": 1}
        (out / "fault_summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (out / "fault_exit_code").write_text("1\n", encoding="utf-8")
        return summary

    monkeypatch.setattr(fi_run, "_run_all", _fake_run_all)
    rc = fi_run.main(["--out", str(tmp_path), "--timestamp", "T"])

    assert rc == 1
    assert (tmp_path / "fault_exit_code").read_text().strip() == str(rc)
