"""Canary turn telemetry — schema v3 record builder.

Deliberately dependency-light (stdlib only) so the canary CONTRACT can be tested
end-to-end without importing the Flask app: the closed-loop test imports
``build_canary_turn_record`` here and feeds its output straight into
``scripts/canary_report.py``. If this module ever grows an app-level import, that
test stops proving anything.

Schema v2 changes vs v1
-----------------------
* ``telemetry_schema_version`` / ``ts`` (UTC) are now mandatory.
* ``endpoint`` distinguishes ``alex`` from ``search_direct`` so the deterministic
  form path can never dilute the agent A/B.
* ``http_status`` + ``turn_outcome`` make crashes/server errors observable.
* Security is a STRUCTURED object with three separate counters instead of one
  ``denied_writes`` int — "denied" (safe, blocked) is not the same event as
  "executed" (zero-tolerance).
* ``dsml_blocked`` is separated from ``dsml_leak``. The two are NOT "caught" vs
  "delivered" — they are which LAYER caught it:

    ``dsml_blocked``
        The primary control caught it BEFORE persistence. Safe: nothing was
        written to the conversation store or auto-memory, nothing was sent, and
        the deterministic fallback answered instead. Reported, never gated on.

    ``dsml_leak``
        Raw markup reached the SERIALIZED response boundary. The final backstop
        replaced the body, so it was NOT actually sent to the user — this counter
        deliberately does not claim otherwise. It is nonetheless a zero-tolerance,
        candidate-release-blocking event, because reaching that point means the
        primary control failed. Scoring it as a successful block would let a
        release ship whose real guard is broken, and the backstop is the last
        thing standing between that bug and a delivered leak.
* ``provider_schema_400_count`` counts ONLY provider-side strict-schema
  rejections. The app's own ApiError(400) validation failures cannot appear here:
  they are raised before the turn anchor and emit no record at all.
* ``llm_usage`` aggregates per-model token usage over every LLM call in the turn.
  Cost is deliberately NOT computed here — it is applied offline from a versioned
  price table (scripts/pricing/) so a price change never rewrites history.
* ``user_id_hash`` (HMAC) replaces the raw user id.
* ``eval_only`` self-declares metrics that prod telemetry cannot determine, so
  the report can distinguish "eval-only" from "missing instrumentation".
* Experimental ``manager_v1`` turns identify whether specialist dispatch was
  configured and may carry a content-free ``multi_agent`` lifecycle diagnostic.
  Legacy/fc records retain their old shape.

Schema v3 changes vs v2
-----------------------
v3 exists because two v2 fields were REDEFINED, not because new ones were added.
Additive fields do not need a version; a field whose meaning changed does, or a
window spanning the change silently averages two different measurements:

* ``llm_calls`` was ``final_state["loop_turn"]`` on fc (agent super-steps, and
  ``null`` on legacy). It is now the OBSERVER's count of billed provider calls on
  every arch, with ``loop_turn`` retained only as a fail-closed lower bound. From
  v3 it therefore also includes the nested tool-internal DeepSeek calls that
  ``llm_interface._call_deepseek`` makes — the ones that were always billed and
  never counted. v2 and v3 ``llm_calls`` are NOT comparable.
* ``tool_batches`` was ``null`` on legacy and "distinct fc artifact turns"
  otherwise; it is now artifact turns plus a legacy execution-plan wave, so it is
  a real number on both arches. ``search_direct_signals`` likewise reports an
  observed ``0`` for both counters instead of ``null``.
* Additive in v3 and safe to ignore: ``variant_id``, the rollout identity block,
  ``root_agent_context``/``agent_role``/``task_id``/``parent_task_id``,
  ``tool_latency``, and the ``multi_agent``
  ``partial``/``denied_calls``/``dropped_error_codes`` counters.
* ``tool_ledger_status`` states whether ``tool_batches`` COULD be counted at all.
  ``tool_batches`` is derived from ``final_state``, and a crashed turn has no
  ``final_state`` — so requiring it unconditionally at v3 made every crash/5xx
  record a guaranteed contract violation, i.e. a permanent INSTRUMENTATION-HOLD on
  any window containing one (14% of legacy history). The marker makes the gap
  explicit and checkable instead: ``"complete"`` promises a real count,
  ``"unavailable"`` states that the ledger died with the turn and REQUIRES
  ``tool_batches`` to be null, and it is only legal on a crash/server_error
  outcome, so it cannot be used to opt a healthy turn out of the requirement.

The consumer (``scripts/canary_report.py``) validates each record under the rules
that applied WHEN IT WAS WRITTEN, keyed off this field. That is the whole point of
bumping it: 2748 historical legacy records and 230 fc records would otherwise have
started failing a contract that did not exist when they were emitted.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

SCHEMA_VERSION = 3
EVENT_NAME = "canary.turn"

ENDPOINT_ALEX = "alex"
ENDPOINT_SEARCH_DIRECT = "search_direct"

# turn_outcome values
OUTCOME_OK = "ok"                    # agent produced a normal answer
OUTCOME_AGENT_ERROR = "agent_error"  # response_type == "error" (handled, HTTP 200)
OUTCOME_CRASH = "crash"              # exception caught by the endpoint (HTTP 200 by design)
OUTCOME_SERVER_ERROR = "server_error"  # escaped to the 500 handler
# Outcomes on which the turn's own bookkeeping (final_state) does not exist. The
# consumer keys the tool-ledger exemption off exactly this set.
UNOBSERVABLE_OUTCOMES = (OUTCOME_CRASH, OUTCOME_SERVER_ERROR)

# tool_ledger_status values.
TOOL_LEDGER_COMPLETE = "complete"        # tool_batches is a real, observed count
TOOL_LEDGER_UNAVAILABLE = "unavailable"  # the ledger died with the turn -> null
VALID_TOOL_LEDGER_STATUSES = (TOOL_LEDGER_COMPLETE, TOOL_LEDGER_UNAVAILABLE)

# Metrics prod telemetry genuinely cannot determine. Emitted as null and declared
# here so the report treats them as EVAL-ONLY rather than missing instrumentation.
# Never emit `false` for these — that would assert a clean observation we did not make.
EVAL_ONLY_FIELDS = ("forbidden_read", "no_evidence_numbers")

# Required fields — the report HOLDs (exit 2) if any is missing or null.
REQUIRED_FIELDS = (
    "telemetry_schema_version", "ts", "endpoint", "agent_arch", "candidate_sha",
    "strict", "request_id", "conversation_id", "http_status", "turn_outcome",
    "turn_latency_ms", "soft_wrapped", "partial", "tool_budget_timeout",
    "dsml_blocked", "dsml_leak", "provider_schema_400_count", "security",
    "user_id_hash_status",
    # Tokens are the cost side of the A/B. A turn whose spend we failed to observe
    # must not average in as if it were free, so the STATUS is required even though
    # llm_usage itself is not: it is the difference between "this turn cost nothing"
    # and "we do not know what this turn cost".
    "llm_usage_status",
)

# llm_usage_status values (mirrors core.turn_observations).
USAGE_COMPLETE = "complete"
USAGE_PARTIAL = "partial"
USAGE_NO_CALLS = "no_llm_calls"
USAGE_NOT_INSTRUMENTED = "not_instrumented"
VALID_USAGE_STATUSES = (USAGE_COMPLETE, USAGE_PARTIAL, USAGE_NO_CALLS,
                        USAGE_NOT_INSTRUMENTED)
_SPECIALIST_ROLES = frozenset({"listings", "mobility", "area_evidence"})
_SPECIALIST_STATUSES = frozenset(
    {"planned", "started", "completed", "partial", "failed", "skipped"}
)
_SPECIALIST_OUTCOME_STATUSES = frozenset({"partial", "failed", "skipped"})
_SPECIALIST_DENIED_STATUS = "denied"
_SPECIALIST_EVENT_FIELDS = (
    "plan_id", "task_id", "parent_task_id", "role", "status",
    "duration_ms", "call_count",
)
# The one optional lifecycle field. Kept out of the tuple above so the exact-shape
# assertion at the bottom of the event builder still fails safe for anything else.
_SPECIALIST_EVENT_OPTIONAL_FIELDS = ("error_code",)
_SPECIALIST_DENIED_EVENT_FIELDS = ("status", "tool", "error_code")
# ``partial`` and ``denied_calls`` are REQUIRED of this producer but OPTIONAL of
# the consumer: a record written by an earlier manager_v1 build has neither, and
# defaulting them to 0 there is correct (nothing was counted) where defaulting a
# core counter would be a fabrication.
_MULTI_AGENT_COUNTER_FIELDS = (
    "planned", "started", "completed", "failed", "skipped", "max_in_flight",
)
_MULTI_AGENT_OPTIONAL_COUNTER_FIELDS = (
    "partial", "denied_calls", "dropped_error_codes",
)
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
REQUIRED_SECURITY_FIELDS = (
    "denied_write_count", "tainted_write_executed_count", "forbidden_write_executed_count",
)

# user_id_hash_status values.
HASH_KEYED = "keyed"                       # stable deployment secret — cohort stats valid
HASH_NO_USER = "no_user"                   # no identity yet (e.g. a pre-identity 5xx) — fine
HASH_UNKEYED = "unkeyed_no_stable_secret"  # NO stable secret — contract violation, holds the gate

# NOTE: there is deliberately NO per-process random-salt fallback. A process-random
# salt re-hashes the same user differently after every restart, so `users` and any
# per-user rate silently decorrelate across restarts — a wrong number that looks
# like a right one. When no stable secret is configured we emit a null hash plus
# HASH_UNKEYED, which the report treats as an instrumentation violation (HOLD).


def _hash_key() -> Optional[bytes]:
    """The stable HMAC key, or None when the deployment has not configured one."""
    for var in ("CANARY_USER_HASH_KEY", "FLASK_SECRET_KEY"):
        v = os.environ.get(var)
        if v:
            return v.encode("utf-8")
    return None


def hash_user_id(user_id: Optional[str]) -> tuple[Optional[str], str]:
    """HMAC-SHA256 the user id. Returns (hex_digest_prefix_or_None, status).

    Truncated to 32 hex chars: collision-safe for any cohort size we will see, and
    short enough to keep the record readable. Never returns the raw id, and never
    returns an unstable digest.
    """
    if user_id is None:
        return None, HASH_NO_USER
    key = _hash_key()
    if key is None:
        return None, HASH_UNKEYED
    digest = hmac.new(key, str(user_id).encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:32], HASH_KEYED


def search_direct_signals() -> Dict[str, Any]:
    """Signals for the deterministic /api/search_direct path.

    Explicit zeros are genuinely provable HERE and only here: this endpoint never
    builds an agent prompt, never calls the LLM, and dispatches no write tool. So
    "no provider call happened" and "no write executed" are facts, not assumptions.
    """
    return {
        "soft_wrapped": False, "wrapped_by": None,
        "partial": False, "tool_budget_timeout": False,
        "security": {"denied_write_count": 0,
                     "tainted_write_executed_count": 0,
                     "forbidden_write_executed_count": 0,
                     # Empty, not null: this endpoint dispatches no tools at all, so
                     # "no write decisions were made" is a fact about it.
                     "write_audit": []},
        "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
        # Not "we failed to measure" — this endpoint provably makes no LLM call, so
        # there is no spend to miss. That is why the status enum has a value for it
        # instead of overloading null.
        "llm_usage": None, "llm_usage_status": USAGE_NO_CALLS,
        # These are genuine observed zeros, not FC-only fields. Keeping them null
        # contradicts no_llm_calls and makes the strict consumer HOLD every window
        # containing deterministic search traffic.
        "llm_calls": 0, "tool_batches": 0,
        # This endpoint dispatches no tools at all, so 0 is an observed count and
        # the ledger is trivially complete.
        "tool_ledger_status": TOOL_LEDGER_COMPLETE,
    }


def unknown_turn_signals(observed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Signals for a turn whose outcome is NOT observable: an agent crash, or a
    request that died at the response boundary (5xx).

    Zeros would be FAIL-OPEN here, which an earlier revision of this module got
    wrong. The agent can dispatch `remember` (a write) and crash afterwards, so a
    0 write count is an assumption, not an observation. Everything the turn's own
    bookkeeping would have reported is therefore null -> the report HOLDs.

    ``observed`` carries the counters that were accumulated OUT-OF-BAND as the turn
    ran (``core.turn_observations``), so they survive the crash that destroyed the
    final_state. Only non-None values are overlaid: an accumulator that was never
    installed reports None and the field stays null, exactly as if Layer B had not
    landed. This is what closes the note that used to sit here — a provider
    strict-schema 400 is a plausible CAUSE of the crash, so it is the one signal
    that most needs to survive it.
    """
    sig = {
        # These are structural: no wrap-up ran, no partial artifact was produced. wrapped_by
        # follows soft_wrapped — with no wrap there is nothing to attribute.
        "soft_wrapped": False, "wrapped_by": None,
        "partial": False, "tool_budget_timeout": False,
        # Unobservable — a write may already have executed before the crash.
        "security": {"denied_write_count": None,
                     "tainted_write_executed_count": None,
                     "forbidden_write_executed_count": None,
                     "write_audit": None},
        # Unobservable — partial output may already have been flushed.
        "dsml_blocked": None, "dsml_leak": None,
        # Observed out-of-band when an accumulator was running; null otherwise.
        "provider_schema_400_count": None,
        "llm_usage": None, "llm_usage_status": USAGE_NOT_INSTRUMENTED,
        "llm_calls": None, "tool_batches": None,
        # tool_batches is derived from final_state, and a crashed turn HAS no
        # final_state. There is no out-of-band accumulator for it, so no overlay
        # below can ever fill it: saying so explicitly is the only honest option.
        # Fabricating a 0 here would report "this turn ran no tools" about a turn
        # that may have run several before dying.
        "tool_ledger_status": TOOL_LEDGER_UNAVAILABLE,
    }
    for field in ("provider_schema_400_count", "llm_usage_status",
                  "dsml_blocked", "dsml_leak",
                  # Same argument as llm_usage below: the observer counts a call at
                  # its completion callback, so the calls that finished BEFORE the
                  # crash are observed facts. Leaving this null while llm_usage
                  # reports their tokens would also make the record internally
                  # inconsistent from v3, where the two are cross-checked.
                  "llm_calls"):
        if observed and observed.get(field) is not None:
            sig[field] = observed[field]
    # The write audit is accumulated at the policy decision point, so a turn that
    # crashed AFTER dispatching a tainted write still reports it. This is the case
    # the docstring above warns about, and the only one that turns it from a
    # permanent HOLD into an answer.
    for field in ("denied_write_count", "tainted_write_executed_count",
                  "forbidden_write_executed_count", "write_audit"):
        if observed and observed.get(field) is not None:
            sig["security"][field] = observed[field]
    # A crashed turn's completed calls still cost real money; report what was
    # observed rather than dropping the turn out of the cost denominator.
    if observed and observed.get("llm_usage_calls"):
        sig["llm_usage"] = aggregate_llm_usage(observed["llm_usage_calls"])
    root_context = observed.get("root_agent_context") if observed else None
    if isinstance(root_context, dict) and root_context:
        sig["root_agent_context"] = dict(root_context)
    for field in ("agent_role", "task_id", "parent_task_id"):
        if observed and observed.get(field) is not None:
            sig[field] = observed[field]
    if observed and isinstance(observed.get("multi_agent"), dict):
        # The record builder applies the content-free whitelist and emits this only
        # for manager_v1. Keeping it here lets a crash retain lifecycle progress.
        sig["multi_agent"] = observed["multi_agent"]
    return sig


def _multi_agent_diagnostics(value: Any) -> Optional[Dict[str, Any]]:
    """Sanitise the optional manager_v1 specialist trace.

    This deliberately accepts no objectives, args, outputs or error strings. A
    malformed projection is omitted rather than allowed to break canary emission.
    """
    if not isinstance(value, dict):
        return None
    try:
        out: Dict[str, Any] = {}
        for field in _MULTI_AGENT_COUNTER_FIELDS:
            raw = value.get(field)
            if isinstance(raw, bool) or not isinstance(raw, int):
                return None
            if raw < 0 or raw > 1_000_000:
                return None
            out[field] = raw
        for field in _MULTI_AGENT_OPTIONAL_COUNTER_FIELDS:
            raw = value.get(field, 0)
            if isinstance(raw, bool) or not isinstance(raw, int):
                return None
            if raw < 0 or raw > 1_000_000:
                return None
            out[field] = raw

        truncated = value.get("events_truncated")
        if not isinstance(truncated, bool):
            return None
        out["events_truncated"] = truncated

        safe_events = []
        events = value.get("events", [])
        if not isinstance(events, list):
            return None
        for raw_event in events[:64]:
            if not isinstance(raw_event, dict):
                continue
            if raw_event.get("status") == _SPECIALIST_DENIED_STATUS:
                # A refused dispatch carries no task identity — only which tool was
                # refused and why. Validated against the same closed grammars as the
                # lifecycle fields so it cannot become a channel for tool arguments.
                tool = raw_event.get("tool")
                tool = tool.strip() if isinstance(tool, str) else ""
                code = raw_event.get("error_code")
                code = code.strip() if isinstance(code, str) else ""
                if not _TOOL_NAME_RE.fullmatch(tool) or not _ERROR_CODE_RE.fullmatch(code):
                    continue
                denied_event = {
                    "status": _SPECIALIST_DENIED_STATUS,
                    "tool": tool,
                    "error_code": code,
                }
                if tuple(denied_event.keys()) == _SPECIALIST_DENIED_EVENT_FIELDS:
                    safe_events.append(denied_event)
                continue
            identifiers = []
            invalid = False
            for field in ("plan_id", "task_id", "parent_task_id"):
                candidate = raw_event.get(field)
                candidate = candidate.strip() if isinstance(candidate, str) else ""
                if not _MACHINE_ID_RE.fullmatch(candidate):
                    invalid = True
                    break
                identifiers.append(candidate)
            if invalid:
                continue
            role = raw_event.get("role")
            status = raw_event.get("status")
            if role not in _SPECIALIST_ROLES or status not in _SPECIALIST_STATUSES:
                continue
            calls = raw_event.get("call_count")
            if (
                isinstance(calls, bool)
                or not isinstance(calls, int)
                or calls < 0
                or calls > 10_000
            ):
                continue
            raw_duration = raw_event.get("duration_ms")
            if raw_duration is None:
                duration = None
            else:
                if (
                    isinstance(raw_duration, bool)
                    or not isinstance(raw_duration, (int, float))
                ):
                    continue
                duration = float(raw_duration)
                if not math.isfinite(duration) or duration < 0:
                    continue
                duration = round(duration, 3)
            event = {
                "plan_id": identifiers[0],
                "task_id": identifiers[1],
                "parent_task_id": identifiers[2],
                "role": role,
                "status": status,
                "duration_ms": duration,
                "call_count": calls,
            }
            error_code = raw_event.get("error_code")
            if error_code is not None:
                error_code = error_code.strip() if isinstance(error_code, str) else ""
                if (
                    not _ERROR_CODE_RE.fullmatch(error_code)
                    or status not in _SPECIALIST_OUTCOME_STATUSES
                ):
                    continue
                event["error_code"] = error_code
            # A local assertion makes future edits fail safe if an unsafe field is
            # ever accidentally added to the diagnostic shape.
            if tuple(event.keys()) in (
                _SPECIALIST_EVENT_FIELDS,
                _SPECIALIST_EVENT_FIELDS + _SPECIALIST_EVENT_OPTIONAL_FIELDS,
            ):
                safe_events.append(event)
        out["events"] = safe_events
        return out
    except Exception:
        return None


_TOOL_LATENCY_FIELDS = ("count", "p50_ms", "max_ms", "timed_out", "abandoned")
_MAX_TOOL_LATENCY_ENTRIES = 32


def _tool_latency_summary(value: Any) -> Optional[Dict[str, Any]]:
    """Sanitise the per-tool latency summary. Content-free by construction.

    Only a TOOL NAME (registry identifier grammar) and five numbers per tool.
    No arguments, no results, no error text and no per-call vector — a vector
    would let the shape of one user's session be reconstructed from an ops log,
    and p50/max answer the question ("which tool is eating the budget") just as
    well. Non-gating: this is a Stage-1 instrument, not a threshold.
    """
    if not isinstance(value, dict):
        return None
    out: Dict[str, Any] = {}
    try:
        for raw_name, raw_stats in list(value.items())[:_MAX_TOOL_LATENCY_ENTRIES]:
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if not _TOOL_NAME_RE.fullmatch(name) or not isinstance(raw_stats, dict):
                continue
            entry: Dict[str, Any] = {}
            ok = True
            for field in ("count", "timed_out", "abandoned"):
                raw = raw_stats.get(field)
                if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 100_000:
                    ok = False
                    break
                entry[field] = raw
            if not ok:
                continue
            for field in ("p50_ms", "max_ms"):
                raw = raw_stats.get(field)
                if raw is None:
                    entry[field] = None
                    continue
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    ok = False
                    break
                number = float(raw)
                if not math.isfinite(number) or number < 0:
                    ok = False
                    break
                entry[field] = round(number, 1)
            if not ok:
                continue
            ordered = {field: entry[field] for field in _TOOL_LATENCY_FIELDS}
            if tuple(ordered.keys()) == _TOOL_LATENCY_FIELDS:
                out[name] = ordered
    except Exception:
        return None
    return out or None


def aggregate_llm_usage(calls: Optional[Iterable[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Aggregate per-call usage dicts into one per-turn usage object.

    Each input item is ``{"model": str, "input_tokens": int, "output_tokens": int,
    "cache_read_tokens": int}``. Returns ``None`` when no usage was captured at all
    (which the report treats as missing instrumentation, NOT as zero spend — the
    distinction matters: zero tokens and unmeasured tokens are different facts).
    """
    if not calls:
        return None
    per_model: Dict[str, Dict[str, int]] = {}
    per_role: Dict[str, Dict[str, int]] = {}
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
    saw_any = False
    for c in calls:
        if not isinstance(c, dict):
            continue
        saw_any = True
        model = str(c.get("model") or "unknown")
        slot = per_model.setdefault(
            model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0})
        for field in ("input_tokens", "output_tokens", "cache_read_tokens"):
            try:
                v = int(c.get(field) or 0)
            except (TypeError, ValueError):
                v = 0
            slot[field] += v
            totals[field] += v
        slot["calls"] += 1
        totals["calls"] += 1
        role = c.get("agent_role")
        if role is not None:
            role_slot = per_role.setdefault(
                str(role),
                {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                 "cache_read_tokens": 0, "models": {}},
            )
            role_slot["calls"] += 1
            role_model_slot = role_slot["models"].setdefault(
                model,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                 "cache_read_tokens": 0},
            )
            role_model_slot["calls"] += 1
            for field in ("input_tokens", "output_tokens", "cache_read_tokens"):
                try:
                    value = int(c.get(field) or 0)
                except (TypeError, ValueError):
                    value = 0
                role_slot[field] += value
                role_model_slot[field] += value
    if not saw_any:
        return None
    result = {**totals, "models": per_model}
    # Optional and additive: legacy/fc calls without an execution context retain
    # the exact historical llm_usage object shape.
    if per_role:
        result["roles"] = per_role
    return result


def build_canary_turn_record(
    *,
    endpoint: str,
    agent_arch: str,
    candidate_sha: str,
    strict: bool,
    request_id: str,
    conversation_id: str,
    user_id: Optional[str],
    http_status: int,
    turn_outcome: str,
    turn_latency_ms: float,
    signals: Optional[Dict[str, Any]] = None,
    ts: Optional[datetime] = None,
    manager_v1_specialists: Optional[bool] = None,
    rollout: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build ONE schema-v2 ``canary.turn`` record. Pure: no I/O, no globals beyond
    the HMAC key lookup. ``signals`` carries the arch-specific per-turn observations
    (see ``_build_fc_signals`` in app/app.py); every field defaults SAFELY, and a
    field we could not observe is emitted as ``null`` rather than a fabricated 0/False.
    """
    sig = signals or {}
    rollout_in = rollout if isinstance(rollout, dict) else {}
    sec_in = sig.get("security") or {}
    uid_hash, hash_status = hash_user_id(user_id)
    stamp = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def _int_or_none(v):
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _count(v):
        """Observed counter: absent -> 0 is WRONG, so absent stays None."""
        return _int_or_none(v)

    def _audit_records(v):
        """Per-decision write-audit detail, bounded and content-free.

        Only the policy-relevant fields are copied. The write's CONTENT is
        deliberately never included: this log is read by whoever is diagnosing a
        HOLD, and the tainted case is precisely the one where the content may be
        attacker-supplied text that nobody asked to have echoed into an ops log.

        ``dispatch_started`` is the honest name for what was observed — the call
        crossed the policy gate. The counters above spell it ``*_executed_count``
        for contract continuity; both mean the gate crossing, never "the write
        landed in the database".
        """
        if not isinstance(v, list):
            return None
        out = []
        for r in v[:20]:  # bounded: a retry storm must not grow the record unboundedly
            if not isinstance(r, dict):
                continue
            record = {
                "tool": r.get("tool"),
                "security_decision": r.get("security_decision"),
                "context_tainted": bool(r.get("context_tainted")),
                "user_authorized": bool(r.get("user_authorized")),
                "dispatch_started": bool(r.get("dispatch_started")),
                "gate_bypassed": bool(r.get("gate_bypassed")),
                "reason": r.get("reason"),
            }
            for field in ("agent_role", "task_id", "parent_task_id"):
                if r.get(field) is not None:
                    record[field] = str(r[field])
            out.append(record)
        return out

    record = {
        "event": EVENT_NAME,
        "telemetry_schema_version": SCHEMA_VERSION,
        "ts": stamp.isoformat(),
        "endpoint": endpoint,
        "agent_arch": agent_arch,
        "candidate_sha": candidate_sha,
        "strict": bool(strict),
        "variant_id": (
            f"{agent_arch}:strict-{1 if strict else 0}:specialists-"
            f"{1 if manager_v1_specialists is True else 0}"
        ),
        # Trusted edge provenance is injected by nginx after overwriting any
        # client copies. Direct :5001/:5002 smoke traffic is explicitly labelled
        # and can therefore never satisfy a public rollout window by accident.
        "rollout_id": rollout_in.get("rollout_id"),
        "rollout_stage": rollout_in.get("rollout_stage"),
        "configured_candidate_percent": rollout_in.get(
            "configured_candidate_percent"
        ),
        "traffic_source": rollout_in.get("traffic_source", "direct"),
        "assigned_pool": rollout_in.get("assigned_pool", "direct"),
        "request_id": request_id,
        "conversation_id": conversation_id,
        "user_id_hash": uid_hash,
        "user_id_hash_status": hash_status,
        "http_status": int(http_status),
        "turn_outcome": turn_outcome,
        # --- degradation -----------------------------------------------------
        "soft_wrapped": bool(sig.get("soft_wrapped", False)),
        # Attribution for a wrapped turn. Deliberately NOT coerced to a string: null means
        # "this turn did not wrap", and the key being ABSENT (older producer) is what the
        # aggregator must report as not-instrumented. Coercing either case to a value would
        # repeat the v1/v2 `security_audit` mistake of reading a gap as a clean result.
        "wrapped_by": (str(sig["wrapped_by"])
                       if sig.get("wrapped_by") is not None else None),
        "partial": bool(sig.get("partial", False)),
        "tool_budget_timeout": bool(sig.get("tool_budget_timeout", False)),
        # --- security (structured; denied != executed) -----------------------
        "security": {
            "denied_write_count": _count(sec_in.get("denied_write_count")),
            "tainted_write_executed_count": _count(sec_in.get("tainted_write_executed_count")),
            "forbidden_write_executed_count": _count(sec_in.get("forbidden_write_executed_count")),
            # The structured decisions the counters are derived from. Present so a
            # HOLD can be diagnosed without re-running the turn: "1 tainted write
            # executed" is actionable only once you can see which tool, on which
            # branch, and why. Not gated on — the counters above are.
            "write_audit": _audit_records(sec_in.get("write_audit")),
        },
        # --- tool-markup: blocked (safe) vs leaked (zero-tolerance) ----------
        "dsml_blocked": _count(sig.get("dsml_blocked")),
        "dsml_leak": _count(sig.get("dsml_leak")),
        # --- provider-side strict-schema rejections only ---------------------
        "provider_schema_400_count": _count(sig.get("provider_schema_400_count")),
        # --- perf / cost inputs ---------------------------------------------
        "turn_latency_ms": round(float(turn_latency_ms), 1),
        "llm_calls": _int_or_none(sig.get("llm_calls")),
        "tool_batches": _int_or_none(sig.get("tool_batches")),
        "llm_usage": sig.get("llm_usage"),
        # Whether that usage can be trusted as the turn's WHOLE spend. Defaults to
        # not_instrumented so a signals dict that predates this field holds the gate
        # instead of silently claiming complete accounting.
        "llm_usage_status": sig.get("llm_usage_status") or USAGE_NOT_INSTRUMENTED,
        # --- explicitly eval-only (null, never False) ------------------------
        "forbidden_read": None,
        "no_evidence_numbers": None,
        "eval_only": list(EVAL_ONLY_FIELDS),
    }
    # ADDITIVE and omitted when the caller did not state it. Emitting a default
    # would be worse than emitting nothing: "unavailable" next to a real
    # `tool_batches` count is a self-contradicting record, and "complete" next to a
    # null is a claim we did not make. Absent, the consumer falls back to the
    # outcome-based rule (a crash/5xx turn had no ledger), which is exactly how it
    # already has to read the v3 records written before this field existed.
    ledger_status = sig.get("tool_ledger_status")
    if ledger_status in VALID_TOOL_LEDGER_STATUSES:
        record["tool_ledger_status"] = ledger_status
    tool_latency = _tool_latency_summary(sig.get("tool_latency"))
    if tool_latency is not None:
        # Additive and arch-agnostic: absent on any turn that dispatched no tool,
        # so legacy/fc records without it keep exactly their historical shape.
        record["tool_latency"] = tool_latency
    root_context = sig.get("root_agent_context")
    if agent_arch == "manager_v1":
        # Configuration identity is distinct from lifecycle activity: a perfectly
        # valid turn may need no specialist tool at all.  Emitting the switch lets
        # the release gate distinguish that case from a candidate that silently
        # started with specialist dispatch disabled.  ``None`` is intentional and
        # fail-closed for older/miswired producers.
        record["manager_v1_specialists"] = (
            manager_v1_specialists
            if isinstance(manager_v1_specialists, bool)
            else None
        )
        multi_agent = _multi_agent_diagnostics(sig.get("multi_agent"))
        if multi_agent is not None:
            record["multi_agent"] = multi_agent
    if not isinstance(root_context, dict):
        root_context = {}
    for field in ("agent_role", "task_id", "parent_task_id"):
        value = sig.get(field)
        if value is None:
            value = root_context.get(field)
        if value is not None:
            record[field] = str(value)
    return record
