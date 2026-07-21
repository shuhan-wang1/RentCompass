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

Exit codes:
    0  proceed / hold-ok (green, or a stage simply not yet eligible to progress)
    2  stage-pause breached (a relative/absolute SLO or degradation threshold tripped)
    3  zero-tolerance breached (a safety invariant tripped — instant rollback)

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
SUPPORTED_SCHEMA_VERSIONS = (2,)
MIN_SCHEMA_VERSION = min(SUPPORTED_SCHEMA_VERSIONS)

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
)

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
VALID_ARCHES = ("fc_loop", "legacy")
VALID_HASH_STATUSES = ("keyed", "no_user", HASH_STATUS_UNKEYED)


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


def validate_record(rec: dict) -> List[str]:
    """Return this record's contract violations. Empty == conformant.

    Missing AND null both count: ``"dsml_leak": null`` asserts nothing, so it must
    not be allowed to satisfy the gate.
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
    for f in REQUIRED_TOP_FIELDS:
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

    # --- ENUMS -------------------------------------------------------------
    if rec.get("endpoint") is not None and record_endpoint(rec) not in KNOWN_ENDPOINTS:
        problems.append(f"endpoint={rec.get('endpoint')!r} not in {list(KNOWN_ENDPOINTS)}")
    if rec.get("turn_outcome") is not None and rec["turn_outcome"] not in VALID_OUTCOMES:
        problems.append(f"turn_outcome={rec['turn_outcome']!r} not in {list(VALID_OUTCOMES)}")
    if rec.get("agent_arch") is not None and rec["agent_arch"] not in VALID_ARCHES:
        problems.append(f"agent_arch={rec['agent_arch']!r} not in {list(VALID_ARCHES)}")
    hs = rec.get("user_id_hash_status")
    if hs is not None and hs not in VALID_HASH_STATUSES:
        problems.append(f"user_id_hash_status={hs!r} not in {list(VALID_HASH_STATUSES)}")
    # keyed implies a digest actually exists — otherwise "keyed" asserts nothing.
    if hs == "keyed" and not rec.get("user_id_hash"):
        problems.append("user_id_hash_status=keyed but user_id_hash is absent/empty")
    # The candidate arch is defined by strict function-calling; a non-strict fc
    # record is not the configuration under test.
    if rec.get("agent_arch") == "fc_loop" and not _truthy(rec.get("strict")):
        problems.append("agent_arch=fc_loop but strict is not true (not the candidate config)")

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


def validate_records(records: Sequence[dict]) -> dict:
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
            if r.get("agent_arch") == "fc_loop" and r.get("candidate_sha") is not None}
    if len(shas) > 1:
        offenders[f"window mixes {len(shas)} candidate_sha values on fc_loop: "
                  f"{sorted(str(s) for s in shas)}"] = len(shas)
        bad = max(bad, 1)
    return {
        "records": len(records),
        "conformant": len(records) - bad,
        "violating": bad,
        "violations": dict(sorted(offenders.items(), key=lambda kv: -kv[1])),
        "candidate_shas": sorted(str(s) for s in shas),
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

def resolve_inputs(inputs: Sequence[str]) -> List[str]:
    """Expand --input values (files, directories, or globs) into a sorted, de-duplicated
    list of file paths. Directories are searched recursively for ``*.jsonl`` and ``*.log``."""
    paths: List[str] = []
    for item in inputs:
        if any(c in item for c in "*?[") and not os.path.isdir(item):
            paths.extend(_glob.glob(item, recursive=True))
        elif os.path.isdir(item):
            for pat in ("*.jsonl", "*.log", "*.ndjson"):
                paths.extend(_glob.glob(os.path.join(item, "**", pat), recursive=True))
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

def canonical_arch(rec: dict) -> str:
    """Map a record's arch to a canonical pool bucket: ``fc`` or ``legacy``."""
    raw = str(rec.get("agent_arch") or rec.get("arch") or "").strip().lower()
    if "fc" in raw:
        return "fc"
    return "legacy"


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


def filter_window(records: Sequence[dict], window_hours: Optional[float],
                  now: datetime) -> List[dict]:
    """Keep records whose ts is within ``window_hours`` of ``now`` (inclusive). Records
    lacking any timestamp are dropped when a window is requested (they cannot be placed)."""
    if window_hours is None:
        return list(records)
    cutoff = now.timestamp() - window_hours * 3600.0
    kept = []
    for r in records:
        ts = record_ts(r)
        if ts is not None and ts.timestamp() >= cutoff - 1e-6:
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

    return {
        "turns": turns,
        "conversations": len(convos),
        "users": len(users),
        "candidate_shas": sorted({str(r.get("candidate_sha")) for r in records
                                  if r.get("candidate_sha") is not None}),
        "strict_true": sum(1 for r in records if _truthy(r.get("strict"))),
        "llm_calls_total": sum(_to_int(r.get("llm_calls")) for r in records),
        "tool_batches_total": sum(_to_int(r.get("tool_batches")) for r in records),
        "latency_n": len(lats),
        "p50_ms": percentile(lats, 0.50),
        "p95_ms": percentile(lats, 0.95),
        "over_30s_count": over,
        "over_30s_rate": _rate(over, turns),
        "soft_wrapped_count": soft,
        "soft_wrapped_rate": _rate(soft, turns),
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
# Verdict                                                                     #
# --------------------------------------------------------------------------- #

def build_verdict(fc: dict, legacy: dict, deltas: dict,
                  stage_eval: Optional[dict],
                  instrumentation: Optional[dict] = None,
                  global_zt: Optional[dict] = None) -> dict:
    """Evaluate shuhan's zero-tolerance and stage-pause rules against the fc pool.

    Precedence for the exit code: zero-tolerance (3) beats stage-pause (2) beats proceed (0).
    A stage evaluated-but-not-eligible is a HOLD (exit 0), not a pause.
    """
    zt_reasons: List[str] = []
    zt_notes: List[str] = []

    # --- ZERO-TOLERANCE (absolute; any >0 on fc => instant rollback) ---
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
            f"DSML/tool-markup leak x{zt['dsml_leak_count']} ({_scope}, must be 0)")
    if zt["api_400_count"] is None:
        instr_reasons.append("provider schema 400s not instrumented")
    elif zt["api_400_count"] > 0:
        _n400 = zt.get("api_400_total", zt["api_400_count"])
        zt_reasons.append(
            f"provider schema 400s on {zt['api_400_count']} turn(s), {_n400} call(s) "
            f"({_scope}, must be 0)")
    # dsml_blocked is a SAFETY signal, not a breach: report it, never gate on it.
    if fc.get("dsml_blocked_count"):
        zt_notes.append(
            f"DSML markup blocked+recovered x{fc['dsml_blocked_count']} (safe path, not a breach)")

    # --- STAGE-PAUSE (SLO / degradation; relative-to-legacy where noted) ---
    sp_reasons: List[str] = []
    sp_notes: List[str] = []

    p50, p95 = fc["p50_ms"], fc["p95_ms"]
    if p50 is not None and p50 > P50_LIMIT_MS:
        sp_reasons.append(f"fc p50 {p50:.0f}ms > {P50_LIMIT_MS:.0f}ms")
    if p95 is not None and p95 > P95_LIMIT_MS:
        sp_reasons.append(f"fc p95 {p95:.0f}ms > {P95_LIMIT_MS:.0f}ms")
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
                f"{label} rate {fc[rate_key]*100:.1f}% > legacy {legacy[rate_key]*100:.1f}% "
                f"+ {RELATIVE_PP:.0f}pp (delta {pp:+.1f}pp)")

    if fc["http_5xx_rate"] is None or legacy["http_5xx_rate"] is None:
        instr_reasons.append("5xx rate not instrumented")
    else:
        pp5 = (fc["http_5xx_rate"] - legacy["http_5xx_rate"]) * 100.0
        if pp5 > RELATIVE_PP:
            sp_reasons.append(
                f"5xx rate {fc['http_5xx_rate']*100:.2f}% > legacy "
                f"{legacy['http_5xx_rate']*100:.2f}% + {RELATIVE_PP:.0f}pp (delta {pp5:+.2f}pp)")

    zt_breached = bool(zt_reasons)
    sp_breached = bool(sp_reasons)

    # Schema-level contract violations (missing/null required fields) are an
    # instrumentation hold too — a record that asserts nothing cannot clear a gate.
    if instrumentation is not None and not instrumentation.get("ok"):
        n = instrumentation.get("violating", 0)
        top = list(instrumentation.get("violations", {}).items())[:3]
        instr_reasons.append(
            f"{n} record(s) violate the v{MIN_SCHEMA_VERSION} contract: "
            + "; ".join(f"{k} (x{v})" for k, v in top))
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
            decision, exit_code = "HOLD", 0
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
    }


# --------------------------------------------------------------------------- #
# Top-level report                                                            #
# --------------------------------------------------------------------------- #

def build_report(records: Sequence[dict], *, window_hours: Optional[float] = None,
                 now_override: Optional[datetime] = None, stage: Optional[str] = None,
                 since: Optional[datetime] = None, skipped: int = 0,
                 inputs: Optional[Sequence[str]] = None) -> dict:
    now = reference_now(records, now_override)

    # Partition by timestamp parseability BEFORE windowing. filter_window silently
    # DROPS any record it cannot place, so validating only the windowed set let a
    # record escape the contract entirely by carrying a missing or corrupt ts.
    undated = [r for r in records if record_ts(r) is None]
    dated = [r for r in records if record_ts(r) is not None]
    windowed = filter_window(dated, window_hours, now)

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
    instrumentation = validate_records(windowed)
    unattributable = [r for r in windowed if record_endpoint(r) not in KNOWN_ENDPOINTS]
    if unattributable:
        instrumentation["violations"][
            f"endpoint missing/unknown (not one of {list(KNOWN_ENDPOINTS)})"
        ] = len(unattributable)
        instrumentation["violating"] = max(instrumentation["violating"], len(unattributable))
        instrumentation["ok"] = False
    if undated:
        instrumentation["violations"][
            "ts missing or unparseable (record cannot be placed in a window, so "
            "windowing would silently drop it)"
        ] = len(undated)
        instrumentation["violating"] += len(undated)
        instrumentation["ok"] = False
    if skipped:
        # An unparseable line is not a harmless comment. The writer is a single logger
        # emitting exactly one json.dumps per line, so a line that will not parse means
        # a truncated or interleaved write — and the record we lost could be precisely
        # the one carrying a violation. Showing the count in a summary row and still
        # exiting 0 is the same fail-open shape we removed everywhere else.
        instrumentation["violations"][
            "unparseable log line (the lost record may have carried a violation)"
        ] = skipped
        instrumentation["violating"] += skipped
        instrumentation["ok"] = False
    instrumentation["unattributable_records"] = len(unattributable)
    instrumentation["undated_records"] = len(undated)
    instrumentation["unparseable_lines"] = skipped

    # SECURITY zero-tolerance is aggregated GLOBALLY, across every public endpoint
    # of the candidate arch. Restricting it to the gate endpoint would mean a real
    # forbidden write or markup leak on search_direct still shipped.
    global_zt = aggregate_zero_tolerance([r for r in windowed if canonical_arch(r) == "fc"])

    by_arch: Dict[str, List[dict]] = {"fc": [], "legacy": []}
    for r in gate_records:
        by_arch[canonical_arch(r)].append(r)

    fc = aggregate_arch(by_arch["fc"])
    legacy = aggregate_arch(by_arch["legacy"])
    deltas = compute_deltas(fc, legacy)

    # Reported for visibility, never mixed into the gate.
    buckets: Dict[str, List[dict]] = {}
    for r in other_records:
        buckets.setdefault(record_endpoint(r) or "unknown", []).append(r)
    excluded = {name: aggregate_arch(rs) for name, rs in buckets.items()}

    stage_eval = evaluate_stage(fc, stage, since, now) if stage else None
    verdict = build_verdict(fc, legacy, deltas, stage_eval, instrumentation, global_zt)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": list(inputs or []),
        "reference_now": now.isoformat(),
        "window_hours": window_hours,
        "records_total": len(records),
        "records_in_window": len(windowed),
        "records_in_gate": len(gate_records),
        "records_excluded_from_gate": len(other_records),
        "records_skipped": skipped,
        "gate_endpoints": list(GATE_ENDPOINTS),
        "instrumentation": instrumentation,
        "zero_tolerance_global": global_zt,
        "arches": {"fc": fc, "legacy": legacy},
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
    fc = report["arches"]["fc"]
    lg = report["arches"]["legacy"]
    d = report["deltas"]
    lines: List[str] = []
    a = lines.append

    a("=" * 74)
    a("CANARY REPORT — fc_loop vs legacy")
    a("=" * 74)
    a(f"generated_at   : {report['generated_at']}")
    a(f"reference_now  : {report['reference_now']}")
    a(f"window_hours   : {report['window_hours']}")
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
    ]
    w0 = max(len(r[0]) for r in rows)
    hdr = f"{'metric':<{w0}}  {'fc':>12}  {'legacy':>12}  {'delta_pp':>9}"
    a(hdr)
    a("-" * len(hdr))
    for name, fcv, lgv, dl in rows:
        a(f"{name:<{w0}}  {fcv:>12}  {lgv:>12}  {dl:>9}")
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

    sp = v["stage_progress"]
    if sp is not None:
        a("")
        a("[STAGE-PROGRESS] (both minima required):")
        a(f"  stage={sp['stage']} min_turns={sp['min_turns']} min_hours={sp['min_hours']}")
        a(f"  fc_turns={sp['fc_turns']} (turns_ok={sp['turns_ok']})  "
          f"elapsed_hours={sp['elapsed_hours']} (hours_ok={sp['hours_ok']})")
        a(f"  eligible={sp['eligible']}")
        if sp["note"]:
            a(f"  note: {sp['note']}")

    a("=" * 74)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="canary_report.py",
        description="Aggregate canary.turn telemetry and evaluate the fc_loop canary gate.")
    p.add_argument("--input", "-i", action="append", default=[], metavar="PATH",
                   help="JSONL/log file, directory (searched recursively), or glob. "
                        "Repeatable.")
    p.add_argument("--window", type=float, default=None, metavar="HOURS",
                   help="Keep only records within HOURS of the latest observed timestamp.")
    p.add_argument("--json", dest="json_out", default=None, metavar="PATH",
                   help="Write the full report as JSON to PATH ('-' for stdout).")
    p.add_argument("--stage", choices=sorted(STAGES), default=None,
                   help="Evaluate stage-progress minima for this stage.")
    p.add_argument("--since", default=None, metavar="ISO",
                   help="Stage start timestamp (ISO-8601) for the elapsed-hours check.")
    p.add_argument("--now", default=None, metavar="ISO",
                   help="Override the 'now' reference (ISO-8601). Default: latest record ts.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the text table (still writes --json and sets exit code).")
    return p.parse_args(argv)


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if not args.input:
        sys.stderr.write("error: at least one --input is required\n")
        return 1

    records, skipped = load_records(args.input)

    since = parse_ts(args.since) if args.since else None
    if args.since and since is None:
        sys.stderr.write(f"error: could not parse --since '{args.since}'\n")
        return 1
    now_override = parse_ts(args.now) if args.now else None
    if args.now and now_override is None:
        sys.stderr.write(f"error: could not parse --now '{args.now}'\n")
        return 1

    report = build_report(records, window_hours=args.window, now_override=now_override,
                          stage=args.stage, since=since, skipped=skipped,
                          inputs=args.input)

    if args.json_out:
        payload = json.dumps(report, indent=2, sort_keys=True)
        if args.json_out == "-":
            sys.stdout.write(payload + "\n")
        else:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")

    if not args.quiet:
        sys.stdout.write(render_text(report) + "\n")

    return int(report["verdict"]["exit_code"])


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
