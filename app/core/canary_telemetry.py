"""Canary turn telemetry — schema v2 record builder.

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
* ``dsml_blocked`` (safe: caught + fell back) is separated from ``dsml_leak``
  (zero-tolerance: markup actually reached the user).
* ``provider_schema_400_count`` counts ONLY provider-side strict-schema
  rejections. The app's own ApiError(400) validation failures cannot appear here:
  they are raised before the turn anchor and emit no record at all.
* ``llm_usage`` aggregates per-model token usage over every LLM call in the turn.
  Cost is deliberately NOT computed here — it is applied offline from a versioned
  price table (scripts/pricing/) so a price change never rewrites history.
* ``user_id_hash`` (HMAC) replaces the raw user id.
* ``eval_only`` self-declares metrics that prod telemetry cannot determine, so
  the report can distinguish "eval-only" from "missing instrumentation".
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

SCHEMA_VERSION = 2
EVENT_NAME = "canary.turn"

ENDPOINT_ALEX = "alex"
ENDPOINT_SEARCH_DIRECT = "search_direct"

# turn_outcome values
OUTCOME_OK = "ok"                    # agent produced a normal answer
OUTCOME_AGENT_ERROR = "agent_error"  # response_type == "error" (handled, HTTP 200)
OUTCOME_CRASH = "crash"              # exception caught by the endpoint (HTTP 200 by design)
OUTCOME_SERVER_ERROR = "server_error"  # escaped to the 500 handler

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
        "soft_wrapped": False, "partial": False, "tool_budget_timeout": False,
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
        "llm_calls": None, "tool_batches": None,
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
        # These are structural: no wrap-up ran, no partial artifact was produced.
        "soft_wrapped": False, "partial": False, "tool_budget_timeout": False,
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
    }
    for field in ("provider_schema_400_count", "llm_usage_status"):
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
    return sig


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
    if not saw_any:
        return None
    return {**totals, "models": per_model}


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
) -> Dict[str, Any]:
    """Build ONE schema-v2 ``canary.turn`` record. Pure: no I/O, no globals beyond
    the HMAC key lookup. ``signals`` carries the arch-specific per-turn observations
    (see ``_build_fc_signals`` in app/app.py); every field defaults SAFELY, and a
    field we could not observe is emitted as ``null`` rather than a fabricated 0/False.
    """
    sig = signals or {}
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
            out.append({
                "tool": r.get("tool"),
                "security_decision": r.get("security_decision"),
                "context_tainted": bool(r.get("context_tainted")),
                "user_authorized": bool(r.get("user_authorized")),
                "dispatch_started": bool(r.get("dispatch_started")),
                "gate_bypassed": bool(r.get("gate_bypassed")),
                "reason": r.get("reason"),
            })
        return out

    return {
        "event": EVENT_NAME,
        "telemetry_schema_version": SCHEMA_VERSION,
        "ts": stamp.isoformat(),
        "endpoint": endpoint,
        "agent_arch": agent_arch,
        "candidate_sha": candidate_sha,
        "strict": bool(strict),
        "request_id": request_id,
        "conversation_id": conversation_id,
        "user_id_hash": uid_hash,
        "user_id_hash_status": hash_status,
        "http_status": int(http_status),
        "turn_outcome": turn_outcome,
        # --- degradation -----------------------------------------------------
        "soft_wrapped": bool(sig.get("soft_wrapped", False)),
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
