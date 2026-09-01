#!/usr/bin/env python3
"""Offline aggregator + gate evaluator for the fc_loop canary (shuhan's 2026-07-20 plan).

Consumes ``canary.turn`` structured log records (one per completed user turn) emitted by
the running app and, per agent architecture (``fc`` pool vs ``legacy`` pool), computes a
comparison table and evaluates shuhan's canary gate rules, printing a verdict and returning
an exit code the CI/rollout driver can branch on.

This module NEVER imports from ``app/`` — it reads the JSONL telemetry contract only. The
app agent owns the producer; this owns the consumer. The contract (one JSON object per
completed turn) carries at least::

    agent_arch, candidate_sha, strict, request_id, conversation_id, user_id,
    soft_wrapped, partial, tool_budget_timeout, security_audit, turn_latency_ms,
    llm_calls, tool_batches

and optionally a ``ts`` field. Optional prod/eval fields (``dsml_leak``, ``400_count``,
``5xx_count``, ``forbidden_read``, ``no_evidence_numbers``) are honoured when present and
reported as "not instrumented" / "requires eval sweep" when absent.

Percentiles use the repo's NEAREST-RANK convention (``evaluation/run_benchmark.py``):
the value at 0-based index ``ceil(pct*n)-1`` (clamped) of the sorted samples — an actual
observed latency, never an interpolation.

Exit codes — only 0/2/3 are GATE VERDICTS (see GATE_EXIT_CODES / EXIT_USAGE below):
    0  proceed                  2  hold, stage-pause, or instrumentation-hold
    3  zero-tolerance breach    1  input/runtime error (bad --input/--since/--now)
   64  CLI usage error: argparse misuse (unknown flag, option missing its argument)

Stdlib only. Deterministic: given the same input files and flags, the output is identical.
"""
from __future__ import annotations

import argparse
import glob as _glob
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Constants — shuhan's thresholds                                             #
# --------------------------------------------------------------------------- #

P50_LIMIT_MS = 6000.0        # fc p50 hard ceiling (stage-pause above this)
P95_LIMIT_MS = 30000.0       # fc p95 hard ceiling (stage-pause above this)
OVER_SLO_MS = 30000.0        # a turn breaching this counts toward the over-30s tail
DEGRADED_RATE_LIMIT = 0.10   # (partial OR soft_wrapped) rate stage-pause ceiling
RELATIVE_PP = 1.0            # relative-to-legacy tolerance in percentage points
SPECIALIST_FAILURE_RATE_LIMIT = 0.05

# Exit codes. 0 / 2 / 3 are GATE VERDICT codes — they mean something about the release
# under test and a rollout driver branches on them (see build_verdict). Everything that
# is NOT a verdict must live outside this set, or the operator cannot tell "the gate
# spoke" from "I mistyped a flag".
#
# argparse's own default abort code is 2, which is STAGE-PAUSE here: `--json` typed
# without its PATH argument therefore exited 2 and was indistinguishable from a breached
# gate to anyone checking only `$?`. CLI misuse gets its own code instead, and no gate
# verdict may ever be reported as EXIT_USAGE (both directions are guarded in run()).
GATE_EXIT_CODES = (0, 2, 3)
EXIT_INPUT_ERROR = 1     # inputs/arguments were readable but unusable
EXIT_USAGE = 64          # sysexits.h EX_USAGE — argparse misuse, never a verdict

# Stage -> (minimum fc turns, minimum elapsed hours). BOTH must be satisfied to progress.
STAGES: Dict[str, Tuple[int, int]] = {
    "internal": (50, 24),
    "c1": (200, 24),
    "c2": (500, 48),
    "c3": (1000, 72),
    "flip": (2000, 168),
}

# Optional-field aliases. A metric is "instrumented" for an arch iff at least one of its
# aliases is a key on at least one record of that arch.
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "dsml_leak": ("dsml_leak", "tool_markup_leak", "markup_leak", "dsml_leaked"),
    "api_400": ("400_count", "api_400", "schema_400", "http_400", "status_400"),
    "http_5xx": ("5xx_count", "http_5xx", "status_5xx", "http_500", "server_error"),
    "forbidden_read": ("forbidden_read", "forbidden_read_count"),
    "no_evidence_numbers": ("no_evidence_numbers", "no_evidence_number",
                            "no_evidence_numbers_count"),
}

# Tokens in a security_audit outcome that mean the bad write did NOT execute (the safe
# A+ denial path) — these are non-clean but never zero-tolerance.
_SAFE_AUDIT_TOKENS = ("deni", "denied", "blocked", "refused", "prevented",
                      "not_executed", "notexecuted", "noop", "skipped", "rejected")

_CLEAN_AUDIT_VALUES = {"", "clean", "ok", "pass", "passed", "none", "clear", "green"}

# --------------------------------------------------------------------------- #
# Schema v2 contract (fail-closed)                                            #
# --------------------------------------------------------------------------- #
#
# v1 inferred "is this metric instrumented?" from KEY EXISTENCE and degraded an
# uninstrumented zero-tolerance metric to an advisory note — so a report could
# exit 0 having never observed the metric. That is fail-OPEN. v2 requires the
# producer to state every gate-relevant fact explicitly; missing or null is an
# INSTRUMENTATION-HOLD (exit 2), never a pass.

# EXACT set, not a floor. A ">= 2" floor is fail-open in the forward direction:
# a future v3 that renames or re-means a field would sail through a consumer that
# has no idea what v3 says, and the gate would score records it cannot actually
# read. This consumer understands exactly these versions; anything else HOLDs
# until someone teaches it the new schema.
SUPPORTED_SCHEMA_VERSIONS = (2, 3)
MIN_SCHEMA_VERSION = min(SUPPORTED_SCHEMA_VERSIONS)
CURRENT_SCHEMA_VERSION = max(SUPPORTED_SCHEMA_VERSIONS)

# v3 REDEFINED `llm_calls` (fc `loop_turn` -> the observer's billed-call count on
# every arch, now including the nested tool-internal DeepSeek calls) and made
# `tool_batches` a real number on legacy instead of null. Two consequences, and
# both are load-bearing:
#
#   1. Each record is validated under the rules that applied WHEN IT WAS WRITTEN.
#      Applying the v3 contract retroactively turned 0 -> 81 of 230 fc records and
#      590 -> 2643 of 2748 legacy records into "violations" of a contract that did
#      not exist when they were emitted. That is not a finding; it is a consumer
#      rewriting history, and it would have held every window containing a single
#      pre-upgrade record.
#   2. A window that MIXES v2 and v3 cannot be compared on those two fields at all,
#      so it holds rather than averaging two different measurements into one mean.
SCHEMA_V3_FIELDS = ("llm_calls", "tool_batches")

# Gate default: only the agent endpoint decides the A/B. The deterministic form
# path (search_direct) is aggregated separately so it cannot dilute agent metrics.
GATE_ENDPOINTS = ("alex",)

# Every endpoint the producer is allowed to emit. A record outside this set (or
# with no endpoint at all) is unattributable and holds the gate — it must never be
# silently dropped, or losing the field would become a way to escape the gate.
KNOWN_ENDPOINTS = ("alex", "search_direct")

REQUIRED_TOP_FIELDS = (
    "telemetry_schema_version", "ts", "endpoint", "agent_arch", "candidate_sha",
    "request_id", "conversation_id", "http_status", "turn_outcome",
    "turn_latency_ms", "soft_wrapped", "partial", "tool_budget_timeout",
    "dsml_blocked", "dsml_leak", "provider_schema_400_count", "security",
    "user_id_hash_status",
    # `strict` identifies the configuration under test, so a record without it is
    # not attributable to a config at all. Previously only fc_loop was checked, so
    # a legacy record could omit it entirely and still validate — which meant the
    # control arm was never pinned to a known configuration.
    "strict",
    # Cost is half of what this A/B decides. A turn whose token usage we failed to
    # observe must not average in as if it were free, so the STATUS is required even
    # though llm_usage itself is not.
    "llm_usage_status",
)

# Required only of a v3+ producer, and only on a turn that could observe them.
# These are the denominators for the cost / tool-overhead side of the gate, so
# null/missing must not aggregate as a free zero-call turn. A v2 record was
# allowed to emit null here and is NOT retroactively in breach.
REQUIRED_TOP_FIELDS_V3 = SCHEMA_V3_FIELDS

# Outcomes on which the turn's own bookkeeping never existed: the exception
# destroyed `final_state`, and `tool_batches` is derived from it. Requiring the v3
# fields here made every crash/5xx record a GUARANTEED violation of a contract it
# is structurally incapable of satisfying — 11 of 11 v3 crash records in the real
# legacy log validated as broken on `tool_batches` alone, with 14% of that log's
# history being crash/server_error, i.e. a permanent INSTRUMENTATION-HOLD whose
# stated reason had nothing to do with the candidate. A crashed turn still HOLDs
# for the reasons it always did (security counters null, llm_usage_status
# not_instrumented); it is no longer ALSO charged with a field it can never have.
UNOBSERVABLE_OUTCOMES = ("crash", "server_error")

# `tool_ledger_status` is how a v3 producer STATES that gap rather than leaving it
# to be inferred. It is checked in both directions: "unavailable" is legal only on
# an unobservable outcome (so a healthy turn cannot opt out of the requirement) and
# obliges `tool_batches` to be null (so the marker cannot sit next to a number and
# mean nothing). Absent is accepted — historical v3 records predate it, and the
# outcome-based exemption above already covers them.
TOOL_LEDGER_COMPLETE = "complete"
TOOL_LEDGER_UNAVAILABLE = "unavailable"
VALID_TOOL_LEDGER_STATUSES = (TOOL_LEDGER_COMPLETE, TOOL_LEDGER_UNAVAILABLE)

VALID_USAGE_STATUSES = ("complete", "partial", "no_llm_calls", "not_instrumented")
# "partial" == calls happened that we could not price; "not_instrumented" == no
# observer at all. Either way the turn's reported spend is an undercount of unknown
# size, which is worse than refusing to answer.
USAGE_STATUS_HOLD = ("partial", "not_instrumented")

#: Additive v3 field: was the LangChain CALLBACK observer attached while the turn
#: ran? Absent on every record written before it existed, and the exemption below
#: is granted only on an explicit `True`, so an older record keeps the old reading.
OBSERVER_INSTALLED_FIELD = "llm_observer_installed"
#: The unmeasured usage status an unobservable turn may carry WITHOUT being charged
#: as an instrumentation violation. `not_instrumented` is deliberately absent: it
#: asserts that nothing was watching, which contradicts the flag that grants the
#: exemption in the first place.
USAGE_STATUS_UNOBSERVABLE_OK = ("partial",)

# A process-random HMAC salt would re-hash the same user differently after every
# restart, silently decorrelating user counts across windows. The producer refuses
# to do that and reports this status instead; the gate must hold on it.
HASH_STATUS_UNKEYED = "unkeyed_no_stable_secret"
REQUIRED_SECURITY_FIELDS = (
    "denied_write_count", "tainted_write_executed_count", "forbidden_write_executed_count",
)

# Metrics prod telemetry cannot determine. A record declares these in `eval_only`;
# they are reported as EVAL-ONLY rather than counted or held on. They are NEVER
# coerced to False/0 — an unmeasured metric is not a clean one.
EVAL_ONLY_KNOWN = ("forbidden_read", "no_evidence_numbers")

VALID_OUTCOMES = ("ok", "agent_error", "crash", "server_error")
FC_RUNTIME_ARCHES = ("fc_loop", "manager_v1")
VALID_ARCHES = ("fc_loop", "manager_v1", "legacy")
VALID_HASH_STATUSES = ("keyed", "no_user", HASH_STATUS_UNKEYED)
VALID_CANARY_WEIGHTS = (0, 5, 20, 50, 100)
VALID_TRAFFIC_SOURCES = ("direct", "edge")
VALID_ASSIGNED_POOLS = ("direct", "legacy", "candidate")
_SPECIALIST_ROLES = frozenset({"listings", "mobility", "area_evidence"})
# `partial` is a terminal outcome in its own right: usable output AND an unmet
# part of the objective. It is NOT scored as a failure — doing so would trip the
# specialist failure-rate stage-pause on turns that answered the user perfectly
# well — and NOT as a completion, which would hide a systematic shortfall.
_SPECIALIST_STATUSES = frozenset(
    {"planned", "started", "completed", "partial", "failed", "skipped"}
)
_SPECIALIST_TERMINAL_STATUSES = frozenset(
    {"completed", "partial", "failed", "skipped"}
)
_SPECIALIST_OUTCOME_STATUSES = frozenset({"partial", "failed", "skipped"})
# A refused dispatch inside an already-running task. Carries no task identity, so
# it is validated as its own shape and never enters the lifecycle arithmetic.
_SPECIALIST_DENIED_STATUS = "denied"
_SPECIALIST_EVENT_FIELDS = frozenset({
    "plan_id", "task_id", "parent_task_id", "role", "status",
    "duration_ms", "call_count",
})
_SPECIALIST_EVENT_FIELDS_WITH_CODE = _SPECIALIST_EVENT_FIELDS | {"error_code"}
_SPECIALIST_DENIED_EVENT_FIELDS = frozenset({"status", "tool", "error_code"})
_SPECIALIST_COUNTER_FIELDS = (
    "planned", "started", "completed", "failed", "skipped", "max_in_flight",
)
# Optional for the CONSUMER only: a record from a manager_v1 build that predates
# these counters has neither, and 0 is the correct reading there (nothing was
# counted) where defaulting a core counter would be a fabrication.
_SPECIALIST_OPTIONAL_COUNTER_FIELDS = (
    "partial", "denied_calls", "dropped_error_codes",
)
_ALL_SPECIALIST_COUNTER_FIELDS = (
    _SPECIALIST_COUNTER_FIELDS + _SPECIALIST_OPTIONAL_COUNTER_FIELDS
)

#: The per-turn specialist lifecycle block.  Earlier schema-v3 drafts named this
#: block ``multi_agent``; it was renamed on 2026-08-31 because specialists make no
#: model call of their own and share the manager's context, so the old name
#: overclaimed the architecture.  Producers emit ``specialist`` ONLY.  The legacy
#: key is still READ here so a stray row written by a pre-rename build is
#: interpreted rather than convicted as a contract violation.
SPECIALIST_BLOCK_KEY = "specialist"
LEGACY_SPECIALIST_BLOCK_KEY = "multi_agent"

#: Sentinel for "the record carries no lifecycle block at all", which is legal and
#: must not be confused with a block whose value is literally ``None`` (which is
#: a malformed block and IS a violation).
_NO_SPECIALIST_BLOCK = object()


def specialist_block(rec: dict) -> tuple:
    """``(block, problems)`` for a record's specialist lifecycle diagnostic.

    ``block`` is ``_NO_SPECIALIST_BLOCK`` when the record carries neither key.
    Carrying BOTH is a violation: there is no defined precedence between two
    lifecycle blocks, and silently preferring one would let a mismatched pair
    through the gate reporting whichever half happened to be well formed.
    """
    has_new = SPECIALIST_BLOCK_KEY in rec
    has_old = LEGACY_SPECIALIST_BLOCK_KEY in rec
    if has_new and has_old:
        return rec[SPECIALIST_BLOCK_KEY], [
            f"record carries both {SPECIALIST_BLOCK_KEY!r} and legacy "
            f"{LEGACY_SPECIALIST_BLOCK_KEY!r} lifecycle blocks; exactly one is allowed"
        ]
    if has_new:
        return rec[SPECIALIST_BLOCK_KEY], []
    if has_old:
        return rec[LEGACY_SPECIALIST_BLOCK_KEY], []
    return _NO_SPECIALIST_BLOCK, []
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


def _check_count(name: str, v) -> List[str]:
    """A counter must be a non-negative int. Booleans are rejected explicitly (True
    would sum as 1 and quietly fabricate an event)."""
    if isinstance(v, bool):
        return [f"{name}={v!r} is a bool, expected a non-negative int"]
    if not isinstance(v, int):
        return [f"{name}={v!r} is not an int"]
    if v < 0:
        return [f"{name}={v} is negative (would cancel a real violation when summed)"]
    return []


def _validate_specialist(value: object, *, crashed: bool = False) -> List[str]:
    """Validate the content-free specialist lifecycle projection.

    Counters are authoritative even when the bounded event ring truncates.  When
    it did not truncate, the event stream is also reconciled so a malformed or
    silently filtered transition cannot make a broken lifecycle look complete.

    Turn-end invariants (the contract with the dispatcher):

        planned >= started
        started == completed + partial + failed
        completed + partial + failed + skipped == planned
        skipped <= planned - started

    The previous form was ``planned == completed+failed+skipped`` and
    ``started == completed+failed``. Both were arithmetically unsatisfiable the
    moment a task ended ``partial``: the outcome existed in the producer and in
    no counter the consumer added up, so a perfectly healthy turn read as
    "lifecycle incomplete".

    The third line is the accounting rule that the ``partial`` rewrite dropped and
    this restores. Without it ``planned=10, started=0, skipped=0`` validated — ten
    tasks the manager planned and then simply lost, with no counter recording what
    became of them. The ONLY legal way for a planned task not to start is
    ``skipped``, so every planned task must land in exactly one terminal bucket.
    (``skipped <= planned - started`` is kept as the tighter statement that a task
    cannot be skipped after it started.)

    ``crashed`` exempts the ARITHMETIC, not the shape. A turn that died mid-flight
    genuinely has tasks with no terminal transition; convicting the record for that
    reports "broken instrumentation" about working instrumentation observing a
    crash. Every event is still validated for shape and safety.
    """
    prefix = "specialist"
    if not isinstance(value, dict):
        return [f"{prefix} is not an object"]
    problems: List[str] = []
    counts: Dict[str, int] = {}
    for field in _SPECIALIST_COUNTER_FIELDS:
        raw = value.get(field)
        field_problems = _check_count(f"{prefix}.{field}", raw)
        problems.extend(field_problems)
        if not field_problems:
            counts[field] = raw
    for field in _SPECIALIST_OPTIONAL_COUNTER_FIELDS:
        if field not in value:
            counts[field] = 0
            continue
        raw = value.get(field)
        field_problems = _check_count(f"{prefix}.{field}", raw)
        problems.extend(field_problems)
        if not field_problems:
            counts[field] = raw
    truncated = value.get("events_truncated")
    if not isinstance(truncated, bool):
        problems.append(f"{prefix}.events_truncated is not a boolean")
    events = value.get("events")
    if not isinstance(events, list):
        problems.append(f"{prefix}.events is not a list")
        events = []

    if len(counts) == len(_ALL_SPECIALIST_COUNTER_FIELDS) and not crashed:
        planned = counts["planned"]
        started = counts["started"]
        started_terminal = counts["completed"] + counts["partial"] + counts["failed"]
        accounted = started_terminal + counts["skipped"]
        max_flight = counts["max_in_flight"]
        if planned < started:
            problems.append(
                f"specialist lifecycle incomplete: started={started} exceeds "
                f"planned={planned}"
            )
        if started != started_terminal:
            problems.append(
                f"specialist lifecycle must be balanced: started={started} but "
                f"completed+partial+failed={started_terminal}"
            )
        if accounted != planned:
            problems.append(
                f"specialist lifecycle must account for every planned task: "
                f"planned={planned} but completed+partial+failed+skipped={accounted}"
            )
        if counts["skipped"] > planned - started:
            problems.append(
                f"specialist lifecycle must be balanced: skipped={counts['skipped']} "
                f"exceeds planned-started={planned - started}"
            )
        if max_flight > started:
            problems.append(
                f"specialist.max_in_flight={max_flight} exceeds started={started}"
            )
        if started > 0 and max_flight < 1:
            problems.append("specialist.max_in_flight must be >= 1 when tasks started")

    status_counts = {status: 0 for status in _SPECIALIST_STATUSES}
    denied_events = 0
    task_states: Dict[tuple, dict] = {}
    for index, event in enumerate(events):
        label = f"specialist.events[{index}]"
        if not isinstance(event, dict):
            problems.append(f"{label} is not an object")
            continue
        if event.get("status") == _SPECIALIST_DENIED_STATUS:
            # A denied dispatch is not a task transition. It is validated for shape
            # and safety only, and is deliberately absent from every lifecycle sum:
            # a refusal is the control working, so counting it as a task outcome
            # would both unbalance the invariants and score a working guard as a
            # regression.
            if set(event) != _SPECIALIST_DENIED_EVENT_FIELDS:
                problems.append(f"{label} has missing or unsafe extra fields")
                continue
            tool = event.get("tool")
            if not isinstance(tool, str) or not _TOOL_NAME_RE.fullmatch(tool):
                problems.append(f"{label}.tool is not a tool identifier")
            code = event.get("error_code")
            if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
                problems.append(f"{label}.error_code is not a closed-set error code")
            denied_events += 1
            continue
        if set(event) not in (
            _SPECIALIST_EVENT_FIELDS, _SPECIALIST_EVENT_FIELDS_WITH_CODE
        ):
            problems.append(f"{label} has missing or unsafe extra fields")
            continue
        identifiers = []
        for field in ("plan_id", "task_id", "parent_task_id"):
            raw = event.get(field)
            if not isinstance(raw, str) or not _MACHINE_ID_RE.fullmatch(raw):
                problems.append(f"{label}.{field} is not a machine identifier")
            identifiers.append(raw)
        role = event.get("role")
        status = event.get("status")
        if role not in _SPECIALIST_ROLES:
            problems.append(f"{label}.role={role!r} is invalid")
        if status not in _SPECIALIST_STATUSES:
            problems.append(f"{label}.status={status!r} is invalid")
            continue
        problems.extend(_check_count(f"{label}.call_count", event.get("call_count")))
        duration = event.get("duration_ms")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or duration < 0
        ):
            problems.append(f"{label}.duration_ms is not a non-negative finite number")
        if "error_code" in event:
            code = event.get("error_code")
            if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
                problems.append(f"{label}.error_code is not a closed-set error code")
            # An error code on planned/started explains an outcome that has not
            # happened yet — a producer bug, not a diagnostic.
            elif status not in _SPECIALIST_OUTCOME_STATUSES:
                problems.append(
                    f"{label}.error_code is not allowed on status {status!r}"
                )
        status_counts[status] += 1

        # A truncated deque may legitimately begin after a task's plan/start, so
        # only reconstruct order when the producer says the complete stream fits.
        if truncated is False:
            key = (identifiers[0], identifiers[1])
            state = task_states.setdefault(
                key,
                {"parent": identifiers[2], "role": role, "seen": set()},
            )
            if state["parent"] != identifiers[2] or state["role"] != role:
                problems.append(f"{label} changes immutable task identity")
            seen = state["seen"]
            if status in seen:
                problems.append(f"{label} duplicates status {status!r}")
            if status != "planned" and "planned" not in seen:
                problems.append(f"{label} occurs before planned")
            if status in {"completed", "partial", "failed"} and "started" not in seen:
                problems.append(f"{label} occurs before started")
            if status == "skipped" and "started" in seen:
                problems.append(f"{label} skips an already-started task")
            if seen.intersection(_SPECIALIST_TERMINAL_STATUSES):
                problems.append(f"{label} occurs after a terminal status")
            seen.add(status)

    if (truncated is False and not crashed
            and len(counts) == len(_ALL_SPECIALIST_COUNTER_FIELDS)):
        for status in _SPECIALIST_STATUSES:
            if status_counts[status] != counts[status]:
                problems.append(
                    f"specialist.{status}={counts[status]} but events contain "
                    f"{status_counts[status]}"
                )
        if denied_events != counts["denied_calls"]:
            problems.append(
                f"specialist.denied_calls={counts['denied_calls']} but events "
                f"contain {denied_events}"
            )
    return problems


def validate_record(rec: dict) -> List[str]:
    """Return this record's contract violations. Empty == conformant.

    Missing AND null both count: ``"dsml_leak": null`` asserts nothing, so it must
    not be allowed to satisfy the gate.

    Rules are applied PER RECORD VERSION. A record is judged by the contract that
    was in force when its producer wrote it, never by a contract that was invented
    afterwards: the v3 requirements below (``llm_calls`` / ``tool_batches``
    non-null, and their reconciliation against ``llm_usage``) are things a v2
    producer was explicitly allowed not to state, so charging a v2 record with
    them is the consumer inventing violations, not finding them. Forward
    compatibility is still refused outright — an UNKNOWN version cannot be
    validated at all, so it holds.

    Rules are also applied PER OBSERVABILITY. A contract may only require what the
    producer was in a position to state. ``tool_batches`` is folded from the
    artifact ledger inside ``final_state``, and a turn that crashed has no
    ``final_state`` — so at v3 every crash/5xx record was a certain violation, and
    a window containing one held forever on an instrumentation complaint that no
    amount of instrumentation could ever satisfy. That is how operators learn to
    ignore INSTRUMENTATION-HOLD. The exemption is narrow (``UNOBSERVABLE_OUTCOMES``
    only) and does not soften anything a crashed turn CAN state: its null security
    counters and ``not_instrumented`` usage status still hold the gate, which is the
    honest reason a crash was never promotable evidence.
    """
    problems: List[str] = []
    ver = rec.get("telemetry_schema_version")
    if isinstance(ver, bool) or not isinstance(ver, int) or ver not in SUPPORTED_SCHEMA_VERSIONS:
        if isinstance(ver, int) and not isinstance(ver, bool) and ver > MIN_SCHEMA_VERSION:
            # Forward-incompatible: we cannot claim to have validated a schema we
            # have never seen, so we refuse rather than guess.
            problems.append(
                f"telemetry_schema_version={ver!r} is newer than this consumer knows "
                f"(supported: {list(SUPPORTED_SCHEMA_VERSIONS)}) — update canary_report.py")
        else:
            problems.append(
                f"telemetry_schema_version={ver!r} not in supported "
                f"{list(SUPPORTED_SCHEMA_VERSIONS)}")
        return problems  # unknown schema: don't cascade every field as its own violation
    declared_eval_only = set(rec.get("eval_only") or ())
    crashed = rec.get("turn_outcome") in UNOBSERVABLE_OUTCOMES
    ledger_status = rec.get("tool_ledger_status")
    if ledger_status is not None:
        if ledger_status not in VALID_TOOL_LEDGER_STATUSES:
            problems.append(
                f"tool_ledger_status={ledger_status!r} not in "
                f"{list(VALID_TOOL_LEDGER_STATUSES)}")
        elif ledger_status == TOOL_LEDGER_UNAVAILABLE:
            if not crashed:
                # Otherwise the marker becomes an opt-out: any turn could declare
                # its own ledger unavailable and stop being measured on tool
                # overhead while still reporting a normal outcome.
                problems.append(
                    f"tool_ledger_status='unavailable' on turn_outcome="
                    f"{rec.get('turn_outcome')!r}: only "
                    f"{list(UNOBSERVABLE_OUTCOMES)} may have no tool ledger")
            if rec.get("tool_batches") is not None:
                problems.append(
                    "tool_ledger_status='unavailable' but tool_batches carries a "
                    "count: the marker and the value contradict each other")
        elif ver >= 3 and rec.get("tool_batches") is None:
            problems.append(
                "tool_ledger_status='complete' but tool_batches is null")
    required = REQUIRED_TOP_FIELDS + (
        REQUIRED_TOP_FIELDS_V3 if (ver >= 3 and not crashed) else ()
    )
    for f in required:
        if f not in rec:
            problems.append(f"missing required field {f!r}")
        elif rec[f] is None:
            problems.append(f"required field {f!r} is null")
    sec = rec.get("security")
    if isinstance(sec, dict):
        for f in REQUIRED_SECURITY_FIELDS:
            if f not in sec:
                problems.append(f"missing required security.{f}")
            elif sec[f] is None:
                problems.append(f"security.{f} is null")
            else:
                problems += _check_count(f"security.{f}", sec[f])
    elif "security" in rec:
        problems.append("security is not an object")

    # --- TYPE / RANGE ------------------------------------------------------
    # Non-null is not enough. These counters are SUMMED across records, so a single
    # negative value would silently cancel a real violation elsewhere in the window.
    for f in ("dsml_blocked", "dsml_leak", "provider_schema_400_count"):
        if rec.get(f) is not None:
            problems += _check_count(f, rec[f])
    lat = rec.get("turn_latency_ms")
    if lat is not None and (isinstance(lat, bool) or not isinstance(lat, (int, float))
                            or lat < 0):
        problems.append(f"turn_latency_ms={lat!r} is not a non-negative number")
    st = rec.get("http_status")
    if st is not None and (isinstance(st, bool) or not isinstance(st, int)
                           or not (100 <= st <= 599)):
        problems.append(f"http_status={st!r} is not a valid HTTP status")
    # Strictly boolean: "0"/"false"/"" are all truthy-or-falsy in ways that differ
    # between _truthy() here and bool() in the producer, and this field decides
    # whether a record counts as the candidate configuration.
    if "strict" in rec and not isinstance(rec["strict"], bool):
        problems.append(f"strict={rec['strict']!r} is not a boolean")
    for field in ("llm_calls", "tool_batches"):
        if rec.get(field) is not None:
            problems += _check_count(field, rec[field])

    # --- ENUMS -------------------------------------------------------------
    if rec.get("endpoint") is not None and record_endpoint(rec) not in KNOWN_ENDPOINTS:
        problems.append(f"endpoint={rec.get('endpoint')!r} not in {list(KNOWN_ENDPOINTS)}")
    if rec.get("turn_outcome") is not None and rec["turn_outcome"] not in VALID_OUTCOMES:
        problems.append(f"turn_outcome={rec['turn_outcome']!r} not in {list(VALID_OUTCOMES)}")
    if rec.get("agent_arch") is not None and rec["agent_arch"] not in VALID_ARCHES:
        problems.append(f"agent_arch={rec['agent_arch']!r} not in {list(VALID_ARCHES)}")
    us = rec.get("llm_usage_status")
    observer_installed = rec.get(OBSERVER_INSTALLED_FIELD)
    if observer_installed is not None and not isinstance(observer_installed, bool):
        problems.append(
            f"{OBSERVER_INSTALLED_FIELD}={observer_installed!r} is not a boolean")
    elif observer_installed is False:
        # THE 2026-07-25 CLASS, stated on the field that actually knows it. The
        # LangChain callback is the only thing that sees ModelRouter calls, so with
        # it absent every counter on this record describes a SUBSET of the turn —
        # `llm_calls`, `llm_usage` and the provider-error counts are floors, and a
        # `complete` status means "complete for what was watched", which is not the
        # same claim.
        #
        # This used to be expressed by degrading the STATUS to `partial`, which had
        # to null the raw path's own observations to avoid contradicting itself —
        # deleting a real 429 to express "something else may be uncounted". Two
        # facts, two fields: the status describes the calls that were observed, and
        # this flag describes whether everything was.
        problems.append(
            f"{OBSERVER_INSTALLED_FIELD}=false: the LLM callback observer was not "
            f"attached, so this record's call counts and token totals omit every "
            f"ModelRouter call the turn made")
    # A turn we could not measure and a process that was not measuring are DIFFERENT
    # facts, and only the second is the defect this gate exists to catch (the
    # 2026-07-25 round: a stale model id produced zero-call telemetry from an
    # unobserved process for a day). A crash/5xx/timeout/cancellation that happened
    # while the callback observer WAS attached is unmeasurable, not uninstrumented:
    # no amount of instrumentation could have priced a call that never reached its
    # completion callback. Charging it as a contract violation held every window
    # containing one — at the crash rates in the real logs, every window — which is
    # how operators learn to ignore INSTRUMENTATION-HOLD, or to switch it off. The
    # spend is still UNMEASURED (canary_cost keeps the turn chargeable and refuses
    # to price it at zero) and the crash still counts against the outcome/5xx rates
    # it already counted against; only the "your telemetry is broken" verdict is
    # withdrawn, and only on positive evidence that it was not.
    #
    # The exemption is narrow by construction: unobservable outcome AND an explicit
    # `llm_observer_installed: true` AND a status that admits partial observation.
    # Absent flag, false flag, `not_instrumented`, or a completed turn -> unchanged.
    unmeasurable_by_outcome = (
        crashed
        and observer_installed is True
        and us in USAGE_STATUS_UNOBSERVABLE_OK
    )
    if us is not None and us not in VALID_USAGE_STATUSES:
        problems.append(f"llm_usage_status={us!r} not in {list(VALID_USAGE_STATUSES)}")
    elif us in USAGE_STATUS_HOLD and not unmeasurable_by_outcome:
        problems.append(
            f"llm_usage_status={us!r}: this turn's token spend is an undercount of "
            f"unknown size, so the cost side of the A/B cannot be evaluated")
    usage = rec.get("llm_usage")
    llm_calls = rec.get("llm_calls")
    if ver < 3:
        # v2 `llm_calls` meant fc super-steps (and null on legacy), so it is NOT
        # the same quantity as `llm_usage.calls` and reconciling them would
        # manufacture a violation out of a definition difference.
        pass
    elif us == "complete":
        if not isinstance(usage, dict):
            problems.append("llm_usage_status='complete' but llm_usage is not an object")
        else:
            for field in ("calls", "input_tokens", "output_tokens", "cache_read_tokens"):
                problems += _check_count(f"llm_usage.{field}", usage.get(field))
            if not isinstance(usage.get("models"), dict):
                problems.append("llm_usage.models is not an object")
            if (
                isinstance(usage.get("calls"), int)
                and not isinstance(usage.get("calls"), bool)
                and isinstance(llm_calls, int)
                and not isinstance(llm_calls, bool)
                and usage["calls"] != llm_calls
            ):
                problems.append(
                    f"llm_usage.calls={usage['calls']} does not match llm_calls={llm_calls}"
                )
        if not isinstance(llm_calls, int) or isinstance(llm_calls, bool) or llm_calls < 1:
            problems.append("llm_usage_status='complete' requires llm_calls >= 1")
    elif us == "no_llm_calls":
        if llm_calls != 0 or isinstance(llm_calls, bool):
            problems.append("llm_usage_status='no_llm_calls' requires llm_calls=0")
        if usage is not None:
            problems.append("llm_usage_status='no_llm_calls' requires llm_usage=null")
    hs = rec.get("user_id_hash_status")
    if hs is not None and hs not in VALID_HASH_STATUSES:
        problems.append(f"user_id_hash_status={hs!r} not in {list(VALID_HASH_STATUSES)}")
    # keyed implies a digest actually exists — otherwise "keyed" asserts nothing.
    if hs == "keyed" and not rec.get("user_id_hash"):
        problems.append("user_id_hash_status=keyed but user_id_hash is absent/empty")
    # Both fc-compatible candidate architectures are defined by strict function
    # calling.  Treating manager_v1 as exempt would admit a different runtime
    # configuration under the same release label.
    arch = rec.get("agent_arch")
    if arch in FC_RUNTIME_ARCHES and not _truthy(rec.get("strict")):
        problems.append(f"agent_arch={arch} but strict is not true (not the candidate config)")
    if arch == "manager_v1":
        specialists = rec.get("manager_v1_specialists")
        if not isinstance(specialists, bool):
            problems.append("manager_v1_specialists is missing/null or not a boolean")
        lifecycle, lifecycle_problems = specialist_block(rec)
        problems.extend(lifecycle_problems)
        if lifecycle is not _NO_SPECIALIST_BLOCK:
            problems.extend(_validate_specialist(lifecycle, crashed=crashed))
            if specialists is False:
                problems.append(
                    "specialist lifecycle present while manager_v1_specialists is false"
                )

    # Additive rollout identity. Historical/direct records may omit it, but any
    # record claiming to come through the trusted edge must be complete and
    # internally consistent. A public-stage invocation applies the stronger
    # requirement that every selected record matches one rollout id/stage/weight.
    variant = rec.get("variant_id")
    if variant is not None:
        expected_variant = (
            f"{arch}:strict-{1 if rec.get('strict') is True else 0}:specialists-"
            f"{1 if rec.get('manager_v1_specialists') is True else 0}"
        )
        if variant != expected_variant:
            problems.append(
                f"variant_id={variant!r} does not match effective config {expected_variant!r}"
            )
    source = rec.get("traffic_source")
    if source is not None and source not in VALID_TRAFFIC_SOURCES:
        problems.append(
            f"traffic_source={source!r} not in {list(VALID_TRAFFIC_SOURCES)}"
        )
    assigned = rec.get("assigned_pool")
    if assigned is not None and assigned not in VALID_ASSIGNED_POOLS:
        problems.append(
            f"assigned_pool={assigned!r} not in {list(VALID_ASSIGNED_POOLS)}"
        )
    if source == "edge":
        rid = rec.get("rollout_id")
        stage = rec.get("rollout_stage")
        weight = rec.get("configured_candidate_percent")
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", rid):
            problems.append("edge record has missing/invalid rollout_id")
        if not isinstance(stage, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,32}", stage):
            problems.append("edge record has missing/invalid rollout_stage")
        if isinstance(weight, bool) or weight not in VALID_CANARY_WEIGHTS:
            problems.append("edge record has missing/invalid configured_candidate_percent")
        expected_pool = "legacy" if arch == "legacy" else "candidate"
        if assigned != expected_pool:
            problems.append(
                f"edge record assigned_pool={assigned!r}, expected {expected_pool!r} "
                f"for agent_arch={arch!r}"
            )

    # An eval-only metric must be declared AND null. Previously only the undeclared
    # case was caught, so a record could declare eval_only and still ship a value —
    # the exact opposite of what the declaration means.
    for f in EVAL_ONLY_KNOWN:
        if f in rec and rec[f] is not None:
            if f in declared_eval_only:
                problems.append(f"{f!r} is declared eval_only but carries a non-null value")
            else:
                problems.append(f"{f!r} carries a prod value but is not declared eval_only")
    # No stable HMAC secret => user_id_hash is not comparable across restarts, so
    # every per-user statistic in this window is unreliable. Hold, don't continue.
    if rec.get("user_id_hash_status") == HASH_STATUS_UNKEYED:
        problems.append(
            "user_id_hash_status=unkeyed_no_stable_secret (no CANARY_USER_HASH_KEY / "
            "FLASK_SECRET_KEY): user hashes are not stable across restarts")
    return problems


def validate_records(records: Sequence[dict], *, candidate_arch: str = "fc_loop") -> dict:
    """Aggregate contract validation across records."""
    offenders: Dict[str, int] = {}
    bad = 0
    for r in records:
        probs = validate_record(r)
        if probs:
            bad += 1
            for p in probs:
                offenders[p] = offenders.get(p, 0) + 1
    # Cross-record: one record per (request_id, endpoint, arch). A duplicate would
    # inflate the turn denominator and so halve every rate — a silent fail-open.
    seen: Dict[tuple, int] = {}
    for r in records:
        k = (r.get("request_id"), record_endpoint(r), r.get("agent_arch"))
        if None in k[:1]:
            continue
        seen[k] = seen.get(k, 0) + 1
    dupes = {k: n for k, n in seen.items() if n > 1}
    if dupes:
        offenders[f"duplicate records for {len(dupes)} (request_id, endpoint, arch) "
                  f"key(s) — one turn must emit exactly one record"] = sum(dupes.values())
        bad = max(bad, sum(dupes.values()))

    # Cross-record: the candidate pool must be ONE build. A window mixing two
    # candidate shas is not a measurement of either of them.
    shas = {r.get("candidate_sha") for r in records
            if r.get("agent_arch") == candidate_arch and r.get("candidate_sha") is not None}
    if len(shas) > 1:
        offenders[f"window mixes {len(shas)} candidate_sha values on {candidate_arch}: "
                  f"{sorted(str(s) for s in shas)}"] = len(shas)
        bad = max(bad, 1)
    # Cross-record: `llm_calls` and `tool_batches` mean different things either
    # side of v3, so a window spanning the upgrade cannot be summed or compared on
    # them. Averaging across the boundary produces a number that describes no
    # build — the same class of error as mixing two candidate shas above, and the
    # reason the version was bumped instead of the fields being changed silently.
    versions = {r.get("telemetry_schema_version") for r in records
                if r.get("telemetry_schema_version") in SUPPORTED_SCHEMA_VERSIONS}
    if len(versions) > 1:
        offenders[
            f"window mixes telemetry_schema_version {sorted(versions)}: "
            f"{list(SCHEMA_V3_FIELDS)} were redefined at v{CURRENT_SCHEMA_VERSION} "
            f"and are not comparable across the boundary"
        ] = len(versions)
        bad = max(bad, 1)
    variants = {
        r.get("manager_v1_specialists")
        for r in records
        if r.get("agent_arch") == "manager_v1"
        and isinstance(r.get("manager_v1_specialists"), bool)
    }
    if candidate_arch == "manager_v1" and len(variants) > 1:
        offenders["window mixes manager_v1_specialists=false/true variants"] = len(variants)
        bad = max(bad, 1)
    return {
        "records": len(records),
        "conformant": len(records) - bad,
        "violating": bad,
        "violations": dict(sorted(offenders.items(), key=lambda kv: -kv[1])),
        "candidate_shas": sorted(str(s) for s in shas),
        "schema_versions": sorted(versions),
        "manager_v1_specialist_variants": sorted(variants),
        "ok": bad == 0 and len(records) > 0,
    }


def record_endpoint(rec: dict) -> str:
    v = rec.get("endpoint")
    return str(v).strip().lower() if v is not None else ""


# --------------------------------------------------------------------------- #
# Line / timestamp parsing (tolerant)                                         #
# --------------------------------------------------------------------------- #

# Leading "2026-07-20T12:34:56.123Z" or "2026-07-20 12:34:56,123" style prefix.
_TS_PREFIX_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)


def parse_line(line: str) -> Optional[dict]:
    """Parse one log line into a record dict, tolerant of both shapes:

      * bare JSON:                ``{"agent_arch": "fc", ...}``
      * prefixed:  ``2026-07-20T12:34:56 INFO canary.turn: {"agent_arch": "fc", ...}``

    Returns ``None`` for blank lines or lines with no decodable JSON object. When a leading
    timestamp prefix is present and the JSON has no ``ts``, the parsed prefix is stashed on
    the record under the private key ``_line_ts`` (a datetime) for later use.
    """
    if line is None:
        return None
    s = line.strip()
    if not s:
        return None
    brace = s.find("{")
    if brace < 0:
        return None
    # raw_decode tolerates trailing content after the object; anchor at the first '{'.
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[brace:])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    if brace > 0:
        prefix_ts = _line_ts_prefix(s[:brace])
        if prefix_ts is not None:
            obj.setdefault("_line_ts", prefix_ts)
    return obj


def _line_ts_prefix(prefix: str) -> Optional[datetime]:
    m = _TS_PREFIX_RE.match(prefix)
    if not m:
        return None
    return parse_ts(m.group(1))


def parse_ts(value) -> Optional[datetime]:
    """Coerce a timestamp (epoch seconds/millis, or an ISO-8601 string) to an aware UTC
    datetime. Returns ``None`` if it cannot be interpreted."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        secs = float(value)
        if secs > 1e12:        # milliseconds since epoch
            secs /= 1000.0
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Pure numeric string -> epoch.
        try:
            return parse_ts(float(s))
        except ValueError:
            pass
        s = s.replace(" ", "T", 1).replace(",", ".")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def record_ts(rec: dict) -> Optional[datetime]:
    """Best-effort turn timestamp: the record's own ``ts`` first, else the parsed log-line
    prefix stashed by :func:`parse_line`."""
    ts = parse_ts(rec.get("ts"))
    if ts is not None:
        return ts
    lt = rec.get("_line_ts")
    if isinstance(lt, datetime):
        return lt if lt.tzinfo else lt.replace(tzinfo=timezone.utc)
    return None


# --------------------------------------------------------------------------- #
# Input loading                                                               #
# --------------------------------------------------------------------------- #

#: The only suffixes a DISCOVERED file may have. An explicitly named file is
#: honoured whatever it is called — that is the operator stating an intent — but
#: anything this script finds for itself must look like a canary log.
LOG_SUFFIXES = (".jsonl", ".log", ".ndjson")


def resolve_inputs(inputs: Sequence[str]) -> List[str]:
    """Expand --input values (files, directories, or globs) into a sorted, de-duplicated
    list of file paths. Directories are searched recursively for ``*.jsonl`` and ``*.log``.

    Discovery is suffix-filtered, and the filter is applied to GLOB results too, not
    just to directory walks. The live log directory holds sidecar files —
    ``canary-legacy.jsonl.bak-20260831`` is a 2 973-record pre-cleanup backup of the
    log next to it — and pulling one in would double every record it copies:
    duplicate request_ids (a HOLD whose stated reason is a duplicate-emission bug
    that never happened) and, for that particular file, the mixed-schema HOLD the
    cleanup existed to remove. The directory walk already excluded it by accident of
    its suffix; ``--input '.runtime/logs/*'`` did not, and neither would a future
    widening of the walk's patterns. So the rule is stated once, here.
    """
    paths: List[str] = []
    for item in inputs:
        if any(c in item for c in "*?[") and not os.path.isdir(item):
            paths.extend(p for p in _glob.glob(item, recursive=True)
                         if p.endswith(LOG_SUFFIXES))
        elif os.path.isdir(item):
            for pat in LOG_SUFFIXES:
                paths.extend(_glob.glob(os.path.join(item, "**", f"*{pat}"),
                                        recursive=True))
        else:
            paths.append(item)
    seen, out = set(), []
    for p in sorted(paths):
        ap = os.path.abspath(p)
        if ap not in seen and os.path.isfile(ap):
            seen.add(ap)
            out.append(ap)
    return out


def load_records(inputs: Sequence[str]) -> Tuple[List[dict], int]:
    """Load every ``canary.turn`` record from the resolved inputs. Returns
    ``(records, skipped_line_count)``. Non-``canary.turn`` records are dropped (the log may
    interleave other events); the name is matched leniently so a bare-JSON stream without an
    explicit name field is still accepted."""
    records: List[dict] = []
    skipped = 0
    for path in resolve_inputs(inputs):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = parse_line(line)
                if rec is None:
                    skipped += 1
                    continue
                if not _is_canary_turn(rec):
                    continue
                records.append(rec)
    return records, skipped


def _is_canary_turn(rec: dict) -> bool:
    name = rec.get("event") or rec.get("name") or rec.get("record") or ""
    if isinstance(name, str) and name:
        return name.strip().lower() in {"canary.turn", "canary_turn"}
    # No explicit event name: accept as a turn record if it carries the arch discriminator.
    return "agent_arch" in rec or "arch" in rec


# --------------------------------------------------------------------------- #
# Field extraction                                                            #
# --------------------------------------------------------------------------- #

def record_arch(rec: dict) -> str:
    """Return the exact normalised architecture label; never guess a fallback."""
    return str(rec.get("agent_arch") or rec.get("arch") or "").strip().lower()


def canonical_arch(rec: dict) -> str:
    """Backward-compatible display bucket for old report consumers.

    Both strict function-calling runtimes are candidate-capable.  Unknown labels
    remain ``unknown`` instead of being silently counted as legacy/control.
    Exact release selection is handled by ``candidate_arch``/``control_arch``.
    """
    raw = record_arch(rec)
    if raw in {"fc", *FC_RUNTIME_ARCHES}:
        return "fc"
    if raw == "legacy":
        return "legacy"
    return "unknown"


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(v)


def _first_present(rec: dict, keys: Sequence[str]) -> Optional[object]:
    for k in keys:
        if k in rec:
            return rec[k]
    return None


def _has_any_key(rec: dict, keys: Sequence[str]) -> bool:
    return any(k in rec for k in keys)


def audit_outcome(rec: dict) -> str:
    """Normalise the ``security_audit`` field to a lowercase outcome string. Accepts a bare
    string, or an object carrying ``outcome``/``result``/``status``."""
    v = rec.get("security_audit")
    if v is None:
        return ""
    if isinstance(v, dict):
        v = v.get("outcome") or v.get("result") or v.get("status") or v.get("verdict") or ""
    return str(v).strip().lower()


def _audit_is_clean(o: str) -> bool:
    return o in _CLEAN_AUDIT_VALUES


def _audit_safe_denied(o: str) -> bool:
    return any(t in o for t in _SAFE_AUDIT_TOKENS)


def latency_ms(rec: dict) -> Optional[float]:
    v = _first_present(rec, ("turn_latency_ms", "latency_ms", "turn_latency"))
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify(rec: dict) -> dict:
    """Per-record flags used by the aggregator — schema v2, structured parsing.

    The producer's ``security`` object is read DIRECTLY; there is no string-token
    sniffing of a free-form outcome. That closes a v1 contract break: the producer
    emitted ``security_audit: {"denied_writes": N}``, which carries none of the
    ``outcome``/``result``/``status`` keys ``audit_outcome()`` looks for, so every
    record — including ones with denied writes — normalised to "" and was scored
    CLEAN.

    Semantics, kept deliberately distinct:
      * denied_write_count      -> non-clean, but SAFE (the write was blocked)
      * *_write_executed_count  -> zero-tolerance (the bad write actually ran)
    """
    sec = rec.get("security")
    sec = sec if isinstance(sec, dict) else {}
    denied = _to_int(sec.get("denied_write_count"))
    tainted_exec = _to_int(sec.get("tainted_write_executed_count"))
    forbidden_exec = _to_int(sec.get("forbidden_write_executed_count"))

    # 5xx is observable from the turn record itself in v2 (http_status/turn_outcome).
    status = rec.get("http_status")
    try:
        status_i = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_i = None
    is_5xx = (status_i is not None and status_i >= 500) or \
             (str(rec.get("turn_outcome") or "").lower() == "server_error")

    # A boundary 5xx never completed a turn, so its "latency" is not a turn latency.
    # Count it as a 5xx, but keep it out of the percentile population (otherwise a
    # burst of fast failures would *improve* p50).
    lat = None if is_5xx else latency_ms(rec)
    return {
        "soft_wrapped": _truthy(rec.get("soft_wrapped")),
        # OPTIONAL field (added after v2 shipped): keep PRESENCE and VALUE apart. A producer
        # predating it omits the KEY, which must read as not-instrumented — never as
        # "not canned". Folding an absent field into a benign value is precisely the shape
        # of the v1-consumer `security_audit` false-green.
        "wrapped_by_present": "wrapped_by" in rec,
        "wrapped_by": rec.get("wrapped_by"),
        "partial": _truthy(rec.get("partial")),
        "tool_budget_timeout": _truthy(rec.get("tool_budget_timeout")),
        # non-clean == any security event at all (denied OR executed)
        "security_non_clean": bool(denied or tainted_exec or forbidden_exec),
        "denied_write": denied,
        "tainted_unauth_write": bool(tainted_exec),
        "forbidden_write": bool(forbidden_exec),
        "latency_ms": lat,
        "over_slo": (lat is not None and lat > OVER_SLO_MS),
        # zero-tolerance signals, now mandatory in the record (validated upstream)
        "dsml_blocked": _to_int(rec.get("dsml_blocked")),
        "dsml_leak": _to_int(rec.get("dsml_leak")) > 0,
        "api_400": _to_int(rec.get("provider_schema_400_count")),
        "http_5xx": 1 if is_5xx else 0,
        # eval-only: never coerced to False here; aggregation reports them as None
        "forbidden_read": rec.get("forbidden_read"),
        "no_evidence_numbers": rec.get("no_evidence_numbers"),
    }


def _to_int(v) -> int:
    if v is None or isinstance(v, bool):
        return 1 if v is True else 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 1 if _truthy(v) else 0


# --------------------------------------------------------------------------- #
# Percentile (repo nearest-rank convention)                                   #
# --------------------------------------------------------------------------- #

def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """NEAREST-RANK percentile: value at 0-based index ``ceil(pct*n)-1`` (clamped to
    ``[0, n-1]``) of the sorted samples. Mirrors ``evaluation/run_benchmark._percentile``."""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if not n:
        return None
    idx = math.ceil(pct * n) - 1
    if idx < 0:
        idx = 0
    elif idx > n - 1:
        idx = n - 1
    return vals[idx]


# --------------------------------------------------------------------------- #
# Windowing                                                                   #
# --------------------------------------------------------------------------- #

def reference_now(records: Sequence[dict], override: Optional[datetime]) -> datetime:
    if override is not None:
        return override
    stamps = [record_ts(r) for r in records]
    stamps = [s for s in stamps if s is not None]
    if stamps:
        return max(stamps)
    return datetime.now(timezone.utc)


def window_bounds(window_hours: Optional[float], since: Optional[datetime],
                  now: datetime) -> Tuple[Optional[datetime], str]:
    """Return ``(cutoff, description)`` for the record filter that will ACTUALLY be applied.

    ``--window HOURS`` and ``--since ISO`` are both LOWER BOUNDS on a record's ts, so
    when both are given the effective cutoff is the LATER of the two (the intersection
    — the most restrictive bound wins). ``cutoff`` is None only when neither was given.

    The human-readable description is produced HERE, next to the cutoff it describes,
    so no other code has to restate the filter from memory. It previously did: the
    ``--expect-turns`` anchor printed a fixed string, ``"the selected --window / --since
    range"``, while ``--since`` was parsed and then used ONLY to compute stage
    elapsed-hours — it never filtered anything. The report therefore claimed a bound it
    had not applied, and the first run of the 2026-07-25 internal round counted a
    warm-up turn from before the stated start and returned INSTRUMENTATION-HOLD.
    """
    labels: List[str] = []
    cutoff: Optional[datetime] = None
    if window_hours is not None:
        wcut = datetime.fromtimestamp(now.timestamp() - window_hours * 3600.0,
                                      tz=timezone.utc)
        cutoff = wcut
        labels.append(f"--window {window_hours:g}h before {now.isoformat()}")
    if since is not None:
        labels.append(f"--since {since.isoformat()}")
        if cutoff is None or since > cutoff:
            cutoff = since
    if cutoff is None:
        return None, "UNFILTERED: every dated record in the inputs (no --window, no --since)"
    prefix = "the later of " if len(labels) > 1 else ""
    return cutoff, f"ts >= {cutoff.isoformat()} ({prefix}{' and '.join(labels)})"


def filter_window(records: Sequence[dict], window_hours: Optional[float],
                  now: datetime, since: Optional[datetime] = None) -> List[dict]:
    """Keep records at/after the effective cutoff from :func:`window_bounds` — i.e. within
    ``window_hours`` of ``now`` AND not older than ``since``. Records lacking any timestamp
    are dropped when a filter is requested (they cannot be placed; ``build_report``
    partitions them out first and holds the gate on them).

    ``since`` is honoured as a filter here. A bound that is parsed, stored where a reader
    could find it, and then never applied is worse than no bound at all: every caller
    reads the flag as "judge only this stage's traffic", and the population silently
    keeps the turns the operator meant to exclude.
    """
    cutoff, _ = window_bounds(window_hours, since, now)
    if cutoff is None:
        return list(records)
    cut = cutoff.timestamp()
    kept = []
    for r in records:
        ts = record_ts(r)
        if ts is not None and ts.timestamp() >= cut - 1e-6:
            kept.append(r)
    return kept


# --------------------------------------------------------------------------- #
# Aggregation                                                                 #
# --------------------------------------------------------------------------- #

def _rate(count: int, total: int) -> float:
    return (count / total) if total else 0.0


def aggregate_zero_tolerance(records: Sequence[dict]) -> dict:
    """Aggregate the SECURITY / correctness zero-tolerance signals across ALL public
    endpoints for one arch.

    Deliberately separate from :func:`aggregate_arch`, which is endpoint-scoped for
    the A/B: latency and degradation are only comparable on the agent endpoint, but
    "a forbidden write executed" is a breach wherever it happened.
    """
    flags = [classify(r) for r in records]
    by_ep: Dict[str, dict] = {}
    for r, f in zip(records, flags):
        ep = record_endpoint(r) or "unknown"
        s = by_ep.setdefault(ep, {"turns": 0, "tainted_unauth_write_count": 0,
                                  "forbidden_write_count": 0, "dsml_leak_count": 0,
                                  "api_400_count": 0, "api_400_total": 0})
        s["turns"] += 1
        s["tainted_unauth_write_count"] += 1 if f["tainted_unauth_write"] else 0
        s["forbidden_write_count"] += 1 if f["forbidden_write"] else 0
        s["dsml_leak_count"] += 1 if f["dsml_leak"] else 0
        s["api_400_count"] += 1 if f["api_400"] > 0 else 0
        s["api_400_total"] += max(0, f["api_400"])
    return {
        "turns": len(records),
        "tainted_unauth_write_count": sum(1 for f in flags if f["tainted_unauth_write"]),
        "forbidden_write_count": sum(1 for f in flags if f["forbidden_write"]),
        "dsml_leak_count": sum(1 for f in flags if f["dsml_leak"]),
        # AFFECTED RECORDS, not a sum of raw counters. Every other zero-tolerance
        # signal already counted records; this one summed values, so a negative
        # counter anywhere in the window could cancel a real 400 and downgrade a
        # BLOCK to a HOLD. Both still refuse to promote, but the operator would be
        # told "instrumentation problem" when the truth was "provider rejected our
        # schema". The magnitude is kept alongside, floored at 0.
        "api_400_count": sum(1 for f in flags if f["api_400"] > 0),
        "api_400_total": sum(max(0, f["api_400"]) for f in flags),
        "by_endpoint": by_ep,
    }


def aggregate_arch(records: Sequence[dict]) -> dict:
    """Aggregate one arch's records into a stats dict."""
    flags = [classify(r) for r in records]
    turns = len(records)
    convos = {str(r.get("conversation_id")) for r in records if r.get("conversation_id") is not None}
    # v2 removed the raw user_id; counting it would report 0 users forever.
    users = {str(r.get("user_id_hash")) for r in records
             if r.get("user_id_hash") is not None}
    lats = [f["latency_ms"] for f in flags if f["latency_ms"] is not None]

    soft = sum(1 for f in flags if f["soft_wrapped"])
    part = sum(1 for f in flags if f["partial"])
    tbt = sum(1 for f in flags if f["tool_budget_timeout"])
    degraded = sum(1 for f in flags if f["soft_wrapped"] or f["partial"])
    over = sum(1 for f in flags if f["over_slo"])

    # Canned-fallback attribution, computed over WRAPPED turns only — the CONVERSION rate is
    # the number that matters to a user. A wrap that still produced a model-written answer is
    # not a degraded experience; one that emitted the deterministic renderer is. Turns whose
    # producer predates `wrapped_by` are counted separately and reported as not-instrumented,
    # never folded in as "not canned".
    wrapped = [f for f in flags if f["soft_wrapped"]]
    attributed = [f for f in wrapped if f["wrapped_by_present"]]
    canned = sum(1 for f in attributed
                 if str(f["wrapped_by"] or "").startswith("fallback"))
    non_clean = sum(1 for f in flags if f["security_non_clean"])

    # v2: the gate-relevant metrics are MANDATORY fields validated before we get
    # here, so they are always countable. No key-existence guessing.
    dsml = sum(1 for f in flags if f["dsml_leak"])
    dsml_blk = sum(f["dsml_blocked"] for f in flags)
    api400 = sum(f["api_400"] for f in flags)
    x5 = sum(f["http_5xx"] for f in flags)

    # Eval-only metrics: None unless a record actually carries an observation.
    # Absent stays None — it must never collapse to 0 and read as "clean".
    def _eval_only_count(key: str) -> Optional[int]:
        observed = [f[key] for f in flags if f[key] is not None]
        return sum(1 for v in observed if _truthy(v)) if observed else None

    fr = _eval_only_count("forbidden_read")
    nen = _eval_only_count("no_evidence_numbers")
    # Reads the legacy ``multi_agent`` key as an alias so a pre-rename row still
    # contributes its counters instead of silently dropping out of the denominator.
    multi = [block for block in (specialist_block(r)[0] for r in records)
             if isinstance(block, dict)]
    specialist_totals = {
        field: sum(_to_int(item.get(field)) for item in multi)
        for field in _ALL_SPECIALIST_COUNTER_FIELDS
    }
    specialist_planned = specialist_totals["planned"]
    # `skipped` is the terminal a task gets when it never STARTED — dispatch denied
    # at preparation, or the turn budget exhausted before the task ran. It is
    # therefore the bucket that a completely non-functional specialist runtime
    # lands in, and the report has to say so out loud AND say why: the error-code
    # breakdown is what turns "180 planned tasks produced nothing" into a
    # diagnosis instead of a mystery. The event ring is bounded, so this is a
    # best-effort attribution of an authoritative counter — a truncated record
    # still contributes its `skipped` count, just fewer codes.
    skipped_codes: Dict[str, int] = {}
    for block in multi:
        events = block.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict) or event.get("status") != "skipped":
                continue
            code = event.get("error_code")
            code = code if isinstance(code, str) and code else "unspecified"
            skipped_codes[code] = skipped_codes.get(code, 0) + 1

    # Forgiven must never mean invisible. These two counters are what let a reader
    # see how much of the window went unpriced, and how much of THAT was the
    # unobservable-outcome exemption rather than broken instrumentation.
    unmeasured_spend = sum(
        1 for r in records if r.get("llm_usage_status") in USAGE_STATUS_HOLD
    )
    unobservable_unmeasured = sum(
        1 for r in records
        if r.get("turn_outcome") in UNOBSERVABLE_OUTCOMES
        and r.get(OBSERVER_INSTALLED_FIELD) is True
        and r.get("llm_usage_status") in USAGE_STATUS_UNOBSERVABLE_OK
    )

    return {
        "turns": turns,
        "conversations": len(convos),
        "unmeasured_spend_turns": unmeasured_spend,
        "unobservable_unmeasured_turns": unobservable_unmeasured,
        "users": len(users),
        "candidate_shas": sorted({str(r.get("candidate_sha")) for r in records
                                  if r.get("candidate_sha") is not None}),
        "strict_true": sum(1 for r in records if _truthy(r.get("strict"))),
        "llm_calls_total": sum(_to_int(r.get("llm_calls")) for r in records),
        "tool_batches_total": sum(_to_int(r.get("tool_batches")) for r in records),
        # The denominator for the line above. A crashed turn has no artifact
        # ledger, so it contributes a null that sums as 0; dividing the total by
        # `turns` would then report tool overhead per turn as if those turns had
        # run no tools. This is the count of turns that could actually state it.
        "tool_batches_observed_turns": sum(
            1 for r in records if r.get("tool_batches") is not None
        ),
        "latency_n": len(lats),
        "p50_ms": percentile(lats, 0.50),
        "p95_ms": percentile(lats, 0.95),
        "over_30s_count": over,
        "over_30s_rate": _rate(over, turns),
        "soft_wrapped_count": soft,
        "soft_wrapped_rate": _rate(soft, turns),
        # Of the wrapped turns that CAN be attributed, how many ended in canned text.
        # None (-> "n/a") when nothing is attributable, so an un-instrumented producer
        # reads as unmeasured rather than as a clean 0%.
        "wrapped_canned_count": canned,
        "wrapped_canned_rate": (_rate(canned, len(attributed)) if attributed else None),
        "wrapped_unattributed_count": len(wrapped) - len(attributed),
        "partial_count": part,
        "partial_rate": _rate(part, turns),
        "tool_budget_timeout_count": tbt,
        "tool_budget_timeout_rate": _rate(tbt, turns),
        "degraded_count": degraded,
        "degraded_rate": _rate(degraded, turns),
        "security_non_clean_count": non_clean,
        "denied_write_count": sum(f["denied_write"] for f in flags),
        "tainted_unauth_write_count": sum(1 for f in flags if f["tainted_unauth_write"]),
        "forbidden_write_count": sum(1 for f in flags if f["forbidden_write"]),
        # v2: mandatory + validated, so always a number (never "not instrumented")
        "dsml_blocked_count": dsml_blk,
        "dsml_leak_count": dsml,
        "api_400_count": api400,
        "http_5xx_count": x5,
        "http_5xx_rate": (_rate(x5, turns) if x5 is not None else None),
        "forbidden_read_count": fr,
        "forbidden_read_rate": (_rate(fr, turns) if fr is not None else None),
        "no_evidence_numbers_count": nen,
        "no_evidence_numbers_rate": (_rate(nen, turns) if nen is not None else None),
        "manager_v1_specialist_variants": sorted({
            r.get("manager_v1_specialists") for r in records
            if isinstance(r.get("manager_v1_specialists"), bool)
        }),
        "specialist_turns": len(multi),
        "specialist": {
            **specialist_totals,
            # `partial` is deliberately NOT in this numerator. A partial task
            # answered the user with a stated gap; scoring it as a failure would
            # stage-pause a release for behaving honestly, and would also make the
            # rate uninterpretable (it would no longer mean "how often does a
            # specialist not deliver"). It is reported alongside so the shortfall
            # is still visible.
            "failure_rate": (
                _rate(specialist_totals["failed"], specialist_planned)
                if specialist_planned else None
            ),
            "partial_rate": (
                _rate(specialist_totals["partial"], specialist_planned)
                if specialist_planned else None
            ),
            # THE gate metric for specialist delivery. `failure_rate` alone scored
            # only the tasks that ran and then failed, so a candidate whose every
            # dispatch was refused BEFORE it started read as `failed=0` /
            # `failure_rate=0.00%` and passed — a fail-open on the one metric the
            # manager_v1 rollout adds, on exactly the runtime it is supposed to be
            # gating. A planned task that produced no specialist answer is a
            # non-delivery whether it failed loudly or never ran, so both buckets
            # are in this numerator. `partial` is still excluded (see above): it
            # answered the user with a stated gap.
            "non_success_rate": (
                _rate(specialist_totals["failed"] + specialist_totals["skipped"],
                      specialist_planned)
                if specialist_planned else None
            ),
            "skipped_rate": (
                _rate(specialist_totals["skipped"], specialist_planned)
                if specialist_planned else None
            ),
            "skipped_error_codes": dict(
                sorted(skipped_codes.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
        },
        # v2 and v3 `llm_calls`/`tool_batches` are different measurements. Reporting
        # which versions produced this arm is what lets a reader tell a real
        # per-call change from a definition change.
        "schema_versions": sorted(
            {r.get("telemetry_schema_version") for r in records
             if isinstance(r.get("telemetry_schema_version"), int)
             and not isinstance(r.get("telemetry_schema_version"), bool)}
        ),
    }


def _pp_delta(fc_rate: Optional[float], legacy_rate: Optional[float]) -> Optional[float]:
    if fc_rate is None or legacy_rate is None:
        return None
    return (fc_rate - legacy_rate) * 100.0


def compute_deltas(fc: dict, legacy: dict) -> dict:
    """fc-minus-legacy deltas in percentage points for the relative-threshold metrics."""
    return {
        "degraded_rate_pp": _pp_delta(fc["degraded_rate"], legacy["degraded_rate"]),
        "soft_wrapped_rate_pp": _pp_delta(fc["soft_wrapped_rate"], legacy["soft_wrapped_rate"]),
        "partial_rate_pp": _pp_delta(fc["partial_rate"], legacy["partial_rate"]),
        "tool_budget_timeout_rate_pp": _pp_delta(fc["tool_budget_timeout_rate"],
                                                 legacy["tool_budget_timeout_rate"]),
        "over_30s_rate_pp": _pp_delta(fc["over_30s_rate"], legacy["over_30s_rate"]),
        "forbidden_read_rate_pp": _pp_delta(fc["forbidden_read_rate"],
                                            legacy["forbidden_read_rate"]),
        "no_evidence_numbers_rate_pp": _pp_delta(fc["no_evidence_numbers_rate"],
                                                 legacy["no_evidence_numbers_rate"]),
        "http_5xx_rate_pp": _pp_delta(fc["http_5xx_rate"], legacy["http_5xx_rate"]),
    }


# --------------------------------------------------------------------------- #
# Stage progress                                                              #
# --------------------------------------------------------------------------- #

def evaluate_stage(fc: dict, stage: str, since: Optional[datetime],
                   now: datetime) -> dict:
    """Evaluate stage-progress minima: BOTH the turn count AND the elapsed hours must clear
    the stage floor. Returns a dict; ``eligible`` is True only when both hold."""
    min_turns, min_hours = STAGES[stage]
    fc_turns = fc["turns"]
    turns_ok = fc_turns >= min_turns

    if since is None:
        elapsed_hours = None
        hours_ok = False
        reason = "no --since given: stage elapsed time unknown -> not eligible"
    else:
        elapsed_hours = (now - since).total_seconds() / 3600.0
        hours_ok = elapsed_hours >= min_hours
        reason = ""

    eligible = turns_ok and hours_ok
    return {
        "stage": stage,
        "min_turns": min_turns,
        "min_hours": min_hours,
        "since": since.isoformat() if since else None,
        "now": now.isoformat(),
        "fc_turns": fc_turns,
        "elapsed_hours": (round(elapsed_hours, 3) if elapsed_hours is not None else None),
        "turns_ok": turns_ok,
        "hours_ok": hours_ok,
        "eligible": eligible,
        "note": reason,
    }


# --------------------------------------------------------------------------- #
# External anchor: reconcile against the count of turns the driver drove       #
# --------------------------------------------------------------------------- #

def evaluate_expected_turns(windowed: Sequence[dict], expected: int,
                            window_desc: Optional[str] = None,
                            candidate_arch: str = "fc_loop") -> dict:
    """Reconcile the run against an EXTERNAL count of turns that were driven.

    The gate's own counters can only describe records that EXIST. They cannot see a
    turn that produced no record at all — an exception before the emit, a logger
    that lost its sink, a pool that never received the traffic. Every rate then
    divides by a denominator that already excludes the failures, so a run in which
    a third of the turns silently vanished reports as a clean run of a smaller
    sample. That is the one failure this whole gate is structurally blind to.

    So the driver states how many turns it drove, and this checks the telemetry
    holds exactly that many ELIGIBLE records:

      * inside the selected window
      * agent_arch == fc_loop      (the candidate pool)
      * endpoint == alex           (the agent path)
      * one single candidate_sha   (one build, not a mixture)
      * schema-valid v2            (a malformed record proves nothing)

    Each is a filter, not a preference: legacy turns, search_direct turns and
    records from an older log cannot be used to make up the count.

    request_ids are then reconciled one for one. A count alone would let a
    duplicated record cover for a missing turn — 50 records where one turn emitted
    twice and another emitted nothing is indistinguishable from 50 clean turns by
    count. Uniqueness is what makes "50" mean fifty distinct turns.

    ``window_desc`` is the description of the filter the CALLER actually applied to
    ``windowed`` (see :func:`window_bounds`); this function cannot observe it, so it
    reports what it was told and says so plainly when it was told nothing. It must
    never invent one: the previous fixed string named ``--since`` as part of the
    window when ``--since`` did not filter at all.
    """
    eligible: List[dict] = []
    ineligible: Dict[str, int] = {}

    def _reject(reason: str) -> None:
        ineligible[reason] = ineligible.get(reason, 0) + 1

    for r in windowed:
        if record_arch(r) != candidate_arch:
            _reject(f"agent_arch is not {candidate_arch}")
            continue
        if record_endpoint(r) not in GATE_ENDPOINTS:
            _reject(f"endpoint is not one of {list(GATE_ENDPOINTS)}")
            continue
        if validate_record(r):
            # A record that violates the contract is not a turn we can count. It
            # already holds the gate via the instrumentation check; excluding it
            # here stops the two failures cancelling out — a malformed record
            # padding the count back up to 50 while asserting nothing.
            _reject("record violates the v2 contract")
            continue
        eligible.append(r)

    counts: Dict[object, int] = {}
    for r in eligible:
        rid = r.get("request_id")
        counts[rid] = counts.get(rid, 0) + 1
    duplicated = {str(k): n for k, n in counts.items() if n > 1}
    unique_ids = len(counts)
    shas = sorted({str(r.get("candidate_sha")) for r in eligible})

    reasons: List[str] = []
    if len(eligible) != expected:
        reasons.append(f"expected {expected} eligible {candidate_arch}/alex turns in the window, "
                       f"found {len(eligible)}")
    if unique_ids != expected:
        reasons.append(f"expected {expected} unique request_ids, found {unique_ids}")
    if duplicated:
        shown = ", ".join(f"{k} x{n}" for k, n in sorted(duplicated.items())[:5])
        reasons.append(f"{len(duplicated)} request_id(s) appear more than once: {shown}")
    if len(shas) > 1:
        reasons.append(f"eligible turns span {len(shas)} candidate_sha values: {shas}")

    return {
        "expected": expected,
        "observed": len(eligible),
        "unique_request_ids": unique_ids,
        "duplicate_request_ids": duplicated,
        "candidate_shas": shas,
        "filters": {
            "window": window_desc or "not stated by the caller (no record filter reported)",
            "agent_arch": candidate_arch,
            "endpoint": list(GATE_ENDPOINTS),
            "schema": (f"contract-valid only, per-record rules for schema "
                       f"v{list(SUPPORTED_SCHEMA_VERSIONS)}"),
            "candidate_sha": "exactly one",
        },
        "ineligible_records": dict(sorted(ineligible.items(), key=lambda kv: -kv[1])),
        "matched": not reasons,
        "reasons": reasons,
    }


def evaluate_expected_rollout_turns(records: Sequence[dict], expected: int) -> dict:
    """Reconcile all selected edge agent turns against an access-log count.

    Unlike ``--expect-turns`` (candidate-only), this is the denominator for the
    whole weighted cohort: candidate plus control. The caller obtains ``expected``
    from the nginx JSON access log after filtering the same rollout id and agent
    endpoint.
    """
    eligible = [
        r for r in records
        if record_endpoint(r) in GATE_ENDPOINTS and not validate_record(r)
    ]
    counts: Dict[object, int] = {}
    for rec in eligible:
        rid = rec.get("request_id")
        counts[rid] = counts.get(rid, 0) + 1
    duplicate = {str(key): count for key, count in counts.items() if count > 1}
    reasons: List[str] = []
    if len(eligible) != expected:
        reasons.append(
            f"expected {expected} edge rollout turns, found {len(eligible)}"
        )
    if len(counts) != expected:
        reasons.append(
            f"expected {expected} unique edge request_ids, found {len(counts)}"
        )
    if duplicate:
        reasons.append(
            f"{len(duplicate)} edge request_id(s) appear more than once"
        )
    return {
        "expected": expected,
        "observed": len(eligible),
        "unique_request_ids": len(counts),
        "candidate_turns": sum(1 for r in eligible if r.get("assigned_pool") == "candidate"),
        "control_turns": sum(1 for r in eligible if r.get("assigned_pool") == "legacy"),
        "duplicate_request_ids": duplicate,
        "matched": not reasons,
        "reasons": reasons,
    }


# --------------------------------------------------------------------------- #
# Verdict                                                                     #
# --------------------------------------------------------------------------- #

def build_verdict(fc: dict, legacy: dict, deltas: dict,
                  stage_eval: Optional[dict],
                  instrumentation: Optional[dict] = None,
                  global_zt: Optional[dict] = None,
                  expected_turns: Optional[dict] = None,
                  expected_rollout_turns: Optional[dict] = None,
                  *, candidate_arch: str = "fc_loop",
                  control_arch: str = "legacy",
                  require_specialists: bool = False) -> dict:
    """Evaluate shuhan's zero-tolerance and stage-pause rules against the fc pool.

    Precedence for the exit code: zero-tolerance (3) beats stage-pause/HOLD (2) beats
    proceed (0).  An observation window that has not met its preregistered minima is not
    permission to advance, so HOLD is deliberately non-zero.
    """
    zt_reasons: List[str] = []
    zt_notes: List[str] = []

    # --- ZERO-TOLERANCE (absolute; any >0 on candidate => instant rollback) ---
    # Sourced from the GLOBAL cross-endpoint aggregate, not the gate slice: a
    # forbidden write or a markup leak is a breach on whichever public endpoint it
    # happened. Falling back to `fc` keeps older callers working.
    zt = global_zt if global_zt is not None else fc
    _scope = ("all endpoints" if global_zt is not None else "gate endpoint only")
    if zt["tainted_unauth_write_count"] > 0:
        zt_reasons.append(
            f"tainted/unauthorized memory write executed "
            f"x{zt['tainted_unauth_write_count']} ({_scope}, must be 0)")
    if zt["forbidden_write_count"] > 0:
        zt_reasons.append(
            f"forbidden write executed x{zt['forbidden_write_count']} ({_scope}, must be 0)")
    # v2 FAIL-CLOSED: an unobserved zero-tolerance metric is NOT a pass. In v1 these
    # branches appended to zt_notes and the report still exited 0.
    instr_reasons: List[str] = []
    if zt["dsml_leak_count"] is None:
        instr_reasons.append("DSML/tool-markup leak not instrumented")
    elif zt["dsml_leak_count"] > 0:
        zt_reasons.append(
            f"DSML/tool-markup reached the response boundary x{zt['dsml_leak_count']} "
            f"({_scope}, must be 0) — the backstop replaced the body so it was NOT "
            f"sent, but the primary pre-persistence control failed")
    if zt["api_400_count"] is None:
        instr_reasons.append("provider schema 400s not instrumented")
    elif zt["api_400_count"] > 0:
        _n400 = zt.get("api_400_total", zt["api_400_count"])
        zt_reasons.append(
            f"provider schema 400s on {zt['api_400_count']} turn(s), {_n400} call(s) "
            f"({_scope}, must be 0)")
    # dsml_blocked is a SAFETY signal, not a breach: the primary control caught the
    # markup before persistence, nothing was written and nothing was sent. Reported,
    # never gated on. Its counterpart dsml_leak above IS a breach even though the
    # boundary backstop stopped the body from being sent — see the contract note in
    # core/canary_telemetry.py: reaching the boundary means the primary control
    # failed, and that is what blocks the release.
    if fc.get("dsml_blocked_count"):
        zt_notes.append(
            f"DSML markup blocked+recovered x{fc['dsml_blocked_count']} (safe path, not a breach)")

    # --- STAGE-PAUSE (SLO / degradation; relative-to-legacy where noted) ---
    sp_reasons: List[str] = []
    sp_notes: List[str] = []

    p50, p95 = fc["p50_ms"], fc["p95_ms"]
    if p50 is not None and p50 > P50_LIMIT_MS:
        sp_reasons.append(f"{candidate_arch} p50 {p50:.0f}ms > {P50_LIMIT_MS:.0f}ms")
    if p95 is not None and p95 > P95_LIMIT_MS:
        sp_reasons.append(f"{candidate_arch} p95 {p95:.0f}ms > {P95_LIMIT_MS:.0f}ms")
    if fc["degraded_rate"] > DEGRADED_RATE_LIMIT:
        sp_reasons.append(
            f"partial+soft_wrapped rate {fc['degraded_rate']*100:.1f}% > "
            f"{DEGRADED_RATE_LIMIT*100:.0f}%")

    # relative-to-legacy metrics (known base98 family) — only when in prod telemetry
    for name, label in (("forbidden_read", "forbidden-read"),
                        ("no_evidence_numbers", "no-evidence-numbers")):
        rate_key = f"{name}_rate"
        if fc[rate_key] is None or legacy[rate_key] is None:
            sp_notes.append(f"{label} rate: requires eval sweep — not in prod telemetry")
            continue
        pp = (fc[rate_key] - legacy[rate_key]) * 100.0
        if pp > RELATIVE_PP:
            sp_reasons.append(
                f"{label} rate {fc[rate_key]*100:.1f}% > {control_arch} "
                f"{legacy[rate_key]*100:.1f}% "
                f"+ {RELATIVE_PP:.0f}pp (delta {pp:+.1f}pp)")

    if fc["http_5xx_rate"] is None or legacy["http_5xx_rate"] is None:
        instr_reasons.append("5xx rate not instrumented")
    else:
        pp5 = (fc["http_5xx_rate"] - legacy["http_5xx_rate"]) * 100.0
        if pp5 > RELATIVE_PP:
            sp_reasons.append(
                f"5xx rate {fc['http_5xx_rate']*100:.2f}% > {control_arch} "
                f"{legacy['http_5xx_rate']*100:.2f}% + {RELATIVE_PP:.0f}pp (delta {pp5:+.2f}pp)")

    specialist = fc.get("specialist") or {}
    specialist_failure_rate = specialist.get("failure_rate")
    if (
        specialist_failure_rate is not None
        and specialist_failure_rate > SPECIALIST_FAILURE_RATE_LIMIT
    ):
        sp_reasons.append(
            f"specialist failure rate {specialist_failure_rate*100:.1f}% > "
            f"{SPECIALIST_FAILURE_RATE_LIMIT*100:.0f}%"
        )
    specialist_non_success_rate = specialist.get("non_success_rate")
    if (
        specialist_non_success_rate is not None
        and specialist_non_success_rate > SPECIALIST_FAILURE_RATE_LIMIT
    ):
        _skipped_codes = specialist.get("skipped_error_codes") or {}
        _why = ("; ".join(f"{k} x{v}" for k, v in list(_skipped_codes.items())[:3])
                or "no error_code recorded")
        sp_reasons.append(
            f"specialist non-delivery rate "
            f"{specialist_non_success_rate*100:.1f}% > "
            f"{SPECIALIST_FAILURE_RATE_LIMIT*100:.0f}% "
            f"(failed {specialist.get('failed')} + skipped "
            f"{specialist.get('skipped')} of {specialist.get('planned')} planned; "
            f"skipped: {_why})"
        )
    if require_specialists:
        # "A plan was made" is not evidence that specialists RAN. The dispatcher
        # rewrites the terminal of any task that never started to `skipped`, so a
        # runtime whose every dispatch is refused still reports planned=N — and the
        # old check (`not specialist.get("planned")`) passed it. Requiring
        # specialists has to mean the three things that can independently be false:
        # planned > 0, started > 0, and at least one task that actually delivered.
        _planned = specialist.get("planned") or 0
        _started = specialist.get("started") or 0
        _delivered = (specialist.get("completed") or 0) + (specialist.get("partial") or 0)
        if not _planned:
            instr_reasons.append(
                "specialist dispatch was required but no planned specialist task was observed"
            )
        elif not _started:
            instr_reasons.append(
                f"specialist dispatch was required but no specialist task ever STARTED "
                f"(planned={_planned}, skipped={specialist.get('skipped')}): the "
                f"specialist runtime did not run in this window"
            )
        elif not _delivered:
            instr_reasons.append(
                f"specialist dispatch was required but no specialist task completed or "
                f"partially completed (planned={_planned}, started={_started}, "
                f"failed={specialist.get('failed')}, skipped={specialist.get('skipped')})"
            )

    zt_breached = bool(zt_reasons)
    sp_breached = bool(sp_reasons)

    # Schema-level contract violations (missing/null required fields) are an
    # instrumentation hold too — a record that asserts nothing cannot clear a gate.
    if instrumentation is not None and not instrumentation.get("ok"):
        n = instrumentation.get("violating", 0)
        top = list(instrumentation.get("violations", {}).items())[:3]
        instr_reasons.append(
            f"{n} record(s) violate the canary telemetry contract "
            f"(supported schema versions {list(SUPPORTED_SCHEMA_VERSIONS)}): "
            + "; ".join(f"{k} (x{v})" for k, v in top))

    # External anchor. A mismatch is an INSTRUMENTATION failure, not a stage pause:
    # it says the telemetry does not describe the run that was driven, so every
    # other number in this report is computed over an unknown denominator. It is
    # deliberately ranked BELOW zero-tolerance — if a run both lost turns and
    # committed a real breach, the breach is the finding that matters and the exit
    # code must stay 3.
    if expected_turns is not None and not expected_turns["matched"]:
        instr_reasons.extend(expected_turns["reasons"])
    if expected_rollout_turns is not None and not expected_rollout_turns["matched"]:
        instr_reasons.extend(expected_rollout_turns["reasons"])
    instr_failed = bool(instr_reasons)

    # --- decision / exit code ---
    # Precedence: a PROVEN breach (3) outranks an unprovable gate (2), which
    # outranks an SLO pause (2). Never fall through to 0 with an unobserved metric.
    if zt_breached:
        decision, exit_code = "CANARY-BLOCK", 3
    elif instr_failed:
        decision, exit_code = "INSTRUMENTATION-HOLD", 2
    elif sp_breached:
        decision, exit_code = "STAGE-PAUSE", 2
    else:
        if stage_eval is not None and not stage_eval["eligible"]:
            decision, exit_code = "HOLD", 2
        elif stage_eval is not None:
            decision, exit_code = "STAGE-PROGRESS-OK", 0
        else:
            decision, exit_code = "PROCEED", 0

    return {
        "decision": decision,
        "exit_code": exit_code,
        "zero_tolerance": {"breached": zt_breached, "reasons": zt_reasons, "notes": zt_notes},
        "instrumentation": {"failed": instr_failed, "reasons": instr_reasons,
                            "contract": instrumentation},
        "stage_pause": {"breached": sp_breached, "reasons": sp_reasons, "notes": sp_notes},
        "stage_progress": stage_eval,
        "expected_turns": expected_turns,
        "expected_rollout_turns": expected_rollout_turns,
    }


# --------------------------------------------------------------------------- #
# Top-level report                                                            #
# --------------------------------------------------------------------------- #

def build_report(records: Sequence[dict], *, window_hours: Optional[float] = None,
                 now_override: Optional[datetime] = None, stage: Optional[str] = None,
                 since: Optional[datetime] = None, skipped: int = 0,
                 inputs: Optional[Sequence[str]] = None,
                 expect_turns: Optional[int] = None,
                 candidate_arch: str = "fc_loop",
                 control_arch: str = "legacy",
                 require_specialists: bool = False,
                 rollout_id: Optional[str] = None,
                 rollout_stage: Optional[str] = None,
                 configured_weight: Optional[int] = None,
                 expect_rollout_turns: Optional[int] = None) -> dict:
    if candidate_arch not in VALID_ARCHES or control_arch not in VALID_ARCHES:
        raise ValueError(
            f"candidate/control architectures must be in {list(VALID_ARCHES)}"
        )
    if candidate_arch == control_arch:
        raise ValueError("candidate_arch and control_arch must be different")
    if require_specialists and candidate_arch != "manager_v1":
        raise ValueError("require_specialists is valid only for manager_v1")
    if rollout_id is not None and expect_rollout_turns is None:
        raise ValueError("rollout_id requires expect_rollout_turns from the edge log")
    if configured_weight is not None and configured_weight not in VALID_CANARY_WEIGHTS:
        raise ValueError(f"configured_weight must be in {list(VALID_CANARY_WEIGHTS)}")
    now = reference_now(records, now_override)

    # Partition by timestamp parseability BEFORE windowing. filter_window silently
    # DROPS any record it cannot place, so validating only the windowed set let a
    # record escape the contract entirely by carrying a missing or corrupt ts.
    undated_all = [r for r in records if record_ts(r) is None]
    dated = [r for r in records if record_ts(r) is not None]
    # ONE source for the cutoff and for the sentence describing it, so the report can
    # never narrate a filter it did not run. `since` bounds the POPULATION here, not
    # just the stage elapsed-hours check it used to feed on its own.
    window_cutoff, window_desc = window_bounds(window_hours, since, now)
    time_windowed = filter_window(dated, window_hours, now, since=since)
    if rollout_id is not None:
        windowed = [
            r for r in time_windowed
            if r.get("traffic_source") == "edge" and r.get("rollout_id") == rollout_id
        ]
        undated = [
            r for r in undated_all
            if r.get("traffic_source") == "edge" and r.get("rollout_id") == rollout_id
        ]
    else:
        windowed = time_windowed
        undated = undated_all

    # v2: PERFORMANCE/quality metrics are decided by the AGENT endpoint only.
    # search_direct is a deterministic, LLM-free path — folding it in would dilute
    # the agent A/B. Security zero-tolerance is handled separately and globally.
    gate_records = [r for r in windowed if record_endpoint(r) in GATE_ENDPOINTS]
    other_records = [r for r in windowed if record_endpoint(r) not in GATE_ENDPOINTS]

    # Validate the WHOLE window, not just the gate slice. Validating after the
    # endpoint filter would be fail-open: a record that lost its `endpoint` field
    # (exactly the instrumentation regression we want to catch, and the shape every
    # pre-v2 record has) would be filtered OUT of the gate and so never validated —
    # it would vanish instead of holding. Telemetry integrity is global.
    instrumentation = validate_records(windowed, candidate_arch=candidate_arch)

    def _hold_instrumentation(reason: str, count: int = 1) -> None:
        instrumentation["violations"][reason] = (
            instrumentation["violations"].get(reason, 0) + count
        )
        instrumentation["violating"] += count
        instrumentation["ok"] = False

    observed_rollout_stages: List[object] = []
    observed_rollout_weights: List[object] = []
    if rollout_id is not None:
        if not windowed:
            _hold_instrumentation(
                f"rollout_id={rollout_id!r} has zero dated edge telemetry records"
            )
        stage_values = {rec.get("rollout_stage") for rec in windowed}
        weight_values = {
            rec.get("configured_candidate_percent") for rec in windowed
        }
        observed_rollout_stages = sorted(stage_values, key=repr)
        observed_rollout_weights = sorted(
            weight_values,
            key=lambda value: (
                0,
                value,
            ) if isinstance(value, int) and not isinstance(value, bool) else (
                1,
                repr(value),
            ),
        )
        if len(stage_values) > 1:
            _hold_instrumentation(
                f"rollout_id={rollout_id!r} mixes rollout_stage values "
                f"{observed_rollout_stages!r}"
            )
        if len(weight_values) > 1:
            _hold_instrumentation(
                f"rollout_id={rollout_id!r} mixes configured weight values "
                f"{observed_rollout_weights!r}"
            )
        for rec in windowed:
            if rec.get("variant_id") is None:
                _hold_instrumentation("edge rollout record is missing variant_id")
            if rollout_stage is not None and rec.get("rollout_stage") != rollout_stage:
                _hold_instrumentation(
                    f"rollout_id={rollout_id!r} mixes/unexpected rollout_stage"
                )
            if (
                configured_weight is not None
                and rec.get("configured_candidate_percent") != configured_weight
            ):
                _hold_instrumentation(
                    f"rollout_id={rollout_id!r} mixes/unexpected configured weight"
                )

    unattributable = [r for r in windowed if record_endpoint(r) not in KNOWN_ENDPOINTS]
    if unattributable:
        _hold_instrumentation(
            f"endpoint missing/unknown (not one of {list(KNOWN_ENDPOINTS)})",
            len(unattributable),
        )
    if undated:
        _hold_instrumentation(
            "ts missing or unparseable (record cannot be placed in a window, so "
            "windowing would silently drop it)",
            len(undated),
        )
    if skipped:
        # An unparseable line is not a harmless comment. The writer is a single logger
        # emitting exactly one json.dumps per line, so a line that will not parse means
        # a truncated or interleaved write — and the record we lost could be precisely
        # the one carrying a violation. Showing the count in a summary row and still
        # exiting 0 is the same fail-open shape we removed everywhere else.
        _hold_instrumentation(
            "unparseable log line (the lost record may have carried a violation)",
            skipped,
        )
    instrumentation["unattributable_records"] = len(unattributable)
    instrumentation["undated_records"] = len(undated)
    instrumentation["unparseable_lines"] = skipped

    # SECURITY zero-tolerance is aggregated GLOBALLY, across every public endpoint
    # of the candidate arch. Restricting it to the gate endpoint would mean a real
    # forbidden write or markup leak on search_direct still shipped.
    global_zt = aggregate_zero_tolerance(
        [r for r in windowed if record_arch(r) == candidate_arch]
    )

    candidate_records = [r for r in gate_records if record_arch(r) == candidate_arch]
    control_records = [r for r in gate_records if record_arch(r) == control_arch]
    unselected_gate_records = [
        r for r in gate_records
        if record_arch(r) not in {candidate_arch, control_arch}
    ]
    if not candidate_records:
        _hold_instrumentation(
            f"candidate arm {candidate_arch!r} has zero gate-endpoint turns"
        )
    if not control_records:
        _hold_instrumentation(
            f"control arm {control_arch!r} has zero gate-endpoint turns"
        )

    fc = aggregate_arch(candidate_records)
    legacy = aggregate_arch(control_records)
    if require_specialists and fc["manager_v1_specialist_variants"] != [True]:
        _hold_instrumentation(
            "manager_v1 specialist rollout requires a single enabled "
            "manager_v1_specialists=true variant"
        )
    deltas = compute_deltas(fc, legacy)

    # Reported for visibility, never mixed into the gate.
    buckets: Dict[str, List[dict]] = {}
    for r in other_records:
        buckets.setdefault(record_endpoint(r) or "unknown", []).append(r)
    excluded = {name: aggregate_arch(rs) for name, rs in buckets.items()}
    unselected_arches: Dict[str, List[dict]] = {}
    for r in unselected_gate_records:
        unselected_arches.setdefault(record_arch(r) or "unknown", []).append(r)

    stage_eval = evaluate_stage(fc, stage, since, now) if stage else None
    # Reconciled against the WINDOWED set, before the endpoint filter, so a turn
    # that landed on the wrong endpoint is counted as ineligible and reported —
    # rather than disappearing from the comparison entirely.
    expected_turns = (evaluate_expected_turns(windowed, expect_turns,
                                              window_desc=window_desc,
                                              candidate_arch=candidate_arch)
                      if expect_turns is not None else None)
    expected_rollout = (
        evaluate_expected_rollout_turns(windowed, expect_rollout_turns)
        if expect_rollout_turns is not None else None
    )
    verdict = build_verdict(fc, legacy, deltas, stage_eval, instrumentation, global_zt,
                            expected_turns, expected_rollout,
                            candidate_arch=candidate_arch,
                            control_arch=control_arch,
                            require_specialists=require_specialists)

    arches = {
        "candidate": fc,
        "control": legacy,
        candidate_arch: fc,
        control_arch: legacy,
    }
    # Stable aliases for historical JSON consumers.  They refer to the exact
    # architecture when present, never to an unknown/fallback bucket.
    if candidate_arch == "fc_loop":
        arches["fc"] = fc
    elif control_arch == "fc_loop":
        arches["fc"] = legacy

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": list(inputs or []),
        "reference_now": now.isoformat(),
        "window_hours": window_hours,
        "since": since.isoformat() if since is not None else None,
        # The cutoff actually applied to the population, and its plain-English form.
        # Emitted so a JSON consumer can reconcile a rate against the exact bound the
        # rate was computed over instead of trusting a flag it did not see applied.
        "window_cutoff": window_cutoff.isoformat() if window_cutoff is not None else None,
        "window_filter": window_desc,
        "records_total": len(records),
        "records_in_time_window": len(time_windowed),
        "records_in_window": len(windowed),
        "records_in_gate": len(gate_records),
        "records_excluded_from_gate": len(other_records),
        "records_skipped": skipped,
        "gate_endpoints": list(GATE_ENDPOINTS),
        "candidate_arch": candidate_arch,
        "control_arch": control_arch,
        "require_specialists": require_specialists,
        "rollout_id": rollout_id,
        "rollout_stage": rollout_stage,
        "configured_candidate_percent": configured_weight,
        "observed_rollout_stages": observed_rollout_stages,
        "observed_rollout_weights": observed_rollout_weights,
        "expect_rollout_turns": expect_rollout_turns,
        "instrumentation": instrumentation,
        "zero_tolerance_global": global_zt,
        "arches": arches,
        "unselected_gate_arches": {
            name: aggregate_arch(items) for name, items in unselected_arches.items()
        },
        "excluded_endpoints": excluded,
        "deltas": deltas,
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

def _fmt(v, kind: str = "num") -> str:
    if v is None:
        return "n/a"
    if kind == "ms":
        return f"{v:.0f}"
    if kind == "pct":
        return f"{v*100:.2f}%"
    if kind == "pp":
        return f"{v:+.2f}"
    return str(v)


def render_text(report: dict) -> str:
    fc = report["arches"].get("candidate", report["arches"].get("fc"))
    lg = report["arches"].get("control", report["arches"].get("legacy"))
    candidate_arch = report.get("candidate_arch", "fc_loop")
    control_arch = report.get("control_arch", "legacy")
    d = report["deltas"]
    lines: List[str] = []
    a = lines.append

    a("=" * 74)
    a(f"CANARY REPORT — {candidate_arch} vs {control_arch}")
    a("=" * 74)
    a(f"generated_at   : {report['generated_at']}")
    a(f"reference_now  : {report['reference_now']}")
    a(f"window_hours   : {report['window_hours']}")
    a(f"since          : {report.get('since')}")
    a(f"rollout        : id={report.get('rollout_id')} "
      f"stage={report.get('rollout_stage')} "
      f"weight={report.get('configured_candidate_percent')}")
    # The filter as APPLIED, printed on the report itself: the operator reading a
    # verdict must be able to see the population it was computed over.
    a(f"record filter  : {report.get('window_filter')}")
    a(f"records        : total={report['records_total']} "
      f"in_window={report['records_in_window']} skipped={report['records_skipped']}")
    a("")

    rows = [
        ("turns", str(fc["turns"]), str(lg["turns"]), ""),
        ("conversations", str(fc["conversations"]), str(lg["conversations"]), ""),
        ("p50 latency ms", _fmt(fc["p50_ms"], "ms"), _fmt(lg["p50_ms"], "ms"), ""),
        ("p95 latency ms", _fmt(fc["p95_ms"], "ms"), _fmt(lg["p95_ms"], "ms"), ""),
        ("over-30s count", str(fc["over_30s_count"]), str(lg["over_30s_count"]), ""),
        ("over-30s rate", _fmt(fc["over_30s_rate"], "pct"), _fmt(lg["over_30s_rate"], "pct"),
         _fmt(d["over_30s_rate_pp"], "pp")),
        ("soft_wrapped rate", _fmt(fc["soft_wrapped_rate"], "pct"),
         _fmt(lg["soft_wrapped_rate"], "pct"), _fmt(d["soft_wrapped_rate_pp"], "pp")),
        # Legacy has no wrap path at all, so the legacy column is structurally n/a here
        # rather than 0 — there is nothing to compare against, not a clean comparison.
        ("  of which canned", _fmt(fc["wrapped_canned_rate"], "pct"), "n/a", ""),
        ("  wrapped, unattributed", str(fc["wrapped_unattributed_count"]), "n/a", ""),
        ("partial rate", _fmt(fc["partial_rate"], "pct"), _fmt(lg["partial_rate"], "pct"),
         _fmt(d["partial_rate_pp"], "pp")),
        ("partial+soft rate", _fmt(fc["degraded_rate"], "pct"),
         _fmt(lg["degraded_rate"], "pct"), _fmt(d["degraded_rate_pp"], "pp")),
        ("tool_budget_timeout", _fmt(fc["tool_budget_timeout_rate"], "pct"),
         _fmt(lg["tool_budget_timeout_rate"], "pct"),
         _fmt(d["tool_budget_timeout_rate_pp"], "pp")),
        ("security non-clean", str(fc["security_non_clean_count"]),
         str(lg["security_non_clean_count"]), ""),
        ("tainted/unauth write", str(fc["tainted_unauth_write_count"]),
         str(lg["tainted_unauth_write_count"]), ""),
        ("forbidden write", str(fc["forbidden_write_count"]),
         str(lg["forbidden_write_count"]), ""),
        ("forbidden-read rate", _fmt(fc["forbidden_read_rate"], "pct"),
         _fmt(lg["forbidden_read_rate"], "pct"), _fmt(d["forbidden_read_rate_pp"], "pp")),
        ("no-evidence-numbers", _fmt(fc["no_evidence_numbers_rate"], "pct"),
         _fmt(lg["no_evidence_numbers_rate"], "pct"),
         _fmt(d["no_evidence_numbers_rate_pp"], "pp")),
        ("dsml leak count", _fmt(fc["dsml_leak_count"]), _fmt(lg["dsml_leak_count"]), ""),
        ("schema/API 400s", _fmt(fc["api_400_count"]), _fmt(lg["api_400_count"]), ""),
        ("5xx rate", _fmt(fc["http_5xx_rate"], "pct"), _fmt(lg["http_5xx_rate"], "pct"),
         _fmt(d["http_5xx_rate_pp"], "pp")),
        # planned/started/skipped are printed TOGETHER on purpose. Printing only
        # `planned` next to a `failed rate` let a window where nothing ever started
        # read as a clean 0.00% — the operator's only hint was a max-in-flight of 0
        # that no threshold looked at.
        ("unmeasured spend turns", str(fc.get("unmeasured_spend_turns", 0)),
         str(lg.get("unmeasured_spend_turns", 0)), ""),
        ("  of which unobservable", str(fc.get("unobservable_unmeasured_turns", 0)),
         str(lg.get("unobservable_unmeasured_turns", 0)), ""),
        ("specialist planned", str(fc["specialist"]["planned"]), "n/a", ""),
        ("specialist started", str(fc["specialist"]["started"]), "n/a", ""),
        ("specialist skipped", str(fc["specialist"]["skipped"]), "n/a", ""),
        ("specialist failed rate", _fmt(fc["specialist"]["failure_rate"], "pct"),
         "n/a", ""),
        ("specialist non-delivery", _fmt(fc["specialist"]["non_success_rate"], "pct"),
         "n/a", ""),
        ("specialist max in-flight", str(fc["specialist"]["max_in_flight"]), "n/a", ""),
    ]
    _skipped_codes = fc["specialist"].get("skipped_error_codes") or {}
    if _skipped_codes:
        rows.append((
            "specialist skip codes",
            ", ".join(f"{k} x{v}" for k, v in list(_skipped_codes.items())[:3]),
            "n/a", "",
        ))
    w0 = max(len(r[0]) for r in rows)
    colw = max(12, len(candidate_arch), len(control_arch))
    hdr = (f"{'metric':<{w0}}  {candidate_arch:>{colw}}  "
           f"{control_arch:>{colw}}  {'delta_pp':>9}")
    a(hdr)
    a("-" * len(hdr))
    for name, fcv, lgv, dl in rows:
        a(f"{name:<{w0}}  {fcv:>{colw}}  {lgv:>{colw}}  {dl:>9}")
    a("")

    v = report["verdict"]
    a("-" * 74)
    a("VERDICT")
    a("-" * 74)
    a(f"decision : {v['decision']}  (exit {v['exit_code']})")

    a("")
    a("[ZERO-TOLERANCE] (instant rollback if any):")
    if v["zero_tolerance"]["reasons"]:
        for r in v["zero_tolerance"]["reasons"]:
            a(f"  BREACH: {r}")
    else:
        a("  clean")
    for n in v["zero_tolerance"]["notes"]:
        a(f"  note: {n}")

    a("")
    a("[STAGE-PAUSE] (pause rollout if any):")
    if v["stage_pause"]["reasons"]:
        for r in v["stage_pause"]["reasons"]:
            a(f"  BREACH: {r}")
    else:
        a("  clean")
    for n in v["stage_pause"]["notes"]:
        a(f"  note: {n}")

    et = v.get("expected_turns")
    if et is not None:
        a("")
        a("[EXTERNAL ANCHOR] (--expect-turns: does the telemetry describe the run?):")
        f = et["filters"]
        a(f"  counting only: agent_arch={f['agent_arch']}  endpoint={f['endpoint']}  "
          f"schema={f['schema']}")
        a(f"                 candidate_sha={f['candidate_sha']}  window={f['window']}")
        a(f"  expected turns      : {et['expected']}")
        a(f"  observed eligible   : {et['observed']}")
        a(f"  unique request_ids  : {et['unique_request_ids']}")
        a(f"  candidate_sha(s)    : {et['candidate_shas'] or 'none'}")
        if et["duplicate_request_ids"]:
            a(f"  DUPLICATE ids       : {et['duplicate_request_ids']}")
        if et["ineligible_records"]:
            a("  ineligible (cannot be used to make up the count):")
            for reason, n in et["ineligible_records"].items():
                a(f"    x{n}  {reason}")
        a(f"  matched             : {et['matched']}")
        for r in et["reasons"]:
            a(f"  MISMATCH: {r}")

    ert = v.get("expected_rollout_turns")
    if ert is not None:
        a("")
        a("[EDGE DENOMINATOR] (nginx access-log turns vs canary telemetry):")
        a(f"  expected edge turns : {ert['expected']}")
        a(f"  observed telemetry  : {ert['observed']}")
        a(f"  unique request_ids  : {ert['unique_request_ids']}")
        a(f"  candidate/control   : {ert['candidate_turns']}/{ert['control_turns']}")
        a(f"  matched             : {ert['matched']}")
        for reason in ert["reasons"]:
            a(f"  MISMATCH: {reason}")

    sp = v["stage_progress"]
    if sp is not None:
        a("")
        a("[STAGE-PROGRESS] (both minima required):")
        a(f"  stage={sp['stage']} min_turns={sp['min_turns']} min_hours={sp['min_hours']}")
        a(f"  {candidate_arch}_turns={sp['fc_turns']} (turns_ok={sp['turns_ok']})  "
          f"elapsed_hours={sp['elapsed_hours']} (hours_ok={sp['hours_ok']})")
        a(f"  eligible={sp['eligible']}")
        if sp["note"]:
            a(f"  note: {sp['note']}")

    a("=" * 74)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

class _UsageExitParser(argparse.ArgumentParser):
    """An ArgumentParser that exits :data:`EXIT_USAGE`, never 2, on misuse.

    argparse's default abort code is 2, and 2 is a GATE VERDICT here (STAGE-PAUSE /
    INSTRUMENTATION-HOLD). So ``--json`` typed without its PATH argument produced
    exit 2, and an operator or CI driver checking only ``$?`` could not tell a typo
    from a breached gate — it would pause a rollout that was never measured, or
    "confirm" a pause that never happened.

    Both argparse exit paths are overridden, not just ``error()``: ``exit()`` is what
    ``error()`` and a few internal paths funnel through, so overriding only the former
    would leave the collision reachable. ``--help`` still exits 0 — asking for help is
    not misuse.
    """

    def error(self, message: str):        # pragma: no cover - exercised via run()
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_USAGE)

    def exit(self, status: int = 0, message: Optional[str] = None):
        if message:
            (sys.stderr if status else sys.stdout).write(message)
        raise SystemExit(EXIT_USAGE if status else 0)


def _build_parser() -> argparse.ArgumentParser:
    p = _UsageExitParser(
        prog="canary_report.py",
        description="Aggregate canary.turn telemetry and evaluate a two-arm canary gate.")
    p.add_argument("--input", "-i", action="append", default=[], metavar="PATH",
                   help="JSONL/log file, directory (searched recursively), or glob. "
                        "Repeatable.")
    p.add_argument("--window", type=float, default=None, metavar="HOURS",
                   help="Keep only records within HOURS of the 'now' reference "
                        "(default reference: the latest observed timestamp).")
    p.add_argument("--json", dest="json_out", default=None, metavar="PATH",
                   help=f"Write the full report as JSON to PATH ('-' for stdout). "
                        f"PATH is REQUIRED; omitting it is a usage error "
                        f"(exit {EXIT_USAGE}), never a gate verdict.")
    p.add_argument("--stage", choices=sorted(STAGES), default=None,
                   help="Evaluate stage-progress minima for this stage.")
    p.add_argument("--candidate-arch", choices=VALID_ARCHES, default="fc_loop",
                   help="Exact candidate architecture (default: fc_loop).")
    p.add_argument("--control-arch", choices=VALID_ARCHES, default="legacy",
                   help="Exact live control architecture (default: legacy).")
    p.add_argument("--require-specialists", action="store_true",
                   help="Require manager_v1_specialists=true and at least one complete "
                        "specialist lifecycle in the selected window.")
    p.add_argument("--rollout-id", default=None, metavar="ID",
                   help="Select only trusted-edge records for this exact rollout ID. "
                        "Requires --expect-rollout-turns.")
    p.add_argument("--rollout-stage", default=None, metavar="NAME",
                   help="Require every selected edge record to carry this stage.")
    p.add_argument("--configured-weight", type=int, choices=VALID_CANARY_WEIGHTS,
                   default=None, metavar="PERCENT",
                   help="Require one configured candidate weight (0/5/20/50/100).")
    p.add_argument("--expect-rollout-turns", type=int, default=None, metavar="N",
                   help="External nginx access-log denominator for candidate+control "
                        "agent turns in --rollout-id.")
    p.add_argument("--since", default=None, metavar="ISO",
                   help="Stage start timestamp (ISO-8601). FILTERS the population: "
                        "records older than this are excluded, exactly like --window "
                        "(both are lower bounds; the later one wins). Also supplies the "
                        "stage elapsed-hours check.")
    p.add_argument("--now", default=None, metavar="ISO",
                   help="Override the 'now' reference (ISO-8601). Default: latest record ts.")
    p.add_argument("--expect-turns", type=int, default=None, metavar="N",
                   help="External anchor: assert the window holds exactly N eligible "
                        "selected-candidate /api/alex turns with N unique request_ids. "
                        "A mismatch "
                        "is an INSTRUMENTATION-HOLD (exit 2) — the telemetry does not "
                        "describe the run that was driven, so every rate in the report "
                        "has an unknown denominator.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the text table (still writes --json and sets exit code).")
    return p


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def run(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        # SOURCE GUARD, not a promise: whatever argparse decides to exit with, a CLI
        # abort can never leave here carrying a gate verdict code. --help/--version
        # exit 0 and are not misuse; everything else becomes EXIT_USAGE.
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return 0 if code == 0 else EXIT_USAGE
    if not args.input:
        sys.stderr.write("error: at least one --input is required\n")
        return EXIT_INPUT_ERROR

    records, skipped = load_records(args.input)

    since = parse_ts(args.since) if args.since else None
    if args.since and since is None:
        sys.stderr.write(f"error: could not parse --since '{args.since}'\n")
        return EXIT_INPUT_ERROR
    now_override = parse_ts(args.now) if args.now else None
    if args.now and now_override is None:
        sys.stderr.write(f"error: could not parse --now '{args.now}'\n")
        return EXIT_INPUT_ERROR

    if args.expect_turns is not None and args.expect_turns < 0:
        sys.stderr.write("error: --expect-turns must be >= 0\n")
        return EXIT_INPUT_ERROR
    if args.expect_rollout_turns is not None and args.expect_rollout_turns < 0:
        sys.stderr.write("error: --expect-rollout-turns must be >= 0\n")
        return EXIT_INPUT_ERROR
    rollout_options = (
        args.rollout_stage is not None
        or args.configured_weight is not None
        or args.expect_rollout_turns is not None
    )
    if rollout_options and args.rollout_id is None:
        sys.stderr.write("error: rollout options require --rollout-id\n")
        return EXIT_INPUT_ERROR
    if args.rollout_id is not None and args.expect_rollout_turns is None:
        sys.stderr.write(
            "error: --rollout-id requires --expect-rollout-turns from nginx access logs\n"
        )
        return EXIT_INPUT_ERROR
    if args.candidate_arch == args.control_arch:
        sys.stderr.write("error: --candidate-arch and --control-arch must differ\n")
        return EXIT_INPUT_ERROR
    if args.require_specialists and args.candidate_arch != "manager_v1":
        sys.stderr.write(
            "error: --require-specialists requires --candidate-arch manager_v1\n"
        )
        return EXIT_INPUT_ERROR

    report = build_report(records, window_hours=args.window, now_override=now_override,
                          stage=args.stage, since=since, skipped=skipped,
                          inputs=args.input, expect_turns=args.expect_turns,
                          candidate_arch=args.candidate_arch,
                          control_arch=args.control_arch,
                          require_specialists=args.require_specialists,
                          rollout_id=args.rollout_id,
                          rollout_stage=args.rollout_stage,
                          configured_weight=args.configured_weight,
                          expect_rollout_turns=args.expect_rollout_turns)

    if args.json_out:
        payload = json.dumps(report, indent=2, sort_keys=True)
        if args.json_out == "-":
            sys.stdout.write(payload + "\n")
        else:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")

    if not args.quiet:
        sys.stdout.write(render_text(report) + "\n")

    code = int(report["verdict"]["exit_code"])
    # The other half of the collision guard: a VERDICT must always speak in gate codes.
    # If a future edit invents one, fail loudly and non-zero rather than hand a driver
    # a code it will read as a usage error or as success.
    if code not in GATE_EXIT_CODES:
        sys.stderr.write(f"internal error: verdict exit code {code} is not one of "
                         f"{list(GATE_EXIT_CODES)}\n")
        return EXIT_INPUT_ERROR
    return code


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
