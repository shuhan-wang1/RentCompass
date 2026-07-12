"""Reusable fault injectors + the per-scenario result record.

Every helper here operates on the REAL production classes (imported lazily after
the env bootstrap). Nothing here fabricates a metric: the injectors merely make a
real tool fail / a real store lock / a real model raise, and the scenarios then
observe how the real code responds.
"""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Per-scenario result
# --------------------------------------------------------------------------- #
@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    fault: str
    # Resilience measurements (None == not applicable to this scenario).
    retry_recovered: Optional[bool] = None
    fallback_succeeded: Optional[bool] = None
    idempotency_held: Optional[bool] = None
    duplicate_write_count: int = 0
    task_completed_after_fault: bool = False
    produced_ungrounded_answer_after_fault: bool = False
    # True == the fault was surfaced/handled honestly (error, caveat, "no data",
    # recovered); False == the system silently fabricated around it.
    fault_surfaced: Optional[bool] = None
    detail: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Tool builders (REAL core.tool_system.Tool / ToolRegistry)
# --------------------------------------------------------------------------- #
_DEFAULT_PARAMS = {
    "type": "object",
    "properties": {"q": {"type": "string", "default": "x"}},
    "required": [],
}


def make_tool(
    name: str,
    func: Callable,
    *,
    side_effect: str = "none",
    output_model: Any = None,
    max_retries: int = 3,
    params: Optional[dict] = None,
):
    """Build a real ``core.tool_system.Tool`` wrapping ``func``."""
    from core.tool_system import Tool

    return Tool(
        name=name,
        description=f"fault-injection tool: {name}",
        func=func,
        parameters=params or _DEFAULT_PARAMS,
        max_retries=max_retries,
        retry_on_error=True,
        side_effect=side_effect,
        output_model=output_model,
    )


def make_registry(*tools, idempotency_db: Optional[Path] = None):
    """Build a real ToolRegistry with an isolated IdempotencyStore + tools."""
    from core.tool_system import ToolRegistry
    from uk_rent_agent.tools.idempotency import IdempotencyStore

    store = None
    if idempotency_db is not None:
        store = IdempotencyStore(Path(idempotency_db))
    reg = ToolRegistry(idempotency_store=store)
    for t in tools:
        reg.register(t)
    return reg


class DurableWriteCounter:
    """A thread-safe stand-in for a durable side effect (e.g. a booking write).

    ``count`` is the number of times the underlying write actually executed —
    the ground truth for the idempotency checks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0

    def write(self, **kwargs) -> dict:
        with self._lock:
            self.count += 1
            n = self.count
        return {"success": True, "receipt_id": f"booking-{n}"}


# --------------------------------------------------------------------------- #
# Retry-backoff speed-up (harness-only; does not change tool logic)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def fast_backoff():
    """Neutralise the exponential-backoff sleeps so retry scenarios run fast.

    Only the *duration* of ``asyncio.sleep`` changes; the retry control flow in
    ``Tool.execute`` is exercised exactly as in production.
    """
    real_sleep = asyncio.sleep

    async def _instant(_delay, *a, **k):
        return await real_sleep(0)

    asyncio.sleep = _instant  # type: ignore[assignment]
    try:
        yield
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Transient / persistent failing functions
# --------------------------------------------------------------------------- #
def transient_failer(exc: Exception, fail_times: int, success_value: Any):
    """Return a sync func that raises ``exc`` the first ``fail_times`` calls,
    then returns ``success_value``. Exercises the real retry loop."""
    state = {"n": 0}
    lock = threading.Lock()

    def _f(**kwargs):
        with lock:
            state["n"] += 1
            n = state["n"]
        if n <= fail_times:
            raise exc
        return success_value

    return _f


def persistent_failer(exc: Exception):
    def _f(**kwargs):
        raise exc

    return _f


# --------------------------------------------------------------------------- #
# MCP fakes (REAL core.mcp_client.MCPToolClient)
# --------------------------------------------------------------------------- #
class _FakeContentItem:
    def __init__(self, text: str):
        self.text = text


class _FakeCallResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [_FakeContentItem(text)]
        self.isError = is_error


class FakeMCPSession:
    """Minimal MCP ClientSession stand-in whose ``call_tool`` returns canned text."""

    def __init__(self, text: str, is_error: bool = False):
        self._text = text
        self._is_error = is_error

    async def call_tool(self, name, kwargs):
        return _FakeCallResult(self._text, self._is_error)


def make_mcp_client(fallback_registry=None, *, session=None):
    """Build a real MCPToolClient WITHOUT starting a subprocess.

    With ``session=None`` it reports ``connected == False`` (so ``execute_tool``
    takes the not-connected fallback path). With a ``FakeMCPSession`` it reports
    connected and ``_call`` will parse that session's response text.
    """
    from core.mcp_client import MCPToolClient

    client = MCPToolClient(
        command="python",
        args=["-c", "pass"],
        fallback_registry=fallback_registry,
        connect_timeout=0.1,
        call_timeout=2.0,
    )
    if session is not None:
        client._session = session
    return client


# --------------------------------------------------------------------------- #
# Model that raises (drives generate_response / critic error handling)
# --------------------------------------------------------------------------- #
class RaisingChatModel:
    """A chat model whose every invocation raises — for synthesis/critic faults."""

    def __init__(self, message: str = "injected model failure"):
        self._message = message
        self.callbacks: List[Any] = []

    async def ainvoke(self, *args, **kwargs):
        raise RuntimeError(self._message)

    def invoke(self, *args, **kwargs):
        raise RuntimeError(self._message)


@contextlib.contextmanager
def patch_router_raises(message: str = "injected model failure"):
    """Patch ``ModelRouter.create`` so every model built raises on invoke."""
    from uk_rent_agent.llm import router as _router

    original = _router.ModelRouter.create

    def _create(self, purpose, **kwargs):
        return RaisingChatModel(message)

    _router.ModelRouter.create = _create
    try:
        yield
    finally:
        _router.ModelRouter.create = original


# --------------------------------------------------------------------------- #
# SQLite brief-unavailability (real exclusive lock on the idempotency DB)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def hold_sqlite_write_lock(path: Path, hold_seconds: float = 0.4):
    """Hold a real EXCLUSIVE write lock on ``path`` for ``hold_seconds``.

    A concurrent ``IdempotencyStore.claim`` will block on SQLite's busy-timeout
    until the lock releases, then proceed — exercising the store's real
    brief-unavailability tolerance.
    """
    acquired = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def _holder():
        conn = sqlite3.connect(str(path), timeout=30)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN EXCLUSIVE")
            acquired.set()
            release.wait(timeout=10)
            conn.execute("COMMIT")
        finally:
            conn.close()
            done.set()

    t = threading.Thread(target=_holder, name="sqlite-lock-holder", daemon=True)
    t.start()
    acquired.wait(timeout=5)
    # Auto-release after hold_seconds so the blocked claim recovers.
    threading.Timer(hold_seconds, release.set).start()
    try:
        yield acquired
    finally:
        release.set()
        done.wait(timeout=5)


# --------------------------------------------------------------------------- #
# Grounding check reused from the deterministic graders (no model call)
# --------------------------------------------------------------------------- #
def answer_is_ungrounded(answer: str, evidence: List[dict]) -> bool:
    """True if ``answer`` contains a verifiable number unsupported by evidence.

    Uses the deterministic ``evaluation.metrics.graders.grade_grounding`` — the
    same grader the benchmark uses — so this is a genuine measurement.
    """
    from evaluation.metrics import graders

    ctx = graders.GradeContext(
        final_answer=answer or "",
        tools_called=[],
        tool_call_events=[],
        evidence=evidence or [],
        route=None,
        user_texts=[],
    )
    g = graders.grade_grounding(ctx)
    return (g.contradicted + g.unsupported) > 0
