"""Process-wide event collector for the offline evaluation framework.

Design goals
------------
* **Additive & OFF by default.** Nothing is captured unless
  ``RENTCOMPASS_EVAL=1`` is set *or* the harness enters :func:`capture_run`.
  When inactive every ``record_*`` helper and every wrapper short-circuits on a
  single cheap boolean check, so production behaviour is byte-for-byte unchanged.
* **Never logs secrets.** Only token *counts*, model names, hashes of tool args
  (never the raw args, which may contain PII), latencies and booleans are
  written. API keys/tokens are never read here.
* **Thread-safe append.** Memory writes happen on a background thread, so the
  JSONL sink guards every append with a lock.

Events are tagged with the active ``run_id``/``case_id``/``config`` via
``contextvars`` (mirrors :mod:`uk_rent_agent.observability`'s ``request_context``).

Event types & their type-specific fields
-----------------------------------------
* ``llm_call``     : provider, model, purpose, input_tokens, output_tokens,
                     cached_tokens, latency_ms, success, retry_count, error
* ``tool_call``    : tool, success, execution_time_ms, retry_count, timeout,
                     empty_result, schema_validation_failure, args_hash, mcp
* ``node_span``    : node, latency_ms, error
* ``critic_verdict``: stage, grounded, issues, critic_attempts
* ``turn``         : route, response_type, critic_attempts, verdict, latency_ms
* ``tool_budget_timeout``: tool, phase, budget_s, elapsed_ms, outcome
* ``turn_soft_wrap``: elapsed_ms, llm_calls, tool_batches
* ``specialist_task``: plan_id, task_id, parent_task_id, role, status,
                       duration_ms, call_count

Every event additionally carries ``type``, ``ts_monotonic`` (``perf_counter``),
``ts_wall`` (epoch seconds) and the ``run_id``/``case_id``/``config`` tags.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import math
import os
import re
import threading
import time
from typing import Any, Iterator, Optional

# --------------------------------------------------------------------------- #
# Activation state
# --------------------------------------------------------------------------- #
# Cheap module-level env snapshot; the context var lets the harness activate
# capture for a scoped block even when the env flag is unset.
_ENV_ACTIVE = os.getenv("RENTCOMPASS_EVAL", "").strip() in {"1", "true", "True", "yes"}

_active_var: contextvars.ContextVar[bool] = contextvars.ContextVar("rc_eval_active", default=False)
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("rc_eval_run_id", default="-")
case_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("rc_eval_case_id", default="-")
config_var: contextvars.ContextVar[str] = contextvars.ContextVar("rc_eval_config", default="-")
_log_path_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("rc_eval_log_path", default=None)

_AGENT_CONTEXT_FIELDS = ("agent_role", "task_id", "parent_task_id")
_SPECIALIST_ROLES = frozenset({"listings", "mobility", "area_evidence"})
_SPECIALIST_STATUSES = frozenset(
    {"planned", "started", "completed", "partial", "failed", "skipped"}
)
_SPECIALIST_OUTCOME_STATUSES = frozenset({"partial", "failed", "skipped"})
# A refused dispatch, mirrored from ``turn_observations.note_specialist_call_denied``.
# Not a task transition: it carries a tool name and a code, no task identity — which
# is why it is deliberately OUTSIDE _SPECIALIST_STATUSES above rather than added to
# it. Putting it there would let a caller emit a task-shaped event with
# status="denied", and `canary_report._validate_specialist` rejects exactly that
# (a denied event must be {status, tool, error_code} and nothing else). The union
# below is the full set this function accepts and is what `record_specialist_lifecycle`
# validates against, so the two shapes' vocabularies live in exactly one place.
_SPECIALIST_DENIED_STATUS = "denied"
_SPECIALIST_EVENT_STATUSES = _SPECIALIST_STATUSES | {_SPECIALIST_DENIED_STATUS}
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
# Same closed grammars as the turn accumulator, re-checked here rather than
# trusted: this is a PUBLIC boundary, so a direct caller must not be able to
# smuggle an error message or a tool argument past it.
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")

DEFAULT_LOG_PATH = os.path.join("evaluation", "results", "events.jsonl")


def is_active() -> bool:
    """Return whether eval capture is currently active. Cheap; call freely."""
    return _ENV_ACTIVE or _active_var.get()


def _refresh_env_flag() -> None:
    """Re-read the env flag (used by tests that toggle ``RENTCOMPASS_EVAL``)."""
    global _ENV_ACTIVE
    _ENV_ACTIVE = os.getenv("RENTCOMPASS_EVAL", "").strip() in {"1", "true", "True", "yes"}


# --------------------------------------------------------------------------- #
# JSONL sink (thread-safe append)
# --------------------------------------------------------------------------- #
class EventSink:
    """Appends structured events as JSON lines to ``path`` under a lock."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def emit(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


_sink_lock = threading.Lock()
_sink: Optional[EventSink] = None


def _resolve_log_path() -> str:
    return _log_path_var.get() or os.getenv("RENTCOMPASS_EVAL_LOG") or DEFAULT_LOG_PATH


def _get_sink() -> EventSink:
    global _sink
    path = _resolve_log_path()
    with _sink_lock:
        if _sink is None or _sink.path != path:
            _sink = EventSink(path)
        return _sink


def reset_sink() -> None:
    """Drop the cached sink (test hook so a new log path takes effect)."""
    global _sink
    with _sink_lock:
        _sink = None


# --------------------------------------------------------------------------- #
# Run scoping
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def capture_run(
    run_id: str,
    case_id: str = "-",
    config_name: str = "-",
    *,
    log_path: Optional[str] = None,
) -> Iterator[None]:
    """Activate capture and tag all events emitted inside the block.

    ``log_path`` overrides ``RENTCOMPASS_EVAL_LOG``/the default for this run
    (handy for tests that want an isolated file).
    """
    tokens = [
        _active_var.set(True),
        run_id_var.set(str(run_id)),
        case_id_var.set(str(case_id)),
        config_var.set(str(config_name)),
    ]
    path_token = _log_path_var.set(log_path) if log_path is not None else None
    if log_path is not None:
        reset_sink()
    try:
        yield
    finally:
        if path_token is not None:
            _log_path_var.reset(path_token)
            reset_sink()
        for var, tok in zip((_active_var, run_id_var, case_id_var, config_var), tokens):
            var.reset(tok)


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #
def _current_agent_context() -> dict[str, str]:
    """Read the shared manager/specialist trace context without making capture brittle."""
    try:
        from uk_rent_agent.observability import current_agent_context

        return current_agent_context()
    except Exception:
        return {}


def _normalise_agent_context(value: Optional[dict]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: str(value[key])
        for key in _AGENT_CONTEXT_FIELDS
        if value.get(key) is not None
    }


def _emit(event_type: str, fields: dict, *, agent_context: Optional[dict] = None) -> None:
    if not is_active():
        return
    event = {
        "type": event_type,
        "ts_monotonic": time.perf_counter(),
        "ts_wall": time.time(),
        "run_id": run_id_var.get(),
        "case_id": case_id_var.get(),
        "config": config_var.get(),
    }
    event.update(fields)
    # ``None`` means capture the context at emission time.  An explicit empty
    # dict means a callback started outside an agent scope and must not be
    # misattributed to whichever scope happens to be active when it ends.
    event.update(
        _current_agent_context()
        if agent_context is None
        else _normalise_agent_context(agent_context)
    )
    try:
        _get_sink().emit(event)
    except Exception:
        # Instrumentation must never break the app.
        pass


def record_llm_call(
    *,
    provider: str,
    model: str,
    purpose: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cached_tokens: Optional[int] = None,
    latency_ms: Optional[float] = None,
    success: bool = True,
    retry_count: int = 0,
    error: Optional[str] = None,
    agent_context: Optional[dict] = None,
) -> None:
    _emit(
        "llm_call",
        {
            "provider": provider,
            "model": model,
            "purpose": purpose,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "latency_ms": latency_ms,
            "success": success,
            "retry_count": retry_count,
            "error": error,
        },
        agent_context=agent_context,
    )


def record_specialist_lifecycle(
    *,
    plan_id: Any = None,
    task_id: Any = None,
    parent_task_id: Any = None,
    role: Any = None,
    status: Any = None,
    duration_ms: Any = None,
    call_count: Any = None,
    error_code: Any = None,
    tool: Any = None,
    **_ignored: Any,
) -> None:
    """Emit a content-free specialist lifecycle event when eval capture is active.

    This public boundary validates the whitelist independently of the turn
    accumulator.  Direct callers therefore cannot smuggle task objectives, args,
    result data or arbitrary error text into the JSONL event stream.

    Two shapes are accepted, mirroring ``core.turn_observations``:

    * a task transition — ``planned/started/completed/partial/failed/skipped``,
      with an optional ``error_code`` on the three unsuccessful outcomes;
    * a refused dispatch — ``status="denied"`` carrying ``tool`` + ``error_code``
      and no task identity, because the task it happened inside is still running.
    """
    if not is_active():
        return
    if status == _SPECIALIST_DENIED_STATUS:
        try:
            safe_tool = tool.strip() if isinstance(tool, str) else ""
            safe_code = error_code.strip() if isinstance(error_code, str) else ""
            if not _TOOL_NAME_RE.fullmatch(safe_tool):
                return
            if not _ERROR_CODE_RE.fullmatch(safe_code):
                return
        except Exception:
            return
        _emit(
            "specialist_task",
            {"status": _SPECIALIST_DENIED_STATUS, "tool": safe_tool,
             "error_code": safe_code},
            agent_context={},
        )
        return
    try:
        ids = []
        for value in (plan_id, task_id, parent_task_id):
            candidate = value.strip() if isinstance(value, str) else ""
            if not _MACHINE_ID_RE.fullmatch(candidate):
                return
            ids.append(candidate)
        safe_role = role.strip() if isinstance(role, str) else ""
        # _SPECIALIST_EVENT_STATUSES is the full accepted set; the denied shape was
        # already handled and returned above, so anything left that is not a task
        # transition is rejected here. Checking against the union rather than a
        # second literal keeps the two shapes' vocabularies in one place -- and
        # gives the constant a caller, which it did not have.
        if safe_role not in _SPECIALIST_ROLES or status not in (
            _SPECIALIST_EVENT_STATUSES - {_SPECIALIST_DENIED_STATUS}
        ):
            return
        if (
            isinstance(call_count, bool)
            or not isinstance(call_count, int)
            or call_count < 0
            or call_count > 10_000
        ):
            return
        if duration_ms is None:
            safe_duration = None
        else:
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
                return
            safe_duration = float(duration_ms)
            if not math.isfinite(safe_duration) or safe_duration < 0:
                return
            safe_duration = round(safe_duration, 3)
        if error_code is None:
            safe_error_code = None
        else:
            safe_error_code = error_code.strip() if isinstance(error_code, str) else ""
            if (
                not _ERROR_CODE_RE.fullmatch(safe_error_code)
                or status not in _SPECIALIST_OUTCOME_STATUSES
            ):
                return
        fields = {
            "plan_id": ids[0],
            "task_id": ids[1],
            "parent_task_id": ids[2],
            "role": safe_role,
            "status": status,
            "duration_ms": safe_duration,
            "call_count": call_count,
        }
        if safe_error_code is not None:
            fields["error_code"] = safe_error_code
    except Exception:
        return
    # Explicit empty context: the event already carries its immutable task labels,
    # and must not acquire a later manager/specialist ContextVar by accident.
    _emit("specialist_task", fields, agent_context={})


def _hash_args(kwargs: dict) -> str:
    """Stable, non-reversible digest of tool args (never store raw args: PII)."""
    try:
        redacted = {k: v for k, v in kwargs.items() if not str(k).startswith("_")}
        payload = json.dumps(redacted, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = repr(sorted(kwargs.keys()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _is_empty(data: Any) -> bool:
    if data is None:
        return True
    if isinstance(data, (list, dict, str, tuple, set)):
        return len(data) == 0
    return False


def record_tool_call(name: str, result: Any, kwargs: dict, *, mcp: bool = False) -> None:
    """Emit a ``tool_call`` event from a ToolResult-like object (duck-typed)."""
    success = bool(getattr(result, "success", False))
    error = getattr(result, "error", None)
    error_l = str(error).lower() if error else ""
    _emit(
        "tool_call",
        {
            "tool": name,
            "success": success,
            "execution_time_ms": getattr(result, "execution_time_ms", None),
            # Per-attempt retry count is not exposed on ToolResult; reported as
            # None (undeterminable at this layer) rather than a misleading 0.
            "retry_count": None,
            "timeout": ("timeout" in error_l) or ("timed out" in error_l),
            "empty_result": success and _is_empty(getattr(result, "data", None)),
            "schema_validation_failure": bool(error) and "validationerror" in error_l,
            "args_hash": _hash_args(kwargs),
            "mcp": mcp,
        },
    )


def record_node(node: str, latency_ms: Optional[float], error: Optional[str] = None) -> None:
    _emit("node_span", {"node": node, "latency_ms": latency_ms, "error": error})


def record_tool_budget_timeout(
    *,
    tool: str,
    phase: str,
    budget_s: float,
    elapsed_ms: float,
    outcome: str,
) -> None:
    """Emit a ``tool_budget_timeout`` event when a tool call breaches its wall-clock
    budget inside the fc loop (the loop agent calls this at the kill site).

    ``phase`` is the loop phase the budget applied to (e.g. ``batch``/``per_tool``);
    ``budget_s`` the ceiling in seconds; ``elapsed_ms`` how long it actually took before
    the kill; ``outcome`` what the loop did (``abandoned``/``partial``/``killed``). Carries
    the same run-context tags as :func:`record_tool_call`. No-op when capture is inactive."""
    _emit(
        "tool_budget_timeout",
        {
            "tool": tool,
            "phase": phase,
            "budget_s": budget_s,
            "elapsed_ms": elapsed_ms,
            "outcome": outcome,
        },
    )


def record_turn_soft_wrap(
    *,
    elapsed_ms: float,
    llm_calls: int,
    tool_batches: int,
    wrapped_by: Optional[str] = None,
) -> None:
    """Emit a ``turn_soft_wrap`` event when a turn hits the soft time/step wrap and the
    loop finalises with what it has (rather than running the full budget).

    ``elapsed_ms`` is the turn time at the wrap; ``llm_calls`` / ``tool_batches`` the work
    done so far. ``wrapped_by`` records HOW the turn was closed — ``llm`` / ``llm_retry``
    (model-written) vs ``fallback_timeout`` / ``fallback_error`` (the deterministic canned
    renderer). Without it a soft-wrap count cannot distinguish a turn that still produced a
    real answer from one that emitted boilerplate, which is the distinction a user feels.
    No-op when capture is inactive."""
    _emit(
        "turn_soft_wrap",
        {
            "elapsed_ms": elapsed_ms,
            "llm_calls": llm_calls,
            "tool_batches": tool_batches,
            "wrapped_by": wrapped_by,
        },
    )


def record_critic(*, stage: str, grounded: Optional[bool], issues: Any, critic_attempts: Optional[int]) -> None:
    _emit(
        "critic_verdict",
        {
            "stage": stage,
            "grounded": grounded,
            "issues": issues,
            "critic_attempts": critic_attempts,
        },
    )


def record_turn(
    *,
    route: Any = None,
    response_type: Optional[str] = None,
    critic_attempts: Optional[int] = None,
    verdict: Any = None,
    latency_ms: Optional[float] = None,
) -> None:
    _emit(
        "turn",
        {
            "route": route,
            "response_type": response_type,
            "critic_attempts": critic_attempts,
            "verdict": verdict,
            "latency_ms": latency_ms,
        },
    )


# --------------------------------------------------------------------------- #
# LangChain LLM instrumentation (callback-based; does NOT alter outputs)
# --------------------------------------------------------------------------- #
def _usage_from_llm_result(response: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Pull (input, output, cached) tokens from a LangChain ``LLMResult``.

    Prefers ``llm_output['token_usage']`` (OpenAI/DeepSeek shape, incl. DeepSeek's
    ``prompt_cache_hit_tokens``), falling back to the message ``usage_metadata``.
    """
    input_tokens = output_tokens = cached = None
    llm_output = getattr(response, "llm_output", None) or {}
    tu = llm_output.get("token_usage") or llm_output.get("usage") or {}
    if tu:
        input_tokens = tu.get("prompt_tokens")
        output_tokens = tu.get("completion_tokens")
        cached = tu.get("prompt_cache_hit_tokens")
    if input_tokens is None:
        try:
            gen = response.generations[0][0]
            um = getattr(gen.message, "usage_metadata", None) or {}
            if um:
                input_tokens = um.get("input_tokens")
                output_tokens = um.get("output_tokens")
                details = um.get("input_token_details") or {}
                if cached is None:
                    cached = details.get("cache_read")
        except Exception:
            pass
    return input_tokens, output_tokens, cached


_llm_callback_cls = None


def _get_llm_callback_cls():
    """Build (once) a BaseCallbackHandler subclass that records llm_call events."""
    global _llm_callback_cls
    if _llm_callback_cls is not None:
        return _llm_callback_cls
    from langchain_core.callbacks import BaseCallbackHandler

    class _EvalLLMCallback(BaseCallbackHandler):
        def __init__(self, provider: str, model: str, purpose: Optional[str]):
            self.provider = provider
            self.model = model
            self.purpose = purpose
            self._starts: dict = {}
            self._retries: dict = {}
            self._agent_contexts: dict = {}

        def _start(self, run_id):
            self._starts[run_id] = time.perf_counter()
            # Capture at START.  LangChain may deliver the terminal callback
            # after the specialist scope has unwound or from another context.
            self._agent_contexts[run_id] = _current_agent_context()

        def on_llm_start(self, serialized, prompts, *, run_id=None, **kwargs):  # noqa: D401
            self._start(run_id)

        def on_chat_model_start(self, serialized, messages, *, run_id=None, **kwargs):
            self._start(run_id)

        def on_retry(self, retry_state, *, run_id=None, **kwargs):
            self._retries[run_id] = self._retries.get(run_id, 0) + 1

        def _latency(self, run_id):
            start = self._starts.pop(run_id, None)
            return (time.perf_counter() - start) * 1000 if start is not None else None

        def on_llm_end(self, response, *, run_id=None, **kwargs):
            it, ot, cached = _usage_from_llm_result(response)
            agent_context = self._agent_contexts.pop(run_id, {})
            record_llm_call(
                provider=self.provider,
                model=self.model,
                purpose=self.purpose,
                input_tokens=it,
                output_tokens=ot,
                cached_tokens=cached,
                latency_ms=self._latency(run_id),
                success=True,
                retry_count=self._retries.pop(run_id, 0),
                agent_context=agent_context,
            )

        def on_llm_error(self, error, *, run_id=None, **kwargs):
            agent_context = self._agent_contexts.pop(run_id, {})
            record_llm_call(
                provider=self.provider,
                model=self.model,
                purpose=self.purpose,
                latency_ms=self._latency(run_id),
                success=False,
                retry_count=self._retries.pop(run_id, 0),
                error=type(error).__name__,
                agent_context=agent_context,
            )

    _llm_callback_cls = _EvalLLMCallback
    return _llm_callback_cls


def instrument_chat_model(model: Any, *, provider: str, model_name: str, purpose: Optional[str] = None) -> Any:
    """Attach a recording callback to a LangChain chat model.

    No-op (returns the *same* object untouched) when capture is inactive, so the
    production path pays only a boolean check. Callbacks never alter model output.
    """
    if not is_active():
        return model
    try:
        handler = _get_llm_callback_cls()(provider, model_name, purpose)
        existing = list(getattr(model, "callbacks", None) or [])
        model.callbacks = existing + [handler]
    except Exception:
        # If anything about the model/callback wiring is unexpected, leave the
        # model exactly as-is rather than risk changing behaviour.
        return model
    return model


# --------------------------------------------------------------------------- #
# Graph-node instrumentation (wraps a node callable; body untouched)
# --------------------------------------------------------------------------- #
def instrument_node(name: str, fn, logger=None):
    """Wrap a LangGraph node callable so it emits a ``node_span`` event.

    Uses the existing :func:`uk_rent_agent.observability.node_span` for logging
    when a ``logger`` is supplied, and additionally routes latency/error into the
    collector. Preserves the sync/async nature of the wrapped node.
    """
    if not is_active():
        return fn

    import asyncio

    try:
        from uk_rent_agent.observability import node_span
    except Exception:
        node_span = None

    # interrupt() (HITL) works by raising GraphInterrupt — normal control flow, not a node
    # failure. It must be re-raised untouched and must NOT be recorded as a node error, or
    # every legitimate pause pollutes the offline error metrics.
    try:
        from langgraph.errors import GraphInterrupt
    except Exception:  # pragma: no cover — langgraph is a hard dependency in practice
        class GraphInterrupt(BaseException):
            pass

    def _span_cm():
        if logger is not None and node_span is not None:
            return node_span(logger, name)
        return contextlib.nullcontext()

    if asyncio.iscoroutinefunction(fn):
        async def _async_wrapper(state, *args, **kwargs):
            started = time.perf_counter()
            err = None
            try:
                with _span_cm():
                    return await fn(state, *args, **kwargs)
            except GraphInterrupt:
                raise  # normal HITL pause — recorded below with err=None
            except Exception as exc:
                err = type(exc).__name__
                raise
            finally:
                record_node(name, (time.perf_counter() - started) * 1000, err)

        return _async_wrapper

    def _sync_wrapper(state, *args, **kwargs):
        started = time.perf_counter()
        err = None
        try:
            with _span_cm():
                return fn(state, *args, **kwargs)
        except GraphInterrupt:
            raise  # normal HITL pause — recorded below with err=None
        except Exception as exc:
            err = type(exc).__name__
            raise
        finally:
            record_node(name, (time.perf_counter() - started) * 1000, err)

    return _sync_wrapper
