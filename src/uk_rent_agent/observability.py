from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="-")
agent_role_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_role", default=None
)
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_task_id", default=None
)
parent_task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_parent_task_id", default=None
)

_AGENT_CONTEXT_FIELDS = ("agent_role", "task_id", "parent_task_id")

logger = logging.getLogger(__name__)

# A client-supplied correlation id is UNTRUSTED INPUT. It is echoed into every
# JsonFormatter line, into the canary record's ``request_id``, and (as
# ``turn:<request_id>``) into the manager root task label — so accepting it
# verbatim let a caller write arbitrary text, of arbitrary length, into ops
# telemetry that is read by humans and shipped off-box. The grammar below is the
# same machine-id shape the rest of the trace layer enforces: it admits a uuid
# hex, a dashed uuid, and the ``svc:1234`` forms real proxies emit, and nothing
# that could carry a user's words.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


def new_request_id(value: str | None = None) -> str:
    """Return a safe request id: the client's, only if it is a machine identifier.

    A rejected value is REPLACED, never sanitised — a truncated or stripped copy
    of attacker-controlled text is still attacker-controlled text, and a partial
    match would also break the "one id, one request" correlation the field
    exists for. The rejected value is deliberately never logged: writing it to
    the log to explain why we refused to write it to the log is the whole defect.

    The replacement is a DIGEST of the rejected value, not a fresh uuid4. Both
    callers in ``app.py`` do::

        request_id = new_request_id(request.headers.get("X-Request-Id"))
        prior = conversation_store.get_request_turn(user_id, request_id)

    — an idempotent-replay lookup. A random replacement gave the same client
    retrying the same request a different id every time, so the replay never
    matched and the whole turn re-ran and re-billed. That hit exactly the callers
    who send a well-formed id of a shape this grammar does not admit (AWS X-Ray
    ``Root=1-...``, base64 trace ids, ids longer than 96 chars). SHA-256 is a
    total, deterministic function: it echoes none of the client's text and keeps
    one-id-one-request intact. Only a genuinely absent id gets a fresh uuid4,
    because there is nothing to correlate with.
    """
    if value is not None and _REQUEST_ID_RE.fullmatch(value):
        return value
    if value is not None:
        logger.debug("observability.request_id.replaced client id did not match "
                     "the machine-identifier grammar")
        return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]
    return uuid.uuid4().hex


def pseudonymous_user_ref(value: str | None) -> str:
    """Return a stable log correlation value without emitting the raw identity."""
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return "-"
    secret = (
        os.getenv("LOG_ID_HMAC_KEY", "").strip()
        or os.getenv("CANARY_HMAC_SECRET", "").strip()
        or os.getenv("FLASK_SECRET_KEY", "").strip()
    )
    if secret:
        digest = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    else:
        # User ids are server-generated high-entropy values. Domain separation
        # still prevents the raw value appearing when local development has no key.
        digest = hashlib.sha256(("rentcompass-log-id\0" + raw).encode("utf-8")).hexdigest()
    return digest[:20]


@contextlib.contextmanager
def request_context(request_id: str, user_id: str) -> Iterator[None]:
    request_token = request_id_var.set(request_id)
    user_token = user_id_var.set(user_id)
    try:
        yield
    finally:
        request_id_var.reset(request_token)
        user_id_var.reset(user_token)


def current_agent_context() -> dict[str, str]:
    """Return the active agent/task trace labels.

    Unset values are omitted rather than returned as ``None``.  That makes this
    context additive: telemetry emitted outside a manager/specialist scope keeps
    exactly its historical shape.
    """
    values = {
        "agent_role": agent_role_var.get(),
        "task_id": task_id_var.get(),
        "parent_task_id": parent_task_id_var.get(),
    }
    return {key: value for key, value in values.items() if value is not None}


def bind_current_context(fn: "Callable[..., Any]") -> "Callable[..., Any]":
    """Bind ``fn`` to a SNAPSHOT of the context active at the moment of binding.

    Why this exists
    ---------------
    ``request_id`` is a ContextVar, and a ContextVar reaches a new execution unit
    only if that unit inherits a context. asyncio does this for you —
    ``create_task``/``call_soon``/``run_coroutine_threadsafe`` all capture
    ``copy_context()`` at the call site — which is why every provider call made
    *during* a turn already carries the turn's id.

    Threads and executors do NOT. ``threading.Thread(target=f)`` and
    ``Executor.submit(f)`` start ``f`` in a fresh, empty context, so anything they
    log reports ``request_id: "-"``. In one 8-minute production window 40 of 93
    billed DeepSeek calls looked like that — post-turn critic regeneration,
    on-demand scraping and auto-memory writes — every one of them billed, and none
    of them attributable to the turn that caused it or countable by the release
    gate. This wrapper is the fix: it makes a thread boundary behave like an await
    boundary.

    It snapshots at BIND time, not at call time. That matters: the point of the
    snapshot is to capture the spawning turn's identity, and by the time a queued
    task actually runs, the request that queued it has usually finished and its
    context has been reset.

    ``turn_observations`` stores a MUTABLE dict in its own ContextVar, so a call
    made under a copied context also lands in the originating turn's observation
    window — the id and the accounting travel together, exactly as they do across
    an await.
    """
    snapshot = contextvars.copy_context()

    @functools.wraps(fn)
    def _run(*args: Any, **kwargs: Any):
        # A Context object cannot be entered twice at once ("cannot enter context:
        # ... is already entered"), so running the snapshot directly would make the
        # returned callable single-use and would raise if two workers ever ran it
        # concurrently. `snapshot.run(copy_context)` mints a fresh copy of the
        # snapshot per invocation, which is what makes this safe to hand to a pool.
        return snapshot.run(contextvars.copy_context).run(fn, *args, **kwargs)

    return _run


def spawn_observed_thread(
    target: "Callable[..., Any]",
    *,
    args: tuple = (),
    kwargs: dict | None = None,
    name: str | None = None,
    daemon: bool = True,
) -> threading.Thread:
    """``threading.Thread`` that inherits the caller's observability context.

    A drop-in replacement for ``threading.Thread(target=..., daemon=True).start()``
    at any post-turn spawn site. The thread is started before returning.
    """
    thread = threading.Thread(
        target=bind_current_context(target),
        args=args,
        kwargs=kwargs or {},
        name=name,
        daemon=daemon,
    )
    thread.start()
    return thread


def submit_with_context(executor: Any, fn: "Callable[..., Any]", *args: Any, **kwargs: Any):
    """``executor.submit`` that carries the caller's observability context in."""
    return executor.submit(bind_current_context(fn), *args, **kwargs)


@contextlib.contextmanager
def agent_execution_context(
    *,
    agent_role: str,
    task_id: str,
    parent_task_id: str | None = None,
) -> Iterator[None]:
    """Scope telemetry to one manager or specialist task.

    ContextVars isolate sibling asyncio tasks while still propagating through
    normal awaits.  The tool offload boundary explicitly copies the current
    context, so the same labels also reach worker-thread tool observations.
    The identifiers are generated trace labels; callers must never place user
    text, task descriptions, prompts, or other PII in them.
    """
    role_token = agent_role_var.set(str(agent_role))
    task_token = task_id_var.set(str(task_id))
    parent_token = parent_task_id_var.set(
        str(parent_task_id) if parent_task_id is not None else None
    )
    try:
        yield
    finally:
        parent_task_id_var.reset(parent_token)
        task_id_var.reset(task_token)
        agent_role_var.reset(role_token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "user_ref": pseudonymous_user_ref(
                getattr(record, "user_id", user_id_var.get())
            ),
        }
        # error_type is an exception CLASS name: "agent.turn.execution_failed" was logged
        # without it, so a deterministic RecursionError looked like an unknown 502 (R2-12).
        for key in ("node", "tool", "latency_ms", "cache_hit", "input_tokens", "output_tokens",
                    "error_type"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        active_agent_context = current_agent_context()
        for key in _AGENT_CONTEXT_FIELDS:
            value = getattr(record, key, active_agent_context.get(key))
            if value is not None:
                data[key] = value
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


try:
    # interrupt() (LangGraph HITL) signals a pause by raising GraphInterrupt; it is control
    # flow, not an error, and must not be logged with an ERROR-level traceback.
    from langgraph.errors import GraphInterrupt as _GraphInterrupt
except Exception:  # pragma: no cover — keeps this module importable without langgraph
    class _GraphInterrupt(BaseException):
        pass


@contextlib.contextmanager
def node_span(logger: logging.Logger, node: str, **attributes: Any) -> Iterator[None]:
    """Local structured span; upgrades to OTel without changing node call sites."""
    started = time.perf_counter()
    span_attributes = {**current_agent_context(), **attributes}
    logger.info("node.start", extra={"node": node, **span_attributes})
    try:
        yield
    except _GraphInterrupt:
        logger.info(
            "node.interrupt",
            extra={"node": node, "latency_ms": (time.perf_counter() - started) * 1000, **span_attributes},
        )
        raise
    except Exception:
        logger.exception(
            "node.error",
            extra={"node": node, "latency_ms": (time.perf_counter() - started) * 1000, **span_attributes},
        )
        raise
    else:
        logger.info(
            "node.end",
            extra={"node": node, "latency_ms": (time.perf_counter() - started) * 1000, **span_attributes},
        )
