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
)
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
                     "forbidden_write_executed_count": 0},
        "dsml_blocked": 0, "dsml_leak": 0, "provider_schema_400_count": 0,
        "llm_usage": None, "llm_calls": None, "tool_batches": None,
    }


def unknown_turn_signals() -> Dict[str, Any]:
    """Signals for a turn whose outcome is NOT observable: an agent crash, or a
    request that died at the response boundary (5xx).

    Zeros would be FAIL-OPEN here, which an earlier revision of this module got
    wrong. The agent can dispatch `remember` (a write) and crash afterwards, so a
    0 write count is an assumption, not an observation. Worse, a provider
    strict-schema 400 is a plausible CAUSE of the crash, so reporting
    provider_schema_400_count=0 would suppress exactly the signal the gate exists
    to catch. Everything unobservable is therefore null -> the report HOLDs.

    Layer B must stash the real provider status into a ContextVar BEFORE re-raising
    so the boundary record can report an actual count instead of null.
    """
    return {
        # These are structural: no wrap-up ran, no partial artifact was produced.
        "soft_wrapped": False, "partial": False, "tool_budget_timeout": False,
        # Unobservable — a write may already have executed before the crash.
        "security": {"denied_write_count": None,
                     "tainted_write_executed_count": None,
                     "forbidden_write_executed_count": None},
        # Unobservable — partial output may already have been flushed, and a
        # provider 400 may itself be the crash cause.
        "dsml_blocked": None, "dsml_leak": None, "provider_schema_400_count": None,
        "llm_usage": None, "llm_calls": None, "tool_batches": None,
    }


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
        # --- explicitly eval-only (null, never False) ------------------------
        "forbidden_read": None,
        "no_evidence_numbers": None,
        "eval_only": list(EVAL_ONLY_FIELDS),
    }
