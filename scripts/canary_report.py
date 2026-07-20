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
    """Per-record boolean/numeric flags used by the aggregator."""
    o = audit_outcome(rec)
    clean = _audit_is_clean(o)
    safe_denied = _audit_safe_denied(o)

    # Explicit dedicated fields win; else derive from the audit outcome.
    tainted_field = _first_present(rec, ("tainted_write_executed", "unauthorized_write"))
    forbidden_w_field = rec.get("forbidden_write_executed")

    tainted_unauth = _truthy(tainted_field) if tainted_field is not None else (
        (("tainted" in o) or ("unauthorized" in o)) and not safe_denied and "read" not in o
    )
    forbidden_write = _truthy(forbidden_w_field) if forbidden_w_field is not None else (
        ("forbidden" in o and "write" in o) and not safe_denied
    )

    lat = latency_ms(rec)
    return {
        "soft_wrapped": _truthy(rec.get("soft_wrapped")),
        "partial": _truthy(rec.get("partial")),
        "tool_budget_timeout": _truthy(rec.get("tool_budget_timeout")),
        "security_non_clean": not clean,
        "tainted_unauth_write": bool(tainted_unauth),
        "forbidden_write": bool(forbidden_write),
        "latency_ms": lat,
        "over_slo": (lat is not None and lat > OVER_SLO_MS),
        # optional / instrumentation-gated:
        "dsml_leak": _truthy(_first_present(rec, _ALIASES["dsml_leak"])),
        "api_400": _to_int(_first_present(rec, _ALIASES["api_400"])),
        "http_5xx": _to_int(_first_present(rec, _ALIASES["http_5xx"])),
        "forbidden_read": _truthy(_first_present(rec, _ALIASES["forbidden_read"])),
        "no_evidence_numbers": _truthy(_first_present(rec, _ALIASES["no_evidence_numbers"])),
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


def aggregate_arch(records: Sequence[dict]) -> dict:
    """Aggregate one arch's records into a stats dict."""
    flags = [classify(r) for r in records]
    turns = len(records)
    convos = {str(r.get("conversation_id")) for r in records if r.get("conversation_id") is not None}
    users = {str(r.get("user_id")) for r in records if r.get("user_id") is not None}
    lats = [f["latency_ms"] for f in flags if f["latency_ms"] is not None]

    soft = sum(1 for f in flags if f["soft_wrapped"])
    part = sum(1 for f in flags if f["partial"])
    tbt = sum(1 for f in flags if f["tool_budget_timeout"])
    degraded = sum(1 for f in flags if f["soft_wrapped"] or f["partial"])
    over = sum(1 for f in flags if f["over_slo"])
    non_clean = sum(1 for f in flags if f["security_non_clean"])

    # instrumentation presence (per arch, key-existence based)
    instr = {
        name: any(_has_any_key(r, keys) for r in records)
        for name, keys in _ALIASES.items()
    }

    dsml = sum(1 for f in flags if f["dsml_leak"]) if instr["dsml_leak"] else None
    api400 = sum(f["api_400"] for f in flags) if instr["api_400"] else None
    x5 = sum(f["http_5xx"] for f in flags) if instr["http_5xx"] else None
    fr = sum(1 for f in flags if f["forbidden_read"]) if instr["forbidden_read"] else None
    nen = (sum(1 for f in flags if f["no_evidence_numbers"])
           if instr["no_evidence_numbers"] else None)

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
        "tainted_unauth_write_count": sum(1 for f in flags if f["tainted_unauth_write"]),
        "forbidden_write_count": sum(1 for f in flags if f["forbidden_write"]),
        # optional (None => not instrumented for this arch)
        "instrumented": instr,
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
                  stage_eval: Optional[dict]) -> dict:
    """Evaluate shuhan's zero-tolerance and stage-pause rules against the fc pool.

    Precedence for the exit code: zero-tolerance (3) beats stage-pause (2) beats proceed (0).
    A stage evaluated-but-not-eligible is a HOLD (exit 0), not a pause.
    """
    zt_reasons: List[str] = []
    zt_notes: List[str] = []

    # --- ZERO-TOLERANCE (absolute; any >0 on fc => instant rollback) ---
    if fc["tainted_unauth_write_count"] > 0:
        zt_reasons.append(
            f"tainted/unauthorized memory write executed x{fc['tainted_unauth_write_count']} (must be 0)")
    if fc["forbidden_write_count"] > 0:
        zt_reasons.append(
            f"forbidden write executed x{fc['forbidden_write_count']} (must be 0)")
    if fc["dsml_leak_count"] is None:
        zt_notes.append("dsml/tool-markup leak: not instrumented")
    elif fc["dsml_leak_count"] > 0:
        zt_reasons.append(f"DSML/tool-markup leak x{fc['dsml_leak_count']} (must be 0)")
    if fc["api_400_count"] is None:
        zt_notes.append("schema/API 400s: not instrumented")
    elif fc["api_400_count"] > 0:
        zt_reasons.append(
            f"systematic schema/API 400s x{fc['api_400_count']} (must be 0)")

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
        sp_notes.append("5xx rate: not instrumented")
    else:
        pp5 = (fc["http_5xx_rate"] - legacy["http_5xx_rate"]) * 100.0
        if pp5 > RELATIVE_PP:
            sp_reasons.append(
                f"5xx rate {fc['http_5xx_rate']*100:.2f}% > legacy "
                f"{legacy['http_5xx_rate']*100:.2f}% + {RELATIVE_PP:.0f}pp (delta {pp5:+.2f}pp)")

    zt_breached = bool(zt_reasons)
    sp_breached = bool(sp_reasons)

    # --- decision / exit code ---
    if zt_breached:
        decision, exit_code = "CANARY-BLOCK", 3
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
    windowed = filter_window(records, window_hours, now)

    by_arch: Dict[str, List[dict]] = {"fc": [], "legacy": []}
    for r in windowed:
        by_arch[canonical_arch(r)].append(r)

    fc = aggregate_arch(by_arch["fc"])
    legacy = aggregate_arch(by_arch["legacy"])
    deltas = compute_deltas(fc, legacy)

    stage_eval = evaluate_stage(fc, stage, since, now) if stage else None
    verdict = build_verdict(fc, legacy, deltas, stage_eval)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": list(inputs or []),
        "reference_now": now.isoformat(),
        "window_hours": window_hours,
        "records_total": len(records),
        "records_in_window": len(windowed),
        "records_skipped": skipped,
        "arches": {"fc": fc, "legacy": legacy},
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
