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
from typing import Any, Dict, Optional

# Set once per request by begin_turn(). Default None => "no turn in progress",
# which is not the same as "a turn that saw nothing".
_turn_obs: contextvars.ContextVar = contextvars.ContextVar("canary_turn_obs", default=None)

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
        return {"provider_schema_400_count": None, "provider_other_400_count": None,
                "llm_usage_calls": None, "llm_usage_status": USAGE_NOT_INSTRUMENTED}
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
    return {
        "provider_schema_400_count": int(obs.get("provider_schema_400", 0)),
        "provider_other_400_count": int(obs.get("provider_other_400", 0)),
        "llm_usage_calls": calls,
        "llm_usage_status": status,
    }


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


def note_provider_error(exc: Any, *, schemas_bound: bool) -> Optional[str]:
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
        errors.append({"type": type(exc).__name__, "status": status,
                       "schemas_bound": bool(schemas_bound), "bucket": bucket})
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


def note_llm_usage(run_id: Any, response: Any, *, configured_model: Optional[str]) -> bool:
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
    obs.setdefault("llm_usage_calls", []).append({
        "model": observed_model or configured_model or "unknown",
        "model_source": ("response" if observed_model
                         else "config" if configured_model else "unknown"),
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
        "cache_read_tokens": usage.get("cache_read_tokens") or 0,
    })
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
    audit[audit_key] = {
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
            # Fallback only; the provider's own answer wins (see extract_model_name).
            self.configured_model = configured_model

        def _note_start(self, run_id, kwargs):
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
            try:
                note_llm_usage(run_id, response, configured_model=self.configured_model)
            except Exception:
                pass  # telemetry must never break a successful turn

        def on_llm_error(self, error, *, run_id=None, **kwargs):
            bound = self._schemas_bound.pop(run_id, False)
            try:
                note_provider_error(error, schemas_bound=bound)
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
