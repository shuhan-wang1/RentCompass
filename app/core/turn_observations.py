"""Per-turn LLM observations that must survive a crash.

Why this exists
---------------
The canary record is assembled in ``app.py`` AFTER the agent graph returns. That
works for anything the graph puts on ``final_state`` — but a turn that *crashes*
never returns a final_state, and a turn that dies at the response boundary never
even gets that far. Those are precisely the turns whose provider errors matter
most: a strict-schema 400 is a plausible CAUSE of the crash, so reporting 0 there
would suppress exactly the signal the gate exists to catch (see the note in
``canary_telemetry.unknown_turn_signals``).

So observations are accumulated here, in a ContextVar, as they happen.

The ContextVar holds a MUTABLE dict that ``begin_turn()`` installs once at the top
of the request. Callers mutate that dict; they never re-``set()`` the var. This
matters: LangGraph runs nodes as tasks (and sync nodes via an executor), and a
child context is a *copy* — a ``set()`` inside a node would be invisible to the
request handler that has to read the value back. A copied context still points at
the SAME dict object, so mutation propagates in every direction. The one thing it
cannot survive is a context that was never copied at all, which is why
``test_turn_observations.py`` exercises the real graphs rather than trusting this
paragraph.

Fail-closed
-----------
"No observations recorded" and "observed, none seen" are different facts and must
never collapse into the same number. If the observer was never installed — a bad
import, a refactor that bypasses ``ModelRouter.create`` — ``snapshot()`` reports
``None`` for every counter, which holds the gate. Only an installed observer can
produce a 0, and only then does 0 mean "we looked and there were none".
"""
from __future__ import annotations

import contextvars
import math
import re
import threading
from collections import deque
from typing import Any, Dict, Optional

# Set once per request by begin_turn(). Default None => "no turn in progress",
# which is not the same as "a turn that saw nothing".
_turn_obs: contextvars.ContextVar = contextvars.ContextVar("canary_turn_obs", default=None)

_AGENT_CONTEXT_FIELDS = ("agent_role", "task_id", "parent_task_id")

# Specialist lifecycle telemetry is intentionally a much smaller surface than the
# task contracts themselves.  In particular, objectives, tool args/results and
# arbitrary error text are not accepted by this module, so they cannot accidentally
# become operations telemetry.  Identifiers follow the same machine-id grammar as
# ``specialist_contracts.Identifier``.
_SPECIALIST_ROLES = frozenset({"listings", "mobility", "area_evidence"})
# ``partial`` is a TERMINAL outcome, not a degraded ``completed``: the task
# produced usable output AND left some of its objective unmet. Folding it into
# either neighbour is the mistake this set exists to prevent — scored as
# completed it hides a systematic shortfall, scored as failed it would trip the
# specialist failure-rate gate on turns that answered the user perfectly well.
_SPECIALIST_STATUSES = frozenset(
    {"planned", "started", "completed", "partial", "failed", "skipped"}
)
_SPECIALIST_TERMINAL_STATUSES = frozenset(
    {"completed", "partial", "failed", "skipped"}
)
# Statuses that may carry an ``error_code``. ``planned``/``started`` cannot: there
# is no outcome yet to explain, and accepting one there would let a producer
# attach a reason to a transition that has not happened.
_SPECIALIST_OUTCOME_STATUSES = frozenset({"partial", "failed", "skipped"})
# THE canonical closed vocabulary for a specialist lifecycle outcome code.
#
# It lives here, and only here, because it used to live in TWO places that never
# referenced each other: ``agent_loop._SPECIALIST_ERROR_CODES`` (the producer's
# filter) and ``turn_observations._ERROR_CODE_RE`` (the grammar). They agreed by
# coincidence, and the day someone added ``"tool-error"`` / ``"TIMEOUT"`` to the
# producer's copy the grammar would have rejected it — silently, and (before this
# revision) by dropping the whole terminal transition. Import THIS name; do not
# re-declare the set.
SPECIALIST_ERROR_CODES = frozenset({
    "dispatch_denied", "tool_error", "timeout", "abandoned",
    "budget_exhausted", "cancelled", "ledger_invalid", "incomplete",
})
# A denied CALL is not a task transition — it is one dispatch the policy refused
# inside an already-started task — so it lives in its own bounded event ring
# under its own status and never enters the lifecycle arithmetic.
_SPECIALIST_DENIED_STATUS = "denied"
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
# Deliberately NARROWER than _MACHINE_ID_RE: an error code is a closed vocabulary
# chosen by the dispatcher, so lowercase snake_case is the whole grammar. Anything
# else is either a typo or free text, and free text is exactly what this module
# refuses to carry.
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Tool names are python identifiers in the registry; no separators, no user text.
# Shape only — membership in the LIVE registry is checked separately, because the
# only producer of a denied event derives the name from the MODEL's plan, so a
# shape check alone would let a 128-character model-chosen identifier into ops
# telemetry. See ``_registered_tool_names``.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
# The stand-in recorded when a denied dispatch names a tool that is not in the
# registry or the specialist allowlist. It is a FIXED string, so the denial is
# still counted and still visible without the model choosing the label.
UNREGISTERED_TOOL = "unregistered"
_MAX_SPECIALIST_EVENTS = 64
# Denied dispatches get their OWN small ring. They used to share the 64-slot
# lifecycle ring, so 64 refusals inside one turn evicted every plan/start/finish
# event — deleting exactly the detail ("which specialist task broke?") that the
# record exists to answer, while the denial storm itself is already summarised by
# the ``denied_calls`` counter.
_MAX_DENIED_EVENTS = 8
_MAX_SPECIALIST_TASKS_PER_TURN = 128
_MAX_SPECIALIST_CALLS_PER_TASK = 10_000

# Module-level, deliberately NOT per-turn: the LLM client is built once and
# memoized, long before any request. If installation ever fails we must report
# null (hold the gate), not 0 (assert a clean observation we never made).
_observer_installed = False


# Arches whose tool-execution node carries the write-audit instrumentation. Set at
# import of the arch module. This proves the instrumented FILE is loaded in this
# process; it does not by itself prove the write branch ran, which is why the audit
# records themselves are what produce a count (see write_audit_snapshot).
_write_auditors: set = set()


def observer_installed() -> bool:
    return _observer_installed


def _mark_observer_installed() -> None:
    global _observer_installed
    _observer_installed = True


def _current_agent_context() -> Dict[str, str]:
    try:
        from uk_rent_agent.observability import current_agent_context

        return current_agent_context()
    except Exception:
        return {}


def _normalise_agent_context(value: Optional[Dict[str, Any]]) -> Dict[str, str]:
    # ``None`` means read the live context.  An explicit empty dict is a real
    # captured value and prevents an end callback being attributed to a later
    # manager/specialist scope.
    source = _current_agent_context() if value is None else value
    if not isinstance(source, dict):
        return {}
    return {
        key: str(source[key])
        for key in _AGENT_CONTEXT_FIELDS
        if source.get(key) is not None
    }


def register_write_auditor(arch: str) -> None:
    """Declare that ``arch``'s tool-execution path records write decisions.

    Called at import of the arch module. Without it, ``write_audit_snapshot``
    reports null for every security counter — an arch whose instrumentation failed
    to load must HOLD the gate, never report a clean 0 derived from the absence of
    records it was never able to write.
    """
    _write_auditors.add(arch)


def begin_turn() -> Dict[str, Any]:
    """Start a fresh observation window for this request. Returns the live dict."""
    obs: Dict[str, Any] = {
        # Provider-side rejections of a request that carried tool/function schemas.
        "provider_schema_400": 0,
        # Provider 400s on calls with NO schemas bound — a different failure (bad
        # params, context length). Counted separately so it can never inflate the
        # zero-tolerance metric.
        "provider_other_400": 0,
        # Everything else the provider refused, kept for forensics only.
        "provider_error_count": 0,
        # Bounded ring of recent classifications; diagnostics, never gated on.
        "provider_errors": [],
        # One entry per completed LLM run, in the shape aggregate_llm_usage expects.
        "llm_usage_calls": [],
        # run_ids already accounted for. LangChain can deliver more than one
        # terminal callback for a run (retries, nested runnables); counting the same
        # run twice would double the turn's reported spend.
        "llm_runs_seen": set(),
        # Runs that finished but reported no usage at all. These are the reason
        # llm_usage_status exists: a call whose tokens we failed to observe is not
        # a call that cost nothing.
        "llm_usage_missing": 0,
        # audit_key -> one write-audit record. Keyed so a tool_call that is
        # classified once and dispatched later yields a SINGLE record.
        "write_audit": {},
        # Tool-call markup caught before it could reach a user-visible surface.
        "dsml_blocked": 0,
        # Markup that survived every in-band guard and was only stopped at the
        # response boundary. Zero-tolerance: it means the primary control failed.
        "dsml_leak": 0,
        # Optional manager/root identity for the turn-level canary record.  It is
        # separate from per-call context because a deterministic zero-LLM turn
        # still needs an attributable root task.
        "root_agent_context": None,
        # Mutable and shared across copied ContextVars, just like the other turn
        # accumulators.  Tool calls can finish on worker threads, so all compound
        # lifecycle transitions are protected by the same per-turn lock.
        "_specialist_trace": {
            "lock": threading.RLock(),
            "events": deque(maxlen=_MAX_SPECIALIST_EVENTS),
            "events_total": 0,
            # Separate ring + separate total: a denial storm must not be able to
            # push the lifecycle events out of the record (see _MAX_DENIED_EVENTS).
            "denied_events": deque(maxlen=_MAX_DENIED_EVENTS),
            "denied_events_total": 0,
            "tasks": {},
            "planned": 0,
            "started": 0,
            "completed": 0,
            "partial": 0,
            "failed": 0,
            "skipped": 0,
            # Dispatches the specialist policy refused. Non-gating diagnostics:
            # a denial is the control WORKING, so it must never be summed into
            # the failure counters the release gate reads.
            "denied_calls": 0,
            # Terminal transitions whose ``error_code`` was outside the closed
            # vocabulary and was therefore DROPPED. The transition itself is still
            # recorded (see note_specialist_event); this counter is what keeps that
            # from being a silent loss of diagnosis.
            "dropped_error_codes": 0,
            "in_flight": 0,
            "max_in_flight": 0,
            "observed": False,
        },
    }
    _turn_obs.set(obs)
    return obs


def current() -> Optional[Dict[str, Any]]:
    return _turn_obs.get()


def end_turn() -> None:
    """Drop the window. Not strictly required (a ContextVar dies with the request),
    but explicit teardown keeps a leaked reference from being mutated after the
    record was already emitted."""
    _turn_obs.set(None)


def note_root_agent_context(
    *,
    agent_role: str,
    task_id: str,
    parent_task_id: Optional[str] = None,
) -> bool:
    """Attach the manager/root identity once; generated IDs must contain no user text.

    "Must contain no user text" used to be a comment. It is now a CHECK, because
    the only caller derives ``task_id`` from the request id, and the request id
    came from a client header — so the rule the docstring stated was enforced
    nowhere and the label landed verbatim in the canary record and in every
    JsonFormatter line. Each id is validated against the same machine-id grammar
    the specialist events use; a value that fails is not sanitised or truncated,
    it is REFUSED (returns False, records nothing), so a caller cannot end up
    shipping a half-scrubbed copy of whatever it was given.
    """
    obs = _turn_obs.get()
    if obs is None or obs.get("root_agent_context") is not None:
        return False
    safe_role = _machine_identifier(agent_role)
    safe_task_id = _machine_identifier(task_id)
    if not safe_role or not safe_task_id:
        return False
    if parent_task_id is None:
        safe_parent_id = None
    else:
        safe_parent_id = _machine_identifier(parent_task_id)
        if not safe_parent_id:
            return False
    root = {"agent_role": safe_role, "task_id": safe_task_id}
    if safe_parent_id is not None:
        root["parent_task_id"] = safe_parent_id
    obs["root_agent_context"] = root
    return True


# --------------------------------------------------------------------------- #
# Specialist task lifecycle                                                   #
# --------------------------------------------------------------------------- #

def _machine_identifier(value: Any) -> Optional[str]:
    try:
        candidate = value.strip() if isinstance(value, str) else ""
        return candidate if _MACHINE_ID_RE.fullmatch(candidate) else None
    except Exception:
        return None


def _specialist_role(value: Any) -> Optional[str]:
    try:
        candidate = value.strip() if isinstance(value, str) else ""
        return candidate if candidate in _SPECIALIST_ROLES else None
    except Exception:
        return None


def _call_count(value: Any, *, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > _MAX_SPECIALIST_CALLS_PER_TASK:
        return None
    return value


def _error_code(value: Any, *, closed_set: Optional[frozenset] = None) -> Optional[str]:
    """A closed-vocabulary outcome code, or None when it is not one.

    The dispatcher's own error TEXT is never accepted here: a provider message or
    an exception's str() can carry the user's query verbatim, and this record ends
    up in ops telemetry. A code is a label the dispatcher chose, so it is safe by
    construction — provided the vocabulary is actually enforced, which is what this
    is.

    ``closed_set`` (normally :data:`SPECIALIST_ERROR_CODES`) is the primary check;
    the regex stays as a SECOND guard so that a future addition to the set which
    does not fit the snake_case grammar is caught here rather than at the consumer.
    Passing ``None`` checks the grammar only — used by the DENIAL path, whose codes
    come from the dispatch policy (a wider vocabulary than the lifecycle outcome
    set) and are non-gating diagnostics.
    """
    try:
        candidate = value.strip() if isinstance(value, str) else ""
        if not _ERROR_CODE_RE.fullmatch(candidate):
            return None
        if closed_set is not None and candidate not in closed_set:
            return None
        return candidate
    except Exception:
        return None


_dispatchable_tool_names: Optional[frozenset] = None


def _registered_tool_names() -> frozenset:
    """Names a specialist dispatch can legitimately be about, or an empty set.

    The source is the DECLARED contract — the union of every specialist role
    allowlist and the manager-only tools — rather than the live registry object,
    because this must work identically in the web process, in an eval process and
    in a unit test, none of which agree on whether a registry has been built. An
    empty result means "cannot check here", and the caller then falls back to the
    shape check alone rather than discarding a denial it has no way to verify.
    """
    global _dispatchable_tool_names
    if _dispatchable_tool_names is not None:
        return _dispatchable_tool_names
    try:
        from uk_rent_agent.agent.specialist_contracts import (
            MANAGER_ONLY_TOOLS,
            SPECIALIST_TOOL_ALLOWLISTS,
        )

        names = set(MANAGER_ONLY_TOOLS)
        for allowed in SPECIALIST_TOOL_ALLOWLISTS.values():
            names.update(allowed)
        _dispatchable_tool_names = frozenset(str(n) for n in names)
    except Exception:
        _dispatchable_tool_names = frozenset()
    return _dispatchable_tool_names


def _tool_name(value: Any, *, allowlist: Optional[frozenset] = None) -> Optional[str]:
    """Validate a tool identifier for shape and, when known, for EXISTENCE.

    ``allowlist`` is the union of names the process will actually dispatch. When
    it is non-empty a name outside it is not returned verbatim — the caller
    substitutes :data:`UNREGISTERED_TOOL` — because the only producer of this
    value reads it out of the MODEL's plan, and a 128-character model-chosen
    identifier is model output, not a registry identifier.
    """
    try:
        candidate = value.strip() if isinstance(value, str) else ""
        if not _TOOL_NAME_RE.fullmatch(candidate):
            return None
        if allowlist and candidate not in allowlist:
            return None
        return candidate
    except Exception:
        return None


def _duration_ms(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        duration = float(value)
    except Exception:
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    return round(duration, 3)


def _emit_specialist_eval_event(event: Dict[str, Any]) -> None:
    """Mirror an accepted event into the opt-in eval sink, best effort."""
    try:
        from evaluation.metrics import collector

        collector.record_specialist_lifecycle(**event)
    except Exception:
        pass


def note_specialist_event(
    status: Any = None,
    *,
    plan_id: Any = None,
    task_id: Any = None,
    parent_task_id: Any = None,
    role: Any = None,
    duration_ms: Any = None,
    call_count: Any = None,
    error_code: Any = None,
    **_ignored: Any,
) -> bool:
    """Record one content-free specialist lifecycle transition.

    This is an instrumentation boundary, so malformed values, out-of-order or
    duplicate transitions, and unexpected keyword arguments are silent no-ops.
    It must never become a reason a user turn fails.

    ``error_code`` is optional and only meaningful on an unsuccessful outcome
    (``partial`` / ``failed`` / ``skipped``). It is a code from
    :data:`SPECIALIST_ERROR_CODES`, never an error message — see ``_error_code``.
    An unusable code DEGRADES the event (the code is dropped, the transition is
    still recorded, ``dropped_error_codes`` is incremented). It used to reject the
    whole call: the counters then stopped at ``started`` while the producer
    believed the task had finished, and the consumer's turn-end invariant
    ``started == completed + partial + failed`` failed — a spurious
    INSTRUMENTATION-HOLD caused by a diagnostics typo. Losing the reason for one
    failure is a diagnostic gap; losing the failure itself is a wrong gate verdict.
    """
    event: Optional[Dict[str, Any]] = None
    dropped_code = False
    try:
        obs = _turn_obs.get()
        trace = obs.get("_specialist_trace") if isinstance(obs, dict) else None
        if not isinstance(trace, dict) or status not in _SPECIALIST_STATUSES:
            return False

        safe_plan_id = _machine_identifier(plan_id)
        safe_task_id = _machine_identifier(task_id)
        safe_parent_id = _machine_identifier(parent_task_id)
        safe_role = _specialist_role(role)
        if not all((safe_plan_id, safe_task_id, safe_parent_id, safe_role)):
            return False

        # A supplied malformed count/duration is DROPPED, never coerced: surprising
        # objects and non-finite floats must not reach JSON logs. Dropping the value
        # rather than the event is the same rule as ``error_code`` below — a bad
        # measurement is a diagnostics bug, and refusing the transition over it turns
        # that into a lifecycle-imbalance HOLD. ``call_count`` then falls back to the
        # task's running count and ``duration_ms`` to null, both of which the record
        # already allows.
        safe_calls = _call_count(call_count)
        safe_duration = _duration_ms(duration_ms)
        safe_error_code = _error_code(error_code, closed_set=SPECIALIST_ERROR_CODES)
        if error_code is not None and (
            safe_error_code is None or status not in _SPECIALIST_OUTCOME_STATUSES
        ):
            # Degrade, never reject. An out-of-vocabulary code, or a code attached
            # to a status that has no outcome to explain, is a producer bug in the
            # DIAGNOSTIC — it says nothing about whether the transition happened.
            # The transition is therefore recorded without the code and the loss is
            # counted, so "we dropped a code" is visible in the record instead of
            # being indistinguishable from "the task never finished".
            safe_error_code = None
            dropped_code = True

        lock = trace.get("lock")
        if lock is None:
            return False
        with lock:
            tasks = trace.get("tasks")
            if not isinstance(tasks, dict):
                return False
            key = (safe_plan_id, safe_task_id)
            task = tasks.get(key)
            if task is None:
                if len(tasks) >= _MAX_SPECIALIST_TASKS_PER_TURN:
                    return False
                task = {
                    "parent_task_id": safe_parent_id,
                    "role": safe_role,
                    "call_count": safe_calls if safe_calls is not None else 0,
                    "seen": set(),
                    "active": False,
                    "terminal": False,
                }
                tasks[key] = task
            elif (
                task.get("parent_task_id") != safe_parent_id
                or task.get("role") != safe_role
            ):
                # The identity of a task is immutable for the duration of the turn.
                return False

            seen = task.get("seen")
            if not isinstance(seen, set) or status in seen:
                return False
            if task.get("terminal"):
                return False
            if status == "planned" and "started" in seen:
                return False

            if safe_calls is None:
                safe_calls = int(task.get("call_count", 0))
            else:
                task["call_count"] = safe_calls

            if status == "started":
                trace["started"] = int(trace.get("started", 0)) + 1
                if not task.get("active"):
                    task["active"] = True
                    trace["in_flight"] = int(trace.get("in_flight", 0)) + 1
                    trace["max_in_flight"] = max(
                        int(trace.get("max_in_flight", 0)),
                        int(trace.get("in_flight", 0)),
                    )
            elif status == "planned":
                trace["planned"] = int(trace.get("planned", 0)) + 1
            else:
                trace[status] = int(trace.get(status, 0)) + 1
                task["terminal"] = True
                if task.get("active"):
                    task["active"] = False
                    trace["in_flight"] = max(
                        0, int(trace.get("in_flight", 0)) - 1
                    )

            seen.add(status)
            trace["observed"] = True
            event = {
                "plan_id": safe_plan_id,
                "task_id": safe_task_id,
                "parent_task_id": safe_parent_id,
                "role": safe_role,
                "status": status,
                "duration_ms": (
                    safe_duration if status in _SPECIALIST_TERMINAL_STATUSES else None
                ),
                "call_count": safe_calls,
            }
            # Additive: an event without a code keeps the exact historical shape,
            # so a consumer that predates this key is unaffected.
            if safe_error_code is not None:
                event["error_code"] = safe_error_code
            if dropped_code:
                trace["dropped_error_codes"] = (
                    int(trace.get("dropped_error_codes", 0)) + 1
                )
            trace["events_total"] = int(trace.get("events_total", 0)) + 1
            trace["events"].append(event)
    except Exception:
        return False

    if event is not None:
        _emit_specialist_eval_event(dict(event))
        return True
    return False


def note_specialist_plan(**fields: Any) -> bool:
    return note_specialist_event("planned", **fields)


def note_specialist_start(**fields: Any) -> bool:
    return note_specialist_event("started", **fields)


def note_specialist_complete(**fields: Any) -> bool:
    return note_specialist_event("completed", **fields)


def note_specialist_fail(**fields: Any) -> bool:
    return note_specialist_event("failed", **fields)


def note_specialist_partial(**fields: Any) -> bool:
    return note_specialist_event("partial", **fields)


def note_specialist_skip(**fields: Any) -> bool:
    return note_specialist_event("skipped", **fields)


def note_specialist_call_denied(*, tool: Any, error_code: Any) -> bool:
    """Record one specialist tool dispatch the policy refused. Returns True if stored.

    Deliberately NOT a lifecycle transition. The task it happened inside is still
    running and will still reach its own terminal status, so counting a denial as
    a task outcome would double-count the task and unbalance the turn-end
    invariants the gate checks. It is also never summed into ``failed``: a denial
    is the control working as designed, and a release gate that treats "we blocked
    a forbidden call" as a regression teaches operators to disable the control.

    Both arguments are validated against closed grammars so no argument value,
    objective or provider message can reach the record through this door. ``tool``
    additionally has to EXIST: the only producer reads the name out of the model's
    own plan (``agent_loop``: ``plan[i][0].get("name")``), so a shape check alone
    let a 128-character model-chosen identifier into ops telemetry. A name outside
    the dispatchable set is replaced by the fixed :data:`UNREGISTERED_TOOL` — the
    denial is still counted and still visible, but the label is ours.

    ``error_code`` here is the DISPATCH POLICY's refusal code, a wider vocabulary
    than the lifecycle outcome set (:data:`SPECIALIST_ERROR_CODES`), so only the
    grammar is enforced. It is non-gating diagnostics either way.
    """
    try:
        obs = _turn_obs.get()
        trace = obs.get("_specialist_trace") if isinstance(obs, dict) else None
        if not isinstance(trace, dict):
            return False
        allowlist = _registered_tool_names()
        safe_tool = _tool_name(tool, allowlist=allowlist)
        if safe_tool is None and _tool_name(tool) is not None:
            # Correct shape, unknown name: record THAT, not the model's string.
            safe_tool = UNREGISTERED_TOOL
        safe_code = _error_code(error_code)
        if not safe_tool or not safe_code:
            return False
        lock = trace.get("lock")
        if lock is None:
            return False
        with lock:
            trace["denied_calls"] = int(trace.get("denied_calls", 0)) + 1
            trace["observed"] = True
            trace["denied_events_total"] = (
                int(trace.get("denied_events_total", 0)) + 1
            )
            # Its OWN bounded ring, not the lifecycle one: a denial storm must
            # neither grow the record without limit nor evict the plan/start/finish
            # events that say which specialist task broke.
            event = {
                "status": _SPECIALIST_DENIED_STATUS,
                "tool": safe_tool,
                "error_code": safe_code,
            }
            trace["denied_events"].append(event)
    except Exception:
        return False
    _emit_specialist_eval_event(dict(event))
    return True


def specialist_snapshot() -> Optional[Dict[str, Any]]:
    """Return the bounded per-turn projection, or ``None`` without a window."""
    try:
        obs = _turn_obs.get()
        trace = obs.get("_specialist_trace") if isinstance(obs, dict) else None
        if not isinstance(trace, dict) or trace.get("lock") is None:
            return None
        with trace["lock"]:
            lifecycle = list(trace.get("events", ()))
            denied = list(trace.get("denied_events", ()))
            truncated = (
                int(trace.get("events_total", 0)) > len(lifecycle)
                or int(trace.get("denied_events_total", 0)) > len(denied)
            )
            return {
                "planned": int(trace.get("planned", 0)),
                "started": int(trace.get("started", 0)),
                "completed": int(trace.get("completed", 0)),
                "partial": int(trace.get("partial", 0)),
                "failed": int(trace.get("failed", 0)),
                "skipped": int(trace.get("skipped", 0)),
                "denied_calls": int(trace.get("denied_calls", 0)),
                "dropped_error_codes": int(trace.get("dropped_error_codes", 0)),
                "max_in_flight": int(trace.get("max_in_flight", 0)),
                # ONE flag for two rings. Either ring overflowing disables the
                # consumer's events-vs-counters reconciliation, which is the
                # conservative reading: the counters stay authoritative.
                "events_truncated": truncated,
                # Lifecycle first, then denials. The two streams are validated by
                # different rules and never reconciled against each other, so the
                # concatenation order carries no meaning the consumer relies on.
                "events": (
                    [dict(item) for item in lifecycle]
                    + [dict(item) for item in denied]
                ),
            }
    except Exception:
        return None


def _specialist_was_observed() -> bool:
    try:
        obs = _turn_obs.get()
        trace = obs.get("_specialist_trace") if isinstance(obs, dict) else None
        if not isinstance(trace, dict) or trace.get("lock") is None:
            return False
        with trace["lock"]:
            return bool(trace.get("observed"))
    except Exception:
        return False


# llm_usage_status values.
USAGE_COMPLETE = "complete"                  # every observed run reported its tokens
USAGE_PARTIAL = "partial"                    # >=1 run finished with no usage — HOLD
USAGE_NO_CALLS = "no_llm_calls"              # the turn made none (e.g. search_direct)
USAGE_NOT_INSTRUMENTED = "not_instrumented"  # no observer / no window — HOLD


def snapshot() -> Dict[str, Any]:
    """The observed counters, or all-None when nothing could have been observed.

    None is the honest answer in two cases: no observer was installed, or no turn
    window was opened. Both mean the gate must HOLD rather than read a fabricated 0.
    """
    obs = _turn_obs.get()
    if obs is None or not _observer_installed:
        result = {"provider_schema_400_count": None, "provider_other_400_count": None,
                  "llm_usage_calls": None, "llm_calls": None,
                  "llm_usage_status": USAGE_NOT_INSTRUMENTED}
        if obs is not None and obs.get("root_agent_context"):
            result["root_agent_context"] = dict(obs["root_agent_context"])
        if _specialist_was_observed():
            result["specialist"] = specialist_snapshot()
        return result
    calls = list(obs.get("llm_usage_calls") or [])
    missing = int(obs.get("llm_usage_missing", 0))
    if missing:
        # A run happened and we could not price it. Reporting the other runs' totals
        # as if they were the turn's totals would understate spend by an unknown
        # amount, which is worse than refusing to answer.
        status = USAGE_PARTIAL
    elif calls:
        status = USAGE_COMPLETE
    else:
        status = USAGE_NO_CALLS
    result = {
        "provider_schema_400_count": int(obs.get("provider_schema_400", 0)),
        "provider_other_400_count": int(obs.get("provider_other_400", 0)),
        "llm_usage_calls": calls,
        # Includes completed calls whose provider response omitted token usage.
        # llm_runs_seen is de-duplicated at the callback boundary, so this is the
        # billed-call denominator even when llm_usage_status is partial.
        "llm_calls": len(obs.get("llm_runs_seen") or ()),
        "llm_usage_status": status,
    }
    if obs.get("root_agent_context"):
        result["root_agent_context"] = dict(obs["root_agent_context"])
    if _specialist_was_observed():
        result["specialist"] = specialist_snapshot()
    return result


# --------------------------------------------------------------------------- #
# Provider error classification                                               #
# --------------------------------------------------------------------------- #

def _status_of(exc: Any) -> Optional[int]:
    """The HTTP status a provider exception carries, or None.

    Duck-typed rather than importing openai: this module is imported by the request
    path and must not drag in a provider SDK, and LangChain may wrap or re-raise the
    error as a different class. Both the openai-SDK shape (``status_code``) and the
    generic ``response.status_code`` shape are handled.
    """
    for attr in ("status_code", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def note_provider_error(exc: Any, *, schemas_bound: bool,
                        agent_context: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Classify and record one provider-side failure. Returns the bucket, or None.

    Classification is STRUCTURAL — the HTTP status the provider returned, and
    whether WE bound tool schemas on the request. It deliberately does not parse the
    provider's prose: error copy is not an API, it varies by model and endpoint, and
    a gate that silently stops matching when a vendor rewrites a sentence is worse
    than no gate. The cost of this choice is that a non-schema 400 on a
    schemas-bound call is counted as a schema 400; that direction is the safe one.
    """
    obs = _turn_obs.get()
    if obs is None:
        return None
    obs["provider_error_count"] = obs.get("provider_error_count", 0) + 1
    status = _status_of(exc)
    bucket = None
    if status == 400:
        bucket = "schema_400" if schemas_bound else "other_400"
        key = "provider_schema_400" if schemas_bound else "provider_other_400"
        obs[key] = obs.get(key, 0) + 1
    errors = obs.setdefault("provider_errors", [])
    if len(errors) < 20:  # bounded: a retry storm must not grow the record without limit
        record = {"type": type(exc).__name__, "status": status,
                  "schemas_bound": bool(schemas_bound), "bucket": bucket}
        record.update(_normalise_agent_context(agent_context))
        errors.append(record)
    return bucket


# --------------------------------------------------------------------------- #
# Token usage                                                                 #
# --------------------------------------------------------------------------- #

def _first_generation(response: Any) -> Any:
    try:
        return response.generations[0][0]
    except Exception:
        return None


def _usage_from_usage_metadata(gen: Any):
    """LangChain's canonical, provider-normalised shape."""
    msg = getattr(gen, "message", None)
    um = getattr(msg, "usage_metadata", None) or {}
    if not um:
        return None
    it, ot = um.get("input_tokens"), um.get("output_tokens")
    if it is None and ot is None:
        return None
    cached = (um.get("input_token_details") or {}).get("cache_read")
    return {"input_tokens": it, "output_tokens": ot, "cache_read_tokens": cached}


def _usage_from_token_usage(blob: Any):
    """The raw OpenAI/DeepSeek shape, wherever it is hiding."""
    tu = (blob or {}).get("token_usage") or (blob or {}).get("usage") or {}
    if not tu:
        return None
    it, ot = tu.get("prompt_tokens"), tu.get("completion_tokens")
    if it is None and ot is None:
        return None
    # DeepSeek reports cache hits as a BREAKDOWN of prompt_tokens, not an extra
    # bucket on top of it. Cost must therefore be (prompt - cache_hit) at the full
    # rate plus cache_hit at the cached rate — never prompt + cache_hit, which is
    # the double-count the price table is still held back to verify.
    return {"input_tokens": it, "output_tokens": ot,
            "cache_read_tokens": tu.get("prompt_cache_hit_tokens")}


def extract_usage(response: Any) -> Optional[Dict[str, Any]]:
    """Token usage for one LLM run, from the FIRST source that has it.

    Three shapes carry the same numbers depending on provider and LangChain
    version. They are tried in priority order and the first hit WINS OUTRIGHT —
    they are never merged for the token counts, because the same run's tokens
    appearing in two places is duplication, not extra information, and summing
    them would silently double the turn's reported spend.

    ``cache_read_tokens`` is the one field allowed to fall back to a lower-priority
    source: it is a breakdown OF input_tokens rather than an addition to them, so
    taking it from elsewhere cannot inflate any total.
    """
    gen = _first_generation(response)
    sources = (
        _usage_from_usage_metadata(gen),
        _usage_from_token_usage(getattr(gen, "generation_info", None)
                                or (getattr(getattr(gen, "message", None),
                                            "response_metadata", None) or {})),
        _usage_from_token_usage(getattr(response, "llm_output", None) or {}),
    )
    winner = next((s for s in sources if s), None)
    if winner is None:
        return None
    if winner.get("cache_read_tokens") is None:
        for other in sources:
            if other and other.get("cache_read_tokens") is not None:
                winner["cache_read_tokens"] = other["cache_read_tokens"]
                break
    return winner


def extract_model_name(response: Any) -> Optional[str]:
    """The model the PROVIDER says answered, or None.

    Preferred over the configured route name because they can diverge — an alias
    resolving server-side, a fallback, a silently upgraded snapshot — and cost is
    attributed per model. The configured name is only ever used as a fallback, and
    the record says so via ``model_source``.
    """
    gen = _first_generation(response)
    for blob in (getattr(getattr(gen, "message", None), "response_metadata", None) or {},
                 getattr(gen, "generation_info", None) or {},
                 getattr(response, "llm_output", None) or {}):
        name = blob.get("model_name") or blob.get("model")
        if isinstance(name, str) and name:
            return name
    return None


def note_llm_usage(run_id: Any, response: Any, *, configured_model: Optional[str],
                   agent_context: Optional[Dict[str, Any]] = None) -> bool:
    """Record one completed LLM run. Returns True if it was counted.

    De-duplicated by run_id: LangChain can deliver a terminal callback more than
    once for the same run (retries, nested runnables), and counting a run twice
    would double the turn's reported spend.
    """
    obs = _turn_obs.get()
    if obs is None:
        return False
    seen = obs.setdefault("llm_runs_seen", set())
    if run_id in seen:
        return False
    seen.add(run_id)

    usage = extract_usage(response)
    if usage is None:
        # The call provably happened — we are in its completion callback — but its
        # tokens are unknown. Recording nothing here would let the turn report the
        # remaining calls' totals as if they were the whole turn's.
        obs["llm_usage_missing"] = obs.get("llm_usage_missing", 0) + 1
        return False
    observed_model = extract_model_name(response)
    call = {
        "model": observed_model or configured_model or "unknown",
        "model_source": ("response" if observed_model
                         else "config" if configured_model else "unknown"),
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "cache_read_tokens": usage.get("cache_read_tokens") or 0,
    }
    call.update(_normalise_agent_context(agent_context))
    obs.setdefault("llm_usage_calls", []).append(call)
    return True


def note_raw_llm_call(run_id: Any, *, usage_blob: Any,
                      configured_model: Optional[str],
                      agent_context: Optional[Dict[str, Any]] = None) -> bool:
    """Record one completed LLM run made WITHOUT LangChain.

    ``install_observer`` attaches a LangChain callback, so it can only see models
    built through ``ModelRouter``. Two production paths are not: ``llm_interface``
    drives the raw ``openai`` SDK directly, and ``llm_config._deepseek_llm`` built an
    unobserved ``ChatOpenAI``. Those calls were real, billed, and absent from
    ``llm_calls`` — 48 of them at p50 934ms in the 2026-07-25 round of record — which
    means every per-call figure derived from that counter understated the turn.

    The bypass was not hidden: this module's own sibling documents it in a docstring.
    It was known and simply never wired, the same shape as ``--since`` computing a
    window it did not filter on.

    Takes the provider's usage blob rather than a response object because there is no
    LangChain envelope to unwrap; extraction, de-duplication by run id and the
    missing-usage accounting are the SAME as ``note_llm_usage`` so the two paths
    cannot drift into reporting different things.
    """
    obs = _turn_obs.get()
    if obs is None:
        return False
    # Observing a call IS the proof that calls on this path are observed. Without this,
    # snapshot() returns all-None for a turn whose only LLM work went through the raw
    # SDK -- the record would be taken and then discarded, which is the very defect
    # this function exists to close. In production _wire_canary_llm_observer() sets the
    # flag at startup and would mask it; relying on that would leave the fix depending
    # on an unrelated commit staying in place.
    _mark_observer_installed()
    seen = obs.setdefault("llm_runs_seen", set())
    if run_id in seen:
        return False
    seen.add(run_id)

    usage = _usage_from_token_usage({"usage": usage_blob or {}})
    if usage is None:
        obs["llm_usage_missing"] = obs.get("llm_usage_missing", 0) + 1
        return False
    call = {
        "model": configured_model or "unknown",
        "model_source": "config" if configured_model else "unknown",
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "cache_read_tokens": usage.get("cache_read_tokens") or 0,
    }
    call.update(_normalise_agent_context(agent_context))
    obs.setdefault("llm_usage_calls", []).append(call)
    return True


# --------------------------------------------------------------------------- #
# Tool-markup (DSML) guard counters                                           #
# --------------------------------------------------------------------------- #
#
# Counted here rather than on the graph state for the same reason as everything
# else in this module: the guards run on paths that can end in a crash or a
# response-boundary failure, and a block that is not counted is indistinguishable
# from a turn that never needed one.

def note_dsml_blocked() -> bool:
    """One piece of tool-call markup stopped before any user-visible surface.

    Deliberately counts turns' worth of blocks, not characters: the metric answers
    "did a control fire", and the raw text is never recorded anywhere — it is
    attacker-reachable content, and an ops log that echoes it is one more place it
    gets replayed from.
    """
    obs = _turn_obs.get()
    if obs is None:
        return False
    obs["dsml_blocked"] = obs.get("dsml_blocked", 0) + 1
    return True


def note_dsml_leak() -> bool:
    """Markup that reached the serialized response body.

    Recorded when only the boundary backstop caught it. That is a leak, not a
    block: the in-band guard was supposed to have handled it, and scoring the
    backstop as a success would let a release ship with its primary control
    broken.
    """
    obs = _turn_obs.get()
    if obs is None:
        return False
    obs["dsml_leak"] = obs.get("dsml_leak", 0) + 1
    return True


def dsml_snapshot() -> Dict[str, Any]:
    """``dsml_blocked`` / ``dsml_leak`` for this turn, or null with no window."""
    obs = _turn_obs.get()
    if obs is None:
        return {"dsml_blocked": None, "dsml_leak": None}
    return {"dsml_blocked": int(obs.get("dsml_blocked", 0)),
            "dsml_leak": int(obs.get("dsml_leak", 0))}


# --------------------------------------------------------------------------- #
# Write-tool security audit                                                   #
# --------------------------------------------------------------------------- #
#
# Two rules shape this section.
#
# 1. The decision is recorded AT THE POLICY DECISION POINT, as a structured value.
#    It is never recovered by reading an exception message. A denial and an
#    ordinary failure can raise the same class (legacy raises a bare
#    PermissionError for its write refusal, and PermissionError also means "the
#    filesystem said no"), so error text cannot separate a security event from an
#    infrastructure one. Only the branch that made the decision knows which it was.
#
# 2. `dispatch_started` means the call CROSSED THE POLICY GATE and entered the
#    tool-call boundary. It does NOT mean the write landed. A write that was
#    dispatched and then timed out, raised, or was rolled back is still a write the
#    policy let through, which is the property being audited. The canary contract
#    spells the derived counters `*_executed_count`; "executed" there carries this
#    same meaning and nothing stronger.

# security_decision values.
DECISION_ALLOWED = "allowed"                    # untainted context; ordinary write
DECISION_CONFIRMED = "confirmed"                # tainted BUT user-authorized (A+ rule 2)
DECISION_DENIED_TAINTED = "denied_tainted"      # tainted + unauthorized -> refused
DECISION_DENIED_RECALL = "denied_recall"        # pure recall turn: nothing new to save
DECISION_DENIED_FORBIDDEN = "denied_forbidden"  # blocked by the plain guardrail
DECISION_LEGACY_OVERRIDE = "legacy_override"    # legacy's allow_tainted_memory=True

_DENIED_DECISIONS = frozenset({DECISION_DENIED_TAINTED, DECISION_DENIED_RECALL,
                               DECISION_DENIED_FORBIDDEN})

VALID_DECISIONS = frozenset({DECISION_ALLOWED, DECISION_CONFIRMED,
                             DECISION_LEGACY_OVERRIDE}) | _DENIED_DECISIONS

AUDIT_INSTRUMENTED = "instrumented"
AUDIT_NOT_INSTRUMENTED = "not_instrumented"


def note_write_decision(*, tool: str, decision: str, context_tainted: bool,
                        user_authorized: bool, audit_key: str,
                        reason: Optional[str] = None,
                        gate_bypassed: bool = False) -> bool:
    """Record the policy outcome for one write-tool call. Returns True if stored.

    Idempotent per ``audit_key`` (the tool_call id or idempotency key): the first
    decision for a key wins, so a re-planned or retried call cannot inflate the
    turn's security counts.
    """
    obs = _turn_obs.get()
    if obs is None:
        return False
    audit = obs.setdefault("write_audit", {})
    if audit_key in audit:
        return False
    record = {
        "tool": tool,
        # Stored VERBATIM, never validated against VALID_DECISIONS here. A producer
        # bug must surface as an unrecognised value in the record, not get quietly
        # coerced into a benign "allowed" that reads clean.
        "security_decision": decision,
        "context_tainted": bool(context_tainted),
        "user_authorized": bool(user_authorized),
        "dispatch_started": False,
        # No policy gate ran on this path at all. Distinct from "the gate ran and
        # said no": there is no decision to trust, so the taint/authorization fields
        # carry no evidence and the counters must not read this as clean.
        "gate_bypassed": bool(gate_bypassed),
        "reason": reason,
    }
    record.update(_normalise_agent_context(None))
    audit[audit_key] = record
    return True


def note_write_dispatch(audit_key: str) -> bool:
    """Mark that a recorded write call crossed the gate and entered the tool call.

    Called immediately BEFORE the dispatch, so a tool that hangs or crashes still
    leaves the audit trail showing the policy let it through.
    """
    obs = _turn_obs.get()
    if obs is None:
        return False
    rec = (obs.get("write_audit") or {}).get(audit_key)
    if rec is None:
        return False
    rec["dispatch_started"] = True
    return True


def write_audit_snapshot(arch: Optional[str]) -> Dict[str, Any]:
    """Derived security counters for this turn, or all-None when uninstrumented.

    ``denied_write_count``
        writes the policy refused. Safe events; they are not violations.

    ``tainted_write_executed_count``
        a write that crossed the gate while the context was tainted AND the user
        had not authorized the content. The rule is STRUCTURAL, not keyed on
        ``legacy_override`` — on legacy that decision is exactly how this arises,
        but an fc_loop regression that let the same write through must count too,
        or the pool that regressed would be the one reporting clean.

        A tainted write the user DID authorize (A+ rule 2: an explicit cue plus
        content that is substantially the user's own words) is a legitimate write
        and is deliberately NOT counted. Counting it would make "记住我的预算
        £1400" a zero-tolerance violation.

    ``forbidden_write_executed_count``
        a write that reached dispatch without a gate's permission — either the gate
        DENIED it and it ran anyway, or no gate ran on that path at all
        (``gate_bypassed``). Both should be unreachable; they are counted precisely
        because an invariant that is never checked is an invariant that is not
        enforced. An ungated dispatch cannot be scored as tainted-or-not, so it is
        never quietly folded into the tainted counter as a 0.
    """
    obs = _turn_obs.get()
    if obs is None or arch not in _write_auditors:
        return {"denied_write_count": None,
                "tainted_write_executed_count": None,
                "forbidden_write_executed_count": None,
                "write_audit_status": AUDIT_NOT_INSTRUMENTED,
                "write_audit": None}
    records = list((obs.get("write_audit") or {}).values())
    denied = sum(1 for r in records if r["security_decision"] in _DENIED_DECISIONS)
    tainted_exec = sum(1 for r in records if r["dispatch_started"]
                       and r["context_tainted"] and not r["user_authorized"])
    forbidden_exec = sum(1 for r in records if r["dispatch_started"]
                         and (r["security_decision"] in _DENIED_DECISIONS
                              or r.get("gate_bypassed")))
    return {"denied_write_count": denied,
            "tainted_write_executed_count": tainted_exec,
            "forbidden_write_executed_count": forbidden_exec,
            "write_audit_status": AUDIT_INSTRUMENTED,
            "write_audit": records}


# --------------------------------------------------------------------------- #
# LangChain callback — one seam for every call site on both arches            #
# --------------------------------------------------------------------------- #

_callback_cls = None


def _get_callback_cls():
    """Build (once) the callback that feeds the accumulator.

    Attaching at ``ModelRouter.create`` covers every LLM call in the process —
    fc_loop's three sites and legacy's six — without editing any of them, and
    without depending on which ``except`` block happens to swallow the error
    afterwards. That matters here: several call sites catch the provider error and
    fall back silently, so a per-site approach would have to touch every one of
    them and would miss the next one somebody adds.
    """
    global _callback_cls
    if _callback_cls is not None:
        return _callback_cls
    from langchain_core.callbacks import BaseCallbackHandler

    class _CanaryLLMObserver(BaseCallbackHandler):
        """Records provider failures. Never alters model output, never raises."""

        def __init__(self, configured_model: Optional[str] = None):
            # run_id -> whether the request carried tool/function schemas. Needed
            # because on_llm_error does not describe the request that failed.
            self._schemas_bound: dict = {}
            self._agent_contexts: dict = {}
            # Fallback only; the provider's own answer wins (see extract_model_name).
            self.configured_model = configured_model

        def _note_start(self, run_id, kwargs):
            self._agent_contexts[run_id] = _current_agent_context()
            try:
                params = kwargs.get("invocation_params") or {}
                bound = bool(params.get("tools") or params.get("functions")
                             or params.get("response_format"))
                self._schemas_bound[run_id] = bound
            except Exception:
                self._schemas_bound[run_id] = False

        def on_llm_start(self, serialized, prompts, *, run_id=None, **kwargs):
            self._note_start(run_id, kwargs)

        def on_chat_model_start(self, serialized, messages, *, run_id=None, **kwargs):
            self._note_start(run_id, kwargs)

        def on_llm_end(self, response, *, run_id=None, **kwargs):
            self._schemas_bound.pop(run_id, None)
            agent_context = self._agent_contexts.pop(run_id, {})
            try:
                note_llm_usage(run_id, response, configured_model=self.configured_model,
                               agent_context=agent_context)
            except Exception:
                pass  # telemetry must never break a successful turn

        def on_llm_error(self, error, *, run_id=None, **kwargs):
            bound = self._schemas_bound.pop(run_id, False)
            agent_context = self._agent_contexts.pop(run_id, {})
            try:
                note_provider_error(error, schemas_bound=bound,
                                    agent_context=agent_context)
            except Exception:
                pass  # telemetry must never convert a provider error into a worse one

    _callback_cls = _CanaryLLMObserver
    return _callback_cls


def install_observer(model: Any, *, configured_model: Optional[str] = None) -> Any:
    """Attach the canary observer to a LangChain chat model, in place.

    Unlike the offline-eval instrumentation this is ALWAYS on: the canary gate is a
    production control, and an observer that only runs under RENTCOMPASS_EVAL would
    observe nothing in the pool it is supposed to be gating. The cost is one
    callback object per model and a dict insert per call.
    """
    try:
        handler = _get_callback_cls()(configured_model)
        existing = list(getattr(model, "callbacks", None) or [])
        model.callbacks = existing + [handler]
        _mark_observer_installed()
    except Exception:
        # Leave the model exactly as-is. observer_installed() stays False, so
        # snapshot() reports null and the gate holds — the failure is loud in the
        # report rather than silent in the data.
        return model
    return model
