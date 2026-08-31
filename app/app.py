# app.py - Enhanced with RAG and LangGraph Agent Framework

import sys
from pathlib import Path
_src_dir = Path(__file__).resolve().parents[1] / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import asyncio
import uuid
import copy
import threading
import os
import time
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import contextvars
from flask import Flask, request, jsonify, render_template, session, g, has_request_context
from flask_cors import CORS
from werkzeug.exceptions import HTTPException, BadRequest, UnsupportedMediaType
import json
import re
from datetime import datetime
from uk_rent_agent.web.session_store import SessionStore
from uk_rent_agent.web.background_jobs import OutboxWorker
from uk_rent_agent.web.conversation_store import (
    ConversationStore, ConversationNotFound, NoCompletedTurn, TurnNotFound,
    TurnNotInConversation, TurnNotCompleted, ConversationBusy,
    PrivacyErasureInProgress,
)
from uk_rent_agent.web.identity import (
    resolve_user_id, normalize_message, valid_user_id, InvalidUserId, InvalidMessage,
)
from uk_rent_agent.web.auth_store import (
    AuthStore, AuthError, InvalidUsername, WeakPassword, UsernameTaken,
)
from uk_rent_agent.config import Config
from uk_rent_agent.agent.architecture import MANAGER_V1_ARCH, uses_fc_runtime
from uk_rent_agent.web.rate_limit import SlidingWindowRateLimiter
from uk_rent_agent.agent.persistence import get_sqlite_checkpointer, get_prefs_store, graph_config
from uk_rent_agent.observability import (
    agent_execution_context,
    new_request_id,
    request_context,
)
from core.data_loader import load_mock_properties_from_csv, load_properties
from core.tool_system import create_tool_registry
from core.langgraph_agent import build_agent_graph, create_initial_state
from core.tools.search_properties import search_properties_impl
from core.context_assembler import (
    assemble as assemble_context,
    build_turn_snapshot,
    snapshot_to_session_patch,
    SnapshotSchemaError,
    update_rolling_summary,
    render_recommended_index,
)
from core.llm_interface import call_ollama


from core.candidate_validation import (
    render_candidate_status, validate_search_payload_with_provider,
)


def _consume_abandoned_graph_task(task) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


class _GraphLoopRunner:
    """One loop-local graph/provider generation in a bounded self-healing pool."""

    def __init__(self):
        self._start_lock = threading.Lock()
        self._ready = threading.Event()
        self._loop = None
        self._thread = None
        self._quarantined = False
        self.graph = None

    def _serve(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def _ensure_started(self):
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._serve, name="agent_graph_loop", daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("agent graph loop failed to start")
        if self._loop is None:
            raise RuntimeError("agent graph loop unavailable")

    def available(self):
        with self._start_lock:
            return not self._quarantined

    def submit(self, coroutine):
        self._ensure_started()
        with self._start_lock:
            if self._quarantined:
                coroutine.close()
                raise RuntimeError("agent graph loop is quarantined")
            loop = self._loop
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def quarantine(self, recovered_callback):
        """Reject new work until a queued heartbeat proves this loop is responsive."""
        self._ensure_started()
        with self._start_lock:
            if self._quarantined:
                return False
            self._quarantined = True
            loop = self._loop

        async def _heartbeat():
            return True

        probe = asyncio.run_coroutine_threadsafe(_heartbeat(), loop)

        def _recovered(_future):
            try:
                _future.result()
            except BaseException:
                return
            with self._start_lock:
                self._quarantined = False
            recovered_callback(self)

        probe.add_done_callback(_recovered)
        return True


_GRAPH_RUNNER_CAPACITY = 2
_GRAPH_RUNNER_ATTR = "_uk_rent_graph_loop_runner"
_graph_runtime_lock = threading.Lock()
_graph_loop_runner = _GraphLoopRunner()
_graph_loop_runners = [_graph_loop_runner]


def _on_graph_runner_recovered(runner):
    """Restore recovered capacity without moving a graph across event loops."""
    global _graph_loop_runner, agent_graph
    with _agent_init_lock:
        with _graph_runtime_lock:
            if _graph_loop_runner is None:
                _graph_loop_runner = runner
                agent_graph = runner.graph


def _quarantine_graph_runner(runner):
    """Rotate to bounded standby capacity while the timed-out loop proves liveness."""
    global _graph_loop_runner, agent_graph
    if not runner.quarantine(_on_graph_runner_recovered):
        return
    with _agent_init_lock:
        with _graph_runtime_lock:
            if _graph_loop_runner is not runner:
                return
            standby = next(
                (item for item in _graph_loop_runners
                 if item is not runner and item.available()),
                None,
            )
            if standby is None and len(_graph_loop_runners) < _GRAPH_RUNNER_CAPACITY:
                standby = _GraphLoopRunner()
                _graph_loop_runners.append(standby)
            _graph_loop_runner = standby
            agent_graph = standby.graph if standby is not None else None
            if standby is None:
                logging.getLogger("app").error(
                    "agent.graph.capacity_unavailable",
                    extra={"runner_capacity": _GRAPH_RUNNER_CAPACITY},
                )


async def _ainvoke_graph_with_timeout(graph, graph_input, graph_config_value, timeout_s: float):
    """Run the graph behind a hard HTTP deadline, isolated from event-loop blocking."""
    if timeout_s <= 0:
        raise asyncio.TimeoutError("agent turn deadline exhausted before graph dispatch")
    # The graph and async clients stay on the runner generation that created them.
    runner = getattr(graph, _GRAPH_RUNNER_ATTR, _graph_loop_runner)
    if runner is None:
        raise RuntimeError("agent graph capacity is temporarily unavailable")
    concurrent_future = runner.submit(
        graph.ainvoke(graph_input, config=graph_config_value))
    future = asyncio.wrap_future(concurrent_future)
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
    except asyncio.TimeoutError:
        concurrent_future.cancel()
        concurrent_future.add_done_callback(_consume_abandoned_graph_task)
        _quarantine_graph_runner(runner)
        raise asyncio.TimeoutError("agent graph exceeded the whole-turn deadline") from None
    except BaseException:
        concurrent_future.cancel()
        concurrent_future.add_done_callback(_consume_abandoned_graph_task)
        raise


def _llm_complete(prompt: str) -> str:
    """Sync completion used by the rolling-summary folder (dependency-injected into
    context_assembler.update_rolling_summary). Never raises — an empty string makes the
    summary folder keep the prior summary unchanged."""
    try:
        return call_ollama(prompt) or ""
    except Exception:
        return ""

def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


app = Flask(__name__, template_folder='.')
_runtime_config = globals().get("_BOOTSTRAP_CONFIG") or Config.from_env()
# Graph selection and strict-schema binding still have a few legacy getenv consumers. Publish
# the already-normalized Config values once so all of them observe the same runtime identity.
os.environ["AGENT_ARCH"] = _runtime_config.agent_arch
os.environ["MANAGER_V1_SPECIALISTS"] = (
    "1" if _runtime_config.manager_v1_specialists_effective else "0"
)
os.environ["DEEPSEEK_STRICT"] = "1" if _runtime_config.deepseek_strict else "0"
os.environ["LLM_PROVIDER"] = _runtime_config.llm_provider
# supports_credentials=True so the signed session cookie (which now carries the
# authenticated identity) survives cross-origin requests when the UI is opened over a
# different origin (e.g. file://). Same-origin (render_template at :5001) works regardless.
CORS(app, origins=list(_runtime_config.cors_origins), supports_credentials=True)
_api_rate_limiter = SlidingWindowRateLimiter(
    db_path=_runtime_config.rate_limit_db_path
)

# Secret key — needed for the server-side `session` cookie used as a per-browser
# identity fallback (priority (c) in resolve_identity). Read from env first so a real
# deployment secret is never clobbered; otherwise use a stable dev secret so cookies
# survive across requests (a random per-boot key would break single-user continuity).
if not app.secret_key:
    app.secret_key = _runtime_config.flask_secret_key or "uk-rent-dev-secret-key-do-not-use-in-prod"

# Session cookie hardening — the signed session cookie now also carries the authenticated
# identity, so lock it to HTTP-only + Lax SameSite. Not marked Secure (local demo runs over
# plain http://localhost); set SESSION_COOKIE_SECURE=1 behind TLS in a real deployment.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_runtime_config.session_cookie_secure,
    PERMANENT_SESSION_LIFETIME=_runtime_config.session_ttl_seconds,
    MAX_CONTENT_LENGTH=_runtime_config.max_request_bytes,
)

# ============================================================================
# Local username/password authentication
# ----------------------------------------------------------------------------
# Credentials live in a gitignored JSON file (password *hashes* only, never plaintext).
# A logged-in session's identity is authoritative — see resolve_identity — so a client
# can no longer impersonate an account by spoofing the X-User-Id header/query/body.
# ============================================================================
auth_store = AuthStore(str(_runtime_config.auth_db_path))
print(f"[STARTUP] Auth store configured "
      f"(require_auth={_runtime_config.require_auth})")

# 统一 UI 模式标志
USE_UNIFIED_UI = True  # 设置为 True 使用新的统一 Alex 界面

# LangGraph Agent — compiled graph (lazy-initialized)
agent_graph = None
_agent_init_lock = threading.Lock()


def _ensure_agent_runtime():
    """Return an atomically published, loop-bound graph/provider generation."""
    global agent_graph, tool_registry, agent_tool_provider

    # The same lock is used by runner rotation, so callers can only observe a
    # complete (runner, graph) generation. A stale caller still retains the old
    # binding and can never submit that graph on the replacement loop.
    with _agent_init_lock:
        runner = _graph_loop_runner
        if runner is None or not runner.available():
            raise RuntimeError("agent graph capacity is temporarily unavailable")

        if (
            tool_registry is not None
            and agent_tool_provider is not None
            and agent_graph is not None
            and getattr(agent_graph, _GRAPH_RUNNER_ATTR, None) is runner
        ):
            return agent_graph

        if tool_registry is None:
            candidate_registry = create_tool_registry()
            from core.tools.web_search import set_tool_registry
            set_tool_registry(candidate_registry)
            tool_registry = candidate_registry

        if agent_tool_provider is None:
            agent_tool_provider = tool_registry

        checkpointer = None
        if _runtime_config.enable_checkpointer and _runtime_config.checkpoint_path:
            checkpointer = get_sqlite_checkpointer(_runtime_config.checkpoint_path)
        store = get_prefs_store() if _runtime_config.enable_store else None
        candidate_graph = build_agent_graph(
            agent_tool_provider,
            checkpointer=checkpointer,
            store=store,
            enable_hitl=_runtime_config.enable_hitl,
            agent_llm=_configured_fc_agent_llm(),
            manager_v1_specialists=MANAGER_V1_SPECIALISTS,
        )
        # Treat inability to bind as a startup failure: silently falling back to
        # the current global runner could reuse async clients across event loops.
        setattr(candidate_graph, _GRAPH_RUNNER_ATTR, runner)
        runner.graph = candidate_graph
        agent_graph = candidate_graph
        logger.info(
            "agent.graph_initialized",
            extra={"agent_arch": AGENT_ARCH, "provider": _runtime_config.llm_provider},
        )
        return candidate_graph

# ============================================================================
# Multi-user identity + per-user isolated state (L2 conversational state)
# ----------------------------------------------------------------------------
# Previously these were bare module globals shared by EVERY caller. They are now
# keyed by user_id so different people get fully isolated conversations. The inner
# shapes are unchanged — single-user behaviour under user_id="default" is identical.
# ============================================================================

def _default_persistent_state():
    """Canonical default cross-turn state (preferences & accumulated criteria).

    Returns a FRESH copy every call so per-user slices never alias each other.
    """
    return {
        'user_preferences': {
            'hard_preferences': [], 'soft_preferences': [],
            'excluded_areas': [], 'required_amenities': [],
            'safety_concerns': [],
        },
        'accumulated_search_criteria': {
            'destination': None, 'max_budget': None, 'max_travel_time': None,
            'property_features': [], 'soft_preferences': [],
            'amenities_of_interest': [],
        },
        'extracted_context': {},
    }


# Per-user L2 stores (was: agent_persistent_state / conversation_history / last_search_results)
# SessionStore is now a HOT CACHE keyed by (user_id, conversation_id); the durable copy of
# conversations / messages / favorites lives in the sqlite ConversationStore below.
_session_store = SessionStore(
    max_users=_runtime_config.session_max_users,
    ttl_seconds=_runtime_config.session_ttl_seconds,
)


def _conversation_db_path():
    """Sqlite path for the durable conversation store. Defaults alongside the LangGraph
    checkpointer under .runtime/; override via CONVERSATION_DB_PATH so a test instance can
    use an isolated file instead of sharing the live server's DB."""
    override = os.getenv("CONVERSATION_DB_PATH")
    if override:
        return override
    cp = _runtime_config.checkpoint_path
    base = Path(cp).parent if cp else (Path(__file__).resolve().parents[1] / ".runtime")
    return str(base / "conversations.sqlite3")


conversation_store = ConversationStore(_conversation_db_path())
print("[STARTUP] Conversation store configured")
_turn_background_jobs = contextvars.ContextVar(
    "turn_background_jobs", default=None
)
_outbox_worker = None
_outbox_worker_lock = threading.Lock()

# ============================================================================
# Canary rollout (Shuhan's design, 2026-07-20) — process-level constants.
# ----------------------------------------------------------------------------
# The deployment runs TWO worker pools (legacy vs fc_loop) with pool-level nginx cutover;
# each conversation durably records the arch/version that last served it for provenance.
# These three values describe THIS process and are read EXACTLY ONCE at startup — never
# per-request (hot-path getenv is forbidden). The normalized Config value is also published to
# the remaining lazy graph getenv consumer, so recorded identity and the built topology cannot
# diverge because of whitespace/case or a later ambient-environment read.
def _read_agent_arch() -> str:
    return _runtime_config.agent_arch


def _read_strict() -> bool:
    return _runtime_config.deepseek_strict


def _read_manager_v1_specialists() -> bool:
    return _runtime_config.manager_v1_specialists_effective


def _startup_git_sha() -> str:
    """Short git SHA if cheaply available at startup, else 'unknown'. Called ONCE."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=2,
        )
        sha = (out.stdout or "").strip()
        if out.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return "unknown"


AGENT_ARCH = _read_agent_arch()
MANAGER_V1_SPECIALISTS = _read_manager_v1_specialists()
DEEPSEEK_STRICT = _read_strict()
# APP_CANDIDATE_SHA is the image tag the fc pool is pinned to; fall back to the local git
# SHA (dev), else "unknown". Read once — a process-level constant, NOT a per-request value.
APP_CANDIDATE_SHA = (os.getenv("APP_CANDIDATE_SHA") or "").strip() or _startup_git_sha()
print(f"[STARTUP] Canary: agent_arch={AGENT_ARCH} candidate_sha={APP_CANDIDATE_SHA} "
      f"strict={DEEPSEEK_STRICT} manager_v1_specialists={MANAGER_V1_SPECIALISTS}")


def _configured_fc_agent_llm():
    """Return the explicit Ollama driver for every FC-compatible architecture."""
    if not uses_fc_runtime(AGENT_ARCH) or _runtime_config.llm_provider != "ollama":
        return None
    from core.llm_config import get_react_llm
    return get_react_llm(low_latency=True)

# Dedicated structured-telemetry logger. app.py otherwise logs via print(); ops attaches a
# handler to "canary" to ship these. Each completed turn emits exactly ONE JSON line via
# _emit_canary_turn(); the event name lives inside the JSON ("event": "canary.turn").
_canary_logger = logging.getLogger("canary")
# General app logger. Separate from _canary_logger, which has its own JSONL sink and
# must carry nothing but canary records.
logger = logging.getLogger("app")

# Marker attribute set on the FileHandler we attach so re-invocation (tests reloading the
# module, or re-calling _wire_canary_sink after monkeypatching env) can find + replace OUR
# handler without stacking duplicates or disturbing handlers ops may have added.
_CANARY_SINK_MARKER = "_canary_sink"
_canary_sink_failures = 0
_canary_sink_lock = threading.Lock()


class _CanaryRotatingFileHandler(RotatingFileHandler):
    def handleError(self, record) -> None:  # noqa: N802 - logging API name
        global _canary_sink_failures
        with _canary_sink_lock:
            _canary_sink_failures += 1
        super().handleError(record)


def canary_sink_health() -> dict:
    raw = (os.getenv("CANARY_LOG_PATH") or "").strip()
    if raw.lower() in {"off", "0", "disabled"}:
        return {"status": "disabled", "required": False, "failures": 0}
    with _canary_sink_lock:
        failures = int(_canary_sink_failures)
    attached = any(
        getattr(handler, _CANARY_SINK_MARKER, False)
        for handler in _canary_logger.handlers
    )
    return {
        "status": "degraded" if failures or not attached else "ok",
        "required": False,
        "failures": failures,
        "attached": attached,
    }


def _wire_canary_sink() -> None:
    """Attach a dedicated file sink to the "canary" logger at import time.

    DEFECT FIX (2026-07-21): the "canary" logger had NO handler anywhere in the repo. Under
    uvicorn (which does not configure the root logger) INFO records hit logging.lastResort
    (WARNING threshold) and canary.turn lines were silently DROPPED — zero telemetry to disk.

    Env `CANARY_LOG_PATH` contract:
      * a path            → write telemetry there.
      * "off"/"0"/"disabled" (case-insensitive) → attach NO handler (telemetry disabled).
      * UNSET / empty     → DEFAULT ENABLED at <runtime_dir>/logs/canary-<arch>.jsonl, where
                            runtime_dir = _runtime_config.checkpoint_path.parent if set else
                            <project_root>/.runtime, and <arch> is AGENT_ARCH.

    The formatter emits ONLY the record message ("%(message)s") because each message already
    IS a bare JSON object — scripts/canary_report.py parses one JSON line per record directly.
    Idempotent: our previously-attached sink (marked via _CANARY_SINK_MARKER) is removed first,
    so re-invocation replaces rather than stacks. Any failure degrades to a printed warning and
    never crashes startup. propagate stays True so container stderr still sees the lines.
    """
    try:
        _canary_logger.setLevel(logging.INFO)

        # Drop any sink WE previously attached (idempotent replace). Leave foreign handlers.
        for h in list(_canary_logger.handlers):
            if getattr(h, _CANARY_SINK_MARKER, False):
                _canary_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass

        raw = (os.getenv("CANARY_LOG_PATH") or "").strip()
        if raw.lower() in {"off", "0", "disabled"}:
            print("[STARTUP] Canary telemetry: disabled")
            return

        if raw:
            path = Path(raw)
        else:
            ckpt = getattr(_runtime_config, "checkpoint_path", None)
            runtime_dir = ckpt.parent if ckpt else (Path(__file__).resolve().parents[1] / ".runtime")
            path = runtime_dir / "logs" / f"canary-{AGENT_ARCH}.jsonl"

        resolved = str(Path(path).resolve())
        # Guard: if an equivalent sink already targets this path, do not add a second one.
        for h in _canary_logger.handlers:
            if isinstance(h, logging.FileHandler) and \
                    str(Path(getattr(h, "baseFilename", "")).resolve()) == resolved:
                print("[STARTUP] Canary telemetry configured")
                return

        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _CanaryRotatingFileHandler(
            path,
            maxBytes=max(1024, int(os.getenv("CANARY_LOG_MAX_BYTES", str(20 * 1024 * 1024)))),
            backupCount=max(1, int(os.getenv("CANARY_LOG_BACKUP_COUNT", "5"))),
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, _CANARY_SINK_MARKER, True)
        _canary_logger.addHandler(handler)
        print("[STARTUP] Canary telemetry configured")
    except Exception as e:  # pragma: no cover — telemetry wiring must never break startup
        print(f"[STARTUP] WARNING: canary telemetry sink wiring failed; error_type={type(e).__name__}")


_wire_canary_sink()

# Per-request carrier for the fc-side turn signals. handle_with_react_agent (which alone can
# see the graph's final_state) fills this after the graph runs; the /api/alex handler reads it
# back after the awaited call (a directly-awaited coroutine shares the caller's context, so the
# .set() is visible on return) to assemble the canary.turn record. None => defaults (a crashed
# turn, or a monkeypatched agent in tests that never sets it). NEVER read in a hot path.
_turn_fc_signals: contextvars.ContextVar = contextvars.ContextVar("turn_fc_signals", default=None)

MAX_HISTORY_LENGTH = 10  # 保留最近10轮对话

# extracted_context 白名单：只回传前端真正需要的房产上下文标量。
# 其余内部字段（previous_search_results / last_results / comparison_properties /
# current_message 以及原始房源大文本）留在服务端，避免把候选池泄露给客户端。
_EXTRACTED_CONTEXT_WHITELIST = (
    "property_address", "property_price", "property_travel_time", "property_url",
)


def _whitelist_extracted_context(ctx) -> dict:
    if not isinstance(ctx, dict):
        return {}
    return {k: ctx[k] for k in _EXTRACTED_CONTEXT_WHITELIST
            if ctx.get(k) not in (None, "", [], {})}


# ============================================================================
# Canary rollout — architecture-provenance reconciliation, per-turn telemetry, headers.
# ============================================================================

@app.after_request
def _canary_headers(response):
    """Stamp the process arch/version on EVERY response (chat, turn, and CRUD alike). A
    single after_request hook is cheaper and less error-prone than per-endpoint edits, and
    the values are process constants (no per-request work). X-Request-Id stays set by the
    turn endpoints themselves (it is per-request)."""
    response.headers["X-Agent-Arch"] = AGENT_ARCH
    response.headers["X-Agent-Version"] = APP_CANDIDATE_SHA
    response.headers["X-Agent-Specialists"] = "1" if MANAGER_V1_SPECIALISTS else "0"
    return response


def _reconcile_agent_arch(user_id: str, conversation_id: str, conv: dict) -> None:
    """Reconcile architecture provenance when a turn starts on an existing conversation.
    Steady-state this is a no-op (stored arch == this process's arch).

    Emergency-rollback path (documented in the design): if the stored arch differs from this
    process's AGENT_ARCH — e.g. an fc conversation now being served by a rebuilt/rolled-back
    legacy process — the process serves it with ITS OWN arch anyway (legacy rebuilds cleanly
    from the shared message history), but we LOG a structured arch_mismatch warning and
    overwrite the stored provenance with this process's arch so subsequent telemetry is
    accurate. A freshly created conversation already carries this process's arch, so this
    never fires for new conversations."""
    stored = (conv or {}).get("agent_arch")
    if stored and stored != AGENT_ARCH:
        user_hash, _ = hash_user_id(user_id)
        conversation_hash, _ = hash_user_id(conversation_id)
        _canary_logger.warning(json.dumps({
            "event": "canary.arch_mismatch",
            "conversation_id_hash": conversation_hash,
            "user_id_hash": user_hash,
            "stored_arch": stored,
            "serving_arch": AGENT_ARCH,
            "candidate_sha": APP_CANDIDATE_SHA,
        }, ensure_ascii=False, default=str))
        conversation_store.set_agent_assignment(
            user_id, conversation_id, AGENT_ARCH, APP_CANDIDATE_SHA, DEEPSEEK_STRICT)


from core.canary_telemetry import (  # noqa: E402
    ENDPOINT_ALEX, ENDPOINT_SEARCH_DIRECT, OUTCOME_AGENT_ERROR, OUTCOME_CRASH,
    OUTCOME_OK, OUTCOME_SERVER_ERROR, aggregate_llm_usage, build_canary_turn_record,
    hash_user_id, search_direct_signals, unknown_turn_signals,
)
from core import turn_observations  # noqa: E402
from core import dsml_guard  # noqa: E402


def _load_write_audit_instrumentation() -> None:
    """Import the active arch's tool-execution module at startup.

    The write audit registers itself at import of the module that owns the policy
    decision point. legacy's module is already imported above; fc_loop's
    (``core.agent_loop``) loads lazily on first use, which would leave the very first
    fc turn reporting ``not_instrumented`` — a HOLD caused by import timing rather
    than by anything about the turn. Importing it here makes registration a property
    of the process instead of of request ordering.
    """
    if not uses_fc_runtime(AGENT_ARCH):
        return
    try:
        if AGENT_ARCH == MANAGER_V1_ARCH:
            # Imports the FC executor and registers manager_v1 as an alias of its
            # write-audited dispatch path.
            import core.manager_v1  # noqa: F401
        else:
            import core.agent_loop  # noqa: F401
    except Exception:
        # Left unregistered deliberately: the counters stay null and the gate HOLDs,
        # which is the right outcome when the instrumented module will not load.
        logger.warning(
            "canary: FC-compatible write-audit instrumentation failed to import "
            "(arch=%s)",
            AGENT_ARCH,
        )


def _wire_canary_llm_observer() -> None:
    """Force the canary LLM observer to install at STARTUP, not on first model use.

    ``turn_observations.snapshot()`` reports all-null + ``not_instrumented`` — which HOLDs
    the gate — whenever ``observer_installed()`` is False. That flag is set as a side effect
    of ``install_observer``, which runs only inside ``ModelRouter.create()``. So until the
    process builds its FIRST model the flag is False, and any turn that closes without
    building one emits a record that violates the v2 contract (null
    ``provider_schema_400_count``, ``llm_usage_status='not_instrumented'``).

    Not hypothetical: the 2026-07-25 fc smoke reproduced it twice. Turn 1 was a greeting,
    the guard fast path answered with ZERO LLM calls, no model was ever constructed, and the
    record was excluded from the gate population — ``--expect-turns 2`` saw 1. A third
    greeting, issued after a turn that HAD built a model, was contract-valid. The condition
    is exactly "a zero-LLM-call turn in a process that has not yet constructed any model",
    and the cost is worse than a miscount: such turns vanish from the denominator of p50 and
    of every rate the gate computes.

    Constructing a client makes no network call, so this is free. It deliberately exercises
    the REAL router path instead of just setting the flag — the flag must mean "an LLM call
    would have been observed", and only running the actual install proves that. On failure
    the flag stays False and the gate keeps HOLDing; this never fabricates instrumentation.

    Mirrors ``_load_write_audit_instrumentation`` above: registration becomes a property of
    the process rather than of request ordering.
    """
    try:
        from uk_rent_agent.llm.router import ModelRouter
        from core import turn_observations

        if turn_observations.observer_installed():
            return
        ModelRouter().create("intent")  # throwaway; construction makes no network call
        if not turn_observations.observer_installed():
            logger.warning("canary: LLM observer did not install at startup — the gate will "
                           "HOLD on any turn that makes no LLM call")
    except Exception as exc:
        logger.warning("canary: LLM observer startup wiring failed; zero-LLM-call turns "
                       "will report not_instrumented and HOLD the gate; error_type=%s", type(exc).__name__)


_load_write_audit_instrumentation()
_wire_canary_llm_observer()


def _dsml_boundary_check(response, payload):
    """Tool-markup guard, LAYER 2: the last look before the bytes leave.

    Scans the SERIALIZED body, which is how it reaches every nested user-visible
    string — card text, clarifying questions, listing descriptions — without
    knowing the payload's shape. Shape knowledge is exactly what goes stale when
    somebody adds a field, and a guard that silently stops covering new fields is
    worse than none.

    A hit is recorded as ``dsml_leak``, NOT ``dsml_blocked``. Layer 1 runs before
    persistence and is the real control; if markup reaches here, layer 1's
    detection is wrong. Scoring this as a successful block would let the gate pass
    a release whose primary control is broken. Nothing ships either way — the body
    is replaced — but the counter says the design failed, not that it worked.
    """
    try:
        if not dsml_guard.scan_serialized(response.get_data()):
            return response
        turn_observations.note_dsml_leak()
        # No raw text in the log line; see the layer-1 note.
        logger.error("canary: tool-call markup reached the response boundary — "
                     "layer 1 missed it (arch=%s)", AGENT_ARCH)
        safe = dict(payload or {})
        safe["message"] = dsml_guard.fallback_text(getattr(g, "reply_language", None))
        safe["response_type"] = "error"
        # Drop every other carrier of model text. Rebuilding from a whitelist rather
        # than editing in place is the only version that stays correct when a new
        # field is added: an unknown key is dropped, not shipped unchecked.
        safe = {k: v for k, v in safe.items()
                if k in ("message", "response_type", "conversation_id", "turn_id")}
        if isinstance(payload, dict):
            payload.clear()
            payload.update(safe)
        replaced = jsonify(safe)
        replaced.status_code = 502
        for k, v in response.headers.items():
            if k.lower().startswith("x-"):
                replaced.headers[k] = v
        replaced.headers["X-Agent-Outcome"] = "error"
        return replaced
    except Exception as exc:
        # The guard must never be the reason a request fails. A scan that raises
        # leaves the original response alone; the counter stays 0 and the turn is
        # reported as it was.
        logger.error("canary: dsml boundary check failed error_type=%s", type(exc).__name__)
        return response


class ResponsePayloadSerializationError(TypeError):
    """The response object cannot satisfy the JSON API contract."""


def _guard_payload_before_persistence(payload: dict) -> tuple[dict, bool]:
    """Scan the complete response object before it can reach any durable store.

    Layer 1 sanitizes the main answer. This structural backstop covers every nested
    user-visible field. A hit means the primary control missed something, so the turn
    is persisted as a failed, whitelisted fallback and no background job is committed.
    """
    # Serialization errors are response-boundary failures, not security findings.
    # Let them reach the existing 500 handler so telemetry records the real failure
    # and the open turn is released; only a scanner failure itself is fail-closed.
    try:
        serialized = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except Exception as exc:
        raise ResponsePayloadSerializationError(
            "response payload is not JSON serializable"
        ) from exc
    try:
        hit = dsml_guard.scan_serialized(serialized)
    except Exception as exc:
        logger.error(
            "response.security_scan_failed",
            extra={"error_type": type(exc).__name__},
        )
        hit = True
    if not hit:
        return payload, False

    turn_observations.note_dsml_leak()
    logger.error(
        "canary: unsafe model/tool markup blocked before persistence (arch=%s)",
        AGENT_ARCH,
    )
    safe = {
        "message": dsml_guard.fallback_text(getattr(g, "reply_language", None)),
        "response_type": "error",
    }
    for key in ("conversation_id", "turn_id"):
        if key in payload:
            safe[key] = payload[key]
    return safe, True


def _crashed_turn_observations() -> dict:
    """Everything the out-of-band accumulators caught before the turn died.

    Merged into one dict because ``unknown_turn_signals`` overlays a flat mapping;
    the write-audit keys and the LLM keys do not collide.
    """
    merged = dict(turn_observations.snapshot())
    merged.update(turn_observations.write_audit_snapshot(AGENT_ARCH))
    merged.update(turn_observations.dsml_snapshot())
    return merged


def _build_fc_signals(final_state) -> dict:
    """Derive the per-turn canary signals from the graph's final_state. Robust to the legacy
    arch (whose final_state may lack the fc channels): everything defaults safely.

    ``llm_calls`` comes from the arch-agnostic callback observer, because legacy
    classifier/responder calls are billed too. ``loop_turn`` is retained only as a
    fail-closed lower bound for FC runtimes: if the graph proves more model steps
    than the observer saw, usage is partial. ``tool_batches`` is derived from the
    shared artifact ledger plus an optional legacy execution-plan wave."""
    if not isinstance(final_state, dict):
        final_state = {}
    artifacts = final_state.get("tool_artifacts") or []
    if not isinstance(artifacts, list):
        artifacts = []
    # partial: any executed tool artifact whose raw_data reports a partial result.
    partial = any(isinstance(a, dict) and isinstance(a.get("raw_data"), dict)
                  and a["raw_data"].get("partial") for a in artifacts)
    # tool_budget_timeout: any per-call/batch/turn budget kill or abandoned/unknown outcome.
    tool_budget_timeout = any(
        isinstance(a, dict) and (a.get("timed_out") or a.get("abandoned")
                                 or a.get("outcome_unknown")) for a in artifacts)
    # --- security (v2: structured; denied != executed) ------------------------
    # Read from the write audit, NOT from tool_artifacts. Artifacts cannot carry this
    # signal: legacy produces none at all, so deriving counts from them handed the
    # control pool a fabricated 0 on every turn — a security audit that reads clean
    # because nothing was ever looked at. The audit records the decision at the
    # policy branch on BOTH arches, and reports null when its instrumentation is
    # absent rather than inferring 0 from an empty list.
    _obs = turn_observations.snapshot()
    _audit = turn_observations.write_audit_snapshot(AGENT_ARCH)
    _dsml = turn_observations.dsml_snapshot()
    is_fc = uses_fc_runtime(AGENT_ARCH)
    observed_llm_calls = _obs.get("llm_calls")
    llm_calls = (
        observed_llm_calls
        if isinstance(observed_llm_calls, int)
        and not isinstance(observed_llm_calls, bool)
        else None
    )
    loop_turn = final_state.get("loop_turn")
    if (
        is_fc
        and isinstance(loop_turn, int)
        and not isinstance(loop_turn, bool)
        and loop_turn >= 0
        and (llm_calls is None or loop_turn > llm_calls)
    ):
        llm_calls = loop_turn
        # The graph observed a provider step whose terminal usage callback did not
        # arrive. Never label the remaining usage complete or zero-call.
        _obs["llm_usage_status"] = "partial"
    artifact_turns = {
        a.get("turn")
        for a in artifacts
        if isinstance(a, dict) and a.get("turn") is not None
    }
    wave_batches = 1 if final_state.get("task_results") else 0
    tool_batches = len(artifact_turns) + wave_batches
    signals = {
        "soft_wrapped": bool(final_state.get("soft_wrapped")),
        # HOW a wrapped turn closed ("llm"/"llm_retry" vs a fallback_* canned renderer).
        # None on a turn that never wrapped; the KEY's absence means the producing build
        # predates this field, which the aggregator reports as "not instrumented" rather
        # than as a benign zero.
        "wrapped_by": final_state.get("wrapped_by"),
        "partial": bool(partial),
        "tool_budget_timeout": bool(tool_budget_timeout),
        "security": {
            "denied_write_count": _audit["denied_write_count"],
            # A write that crossed the gate while tainted and unauthorized. A tainted
            # write the user DID authorize (A+ rule 2) is legitimate and excluded —
            # "记住我的预算 £1400" must not read as a zero-tolerance violation.
            "tainted_write_executed_count": _audit["tainted_write_executed_count"],
            # A denied write that reached dispatch anyway: an invariant breach.
            "forbidden_write_executed_count": _audit["forbidden_write_executed_count"],
            # The structured decisions behind the counters, so a HOLD can be
            # diagnosed without re-running the turn.
            "write_audit": _audit["write_audit"],
        },
        # From the accumulator, NOT final_state: the guards that produce these run at
        # the response boundary and in the wrap-up fallback, both outside anything a
        # graph channel can carry — and one of them fires on turns where final_state
        # no longer exists.
        "dsml_blocked": _dsml["dsml_blocked"],
        "dsml_leak": _dsml["dsml_leak"],
        # NOT read from final_state: nothing writes that key, and a graph channel
        # cannot carry a signal off a turn that never returned one. The accumulator
        # is arch-agnostic (the observer sits at ModelRouter.create, which both
        # arches build every client through) and reports null — never 0 — when no
        # observer was installed.
        "provider_schema_400_count": _obs["provider_schema_400_count"],
        # Same accumulator, same reason: usage is captured per LLM call as it
        # completes, so it is arch-agnostic and does not depend on any graph channel.
        "llm_usage": aggregate_llm_usage(_obs["llm_usage_calls"]),
        "llm_usage_status": _obs["llm_usage_status"],
        "llm_calls": llm_calls,
        "tool_batches": tool_batches,
    }
    # manager_v1 installs a root context at the graph invocation boundary.  Keep
    # legacy/fc_loop records byte-for-byte compatible by adding these labels only
    # when that root context was explicitly observed.
    root_agent_context = _obs.get("root_agent_context")
    if isinstance(root_agent_context, dict):
        signals["root_agent_context"] = dict(root_agent_context)
        for field in ("agent_role", "task_id", "parent_task_id"):
            value = root_agent_context.get(field)
            if value is not None:
                signals[field] = value
    multi_agent = _obs.get("multi_agent")
    if AGENT_ARCH == MANAGER_V1_ARCH and isinstance(multi_agent, dict):
        # Optional, non-gating diagnostics. The telemetry layer already strips
        # objectives, arguments, data, errors and any other user-derived content.
        signals["multi_agent"] = dict(multi_agent)
    return signals




def _emit_canary_turn(*, endpoint: str, conversation_id: str, user_id: str, request_id: str,
                      http_status: int, turn_outcome: str,
                      turn_latency_ms: float, fc_signals: dict | None) -> None:
    """Emit exactly ONE structured JSON line per completed turn (event: canary.turn).

    Schema v2: the record is assembled by app.core.canary_telemetry so the exact
    shape the gate consumes is testable without importing this module (see
    tests/test_canary_contract.py). Best-effort: telemetry must never break a turn.

    A signal we could not observe is passed through as None, NOT as 0/False. The
    report treats a null required field as INSTRUMENTATION-HOLD, so partial
    instrumentation blocks promotion instead of silently reading as clean.
    """
    try:
        def _rollout_identity() -> dict:
            if not has_request_context():
                return {
                    "rollout_id": None,
                    "rollout_stage": None,
                    "configured_candidate_percent": None,
                    "traffic_source": "direct",
                    "assigned_pool": "direct",
                }
            source = (request.headers.get("X-RentCompass-Traffic-Source") or "direct").strip()
            rollout_id = request.headers.get("X-RentCompass-Rollout-ID")
            stage = request.headers.get("X-RentCompass-Rollout-Stage")
            pool = request.headers.get("X-RentCompass-Assigned-Pool")
            weight_raw = request.headers.get("X-RentCompass-Rollout-Weight")
            try:
                weight = int(weight_raw) if weight_raw is not None else None
            except (TypeError, ValueError):
                weight = None
            safe_id = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
            safe_stage = re.compile(r"^[A-Za-z0-9._:-]{1,32}$")
            return {
                "rollout_id": rollout_id if isinstance(rollout_id, str) and safe_id.fullmatch(rollout_id) else None,
                "rollout_stage": stage if isinstance(stage, str) and safe_stage.fullmatch(stage) else None,
                "configured_candidate_percent": weight if weight in {0, 5, 20, 50, 100} else None,
                "traffic_source": source if source in {"edge", "direct"} else "invalid",
                "assigned_pool": pool if pool in {"legacy", "candidate"} else "direct",
            }

        signals = (unknown_turn_signals(_crashed_turn_observations())
                   if fc_signals is None else dict(fc_signals))
        # Re-read the tool-markup counters HERE rather than trusting the snapshot
        # inside fc_signals. _build_fc_signals runs when the graph returns, which is
        # before layer 1 sanitizes the response and well before the boundary scan —
        # so a count taken there misses every block the guards actually made and the
        # record reports 0 for a turn where a control fired.
        _dsml_now = turn_observations.dsml_snapshot()
        if _dsml_now["dsml_blocked"] is not None:
            signals.update(_dsml_now)
        record = build_canary_turn_record(
            endpoint=endpoint,
            agent_arch=AGENT_ARCH,
            candidate_sha=APP_CANDIDATE_SHA,
            strict=DEEPSEEK_STRICT,
            request_id=request_id,
            conversation_id=conversation_id,
            user_id=user_id,
            http_status=http_status,
            turn_outcome=turn_outcome,
            turn_latency_ms=turn_latency_ms,
            signals=signals,
            manager_v1_specialists=(
                MANAGER_V1_SPECIALISTS if AGENT_ARCH == MANAGER_V1_ARCH else None
            ),
            rollout=_rollout_identity(),
        )
        _canary_logger.info(json.dumps(record, ensure_ascii=False, default=str))
        # One record per request. Mark it so a later failure at the response
        # boundary does not emit a SECOND record for the same turn.
        try:
            g.canary_emitted = True
        except Exception:
            pass  # emitted outside a request context (unit tests / sink wiring)
    except Exception as e:  # pragma: no cover — telemetry must never break a turn
        print(f"[canary] turn record emit failed; error_type={type(e).__name__}")


def _get_session(user_id, conversation_id):
    """Return the hot-cache slice for (user_id, conversation_id), rehydrating history
    from the durable sqlite store on a cache miss (fresh slice / after a restart)."""
    sess = _session_store.get(user_id, conversation_id)
    if not sess.rehydrated:
        # Durable snapshot rehydrate (Section 4.3): the latest completed turn's snapshot
        # is the authoritative source of user_preferences / accumulated_search_criteria /
        # last_results / rolling_summary — this is what makes criteria survive a restart
        # (the old message-only rehydrate lost them). Falls back cleanly on any failure.
        try:
            snap = conversation_store.latest_snapshot(user_id, conversation_id)
            if snap:
                patch = snapshot_to_session_patch(snap)  # SnapshotSchemaError on old ver
                ps = sess.persistent_state
                if patch.get("user_preferences"):
                    ps["user_preferences"] = patch["user_preferences"]
                if patch.get("accumulated_search_criteria"):
                    ps["accumulated_search_criteria"] = patch["accumulated_search_criteria"]
                ec = ps.setdefault("extracted_context", {})
                if patch.get("rolling_summary"):
                    ec["rolling_summary"] = patch["rolling_summary"]
                if patch.get("rolling_summary_through_turn_id"):
                    ec["rolling_summary_through_turn_id"] = patch["rolling_summary_through_turn_id"]
                if patch.get("last_results") and not sess.last_results:
                    sess.last_results = patch["last_results"]
                    previous, structured = _build_results_context(patch["last_results"])
                    ec["previous_search_results"] = previous
                    ec["last_results"] = structured
                # 累计推荐注册表随快照存活重启/fork（轻量条目，体积可控）。
                if patch.get("recommended_registry") and not ec.get("recommended_registry"):
                    ec["recommended_registry"] = patch["recommended_registry"]
        except SnapshotSchemaError:
            pass  # unknown schema → fall through to the legacy message-only rehydrate
        except Exception as e:
            logger.warning(
                "session.rehydrate_snapshot_skipped",
                extra={"error_type": type(e).__name__},
            )
        try:
            if not sess.history:
                sess.history = conversation_store.rehydrate_history(
                    user_id, conversation_id, MAX_HISTORY_LENGTH)
            # Rehydrate the last structured search as well as text history. A browser
            # refresh must not make property follow-ups lose their target merely because
            # the in-memory cache was evicted.
            if not sess.last_results:
                for message in reversed(conversation_store.get_messages(user_id, conversation_id)):
                    recommendations = message.get('recommendations')
                    if message.get('role') == 'assistant' and isinstance(recommendations, list) and recommendations:
                        sess.last_results = recommendations
                        previous, structured = _build_results_context(recommendations)
                        sess.persistent_state.setdefault('extracted_context', {})
                        sess.persistent_state['extracted_context']['previous_search_results'] = previous
                        sess.persistent_state['extracted_context']['last_results'] = structured
                        break
        except Exception as e:
            logger.warning(
                "session.rehydrate_failed",
                extra={"error_type": type(e).__name__},
            )
        sess.rehydrated = True
    return sess


# ============================================================================
# API error contract — every /api/* failure returns JSON, never an HTML page.
# ============================================================================

class ApiError(Exception):
    """Raised anywhere in a request to short-circuit to a JSON error response."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _is_api_path() -> bool:
    try:
        return request.path.startswith('/api/')
    except Exception:
        return False


@app.errorhandler(ApiError)
def _handle_api_error(e: ApiError):
    return jsonify({"error": e.message}), e.status


@app.errorhandler(HTTPException)
def _handle_http_exception(e: HTTPException):
    # Malformed JSON (400), wrong Content-Type (415), 404/405 etc. → JSON under /api/*.
    if _is_api_path():
        message = {
            400: "bad request", 404: "not found", 405: "method not allowed",
            415: "unsupported media type", 500: "internal server error",
        }.get(e.code, (e.description or e.name or "error"))
        return jsonify({"error": message}), (e.code or 500)
    return e


@app.errorhandler(Exception)
def _handle_uncaught(e: Exception):
    if isinstance(e, ApiError):
        return _handle_api_error(e)
    if isinstance(e, HTTPException):
        return _handle_http_exception(e)
    print(f"[app] uncaught error_type={type(e).__name__}")
    if _is_api_path():
        # http_status is finalised HERE, at the response boundary. Without this the
        # 5xx would be structurally unobservable: the in-endpoint emit sits INSIDE
        # the try that just blew up, so a turn that dies before (or instead of)
        # reaching it would vanish from telemetry entirely — silently shrinking the
        # denominator instead of recording a server error.
        _emit_canary_boundary_5xx()
        # Generic message only — never leak a traceback to the client.
        return jsonify({"error": "internal server error"}), 500
    raise e


# Paths whose failures belong to the canary gate. A 5xx anywhere else is a normal
# app error, not a canary turn, and must not be injected into the A/B population.
_CANARY_PATHS = {"/api/alex": ENDPOINT_ALEX, "/api/search_direct": ENDPOINT_SEARCH_DIRECT}


def _emit_canary_boundary_5xx() -> None:
    """Emit the canary record for a request that died before/instead of its normal
    emit. Correlation ids come from `g` (stamped at the top of each canary endpoint)
    so the record still joins to the request when the failure happened after the
    endpoint started; when it happened earlier they are explicit sentinels rather
    than nulls, which keeps the record contract-conformant and therefore COUNTED as
    a 5xx instead of holding the whole gate as malformed."""
    try:
        if getattr(g, "canary_emitted", False):
            # The turn already emitted its record; anything that fails afterwards
            # (an after_request hook, the WSGI write) must not duplicate it — the
            # gate counts turns, so a double record inflates the denominator and
            # halves every rate. Note the endpoints emit only AFTER jsonify()
            # succeeds, so the common "serialization blew up" case arrives here
            # with the flag still False and is correctly recorded as a 500.
            return
        endpoint = _CANARY_PATHS.get(getattr(request, "path", "") or "")
        if endpoint is None:
            return  # not a canary endpoint — not our population
        started = getattr(g, "canary_turn_started", None)
        latency = ((time.perf_counter() - started) * 1000.0) if started else 0.0
        _emit_canary_turn(
            endpoint=endpoint,
            conversation_id=getattr(g, "canary_conversation_id", None) or "unknown",
            user_id=getattr(g, "canary_user_id", None),
            request_id=getattr(g, "canary_request_id", None) or "unknown",
            http_status=500,
            turn_outcome=OUTCOME_SERVER_ERROR,
            turn_latency_ms=latency,
            # None (not unknown_turn_signals()) so this goes through the SAME overlay
            # as the crash path in _emit_canary_turn — a boundary 5xx must report the
            # provider errors the accumulator saw, not a blanket null.
            fc_signals=None)
    except Exception as e:  # pragma: no cover — never break the error response
        print(f"[canary] boundary 5xx record emit failed; error_type={type(e).__name__}")


def _request_body():
    """Best-effort JSON body for identity on GET/DELETE (never raises)."""
    try:
        if request.mimetype == 'application/json' and request.data:
            data = request.get_json(silent=True)
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def get_json_or_400() -> dict:
    """Parse a REQUIRED JSON object body, mapping Flask's HTML errors to JSON per contract."""
    try:
        data = request.get_json(silent=False)
    except UnsupportedMediaType:
        raise ApiError(415, "Content-Type must be application/json")
    except BadRequest:
        raise ApiError(400, "malformed JSON body")
    except Exception:
        raise ApiError(400, "malformed JSON body")
    if data is None:
        raise ApiError(400, "request body must be JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "request body must be a JSON object")
    return data


def _validate_conversation_id(data: dict):
    """Validate the OPTIONAL conversation_id on a request body and return it (or None).

    A list/dict/number conversation_id is truthy, so it slips past the `if conversation_id`
    guard and reaches sqlite as a bind parameter, raising a 500 BEFORE the agent try/except
    wrapper. Reject any present-but-not-a-non-empty-string value here as a 400 — this is
    input validation performed BEFORE agent invocation, which the always-200-for-agent-errors
    contract explicitly permits (that contract only covers agent/tool-side failures).

    None / omitted → returns None (implicitly create a new conversation). A non-empty string
    that names no existing conversation is still valid and returns as-is (→ 200 downstream).
    """
    cid = data.get('conversation_id')
    if cid is not None and (not isinstance(cid, str) or not cid.strip()):
        raise ApiError(400, "conversation_id must be a string")
    return cid


def _authed_user_id():
    """Return the authenticated user_id if the session is logged in, else None.

    A logged-in session is authoritative: its identity was proven by a password and cannot
    be overridden by a (spoofable) client-supplied user_id. Returns None for guests, which
    preserves the original header/query/cookie/mint resolution untouched.
    """
    try:
        if session.get('authenticated'):
            uid = session.get('auth_user_id')
            if valid_user_id(uid or ''):
                return uid
    except Exception:
        pass
    return None


def resolve_identity(data=None):
    """Resolve (user_id, session_id) with the uniform contract priority:
    authenticated session > body user_id > X-User-Id header > ?user_id= query >
    Flask session cookie > mint.

    A client-supplied id (body/header/query) violating the regex → ApiError 400.
    session_id mirrors user_id (kept for signature compatibility); the conversation axis
    is threaded separately as conversation_id.
    """
    authed = _authed_user_id()
    if authed is not None:
        return authed, authed
    allow_legacy_id = _runtime_config.allow_legacy_client_user_id
    body_uid = data.get('user_id') if allow_legacy_id and isinstance(data, dict) else None
    try:
        header_uid = request.headers.get('X-User-Id') if allow_legacy_id else None
    except Exception:
        header_uid = None
    try:
        query_uid = request.args.get('user_id') if allow_legacy_id else None
    except Exception:
        query_uid = None
    try:
        cookie_uid = session.get('user_id')
    except Exception:
        cookie_uid = None

    try:
        uid, minted = resolve_user_id(
            body_uid=body_uid, header_uid=header_uid, query_uid=query_uid,
            cookie_uid=cookie_uid, mint=lambda: uuid.uuid4().hex,
        )
    except InvalidUserId:
        raise ApiError(400, "invalid user_id")
    if minted:
        try:
            session['user_id'] = uid
        except Exception:
            pass
    return uid, uid


def _identity_from_request(data=None):
    """Resolve identity for handlers that may not have parsed a body (GET/DELETE)."""
    if data is None:
        data = _request_body()
    return resolve_identity(data)


def _delete_checkpoint_thread(user_id: str, conversation_id: str) -> dict:
    """Drop and verify one LangGraph checkpoint thread.

    Callers that are not privacy boundaries may ignore the structured result. The
    erasure route treats any failed/residual result as partial and never claims success.
    """
    try:
        if not (_runtime_config.enable_checkpointer and _runtime_config.checkpoint_path):
            return {"status": "disabled", "residual": False}
        cp = get_sqlite_checkpointer(_runtime_config.checkpoint_path)
        if cp is None:
            return {"status": "failed", "residual": None, "error_type": "Unavailable"}
        thread = f"{user_id}:{conversation_id}"
        cp.delete_thread(thread)
        residual = cp.get_tuple(graph_config(user_id, conversation_id)) is not None
        if residual:
            return {"status": "failed", "residual": True, "error_type": "ResidualData"}
        return {"status": "deleted", "residual": False}
    except Exception as e:
        logger.error(
            "checkpoint.delete_failed",
            extra={"error_type": type(e).__name__},
        )
        return {"status": "failed", "residual": None, "error_type": type(e).__name__}

# --- Tool System Setup (从 fengyuan-agent 迁移) ---
print("[STARTUP] Initializing Tool System...")
try:
    tool_registry = create_tool_registry()
    print(f"✓ [STARTUP] Tool System initialized with {len(tool_registry.tools)} tools")
    
    # 🆕 设置 tool_registry 到 web_search，让它可以调用其他工具
    from core.tools.web_search import set_tool_registry
    set_tool_registry(tool_registry)
    
except Exception as e:
    print(f"⚠️  [STARTUP] Tool System initialization failed; error_type={type(e).__name__}")
    tool_registry = None

# --- MCP tool client (optional) ---
# The agent executes tools via the MCP server (stdio); on any failure it falls back
# to the in-process registry. Disable entirely with env USE_MCP_TOOLS=0.
import os as _os
agent_tool_provider = tool_registry
if _os.environ.get("USE_MCP_TOOLS", "0").lower() not in ("0", "false", "no"):
    try:
        import sys as _sys
        from core.mcp_client import MCPToolClient
        _mcp_client = MCPToolClient(
            command=_sys.executable,
            args=["mcp_server.py"],
            cwd=_os.path.dirname(_os.path.abspath(__file__)),
            fallback_registry=tool_registry,
        ).start()
        import atexit
        atexit.register(_mcp_client.close)
        if _mcp_client.connected:
            agent_tool_provider = _mcp_client
            print(f"✓ [STARTUP] Agent tools served via MCP ({len(_mcp_client.list_tool_names())} tools)")
        else:
            print("⚠️  [STARTUP] MCP not connected; using in-process tool registry")
    except Exception as _e:
        print(f"⚠️  [STARTUP] MCP init failed; error_type={type(_e).__name__}; using in-process tool registry")

# --- Optional RAG setup ---
# Embedding-model startup can perform slow network/cache discovery. Keep the
# deterministic search path immediately available and require an explicit opt-in
# for eager RAG construction; the search tool can still initialise it lazily.
rag_coordinator = None
if _bool_env("RAG_EAGER_INIT", False):
    print("[STARTUP] Initializing optional RAG Coordinator...")
    try:
        # Import lazily: optional embedding dependencies must not prevent the
        # deterministic listing search from serving real results.
        from rag.rag_coordinator import RAGCoordinator
        rag_coordinator = RAGCoordinator()
        print("✓ [STARTUP] RAGCoordinator initialized successfully")
    except Exception as e:
        print(f"❌ RAG initialization failed; error_type={type(e).__name__}")
        # RAG is optional. Search falls back to deterministic ranking.
        rag_coordinator = None
else:
    print("[STARTUP] Optional RAG eager initialization disabled")

print("[STARTUP] Loading properties (PROPERTY_SOURCE=%s)..." % _os.getenv("PROPERTY_SOURCE", "auto"))
all_properties = load_properties()
print(f"✓ [STARTUP] Loaded {len(all_properties)} properties")

# ✅ FIXED: 确保在建立索引前处理所有属性，添加 parsed_price
if all_properties and rag_coordinator is not None:
    from core.data_loader import parse_price
    for prop in all_properties:
        if 'parsed_price' not in prop:
            prop['parsed_price'] = parse_price(prop.get('Price'))

if all_properties and rag_coordinator is not None:
    print("[STARTUP] Building FAISS index for property embeddings... (This may take a moment)")
    try:
        rag_coordinator.property_store.build_index(all_properties)
        from core.tools.search_properties import set_rag_coordinator
        set_rag_coordinator(rag_coordinator)
        print("✓ [STARTUP] FAISS index built successfully. Starting server...")
    except Exception as e:
        print(f"❌ ERROR building FAISS index; error_type={type(e).__name__}")
        rag_coordinator = None
elif not all_properties:
    print("⚠️  WARNING: No properties loaded from CSV. RAG search may not work properly.")
# ------------------------------------

@app.route('/')
def index():
    """Serves the main HTML page."""
    return render_template('unified-ui.html')


# ============================================================================
# Authentication routes (local username/password)
# ----------------------------------------------------------------------------
# Login/register prove an identity with a password and pin it into the signed session
# cookie; resolve_identity then treats that identity as authoritative. Guests (no session)
# keep the original self-declared-id behaviour, so this layer is purely additive.
# ============================================================================

def _current_auth() -> dict:
    """Public auth view for the current session, or {"authenticated": False} for a guest."""
    if session.get('authenticated') and valid_user_id(session.get('auth_user_id') or ''):
        return {
            "authenticated": True,
            "user_id": session.get('auth_user_id'),
            "username": session.get('username'),
            "display_name": session.get('display_name') or session.get('username'),
        }
    return {"authenticated": False}


def _establish_session(view: dict) -> None:
    """Persist a verified account into the signed session cookie."""
    session['authenticated'] = True
    session['auth_user_id'] = view['user_id']
    session['username'] = view['username']
    session['display_name'] = view['display_name']
    session.permanent = True


@app.before_request
def _enforce_auth():
    """When REQUIRE_AUTH is on, gate every /api/* route (except /api/auth/*) behind login."""
    if not _runtime_config.require_auth:
        return None
    if request.method == 'OPTIONS':
        return None  # never block CORS preflight
    path = request.path or ''
    if not path.startswith('/api/') or path.startswith('/api/auth/'):
        return None
    if session.get('authenticated'):
        return None
    return jsonify({"error": "authentication required"}), 401


def _rate_limit_subject() -> str:
    user_id = _authed_user_id()
    if user_id:
        return f"user:{user_id}"
    remote = request.remote_addr or "unknown"
    if remote in {"127.0.0.1", "::1"}:
        # Direct WSGI/test deployments may not run Uvicorn's proxy middleware.
        # Production nginx overwrites XFF with exactly one address; accepting
        # the rightmost value here preserves that one-hop contract.
        xff = request.headers.get("X-Forwarded-For", "")
        forwarded = xff.rsplit(",", 1)[-1].strip() if xff else ""
        if forwarded:
            remote = forwarded
    return f"ip:{remote}"


@app.before_request
def _limit_expensive_api_requests():
    if request.method == 'OPTIONS' or not request.path.startswith('/api/'):
        return None
    limits = {
        '/api/alex': 12,
        '/api/search_direct': 20,
        '/api/generate_map': 6,
        '/api/auth/login': 10,
        '/api/auth/register': 5,
    }
    limit = limits.get(request.path, 120)
    allowed, retry_after = _api_rate_limiter.allow(
        f"{request.path}:{_rate_limit_subject()}",
        limit=limit,
        window_seconds=_runtime_config.rate_limit_window_seconds,
    )
    if allowed:
        return None
    response = jsonify({"error": "too many requests; please try again shortly"})
    response.status_code = 429
    response.headers['Retry-After'] = str(retry_after)
    return response


@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    """Create an account and log it in. Body {username, password, display_name?}."""
    data = get_json_or_400()
    try:
        view = auth_store.register(
            data.get('username'), data.get('password'), data.get('display_name'))
    except UsernameTaken:
        raise ApiError(409, "username already taken")
    except (InvalidUsername, WeakPassword) as e:
        raise ApiError(400, str(e))
    except AuthError as e:
        raise ApiError(400, str(e))
    _establish_session(view)
    logger.info("auth.registered")
    return jsonify({"authenticated": True, **view})


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """Verify credentials and start a session. Body {username, password}."""
    data = get_json_or_400()
    view = auth_store.verify(data.get('username'), data.get('password'))
    if not view:
        raise ApiError(401, "invalid username or password")
    _establish_session(view)
    logger.info("auth.login")
    return jsonify({"authenticated": True, **view})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    """Clear the authenticated identity (and the guest fallback id) from the session."""
    for k in ('authenticated', 'auth_user_id', 'username', 'display_name', 'user_id'):
        session.pop(k, None)
    return jsonify({"authenticated": False})


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    """Report the current session's auth state (used by the frontend on load)."""
    return jsonify(_current_auth())

# ============================================================================
# 统一的 Alex API 端点 - LangGraph StateGraph 架构
#
# 核心原则：
# 1. 没有关键词匹配 - 完全由 LLM 决定使用哪个工具
# 2. 所有请求都通过 LangGraph StateGraph Agent
# 3. search_properties 工具内部整合了 Fine-tuned Model
# ============================================================================

def _request_replay_response(user_id: str, request_id: str, turn: dict):
    """Return a durable response for a duplicate request without running the agent again."""
    if turn.get("status") == "running":
        response = jsonify({
            "error": "request is still in progress",
            "conversation_id": turn.get("conversation_id"),
            "turn_id": turn.get("id"),
            "request_id": request_id,
            "replayed": True,
        })
        response.status_code = 409
        response.headers["Retry-After"] = "1"
    else:
        payload = conversation_store.get_turn_response(user_id, turn.get("id"))
        if payload is None:
            raise ApiError(500, "persisted request has no response")
        response = jsonify(payload)
        response.status_code = 502 if turn.get("status") == "failed" else 200
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Idempotent-Replay"] = "1"
    return response


@app.route('/api/alex', methods=['POST'])
async def api_alex():
    """
    统一的 Alex 端点 - 纯 ReAct Agent 架构
    
    所有用户请求都交给 ReAct Agent 处理，由 LLM 自主决定：
    - 是否需要搜索房源（调用 search_properties 工具）
    - 是否需要检查安全（调用 check_safety 工具）
    - 是否需要计算通勤（调用 calculate_commute 工具）
    - 是否需要查询天气（调用 get_weather 工具）
    - 是否需要搜索附近设施（调用 search_nearby_pois 工具）
    - 或者直接回答用户问题
    """
    # --- parse + validate (these raise ApiError → JSON 4xx, NOT 500) -----------
    data = get_json_or_400()
    _validate_conversation_id(data)  # reject list/dict/non-string cid before it hits sqlite
    user_id, _session_id = resolve_identity(data)
    try:
        user_message = normalize_message(data.get('message'))
    except InvalidMessage as e:
        raise ApiError(400, str(e))

    context = data.get('context', {}) or {}
    is_continuation = data.get('is_continuation', False)
    # 前端 UI 语言（并行 agent 发送 ui_language）；缺失/非法按 'en'。用于回复语言决策。
    ui_language = _normalize_ui_language(data.get('ui_language'))

    request_id = new_request_id(request.headers.get("X-Request-Id"))
    # Idempotency is user-global, not merely conversation-local. This matters when
    # the first request implicitly creates a conversation and the client retries
    # before it has received that conversation id.
    existing_turn = conversation_store.get_request_turn(user_id, request_id)
    if existing_turn is not None:
        return _request_replay_response(user_id, request_id, existing_turn)

    # --- resolve / implicitly create the conversation --------------------------
    # New conversations are stamped with THIS process's architecture provenance at creation;
    # existing ones are reconciled after a pool switch (normally a no-op).
    conversation_id = data.get('conversation_id')
    conv = conversation_store.get_conversation(user_id, conversation_id) if conversation_id else None
    if conv:
        conversation_id = conv["id"]
        _reconcile_agent_arch(user_id, conversation_id, conv)
    else:
        conversation_id = None

    # Correlation for a boundary 5xx (see _emit_canary_boundary_5xx): stamped as
    # early as possible so even a failure before the turn anchor is joinable.
    g.canary_request_id = request_id

    # One transaction creates an implicit conversation (if needed), stores the user
    # message, claims the request id and acquires the cross-process conversation lease.
    try:
        turn = conversation_store.start_request_turn(
            user_id,
            conversation_id,
            request_id,
            user_message,
            lease_seconds=_runtime_config.turn_lease_seconds,
            create_title=_derive_title(user_message),
            agent_arch=AGENT_ARCH,
            agent_version=APP_CANDIDATE_SHA,
            strict=DEEPSEEK_STRICT,
        )
    except ConversationBusy as exc:
        response = jsonify({
            "error": "another turn is already running for this conversation",
            "running_turn_id": exc.turn_id,
            "conversation_id": conversation_id,
        })
        response.status_code = 409
        response.headers["Retry-After"] = str(exc.retry_after)
        response.headers["X-Request-Id"] = request_id
        return response
    except PrivacyErasureInProgress:
        response = jsonify({"error": "privacy erasure is in progress"})
        response.status_code = 423
        response.headers["Retry-After"] = "1"
        response.headers["X-Request-Id"] = request_id
        return response

    if turn.get("replayed"):
        return _request_replay_response(user_id, request_id, turn)

    conversation_id = turn["conversation_id"]
    turn_id = turn["id"]
    uid_hash, _ = hash_user_id(user_id)
    logger.info(
        "agent.turn.start",
        extra={
            "request_id": request_id,
            "conversation_id": conversation_id,
            "user_id_hash": uid_hash,
            "agent_arch": AGENT_ARCH,
            "is_continuation": bool(is_continuation),
            "message_chars": len(user_message),
            "context_keys": sorted(str(key) for key in context.keys()) if isinstance(context, dict) else [],
        },
    )

    _turn_crashed = False
    # Canary: clear any inherited fc-signal carrier, then time the whole turn. handle_with_
    # react_agent fills _turn_fc_signals from the graph's final_state; a crash leaves it None
    # (→ safe defaults on the record).
    _turn_fc_signals.set(None)
    # Open the observation window BEFORE the agent runs. Unlike _turn_fc_signals (which is
    # filled from final_state and so exists only on a turn that RETURNED), this accumulates
    # as calls happen, so a turn that crashes — or dies at the response boundary — still
    # reports what its provider actually did. That is the whole point: a strict-schema 400
    # is a plausible CAUSE of a crash, so it must not vanish with the final_state.
    turn_observations.begin_turn()
    _turn_started_ms = time.perf_counter()
    g.canary_turn_started = _turn_started_ms
    g.canary_conversation_id = conversation_id
    g.canary_user_id = user_id
    _outbox_token = _turn_background_jobs.set([])
    _background_jobs = []
    try:
        # 所有请求都通过 ReAct Agent 处理
        with request_context(request_id, user_id):
            payload = await handle_with_react_agent(
                user_message, context, is_continuation, user_id, conversation_id, request_id,
                ui_language=ui_language, turn=turn,
            )
    except Exception as e:
        logger.error(
            "agent.turn.execution_failed",
            extra={
                "request_id": request_id,
                "conversation_id": conversation_id,
                "error_type": type(e).__name__,
                "agent_arch": AGENT_ARCH,
            },
        )
        _turn_crashed = True
        # 错误文案也遵循回复语言策略（本条消息含中文→中文，否则跟随 UI 语言）。
        _err_zh = _resolve_reply_language(user_message, ui_language) == "zh"
        payload = {
            "response_type": "error",
            "message": ("抱歉，处理您的请求时出错了。请稍后再试。" if _err_zh
                        else "Sorry, something went wrong while handling your request. Please try again."),
        }
    finally:
        _background_jobs = list(_turn_background_jobs.get() or [])
        _turn_background_jobs.reset(_outbox_token)

    # conversation_id + turn_id are echoed in EVERY response (incl. errors + implicit creation).
    payload["conversation_id"] = conversation_id
    payload["turn_id"] = turn_id
    _serialization_failed = False
    try:
        payload, _boundary_blocked = _guard_payload_before_persistence(payload)
    except ResponsePayloadSerializationError as e:
        logger.error(
            "response.serialization_failed",
            extra={"request_id": request_id, "error_type": type(e.__cause__).__name__},
        )
        payload = {
            "response_type": "error",
            "message": "internal server error",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
        }
        _boundary_blocked = False
        _serialization_failed = True
    if _boundary_blocked:
        _turn_crashed = True
    if _serialization_failed:
        _turn_crashed = True

    # Build the snapshot before opening the final database transaction. A successful
    # response without a durable snapshot is not a completed turn.
    _terminal_failed = _turn_crashed or payload.get("response_type") == "error"
    _snapshot = None
    if not _terminal_failed:
        try:
            _snapshot = _build_turn_snapshot_after_turn(
                user_id, conversation_id, turn_id
            )
        except Exception as e:
            logger.error(
                "agent.turn.snapshot_failed",
                extra={
                    "request_id": request_id,
                    "conversation_id": conversation_id,
                    "error_type": type(e).__name__,
                },
            )
            _turn_crashed = True
            _terminal_failed = True
            _err_zh = _resolve_reply_language(user_message, ui_language) == "zh"
            payload = {
                "response_type": "error",
                "message": (
                    "抱歉，无法可靠保存本轮结果，请稍后重试。"
                    if _err_zh
                    else "Sorry, this turn could not be saved reliably. Please try again."
                ),
                "conversation_id": conversation_id,
                "turn_id": turn_id,
            }

    # Assistant message, terminal status and snapshot are committed together. A
    # database failure escapes to the API 500 handler; fail_turn releases the lease
    # so the conversation is not permanently wedged.
    try:
        conversation_store.finalize_request_turn(
            user_id,
            turn_id,
            status="failed" if _terminal_failed else "completed",
            assistant_content=payload.get("message", ""),
            response_type=payload.get("response_type"),
            recommendations=payload.get("recommendations"),
            snapshot=_snapshot,
            background_jobs=([] if _terminal_failed else _background_jobs),
        )
    except Exception as e:
        logger.error(
            "agent.turn.persistence_failed",
            extra={
                "request_id": request_id,
                "conversation_id": conversation_id,
                "error_type": type(e).__name__,
            },
        )
        try:
            conversation_store.fail_turn(user_id, turn_id)
        finally:
            _session_store.clear(user_id, conversation_id)
        raise

    if _terminal_failed:
        # Discard any speculative L2 mutation from a failed run; the next request
        # rehydrates from the latest completed durable snapshot.
        _session_store.clear(user_id, conversation_id)
    elif _background_jobs:
        try:
            _ensure_outbox_worker()
        except Exception as e:
            # The jobs are already committed. A later request/process will restart
            # the consumer, so worker startup failure must not falsify this turn.
            logger.error(
                "background_worker.start_failed",
                extra={"error_type": type(e).__name__},
            )

    _http_status = 500 if _serialization_failed else (502 if _terminal_failed else 200)

    # ORDER MATTERS: the response is fully materialized BEFORE the canary record is
    # emitted. jsonify() is the last thing that can still fail (a non-serializable
    # payload raises here), and if it does we must NOT have already logged
    # http_status=200 + set g.canary_emitted — that combination would permanently
    # record a 200 for a request the user received as a 500, and would suppress the
    # boundary record that should have reported it. Emitting after serialization
    # means a failure falls through to _handle_uncaught with canary_emitted still
    # False, so exactly one record is written and it says server_error.
    response = jsonify(payload)
    response.status_code = _http_status
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Agent-Outcome"] = "error" if _terminal_failed else "ok"
    response = _dsml_boundary_check(response, payload)

    # Canary: one structured record per turn (both archs; fc signals default when absent).
    # http_status reflects the actual response, including a controlled agent 502.
    _emit_canary_turn(
        endpoint=ENDPOINT_ALEX,
        conversation_id=conversation_id, user_id=user_id, request_id=request_id,
        http_status=response.status_code,
        turn_outcome=(OUTCOME_SERVER_ERROR if _serialization_failed else
                      OUTCOME_CRASH if _turn_crashed else
                      OUTCOME_AGENT_ERROR if payload.get("response_type") == "error"
                      else OUTCOME_OK),
        turn_latency_ms=(time.perf_counter() - _turn_started_ms) * 1000.0,
        fc_signals=_turn_fc_signals.get())

    return response


def _derive_title(message: str) -> str:
    """Human-friendly conversation title from the first user message (implicit creation).

    Defense-in-depth against stored XSS: this title is auto-generated server-side and
    returned verbatim by GET /api/conversations, so it must not carry executable markup.
    Strip whole HTML tags (<img ...>, </script>) then any stray angle brackets so a
    payload like "<img src=x onerror=alert(1)>hello" survives only as inert plain text.
    Stored message CONTENT is deliberately NOT altered — only this derived label.
    """
    raw = message or ""
    no_tags = re.sub(r"<[^>]*>", "", raw)
    plain = no_tags.replace("<", "").replace(">", "")
    text = " ".join(plain.split())
    if not text:
        return "New chat"
    return text[:40] + ("…" if len(text) > 40 else "")


# ============================================================================
# 回复语言策略（产品规则）
# ----------------------------------------------------------------------------
# reply_language 决策（"仅当 UI=en 且本条消息是英文时才用英文回复"）：
#   1) 当前用户消息含中日韩字符 → 'zh'（无论前端 UI 语言）；
#   2) 否则前端 UI 语言为 'en' → 'en'；
#   3) 否则 → 'zh'。
# 之前 /api/search_direct 与 "search anyway" 路径没有消息可推断语言，工具只按单条消息
# 做 is_cjk，于是中文对话里搜索摘要却是英文。UI 语言由前端 ui_language 传入（缺失/非法
# 一律按 'en' 英文界面处理）。
# ============================================================================

# 中日韩字符区间（与 search tool 的 _has_cjk 保持一致），用于"本条消息是否含中文"。
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")


def _has_cjk(text) -> bool:
    """本条文本是否含中日韩字符（主要判定中文），用于回复语言决策。"""
    return bool(_CJK_RE.search(text or ""))


def _normalize_ui_language(value) -> str:
    """规范化前端 UI 语言：仅接受 'zh'|'en'，其它/缺失一律按 'en'（英文界面默认）。"""
    if isinstance(value, str) and value.strip().lower() in ("zh", "en"):
        return value.strip().lower()
    return "en"


def _resolve_reply_language(user_message, ui_language) -> str:
    """回复语言决策（见上）：本条消息含中文→'zh'；否则 UI=en→'en'；否则→'zh'。"""
    if _has_cjk(user_message):
        return "zh"
    return "en" if _normalize_ui_language(ui_language) == "en" else "zh"


def _listing_url_key(url):
    """规范化房源 URL（小写/去首尾空白/去尾斜杠）—— 房源的唯一身份。"""
    return str(url or '').strip().lower().rstrip('/')


def _listing_price_key(price):
    """价格里的数字（"£850/month" / 850 → "850"）。仅用于区分同名的两套房源；无数字返回 ''。"""
    return re.sub(r'\D', '', '' if price is None else str(price))


def _match_listing_by_address(rows, addr_key, url_key, price_key,
                              fields=('address', 'url', 'price')):
    """在 rows 里按地址找出"确实可能就是该载荷所指的那一套"。返回 ``(命中行|None, 是否歧义)``。

    OnTheMarket 上的房源名（地址）不是身份标识：同一条街名下经常挂着完全不同的两套房
    （实例：details/17896573 与 details/17896574 都叫 "Marriott Road, London"）。所以这里
    绝不返回"地址相同的第一条"：
      ① 载荷带 URL 时，URL 不同（且非空）的行就是另一套房，按地址也不认；
      ② 若仍有多条身份不同的行同名，用载荷价格再收窄；
      ③ 收窄后仍有歧义 → 返回 (None, True)（宁可解析不出来，也不能拿另一套房的数据回答）。

    ``fields`` 给出 (地址键, URL 键, 价格键)，因为会话/注册表用小写键、demo CSV 用首字母大写键。"""
    addr_f, url_f, price_f = fields
    if not addr_key:
        return None, False
    cands = [r for r in (rows or [])
             if isinstance(r, dict)
             and str(r.get(addr_f) or '').lower().strip() == addr_key]
    if url_key:
        cands = [r for r in cands if _listing_url_key(r.get(url_f)) in ('', url_key)]
    if len(cands) > 1 and price_key:
        priced = [r for r in cands if _listing_price_key(r.get(price_f)) == price_key]
        if priced:
            cands = priced
    if not cands:
        return None, False
    if len({_listing_url_key(r.get(url_f)) for r in cands}) > 1:
        return None, True   # 同名但身份不同 —— 不猜
    return cands[0], False


def _resolve_focus_listing(property_info, last_results, csv_properties,
                           registry=None, cache_lookup=None):
    """解析前端每张卡片 "Ask AI" 载荷 {property:{address,price,travel_time,url}} 对应的
    真实房源，返回 (要并入 extracted_context 的字段 dict, 命中来源)。

    解析顺序（Problem 2 修复 —— 删掉旧的子串/模糊匹配，那正是"实时抓取的曼城房源被
    误匹配到伦敦 demo CSV、把错城市的设施/描述串进上下文"的 bug）：
      ① 会话 last_results 里 URL 精确匹配（忽略大小写/首尾空白）；
      ② 会话 last_results 里地址精确匹配（忽略大小写/首尾空白）；
      ②.5 累计推荐注册表（历史所有轮次的推荐）URL/地址精确匹配 → 命中后用注入的 cache_lookup
          （find_cached_listing_by_url 等价物）取完整字段（描述/设施/政策），让"点历史轮次
          推荐房源的 Ask AI"也能解析出真实完整数据；cache 未命中时退回注册表轻量字段；
      ③ demo CSV all_properties 里地址精确匹配（仅 ==，无子串/模糊）；
      ④ 都不中 → 只保留载荷标量（address/price/travel_time），与旧行为一致。

    纯函数：不加锁、不读共享状态。``registry`` 是每会话累计推荐注册表（轻量条目列表），
    ``cache_lookup`` 是注入的 ``callable(url) -> 完整房源 dict | None``（便于测试）。调用方须在
    phase-1 turn_lock 内先把"完整推荐列表"（挂在 session 对象上的 _sess.last_results，非
    extracted_context 里截断的 6 条）浅拷贝传进来，解析对照的就是该快照。会话命中喂真实房源全量
    字段（键名与 agent 文件读取的一致，缺失键被容忍）；CSV 命中沿用旧键（amenities/guest_policy/…）。"""
    if not isinstance(property_info, dict):
        property_info = {}
    property_address = property_info.get('address') or ''
    payload_url = property_info.get('url') or ''
    # ④ 兜底标量（其它档命中后按需覆盖）
    ctx = {
        'property_address': property_address,
        'property_price': property_info.get('price'),
        'property_travel_time': property_info.get('travel_time'),
    }
    addr_key = property_address.lower().strip()
    url_key_norm = _listing_url_key(payload_url)
    price_key = _listing_price_key(property_info.get('price'))
    # 同名不同套时，②/②.5/③ 三档都不猜（_match_listing_by_address 返回 None）；本标记让调用方
    # 拿到 'ambiguous' 而不是 'scalar'，区分"没找到"与"同名的有好几套"。
    ambiguous = False

    # ① URL 精确匹配 → ② 地址匹配（都对照完整 last_results 快照）。URL 是身份，地址不是。
    session_hit = None
    if url_key_norm:
        for rec in (last_results or []):
            if isinstance(rec, dict) and _listing_url_key(rec.get('url')) == url_key_norm:
                session_hit = rec
                break
    if session_hit is None and addr_key:
        session_hit, ambiguous = _match_listing_by_address(
            last_results, addr_key, url_key_norm, price_key)

    if session_hit is not None:
        # 用真实完整记录填充 extracted_context（agent 文件按同名键读取）。
        ctx['property_address'] = session_hit.get('address') or property_address
        if session_hit.get('price') is not None:
            ctx['property_price'] = session_hit.get('price')
        if session_hit.get('travel_time') is not None:
            ctx['property_travel_time'] = session_hit.get('travel_time')
        ctx['property_url'] = session_hit.get('url') or ''
        ctx['description'] = session_hit.get('description') or ''
        ctx['available_from'] = session_hit.get('available_from') or ''
        ctx['availability_status'] = session_hit.get('availability_status') or ''
        ctx['bedrooms'] = session_hit.get('bedrooms')
        ctx['property_type'] = session_hit.get('property_type')
        ctx['area'] = session_hit.get('area')
        ctx['budget_status'] = session_hit.get('budget_status') or ''
        # The listing's own coordinates. Carried so a "what's nearby" question can centre its
        # radius on the property instead of geocoding its display name — which for
        # "Rugby House 6 Great Ormond Street, Islington WC1N" resolved to nothing, and for
        # "Caledonian Road, London" to the middle of a 2 km road.
        if session_hit.get('geo_location'):
            ctx['geo_location'] = session_hit.get('geo_location')
        return ctx, 'session'

    # ②.5 累计推荐注册表命中（历史所有轮次的推荐，不只最近一轮）。URL 优先、地址次之精确匹配；
    #     命中后用注入的 cache_lookup 按 URL 取 sqlite 缓存里的完整房源（描述/设施/政策等大字段），
    #     缓存未命中则退回注册表轻量字段（地址/价格/通勤/区域/可入住日）。
    reg_hit = None
    if registry:
        if url_key_norm:
            for e in registry:
                if isinstance(e, dict) and _listing_url_key(e.get('url')) == url_key_norm:
                    reg_hit = e
                    break
        if reg_hit is None and addr_key:
            reg_hit, _reg_ambiguous = _match_listing_by_address(
                registry, addr_key, url_key_norm, price_key)
            ambiguous = ambiguous or _reg_ambiguous
    if reg_hit is not None:
        ctx['property_address'] = reg_hit.get('address') or property_address
        if reg_hit.get('price') is not None:
            ctx['property_price'] = reg_hit.get('price')
        if reg_hit.get('travel_time') is not None:
            ctx['property_travel_time'] = reg_hit.get('travel_time')
        reg_url = reg_hit.get('url') or payload_url
        ctx['property_url'] = reg_url
        if reg_hit.get('area'):
            ctx['area'] = reg_hit.get('area')
        if reg_hit.get('available_from'):
            ctx['available_from'] = reg_hit.get('available_from')
        full = None
        if cache_lookup is not None and reg_url:
            try:
                full = cache_lookup(reg_url)
            except Exception:
                full = None
        if isinstance(full, dict):
            # 缓存房源是抓取"富 schema"（首字母大写键），与 demo CSV 同形 —— 沿用相同映射。
            if full.get('Address'):
                ctx['property_address'] = full.get('Address')
            if full.get('Price') not in (None, ''):
                ctx['property_price'] = full.get('Price')
            ctx['description'] = full.get('Description') or ctx.get('description') or ''
            ctx['room_type'] = full.get('Room_Type_Category', '')
            ctx['amenities'] = full.get('Detailed_Amenities', '')
            ctx['guest_policy'] = full.get('Guest_Policy', '')
            ctx['payment_rules'] = full.get('Payment_Rules', '')
            ctx['excluded_features'] = full.get('Excluded_Features', '')
            if full.get('URL'):
                ctx['property_url'] = full.get('URL')
            if full.get('Available From'):
                ctx['available_from'] = full.get('Available From')
            if full.get('geo_location'):
                ctx['geo_location'] = full.get('geo_location')
            return ctx, 'registry+cache'
        return ctx, 'registry'

    # ③ demo CSV 精确地址匹配（仅 ==；子串/模糊分支已删除）。同名不同套同样不猜。
    if addr_key:
        csv_hit, _csv_ambiguous = _match_listing_by_address(
            csv_properties, addr_key, url_key_norm, price_key,
            fields=('Address', 'URL', 'Price'))
        ambiguous = ambiguous or _csv_ambiguous
        if csv_hit is not None:
            ctx['room_type'] = csv_hit.get('Room_Type_Category', '')
            ctx['amenities'] = csv_hit.get('Detailed_Amenities', '')
            ctx['guest_policy'] = csv_hit.get('Guest_Policy', '')
            ctx['payment_rules'] = csv_hit.get('Payment_Rules', '')
            ctx['excluded_features'] = csv_hit.get('Excluded_Features', '')
            ctx['description'] = csv_hit.get('Description', '')
            ctx['enhanced_description'] = csv_hit.get('Enhanced_Description', '')
            ctx['property_url'] = csv_hit.get('URL', '')
            return ctx, 'csv'

    # 'ambiguous' ≠ 'scalar'：同名的房源有好几套、身份无法判定，绝不绑定其中任意一套。
    # ctx 形状与 scalar 完全一致（只有载荷标量），差别只在来源标记。
    return ctx, ('ambiguous' if ambiguous else 'scalar')


def _build_viewed_properties_context(properties, last_results, csv_properties, max_items=10):
    if not isinstance(properties, list):
        return ''
    rows = []
    seen = set()
    for item in properties[-max_items:]:
        if not isinstance(item, dict):
            continue
        resolved, _source = _resolve_focus_listing(item, last_results, csv_properties)
        address = str(resolved.get('property_address') or '').strip()
        url = str(resolved.get('property_url') or item.get('url') or '').strip()
        price = str(resolved.get('property_price') or '').strip()
        # 无 url 时地址单独不足以标识一套房（同名≠同一套），带上价格再去重。
        key = (('url', _listing_url_key(url)) if url
               else ('address', address.lower(), _listing_price_key(price)))
        if not address or key in seen:
            continue
        seen.add(key)
        rows.append((address[:500], str(resolved.get('property_price') or '').strip()[:100],
                     str(resolved.get('property_travel_time') or '').strip()[:100], url[:2000]))

    lines = []
    for index, (address, price, travel_time, url) in enumerate(rows, 1):
        lines.append(f'{index}. Address: {address}')
        if price:
            lines.append(f'   Price: {price}')
        if travel_time:
            lines.append(f'   Commute: {travel_time}')
        if url:
            lines.append(f'   Listing URL: {url}')
    return '\n'.join(lines)


# ── 累计推荐注册表（recommended registry）──────────────────────────────────────
# 每次搜索产出推荐时把本轮推荐 merge 进每会话累计注册表，轻量条目仅
# {index(首见顺序，稳定), address, price, area, travel_time, url, available_from}，
# 按 url（无 url 用地址）去重，首见 index 不变，上限 _REGISTRY_MAX_ENTRIES。用户可追问任何
# 历史轮次推荐过的房源；完整信息（描述/设施/政策）不塞进注册表，由 get_property_details 按 URL
# 命中 sqlite 缓存取回。
_REGISTRY_MAX_ENTRIES = 200


def _registry_entry_key(url, address, price=None):
    """去重键：优先规范化后的 url（小写/去首尾空白/去尾斜杠）；都空 → None。

    无 url 时退回 (地址, 价格)：房源名不是身份标识 —— OnTheMarket 上同名的两条挂牌经常是
    两套不同的房子（details/17896573 与 17896574 都叫 "Marriott Road, London"），只按地址
    去重会把它们合成一条，用户就再也点不到其中一套。价格是这里唯一还拿得到的判别位。"""
    u = str(url or '').strip().lower().rstrip('/')
    if u:
        return ('url', u)
    a = str(address or '').strip().lower()
    return ('address', a, _listing_price_key(price)) if a else None


def _merge_recommended_registry(existing, recommendations, max_items=_REGISTRY_MAX_ENTRIES):
    """把本轮 recommendations merge 进累计注册表（纯函数，返回新列表，不改动入参）。

    去重：按 _registry_entry_key（url 优先，无 url 时按 地址+价格）；已存在的条目原样保留（首见 index 稳定）；
    新条目 index = 现有最大 index + 1（单调递增，不复用/不冲突）。达到 max_items 后不再追加新条目。"""
    registry = [dict(e) for e in (existing or []) if isinstance(e, dict)]
    seen = {}
    max_index = 0
    for e in registry:
        key = _registry_entry_key(e.get('url'), e.get('address'), e.get('price'))
        if key is not None:
            seen[key] = e
        try:
            max_index = max(max_index, int(e.get('index', 0)))
        except (TypeError, ValueError):
            pass
    for rec in (recommendations or []):
        if not isinstance(rec, dict):
            continue
        if rec.get('candidate_status') in {'excluded', 'unknown'}:
            # Excluded or unverified listings never enter durable recommendations.
            continue
        key = _registry_entry_key(rec.get('url'), rec.get('address'), rec.get('price'))
        if key is None or key in seen:
            continue
        if len(registry) >= max_items:
            break
        max_index += 1
        entry = {
            'index': max_index,
            'address': rec.get('address') or '',
            'price': rec.get('price'),
            'area': rec.get('area'),
            'travel_time': rec.get('travel_time'),
            'url': rec.get('url') or '',
            'available_from': rec.get('available_from'),
            # A "lat, lon" string — the one big-field exception to summaries-only, because it
            # is what lets a POI/map lookup for a listing shown many turns ago be centred on
            # that listing instead of on a geocode of its display name. Never rendered into
            # the prompt (render_recommended_index ignores it).
            'geo_location': rec.get('geo_location') or '',
        }
        registry.append(entry)
        seen[key] = entry
    return registry


def _build_focus_stack_records(focus_items, last_results, csv_properties,
                               registry=None, cache_lookup=None):
    """把前端 focus_stack（旧→新，最后一个为当前聚焦）逐个解析成结构化房源记录，供指代锚定
    （langgraph 读 extracted_context['focus_stack']）+ 上下文渲染。每条走 _resolve_focus_listing
    （会话快照 → 注册表+缓存 → demo CSV → 标量兜底），返回与 last_results 记录同形的 dict 列表
    （name/address/price/travel_time/url/description/…）。纯函数。"""
    records = []
    for item in (focus_items or []):
        if not isinstance(item, dict):
            continue
        ctx, _src = _resolve_focus_listing(
            item, last_results, csv_properties, registry=registry, cache_lookup=cache_lookup)
        addr = ctx.get('property_address') or ''
        records.append({
            'name': addr.split(',')[0].strip() if addr else '',
            'address': addr,
            'price': ctx.get('property_price'),
            'travel_time': ctx.get('property_travel_time'),
            'url': ctx.get('property_url') or item.get('url') or '',
            'description': ctx.get('description'),
            'available_from': ctx.get('available_from'),
            'availability_status': ctx.get('availability_status'),
            'bedrooms': ctx.get('bedrooms'),
            'property_type': ctx.get('property_type'),
            'area': ctx.get('area'),
            'budget_status': ctx.get('budget_status'),
            # Coordinates ride along so a POI question about the focused listing is centred
            # on the listing, not on a geocode of its display name.
            'geo_location': ctx.get('geo_location'),
        })
    return records


# Sentinel for "argument not supplied" so a helper can distinguish "keep the current
# cached value" (arg omitted) from "explicitly set it to this value" (incl. None).
_UNSET = object()


def _build_results_context(recommendations):
    """Build the (prev_results_context, structured_results) pair that lets follow-up
    turns resolve ordinal / name references ("the second one", "Maple Court").

    Pure — touches NO shared state. Returns (None, None) when there are no recs.

    D3: built ONLY from the real, city-correct tool recommendations. The old inlined
    code enriched each row from the bundled London demo CSV, which leaked wrong-city
    amenities/URLs into follow-up detail answers. Each structured record keeps the FULL
    listing fields so an ordinal/name follow-up resolves to the ACTUAL listing and never
    falls back to demo data.
    """
    if not recommendations:
        return None, None
    prev_results_context = "\n"
    structured_results = []  # 结构化，供 _resolve_last_result / _resolve_target_address 解析
    for i, rec in enumerate(recommendations[:6], 1):
        addr = rec.get('address', 'Unknown')
        price = rec.get('price', 'N/A')
        travel = rec.get('travel_time', 'N/A')
        property_name = addr.split(',')[0].strip()

        structured_results.append({
            'name': property_name,
            'address': addr,
            'price': price,
            'travel_time': travel,
            'bedrooms': rec.get('bedrooms'),
            'property_type': rec.get('property_type'),
            'budget_status': rec.get('budget_status'),
            'source': rec.get('source'),
            'url': rec.get('url'),
            'explanation': rec.get('explanation'),
            'geo_location': rec.get('geo_location'),
            # 🆕 多区域来源 + OnTheMarket 完整描述（结构化保存完整文本，供后续问答解析）。
            'area': rec.get('area'),
            'description': rec.get('description'),
            # 🆕 可入住日期 + 与期望入住日的匹配标注（供后续"这套几月能住"等问题解析）。
            'available_from': rec.get('available_from'),
            'availability_status': rec.get('availability_status'),
        })

        prev_results_context += f"{i}. **{property_name}**\n"
        prev_results_context += f"   - Full Address: {addr}\n"
        prev_results_context += f"   - Price: {price}\n"
        prev_results_context += f"   - Commute: {travel}\n"
        if rec.get('bedrooms') not in (None, '', 'N/A'):
            prev_results_context += f"   - Bedrooms: {rec.get('bedrooms')}\n"
        if rec.get('property_type'):
            prev_results_context += f"   - Type: {rec.get('property_type')}\n"
        if rec.get('area'):
            prev_results_context += f"   - Area: {rec.get('area')}\n"
        if rec.get('budget_status'):
            prev_results_context += f"   - Budget: {rec.get('budget_status')}\n"
        # 🆕 可入住日期喂给 Agent（非空才写；未知则省略，避免编造）。
        if rec.get('available_from'):
            prev_results_context += f"   - Available from: {rec.get('available_from')}\n"
        if rec.get('availability_status'):
            prev_results_context += f"   - Move-in fit: {rec.get('availability_status')}\n"
        # 🆕 把真实房源描述喂给 Agent（截断到可控长度，避免 prompt 膨胀；完整文本在
        # structured_results 里）。让后续"这套家具全吗/含账单吗/离地铁多远"能被真实回答。
        _desc = (rec.get('description') or '').strip()
        if _desc:
            prev_results_context += (
                f"   - Description: {_desc[:600]}{'…' if len(_desc) > 600 else ''}\n"
            )
        if rec.get('url'):
            prev_results_context += f"   - URL: {rec.get('url')}\n"
        prev_results_context += "\n"
    return prev_results_context, structured_results


# A reply has to name at least this many DISTINCT cached listings before we treat it as a
# listing enumeration. 1 would fire on "tell me more about Woburn Place" — a single-listing
# follow-up, where repainting the panel would clobber a newer result set the user is
# looking at. 2+ is the shape of "here are the properties I found".
_NARRATED_LISTING_MIN_HITS = 2
# Below this length a name is too generic to match on ("Bow", "Kew"), so a stray word in
# prose would count as a hit.
_NARRATED_LISTING_MIN_NAME_LEN = 4


def _narrates_cached_listings(response_text, last_results,
                              min_hits=_NARRATED_LISTING_MIN_HITS):
    """True when `response_text` enumerates listings that are already in `last_results`.

    ISSUE #78. A turn that calls NO tool still has the previous turn's listings in its
    context (we put them there — see _build_results_context), so the model can answer
    "show me those flats again" by re-narrating them. tool_data is then empty, the payload
    carries no recommendations, and the frontend's paint branch never runs: the user reads
    five properties in the chat while the right-hand panel shows whatever was there before
    (in the reported case, an empty form-search result). Detecting the narration lets the
    caller re-attach the set the model is actually talking about.

    Matching is on the leading address segment ("Woburn Place, London WC1H" -> "Woburn
    Place"), the same key _build_results_context feeds the model, counted over DISTINCT
    names so a set holding one address twice can't reach the threshold on its own.
    """
    if not response_text or not last_results:
        return False
    haystack = str(response_text).lower()
    seen = set()
    for rec in last_results:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get('address') or '').split(',')[0].strip()
        if len(name) < _NARRATED_LISTING_MIN_NAME_LEN:
            continue
        key = name.lower()
        if key in seen or key not in haystack:
            continue
        seen.add(key)
        if len(seen) >= min_hits:
            return True
    return False


def _build_turn_snapshot_after_turn(user_id, conversation_id, turn_id):
    """Build the post-turn context snapshot AFTER _write_back_turn ran.

    Runs under the per-conversation turn lock so the read of _sess.persistent_state is
    consistent with the just-completed write-back. The HTTP request path passes the
    returned value to ConversationStore.finalize_request_turn so assistant/status/snapshot
    commit in one transaction.

    context_revision: a monotonic per-conversation counter = previous snapshot's
    context_revision + 1 (starting at 1). complete_turn has already marked THIS turn
    completed but its snapshot row does not exist yet, so latest_snapshot() returns the
    PREVIOUS turn's snapshot — the revision keeps climbing across turns and across a fork
    (the child inherits the copied snapshots and continues from their revision).
    """
    prev = conversation_store.latest_snapshot(user_id, conversation_id)
    if isinstance(prev, dict):
        try:
            context_revision = int(prev.get("context_revision", 0)) + 1
        except (TypeError, ValueError):
            context_revision = 1
    else:
        context_revision = 1
    with _session_store.turn_lock(user_id, conversation_id):
        _sess = _get_session(user_id, conversation_id)
        return build_turn_snapshot(
            turn_id=turn_id,
            persistent_state=_sess.persistent_state,
            context_revision=context_revision,
        )


def _save_turn_snapshot_after_turn(user_id, conversation_id, turn_id):
    """Compatibility helper for non-request callers; request routes finalize atomically."""
    snapshot = _build_turn_snapshot_after_turn(user_id, conversation_id, turn_id)
    conversation_store.save_turn_snapshot(user_id, conversation_id, turn_id, snapshot)
    return snapshot


def _queue_background_job(job: dict) -> bool:
    """Attach work to the active turn; finalization persists it atomically."""
    jobs = _turn_background_jobs.get()
    if jobs is None:
        logger.warning(
            "background_job.no_turn_outbox",
            extra={"job_kind": str((job or {}).get("kind") or "unknown")},
        )
        return False
    jobs.append(copy.deepcopy(job))
    return True


def _process_background_job(job: dict, worker_id: str) -> None:
    """Prepare and apply one leased outbox job.

    Summary preparation is checkpointed before it mutates the latest snapshot, so a
    crash re-applies the same value rather than making a second LLM call. Memory uses
    its own per-user idempotency key and is therefore safe under at-least-once delivery.
    """
    kind = job.get("kind")
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("background job payload is invalid")

    if kind == "rolling_summary":
        result = job.get("result")
        if not isinstance(result, dict):
            latest = conversation_store.latest_snapshot(
                job["user_id"], job["conversation_id"]
            ) or {}
            prior = latest.get("summary")
            summary = update_rolling_summary(
                _llm_complete,
                prior,
                payload.get("dropped_turns") or [],
                payload.get("reply_language") or "en",
            )
            result = {
                "summary": summary,
                "through_turn_id": payload.get("through_turn_id") or job["turn_id"],
            }
            conversation_store.save_background_job_result(
                job["id"], worker_id, result
            )
        through = str(result.get("through_turn_id") or job["turn_id"])
        if not conversation_store.patch_latest_snapshot_summary(
            job["user_id"], job["conversation_id"],
            str(result.get("summary") or ""), through,
        ):
            raise RuntimeError("no completed snapshot exists for summary job")
        with _session_store.turn_lock(job["user_id"], job["conversation_id"]):
            sess = _get_session(job["user_id"], job["conversation_id"])
            ec = sess.persistent_state.setdefault("extracted_context", {})
            ec["rolling_summary"] = str(result.get("summary") or "")
            ec["rolling_summary_through_turn_id"] = through
        return

    if kind == "memory_turn":
        if not isinstance(job.get("result"), dict):
            from rag.agent_memory import get_agent_memory
            ok = get_agent_memory().remember_turn(
                payload.get("user_message") or "",
                payload.get("assistant_message") or "",
                session_id=payload.get("session_id") or job["user_id"],
                user_id=job["user_id"],
                tool_used=payload.get("tool_used"),
                idempotency_key=payload.get("idempotency_key"),
                conversation_id=job["conversation_id"],
                turn_id=job["turn_id"],
                turn_started_at=payload.get("turn_started_at"),
                context_tainted=bool(payload.get("context_tainted", False)),
            )
            if ok is False:
                raise RuntimeError("memory store did not acknowledge the turn")
            conversation_store.save_background_job_result(
                job["id"], worker_id, {"stored": True}
            )
        return

    raise ValueError(f"unsupported background job kind: {kind!r}")


def _ensure_outbox_worker():
    """Start/restart the durable consumer and wake it after a commit."""
    global _outbox_worker
    configured = os.getenv("BACKGROUND_JOBS_ENABLED")
    if configured is None and "PYTEST_CURRENT_TEST" in os.environ:
        return None
    if configured is not None and configured.strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        return None
    with _outbox_worker_lock:
        if _outbox_worker is None:
            _outbox_worker = OutboxWorker(
                conversation_store,
                _process_background_job,
                poll_seconds=float(os.getenv("BACKGROUND_JOB_POLL_S", "2")),
                lease_seconds=int(os.getenv("BACKGROUND_JOB_LEASE_S", "300")),
                max_attempts=int(os.getenv("BACKGROUND_JOB_MAX_ATTEMPTS", "5")),
            )
        _outbox_worker.start()
        _outbox_worker.wake()
        return _outbox_worker


def _spawn_rolling_summary_update(user_id, conversation_id, dropped_turns,
                                  through_turn_id, reply_language):
    """Queue a durable summary fold; no LLM work runs before turn commit."""
    return _queue_background_job({
        "kind": "rolling_summary",
        "dedupe_key": f"turn:{through_turn_id}:rolling-summary",
        "payload": {
            "dropped_turns": copy.deepcopy(dropped_turns),
            "through_turn_id": through_turn_id,
            "reply_language": reply_language,
        },
    })


def _write_back_turn(user_id, conversation_id, user_message, assistant_text,
                     recommendations, *, user_preferences=_UNSET,
                     accumulated_search_criteria=_UNSET, criteria_overwrite=None,
                     turn_id=None, reply_language="en"):
    """Atomic phase-3 L2 write-back shared by the ReAct path (handle_with_react_agent)
    and the deterministic /api/search_direct endpoint (pure refactor — the ReAct path's
    behaviour is unchanged from the previously-inlined version).

    Under the per-conversation turn lock — an in-place append + slice-trim keeps the
    SAME list object, so a concurrent same-conversation turn's append is never clobbered
    (the original defect this lock fixes):
      • when supplied, REPLACE the user_preferences / accumulated_search_criteria
        snapshots (ReAct path forwards the graph's final_state values; omit an arg to
        keep the current cached value);
      • when supplied, .update() the accumulated_search_criteria with criteria_overwrite
        — a form submit is authoritative, so its scalar fields OVERWRITE while the list
        fields (property_features / soft_preferences / amenities_of_interest) stay as-is;
      • append this turn to history and slice-trim to MAX_HISTORY_LENGTH;
      • when recommendations exist, cache last_results + the previous_search_results /
        last_results context blocks so ordinal/name follow-ups resolve correctly.

    Returns (prev_results_context, structured_results) for callers that want them.
    """
    prev_results_context, structured_results = _build_results_context(recommendations)
    dropped_turns = []
    with _session_store.turn_lock(user_id, conversation_id):
        _sess = _get_session(user_id, conversation_id)
        if user_preferences is not _UNSET:
            _sess.persistent_state['user_preferences'] = user_preferences
        if accumulated_search_criteria is not _UNSET:
            _sess.persistent_state['accumulated_search_criteria'] = accumulated_search_criteria
        if criteria_overwrite:
            _sess.persistent_state.setdefault('accumulated_search_criteria', {})
            _sess.persistent_state['accumulated_search_criteria'].update(criteria_overwrite)
        _sess.history.append({'user': user_message, 'assistant': (assistant_text or '')[:500]})
        if len(_sess.history) > MAX_HISTORY_LENGTH:
            # Capture the turns about to fall out of the hot window so they can be folded
            # into the rolling summary (background) — otherwise their context is lost.
            dropped_turns = [dict(h) for h in _sess.history[:-MAX_HISTORY_LENGTH]]
            del _sess.history[:-MAX_HISTORY_LENGTH]
        if recommendations:
            _sess.last_results = recommendations
            _sess.persistent_state.setdefault('extracted_context', {})
            _ec = _sess.persistent_state['extracted_context']
            _ec['previous_search_results'] = prev_results_context
            _ec['last_results'] = structured_results
            # 累计推荐注册表：把本轮推荐 merge 进历史注册表（去重/首见 index 稳定/上限），
            # 让后续可追问任何历史轮次推荐过的房源。持久化经 build_turn_snapshot 白名单存活重启/fork。
            # 注意：喂完整 recommendations（前端展示多少就登记多少），不能用截断到 6 条的
            # structured_results —— 否则第 7 条以后展示过的房源永远进不了注册表。
            _ec['recommended_registry'] = _merge_recommended_registry(
                _ec.get('recommended_registry'), recommendations)
            print(f"[state] 💾 已保存 {len(recommendations)} 个搜索结果到上下文"
                  f"（注册表 {len(_ec['recommended_registry'])} 条）")
    # Rolling-summary fold happens OUTSIDE the lock (spawns its own thread). Gated on a
    # real trim + a turn_id (the through-turn marker); legacy callers without turn_id skip.
    if dropped_turns and turn_id:
        _spawn_rolling_summary_update(
            user_id, conversation_id, dropped_turns, turn_id, reply_language)
    return prev_results_context, structured_results


_COMPARISON_QUERY_PATTERNS = (
    r"\bcompare\b",
    r"\bvs\.?\b",
    r"\bversus\b",
    r"\bbetween\b",
    r"\bor\b",
    r"\bbetter\b",
    r"\bwhich\s+one\b",
    r"\bdeciding\s+between\b",
)


def _is_comparison_query(message: str) -> bool:
    """Recognise comparison language without matching ``or`` inside ordinary words.

    The old substring check classified "not sure" and "for commute" as property
    comparisons because both contain the letters ``or``.
    """
    return any(re.search(pattern, message or "", re.IGNORECASE)
               for pattern in _COMPARISON_QUERY_PATTERNS)


async def handle_with_react_agent(user_message: str, context: dict, is_continuation: bool,
                                  user_id: str = "default", conversation_id: str = "default",
                                  request_id: str | None = None, ui_language: str = "en",
                                  turn: dict | None = None):
    """
    使用 LangGraph Agent 处理所有用户请求 - 纯 LLM 驱动

    LangGraph Agent 会自主决定：
    1. 是否需要调用 search_properties 工具搜索房源
    2. 是否需要调用其他工具（安全检查、通勤计算等）
    3. 或者直接回答用户问题

    没有任何关键词匹配 - 完全由 LLM 决策
    """
    global agent_graph, tool_registry, agent_tool_provider

    # ── Phase 1: snapshot THIS conversation's L2 state under the per-conv lock ──
    # The turn lock makes the read here and the write-back in phase 3 atomic vs.
    # concurrent same-conversation requests, WITHOUT being held across the slow LLM
    # call in phase 2. We work off deep-copied snapshots so the graph mutating its
    # inputs can never corrupt the shared cached state mid-flight.
    turn_lock = _session_store.turn_lock(user_id, conversation_id)
    with turn_lock:
        _sess = _get_session(user_id, conversation_id)
        persistent_snapshot = copy.deepcopy(_sess.persistent_state)
        history_snapshot = list(_sess.history)
        # 🆕 Ask-AI 聚焦解析要对照"完整推荐列表"，它挂在 session 对象上（_sess.last_results，
        # extracted_context 里只留 6 条）。在同一把锁内浅拷贝，避免跨慢速 LLM 调用再次加锁。
        last_results_snapshot = list(_sess.last_results or [])

    request_graph = _ensure_agent_runtime()

    # ── 构建本轮 extracted_context ──────────────────────────────
    extracted_context = dict(persistent_snapshot.get('extracted_context', {}))

    # Refinement-in-place needs the COMPLETE set the panel is currently rendering, not the
    # 6-row prompt digest in extracted_context['last_results'] — narrowing over the digest
    # would silently drop listings 7..N from the panel. _sess.last_results holds the full
    # list (snapshotted above under the turn lock); the graph reads it via
    # core.langgraph_agent._refinable_previous_results. Deliberately NOT persisted: it is
    # absent from the build_turn_snapshot whitelist (so no snapshot-schema change) and from
    # _EXTRACTED_CONTEXT_WHITELIST (so it never leaves the server); after a restart the
    # refinement path falls back to the digest.
    if last_results_snapshot:
        extracted_context['last_results_full'] = last_results_snapshot

    # focus 栈（多聚焦）：优先读 context.focus_stack（数组，旧→新，最后一个=当前聚焦），缺失时
    # 退化为 [context.property]（向后兼容旧前端）。逐个用 _resolve_focus_listing 解析（会话 last_results
    # 快照 → 累计推荐注册表+sqlite 缓存 → demo CSV → 标量兜底），结构化记录挂 extracted_context['focus_stack']
    # 供 langgraph 指代锚定；栈顶继续填充既有 property_* 单聚焦键，保证下游不回归。
    _accum_registry = extracted_context.get('recommended_registry') or []
    # 注册表 URL → sqlite 完整房源的注入式查询，加每轮 memo 避免同 URL 重复全表扫描。
    _focus_cache_memo = {}

    def _memo_cache_lookup(url):
        key = str(url or '').strip().lower().rstrip('/')
        if not key:
            return None
        if key not in _focus_cache_memo:
            try:
                from core.scraping.on_demand import find_cached_listing_by_url
                _focus_cache_memo[key] = find_cached_listing_by_url(url)
            except Exception:
                _focus_cache_memo[key] = None
        return _focus_cache_memo[key]

    focus_items = None
    if context:
        _fs = context.get('focus_stack')
        if isinstance(_fs, list) and _fs:
            focus_items = [f for f in _fs if isinstance(f, dict)]
        elif context.get('property'):
            focus_items = [context.get('property')]
    if focus_items:
        focus_records = _build_focus_stack_records(
            focus_items, last_results_snapshot, all_properties,
            registry=_accum_registry, cache_lookup=_memo_cache_lookup)
        if focus_records:
            extracted_context['focus_stack'] = focus_records
        # 栈顶（当前聚焦）填充既有 property_* 单聚焦键（含 CSV-only 键，故单独解析一次，走 memo 免重复扫描）。
        top_ctx, focus_source = _resolve_focus_listing(
            focus_items[-1], last_results_snapshot, all_properties,
            registry=_accum_registry, cache_lookup=_memo_cache_lookup)
        extracted_context.update(top_ctx)
        print(f"[LangGraph] 📍 Ask-AI focus_count={len(focus_records)} "
              f"focus_source_present={bool(focus_source)}")

    # 累计推荐注册表 → 紧凑编号索引块注入上下文（仅摘要；完整信息由 get_property_details 按 URL 取）。
    if _accum_registry:
        _idx_block = render_recommended_index(_accum_registry)
        if _idx_block:
            extracted_context['recommended_index'] = _idx_block

    # ── 检测对比查询 ─────────────────────────────────────────────
    viewed_properties_context = _build_viewed_properties_context(
        (context or {}).get('viewed_properties'), last_results_snapshot, all_properties)
    if viewed_properties_context:
        extracted_context['viewed_properties'] = viewed_properties_context

    is_comparison_query = _is_comparison_query(user_message)

    if is_comparison_query:
        print(f"[LangGraph] 🔄 检测到对比查询，正在加载房产数据...")
        mentioned_properties = []
        for prop in all_properties:
            prop_name = prop.get('Address', '').split(',')[0].strip().lower()
            name_words = prop_name.split()
            for word in name_words:
                if len(word) > 3 and word.lower() in user_message.lower():
                    mentioned_properties.append(prop)
                    print(f"[LangGraph] ✅ 找到提及的房产 address_chars={len(str(prop.get('Address') or ''))}")
                    break

        if mentioned_properties:
            comparison_context = "\n=== Properties to Compare ===\n"
            for i, prop in enumerate(mentioned_properties[:3], 1):
                comparison_context += f"\n**Property {i}: {prop.get('Address', '').split(',')[0]}**\n"
                comparison_context += f"- Price: {prop.get('Price', 'N/A')}\n"
                comparison_context += f"- Room Type: {prop.get('Room_Type_Category', 'N/A')}\n"
                comparison_context += f"- Amenities: {prop.get('Detailed_Amenities', 'N/A')}\n"
                comparison_context += f"- Guest Policy: {prop.get('Guest_Policy', 'N/A')}\n"
                comparison_context += f"- Payment Rules: {prop.get('Payment_Rules', 'N/A')}\n"
                comparison_context += f"- NOT Included: {prop.get('Excluded_Features', 'N/A')}\n"
                comparison_context += f"- Commute Info: {prop.get('Description', 'N/A')}\n"
            extracted_context['comparison_properties'] = comparison_context
            print(f"[LangGraph] 📊 已加载 {len(mentioned_properties)} 个房产的对比数据")

    # ── 构建包含历史 + 长期记忆 + 滚动摘要的查询（统一走 context_assembler）──
    has_property_context = bool(extracted_context.get('property_address'))
    if has_property_context:
        print(f"[LangGraph] 📍 用户正在询问关于特定房产的问题，将使用房产上下文回答")

    # 长期记忆（Generative-Agents 评分检索）——按 user_id 命名空间共享（跨会话），branch_lineage
    # 让分叉会话只看到它真正继承的 episodic 记忆（semantic/reflection 仍全局）。
    _mem_block = ""
    try:
        from rag.agent_memory import get_agent_memory
        _am = get_agent_memory()
        _lineage = conversation_store.get_branch_lineage(user_id, conversation_id)
        _mems = _am.retrieve(user_message, session_id=user_id, user_id=user_id, n=6,
                             branch_lineage=_lineage)
        _mem_block = _am.format_for_prompt(_mems)
        if _mem_block:
            print(f"[Memory] 🧠 注入 {len(_mems)} 条相关记忆")
    except Exception as _e:
        print(f"[Memory] retrieve skipped; error_type={type(_e).__name__}")

    # Assemble the legacy query as before. FC-compatible arches have a dedicated channel for
    # long-term memory; putting the same block into user_query would show it twice in
    # its message array and makes the raw user turn ambiguous to downstream tools.
    _uses_fc_runtime = uses_fc_runtime(AGENT_ARCH)
    query_with_history = assemble_context(
        user_message=user_message,
        history=history_snapshot,
        memory_block="" if _uses_fc_runtime else _mem_block,
        has_property_context=has_property_context,
        rolling_summary=(persistent_snapshot.get('extracted_context') or {}).get('rolling_summary'),
    )

    # 原始当前消息（不含记忆/历史前缀）——供工具做"仅基于本条消息"的解析
    # (预算/通勤正则、postcode/序数解析)，避免误抓注入记忆里的旧值。
    extracted_context['current_message'] = user_message
    # 会话历史（SessionStore shape: [{"user":.., "assistant":..}, ...]）——无条件写入
    # extracted_context。legacy 字符串装配路径（assemble_context 上面已单独喂过 history）
    # 忽略该键；fc_loop 的 assemble_messages 用它构造 user/assistant 消息对，否则 fc 模型
    # 拿不到任何对话历史。放在这里（早于建图）保证两条路径都可用。
    extracted_context['history'] = history_snapshot
    # 🆕 回复语言（产品规则）：本条消息含中文→'zh'；否则 UI=en→'en'；否则 'zh'。用 pristine
    # user_message（早于记忆/历史前缀），图 agent 读取该键并转发给 search 工具，使 /api/alex
    # 与 "search anyway" 路径不再中英混杂。
    extracted_context['reply_language'] = _resolve_reply_language(user_message, ui_language)

    # ── 构建 AgentState 并调用 LangGraph ─────────────────────────
    # session_id passed to the graph/checkpointer IS the conversation_id, so the
    # checkpointer thread_id = f"{user_id}:{conversation_id}".
    initial_state = create_initial_state(
        user_query=query_with_history,
        extracted_context=extracted_context,
        user_preferences=persistent_snapshot['user_preferences'],
        accumulated_search_criteria=persistent_snapshot['accumulated_search_criteria'],
        user_id=user_id,
        session_id=conversation_id,
        request_id=request_id,
        memory_context=_mem_block if _uses_fc_runtime else "",
    )

    # ── Phase 2: the slow LLM call — NO turn lock held here ──────
    print(f"[LangGraph] ▶ 开始执行 graph.ainvoke() ...")
    import time as _eval_time
    _eval_turn_started = _eval_time.perf_counter()
    initial_state["turn_start_monotonic"] = _eval_turn_started
    # GRAPH_RECURSION_LIMIT 由 core.langgraph_agent 导出（并行 agent 落地，值 80）；防御式取值
    # （getattr 默认 80），即便本文件先落地、常量尚未存在也可用。合并进 graph_config 的现有配置。
    import core.langgraph_agent as _lga_mod
    _graph_cfg = dict(graph_config(user_id, conversation_id, request_id=request_id))
    _graph_cfg["recursion_limit"] = getattr(_lga_mod, "GRAPH_RECURSION_LIMIT", 80)
    # HITL resume wiring: if this thread is paused at confirm_search, a clear yes/no reply
    # resumes the interrupted run with Command(resume=...). Any other reply falls through to
    # fresh input, which (verified on langgraph 1.2.8) cleanly restarts from START and
    # deliberately abandons the pending confirmation — the user changed topic.
    graph_input = initial_state
    if _runtime_config.enable_hitl:
        try:
            _snap = await request_graph.aget_state(_graph_cfg)
            _pending_confirm = bool(_snap.next) and "confirm_search" in _snap.next
        except Exception:
            _pending_confirm = False
        if _pending_confirm:
            from core.graph_advanced import parse_confirmation_reply
            _decision = parse_confirmation_reply(user_message)
            if _decision is not None:
                from langgraph.types import Command as _LGCommand
                graph_input = _LGCommand(
                    resume=(True if _decision == "proceed" else {"action": "cancel"})
                )
                print(f"[LangGraph] ⏯ HITL resume: {_decision}")
    # Last-resort request boundary applies to BOTH architectures. Node-level budgets should
    # normally finish first, but no provider/tool regression may leave an HTTP turn open forever.
    from core.agent_loop import _turn_ceiling_s
    _graph_timeout_s = max(
        0.001,
        _turn_ceiling_s() - (_eval_time.perf_counter() - _eval_turn_started),
    )
    try:
        if AGENT_ARCH == MANAGER_V1_ARCH:
            root_task_id = f"turn:{request_id}"
            turn_observations.note_root_agent_context(
                agent_role="manager",
                task_id=root_task_id,
                parent_task_id=None,
            )
            with agent_execution_context(
                agent_role="manager",
                task_id=root_task_id,
                parent_task_id=None,
            ):
                final_state = await _ainvoke_graph_with_timeout(
                    request_graph, graph_input, _graph_cfg, _graph_timeout_s
                )
        else:
            final_state = await _ainvoke_graph_with_timeout(
                request_graph, graph_input, _graph_cfg, _graph_timeout_s
            )
    except asyncio.TimeoutError:
        logger.error(
            "agent.graph.turn_timeout",
            extra={
                "request_id": request_id,
                "conversation_id": conversation_id,
                "agent_arch": AGENT_ARCH,
                "timeout_s": _turn_ceiling_s(),
            },
        )
        raise
    print(f"[LangGraph] ✓ 完成!")

    # HITL safety net: if the graph paused at confirm_search (enable_hitl), ainvoke returns
    # with __interrupt__ set and no final_response. Surface the confirmation prompt instead
    # of crashing; resuming (graph.ainvoke(Command(resume=...), config)) is exercised in the
    # demo/tests, not this single-shot endpoint.
    _intr = final_state.get("__interrupt__") if isinstance(final_state, dict) else None
    if _intr and not final_state.get("final_response"):
        _payload = getattr(_intr[0], "value", {}) if _intr else {}
        final_state["final_response"] = (
            (_payload.get("question") if isinstance(_payload, dict) else None)
            or "I'm about to run some property searches — please confirm to proceed."
        )
        final_state["response_type"] = "answer"

    # ── Canary telemetry: publish the fc-side turn signals for /api/alex to record ──
    # Derived from the graph's final_state (fc channels) here — the only place that sees it;
    # the caller reads them back via the _turn_fc_signals ContextVar after this await returns.
    try:
        _turn_fc_signals.set(_build_fc_signals(final_state))
    except Exception:
        pass

    # ── Offline-eval turn row (additive; no-op unless RENTCOMPASS_EVAL active) ──
    try:
        from evaluation.metrics import collector as _eval_collector
        if _eval_collector.is_active():
            _eval_collector.record_turn(
                route=final_state.get('tool_decision'),
                response_type=final_state.get('response_type', 'answer'),
                critic_attempts=final_state.get('critic_attempts'),
                verdict=final_state.get('verdict'),
                latency_ms=(_eval_time.perf_counter() - _eval_turn_started) * 1000,
            )
    except Exception:
        pass

    response_text = final_state.get('final_response', '')
    response_type = final_state.get('response_type', 'answer')
    tool_data = final_state.get('tool_data', {})
    recommendations = tool_data.get('recommendations')

    # ── Tool-markup guard, LAYER 1: before anything persists this ──
    # Placement is the whole point. response_text goes into the conversation DB
    # (_write_back_turn, just below) and into auto-memory (remember_turn_async)
    # before any payload exists, so a guard at the HTTP boundary would still let raw
    # control markup land in storage — where it is replayed into the next turn's
    # context and may be acted on. Both arches converge here, so one scan covers
    # fc_loop and legacy without either graph having to cooperate.
    _reply_lang = extracted_context.get('reply_language', 'en')
    try:
        g.reply_language = _reply_lang  # so the boundary fallback matches the conversation
    except Exception:
        pass
    response_text, _dsml_hit = dsml_guard.sanitize_user_text(
        response_text, reply_language=_reply_lang)
    if _dsml_hit:
        turn_observations.note_dsml_blocked()
        # No raw text in the log line: it is attacker-reachable content, and an ops
        # log that echoes it is one more surface it can be replayed from.
        logger.warning("canary: tool-call markup blocked before persistence (arch=%s)",
                       AGENT_ARCH)

    print(f"[LangGraph] Response Type: {response_type}")

    # ── ISSUE #78: a re-narration must repaint the panel it is narrating ──
    # No tool ran this turn, so tool_data is empty — but the model still enumerated
    # listings, because the previous turn's results live in its context. Shipping that
    # reply with no recommendations is what left the chat listing five flats next to an
    # empty results panel. Re-attach the cached set so text and panel agree again.
    #
    # The FULL cached set is re-attached, not just the named subset: a search turn already
    # paints all N while the reply details only the top few (that is what the working turn
    # in the report did), so the panel must not shrink just because no tool ran. Placed
    # ahead of _write_back_turn and the conversation-store write so the persisted message
    # carries them too — otherwise a page reload would reproduce the empty panel.
    if not recommendations:
        try:
            _cached = _get_session(user_id, conversation_id).last_results
            if _narrates_cached_listings(response_text, _cached):
                recommendations = _cached
                response_type = 'search'
                print(f"[state] 🔁 无工具调用但复述了 {len(_cached)} 条历史房源 → 回填面板")
        except Exception as _e:  # never turn a good turn into an error over a repaint
            print(f"[state] listing re-attach skipped; error_type={type(_e).__name__}")

    # ── Phase 3: build the results context + atomic write-back of L2 state ──
    # Extracted into _write_back_turn so the deterministic /api/search_direct endpoint
    # reuses the EXACT same logic. Forward the graph's final_state snapshots (falling
    # back to _UNSET → "keep the current cached value" when a key is absent, exactly as
    # the previous inlined `final_state.get(key, <current>)` did). The prev-results
    # context is cached inside the helper; this path doesn't need it returned.
    _write_back_turn(
        user_id, conversation_id, user_message, response_text, recommendations,
        user_preferences=final_state.get('user_preferences', _UNSET),
        accumulated_search_criteria=final_state.get('accumulated_search_criteria', _UNSET),
        turn_id=(turn.get("id") if isinstance(turn, dict) else None),
        reply_language=extracted_context.get('reply_language', 'en'),
    )

    # ── 长期记忆：随 turn 原子提交到 durable outbox，响应路径不做 LLM/SQLite 写入 ──
    # 记忆按 user_id 命名空间共享（跨会话）；worker 使用 idempotency_key 做 at-least-once
    # 去重。进程若在响应后退出，租约到期后另一进程会继续，不会静默丢任务。
    _td = final_state.get('tool_decision')
    _tool_used = _td.get('tool') if isinstance(_td, dict) else None
    _queue_background_job({
        "kind": "memory_turn",
        "dedupe_key": f"turn:{request_id}:memory" if request_id else (
            f"turn:{turn.get('id')}:memory" if isinstance(turn, dict) else "memory:unanchored"
        ),
        "payload": {
            "user_message": user_message,
            "assistant_message": response_text,
            "session_id": user_id,
            "tool_used": _tool_used,
            "idempotency_key": f"turn:{request_id}" if request_id else None,
            "turn_started_at": (turn.get("started_at") if isinstance(turn, dict) else None),
            # Taint A+ (§2.8c): a tainted turn hardens the auto-memory bypass to
            # user-only fact extraction so untrusted content can't seed durable memory.
            "context_tainted": bool(final_state.get('context_tainted', False)),
        },
    })

    # ── 构建响应 payload（conversation_id 由调用方 api_alex 注入）──
    _tool_data = tool_data if isinstance(tool_data, dict) else {}
    if recommendations:
        # Frontend contract: forward the canonical search_criteria (Agent 2's
        # format_output stores it in tool_data for found searches) so the search form
        # can reflect what was actually searched. Defaults to {} when absent.
        return {
            "response_type": "search",
            "message": response_text,
            "recommendations": recommendations,
            "search_criteria": _tool_data.get('search_criteria') or {},
            # 🆕 目的地附近推荐居住区（可点击 chips → 多区域再搜）。
            "area_recommendations": _tool_data.get('area_recommendations') or [],
        }

    if response_type == 'question' or response_type == 'clarification':
        # Frontend contract: on a search-criteria clarification, forward Agent 2's
        # missing_fields / known_criteria (present in tool_data) so the form can
        # highlight what's still needed. Only included when the graph supplied them.
        payload = {
            "response_type": "clarification",
            "message": response_text,
            "agent_state": "waiting_for_input",
            "extracted_context": _whitelist_extracted_context(extracted_context),
        }
        if 'missing_fields' in _tool_data:
            payload["missing_fields"] = _tool_data['missing_fields']
        # Optional (never gate-triggering) fields the gate also mentions — currently
        # just 'move_in'. Kept separate from missing_fields so the recommended-field
        # contract (and its tests) stays frozen.
        if 'missing_optional_fields' in _tool_data:
            payload["missing_optional_fields"] = _tool_data['missing_optional_fields']
        if 'known_criteria' in _tool_data:
            payload["known_criteria"] = _tool_data['known_criteria']
        # clarification_kind distinguishes the hard area gate ('missing_area') from the
        # soft recommended-criteria gate ('soft_criteria') for the frontend.
        if 'clarification_kind' in _tool_data:
            payload["clarification_kind"] = _tool_data['clarification_kind']
        return payload

    if response_type == 'answer':
        return {
            "response_type": "chat",
            "message": response_text,
        }

    return {
        "response_type": "chat",
        "message": response_text or "I'm here to help! What would you like to know?",
    }


# ============================================================================
# Deterministic direct-search endpoint — bypasses the LLM router entirely
# ----------------------------------------------------------------------------
# The frontend search form submits structured criteria here; we call the
# search_properties tool DIRECTLY (no LangGraph, no critic, no memory write) so a
# form submit is fast and fully deterministic. The conversational L2 state is still
# updated via the SAME _write_back_turn helper the ReAct path uses, so a follow-up
# CHAT turn on /api/alex sees the form's criteria + results as context.
# ============================================================================

def _coerce_optional_int(value, field_name, *, min_value, max_value):
    """Coerce an optional numeric criterion to an int within the inclusive range
    [min_value, max_value], or None when absent/blank. Rejects with ApiError(400):

      • non-numeric values ("abc", objects) → "must be an integer";
      • fractional numbers — a JSON float like 3.7 (or a numeric string "3.7") would
        otherwise be silently floored by int(); reject it as "must be a whole number";
      • out-of-range values (e.g. max_budget 0, bedrooms 1000, negatives, absurdly
        large) → "must be between {min} and {max}".

    Booleans are rejected too (JSON true/false are ints in Python and must not pass as
    counts). None / "" → None ("unspecified"), which stays a valid criterion.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ApiError(400, f"{field_name} must be an integer")
    if isinstance(value, float):
        # 3.7 -> reject; 3.0 -> accept as 3.
        if not value.is_integer():
            raise ApiError(400, f"{field_name} must be a whole number")
        n = int(value)
    elif isinstance(value, int):
        n = value
    else:
        # Strings / other: parse strictly. "1500" -> 1500; "3.7"/"abc" -> ValueError.
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            raise ApiError(400, f"{field_name} must be an integer")
    if n < min_value or n > max_value:
        raise ApiError(400, f"{field_name} must be between {min_value} and {max_value}")
    return n


def _coerce_bool(value) -> bool:
    """Coerce a JSON bool (or a common truthy string) to a real bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_optional_iso_date(value, field_name):
    """Coerce an optional move-in date to a strict 'YYYY-MM-DD' string, or None when
    absent/blank. Rejects with ApiError(400): a non-string, a wrong shape, or a
    well-formed-but-impossible calendar date (e.g. 2026-02-31). '' / None -> None."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"{field_name} must be a date string (YYYY-MM-DD)")
    v = value.strip()
    if not v:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        raise ApiError(400, f"{field_name} must be in YYYY-MM-DD format")
    try:
        datetime.strptime(v, "%Y-%m-%d")  # reject impossible calendar dates
    except ValueError:
        raise ApiError(400, f"{field_name} is not a valid calendar date")
    return v


def _compose_search_line(area, max_budget, budget_period, bedrooms,
                         no_commute, commute_destination, max_commute_time,
                         move_in_date=None, reply_language="en") -> str:
    """A compact one-liner describing a direct search — reused as the conversation title,
    the persisted user turn, and the tool's user_query. Localized zh/en per reply_language
    (表单直搜无消息，故按前端 UI 语言定回复语言)。无 emoji（对话面禁用 emoji）。"""
    if reply_language == "zh":
        parts = [f"搜索：{area}"]
        if max_budget is not None:
            per = "周" if budget_period == "week" else "月"
            parts.append(f"≤£{max_budget}/{per}")
        if bedrooms is not None:
            parts.append(f"{bedrooms} 室")
        if no_commute:
            parts.append("不通勤")
        elif commute_destination:
            if max_commute_time is not None:
                parts.append(f"≤{max_commute_time}分钟到{commute_destination}")
            else:
                parts.append(f"通勤至{commute_destination}")
        if move_in_date:
            parts.append(f"入住 ≥{move_in_date}")
        return " | ".join(parts)
    parts = [f"Search: {area}"]
    if max_budget is not None:
        per = "wk" if budget_period == "week" else "mo"
        parts.append(f"≤£{max_budget}/{per}")
    if bedrooms is not None:
        parts.append(f"{bedrooms} bed")
    if no_commute:
        parts.append("no commute")
    elif commute_destination:
        if max_commute_time is not None:
            parts.append(f"≤{max_commute_time}min to {commute_destination}")
        else:
            parts.append(f"to {commute_destination}")
    if move_in_date:
        parts.append(f"move-in ≥{move_in_date}")
    return " | ".join(parts)


def _search_result_failed(result) -> bool:
    """Distinguish an empty successful search from a structured tool failure."""
    return (not isinstance(result, dict) or result.get('success') is False
            or result.get('status') == 'error')


@app.route('/api/search_direct', methods=['POST'])
async def api_search_direct():
    """Deterministic structured search — the frontend form's backend path.

    Bypasses the LLM router entirely: validates the submitted criteria, calls the
    search_properties tool DIRECTLY, updates the same L2 conversational state a chat
    turn would, and ALWAYS answers with response_type "search" (or "error" on a tool
    failure). Identity + REQUIRE_AUTH gating are identical to /api/alex (path under /api/).
    """
    # --- parse + validate (ApiError → JSON 4xx, NOT 500) -----------------------
    data = get_json_or_400()
    _validate_conversation_id(data)  # reject list/dict/non-string cid before it hits sqlite
    user_id, _session_id = resolve_identity(data)

    # 回复语言：表单直搜没有消息可推断，故直接采用前端 UI 语言（缺失/非法按 'en'）。
    # 透传给 search 工具（覆盖其基于消息的 is_cjk 推断），并本地化本端点自己拼的文案。
    ui_language = _normalize_ui_language(data.get('ui_language'))
    reply_language = ui_language

    criteria = data.get('criteria')
    if not isinstance(criteria, dict):
        raise ApiError(400, "criteria must be an object")

    # 🆕 多区域：接受 areas 列表（与单 area 并存）。缺 area 但有 areas 时以 areas[0] 补齐；
    # 既无 area/areas 又无通勤目的地时才报错——仅有通勤目的地时，工具会把居住区域默认为
    # 目的地所在区域（非阻塞默认）。
    raw_areas = criteria.get('areas')
    areas = []
    if isinstance(raw_areas, list):
        for _a in raw_areas:
            if isinstance(_a, str) and _a.strip() and _a.strip() not in areas:
                areas.append(_a.strip())
    area = criteria.get('area')
    area = area.strip() if isinstance(area, str) and area.strip() else None
    if not area and areas:
        area = areas[0]
    elif area and area not in areas:
        areas = [area] + areas
    _cd = criteria.get('commute_destination')
    _has_commute_dest = isinstance(_cd, str) and bool(_cd.strip())
    if not area and not areas and not _has_commute_dest:
        raise ApiError(400, "area or commute_destination is required")

    # Sane inclusive ranges (documented on _coerce_optional_int):
    #   max_budget      £[1, 100000]  — 0 is not a real limit; reject fractional/absurd.
    #   bedrooms        [0, 20]       — 0 = studio/any; reject negative and >20.
    #   max_commute_time [1, 300] min — reject 0/negative and absurdly large.
    max_budget = _coerce_optional_int(
        criteria.get('max_budget'), "max_budget", min_value=1, max_value=100000)
    bedrooms = _coerce_optional_int(
        criteria.get('bedrooms'), "bedrooms", min_value=0, max_value=20)
    max_commute_time = _coerce_optional_int(
        criteria.get('max_commute_time'), "max_commute_time", min_value=1, max_value=300)
    no_commute = _coerce_bool(criteria.get('no_commute'))
    budget_period = "week" if str(criteria.get('budget_period') or "month").strip().lower() == "week" else "month"

    commute_destination = criteria.get('commute_destination')
    if isinstance(commute_destination, str):
        commute_destination = commute_destination.strip() or None
    else:
        commute_destination = None

    # room_type: canonical enum ('studio'|'ensuite'|'shared') or None (any). Unknown
    # values are dropped so a bad form value never silently narrows the search.
    room_type = criteria.get('room_type')
    if isinstance(room_type, str):
        room_type = room_type.strip().lower() or None
        if room_type not in ('studio', 'ensuite', 'shared'):
            room_type = None
    else:
        room_type = None

    # move_in_date: OPTIONAL 'YYYY-MM-DD'. Strictly validated (format + real calendar
    # date) — garbage is rejected with 400; ''/None is a valid "unspecified". Never
    # blocks the search itself.
    move_in_date = _coerce_optional_iso_date(criteria.get('move_in_date'), "move_in_date")

    # no_commute is authoritative: drop any commute constraint from the TOOL call (the
    # raw commute_destination is still mirrored into the accumulated criteria below).
    if no_commute:
        max_commute_time = None
    tool_commute_destination = None if no_commute else commute_destination

    _area_label = area or commute_destination or ('你的区域' if reply_language == 'zh' else 'your area')
    readable = _compose_search_line(
        _area_label, max_budget, budget_period, bedrooms,
        no_commute, commute_destination, max_commute_time, move_in_date,
        reply_language=reply_language)

    request_id = new_request_id(request.headers.get("X-Request-Id"))
    existing_turn = conversation_store.get_request_turn(user_id, request_id)
    if existing_turn is not None:
        return _request_replay_response(user_id, request_id, existing_turn)

    # --- resolve / implicitly create the conversation (mirrors /api/alex) -------
    conversation_id = data.get('conversation_id')
    conv = conversation_store.get_conversation(user_id, conversation_id) if conversation_id else None
    if conv:
        conversation_id = conv["id"]
        _reconcile_agent_arch(user_id, conversation_id, conv)
    else:
        conversation_id = None

    g.canary_request_id = request_id
    try:
        turn = conversation_store.start_request_turn(
            user_id,
            conversation_id,
            request_id,
            readable,
            lease_seconds=_runtime_config.turn_lease_seconds,
            create_title=_derive_title(readable),
            agent_arch=AGENT_ARCH,
            agent_version=APP_CANDIDATE_SHA,
            strict=DEEPSEEK_STRICT,
        )
    except ConversationBusy as exc:
        response = jsonify({
            "error": "another turn is already running for this conversation",
            "running_turn_id": exc.turn_id,
            "conversation_id": conversation_id,
        })
        response.status_code = 409
        response.headers["Retry-After"] = str(exc.retry_after)
        response.headers["X-Request-Id"] = request_id
        return response
    except PrivacyErasureInProgress:
        response = jsonify({"error": "privacy erasure is in progress"})
        response.status_code = 423
        response.headers["Retry-After"] = "1"
        response.headers["X-Request-Id"] = request_id
        return response

    if turn.get("replayed"):
        return _request_replay_response(user_id, request_id, turn)
    conversation_id = turn["conversation_id"]
    turn_id = turn["id"]

    # Open an observation window here too. This path makes no LLM call, so
    # search_direct_signals() can assert provable zeros for everything else — but the
    # boundary scan still runs, and without a window note_dsml_leak() would be a
    # silent no-op, so a leak here would report 0. A control whose alarm is
    # disconnected on one endpoint is not a control on that endpoint.
    turn_observations.begin_turn()
    _turn_started_ms = time.perf_counter()
    g.canary_turn_started = _turn_started_ms
    g.canary_conversation_id = conversation_id
    g.canary_user_id = user_id

    uid_hash, _ = hash_user_id(user_id)
    logger.info(
        "search_direct.turn.start",
        extra={
            "request_id": request_id,
            "conversation_id": conversation_id,
            "user_id_hash": uid_hash,
            "agent_arch": AGENT_ARCH,
            "area_count": len(areas),
            "has_budget": max_budget is not None,
            "has_commute": bool(tool_commute_destination),
        },
    )

    # --- call the search tool DIRECTLY (no LangGraph / critic / memory) ---------
    try:
        from core.agent_loop import _turn_ceiling_s
        _direct_deadline = time.monotonic() + _turn_ceiling_s()
        with request_context(request_id, user_id):
            result = await search_properties_impl(
                user_query=readable,
                area=area,
                areas=areas or None,
                commute_destination=tool_commute_destination,
                max_budget=max_budget,
                max_commute_time=max_commute_time,
                no_commute=no_commute,
                bedrooms=bedrooms,
                budget_period=budget_period,
                room_type=room_type,
                move_in_date=move_in_date,
                # The panel Search button is an explicit user confirmation, so this path
                # BYPASSES the soft criteria gate (never returns a soft clarification).
                confirmed=True,
                # 表单直搜无消息可推断语言 → 显式透传回复语言，覆盖工具的 is_cjk 推断。
                reply_language=reply_language,
                _deadline_monotonic=_direct_deadline,
            )
        if _search_result_failed(result):
            # The tool returns structured failures instead of raising. Do not turn a
            # provider/RAG failure into the misleading "no matching properties" state.
            raise RuntimeError((result or {}).get('error', 'property search failed'))
        result, commute_evidence = await validate_search_payload_with_provider(
            tool_registry, result,
            timeout_s=20.0, deadline_monotonic=_direct_deadline)
        result["commute_evidence"] = commute_evidence
        if result.get("candidate_validation") is not None:
            result["candidate_status_text"] = render_candidate_status(
                result["candidate_validation"], language=reply_language)
        # 工具已按 reply_language 本地化 summary；仅兜底文案由本端点自己本地化。
        recommendations = result.get('recommendations') or []
        if recommendations:
            _fallback = (f"为你找到 {len(recommendations)} 套匹配房源。" if reply_language == 'zh'
                         else f"Found {len(recommendations)} matching properties.")
        else:
            _fallback = ("没有找到符合条件的房源，试着放宽搜索条件。" if reply_language == 'zh'
                         else "No matching properties found. Try widening your criteria.")
        message = (result.get("candidate_status_text")
                   if result.get("candidate_status_text")
                   and (recommendations or result.get("over_budget_alternatives"))
                   else result.get('summary') or result.get('message') or _fallback)
        payload = {
            "response_type": "search",
            "message": message,
            "recommendations": recommendations,
            "search_criteria": result.get('search_criteria') or {},
            # 🆕 目的地附近"已验证的推荐居住区"，前端渲染为可点击 chips → 一键多区域再搜。
            "candidate_states": result.get("candidate_states") or [],
            "excluded_candidates": result.get("excluded_candidates") or [],
            "unverified_candidates": result.get("unverified_candidates") or [],
            "commute_evidence": result.get("commute_evidence") or [],
            "area_recommendations": result.get('area_recommendations') or [],
        }
    except Exception as e:
        logger.error(
            "search_direct.turn.execution_failed",
            extra={
                "request_id": request_id,
                "conversation_id": conversation_id,
                "error_type": type(e).__name__,
            },
        )
        recommendations = []
        message = ("抱歉，搜索房源时出错了。请稍后再试。" if reply_language == 'zh'
                   else "Sorry, something went wrong while searching. Please try again.")
        payload = {
            "response_type": "error",
            "message": message,
            "recommendations": [],
            "search_criteria": {},
        }

    # conversation_id + turn_id echoed in EVERY response (incl. errors + implicit creation).
    payload["conversation_id"] = conversation_id
    payload["turn_id"] = turn_id
    _serialization_failed = False
    try:
        payload, _boundary_blocked = _guard_payload_before_persistence(payload)
    except ResponsePayloadSerializationError as e:
        logger.error(
            "response.serialization_failed",
            extra={"request_id": request_id, "error_type": type(e.__cause__).__name__},
        )
        payload = {
            "response_type": "error",
            "message": "internal server error",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
        }
        _boundary_blocked = False
        _serialization_failed = True
    if _boundary_blocked:
        recommendations = []
        message = payload["message"]
    if _serialization_failed:
        recommendations = []
        message = payload["message"]

    _terminal_failed = (
        _serialization_failed or _boundary_blocked
        or payload.get("response_type") == "error"
    )
    _snapshot = None
    _background_jobs = []
    if not _terminal_failed:
        _outbox_token = _turn_background_jobs.set([])
        try:
            # A form submit is authoritative, so overwrite scalar accumulated criteria.
            # No long-term-memory write occurs on this deterministic path.
            _write_back_turn(
                user_id, conversation_id, readable, message, recommendations,
                criteria_overwrite={
                    'area': area,
                    'areas': areas,
                    'commute_destination': commute_destination,
                    'destination': commute_destination,
                    'max_budget': max_budget,
                    'max_travel_time': max_commute_time,
                    'no_commute': no_commute,
                    'bedrooms': bedrooms,
                    'budget_period': budget_period,
                    'room_type': room_type,
                    'move_in_date': move_in_date,
                },
                turn_id=turn_id,
                reply_language=reply_language,
            )
            _snapshot = _build_turn_snapshot_after_turn(
                user_id, conversation_id, turn_id
            )
        except Exception as e:
            logger.error(
                "search_direct.turn_state_failed",
                extra={
                    "request_id": request_id,
                    "conversation_id": conversation_id,
                    "error_type": type(e).__name__,
                },
            )
            _terminal_failed = True
            recommendations = []
            message = (
                "抱歉，无法可靠保存搜索结果，请稍后重试。"
                if reply_language == "zh"
                else "Sorry, the search result could not be saved reliably. Please try again."
            )
            payload = {
                "response_type": "error",
                "message": message,
                "recommendations": [],
                "search_criteria": {},
                "conversation_id": conversation_id,
                "turn_id": turn_id,
            }
        finally:
            _background_jobs = list(_turn_background_jobs.get() or [])
            _turn_background_jobs.reset(_outbox_token)

    try:
        conversation_store.finalize_request_turn(
            user_id,
            turn_id,
            status="failed" if _terminal_failed else "completed",
            assistant_content=message,
            response_type=payload.get("response_type"),
            recommendations=recommendations,
            snapshot=_snapshot,
            background_jobs=([] if _terminal_failed else _background_jobs),
        )
    except Exception as e:
        logger.error(
            "search_direct.turn.persistence_failed",
            extra={
                "request_id": request_id,
                "conversation_id": conversation_id,
                "error_type": type(e).__name__,
            },
        )
        try:
            conversation_store.fail_turn(user_id, turn_id)
        finally:
            _session_store.clear(user_id, conversation_id)
        raise

    if not _terminal_failed and _background_jobs:
        try:
            _ensure_outbox_worker()
        except Exception as e:
            logger.error(
                "background_worker.start_failed",
                extra={"error_type": type(e).__name__},
            )

    if _terminal_failed:
        _session_store.clear(user_id, conversation_id)

    # Serialize BEFORE emitting — same ordering rule as /api/alex: a jsonify() failure
    # must reach the 500 handler with canary_emitted still False so the boundary
    # record is the one and only record, and it reports the 500 the user actually got.
    response = jsonify(payload)
    response.status_code = 500 if _serialization_failed else (502 if _terminal_failed else 200)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Agent-Outcome"] = "error" if _terminal_failed else "ok"
    # This path is LLM-free, so a hit here would be genuinely surprising — which is
    # the reason to check rather than to assume.
    response = _dsml_boundary_check(response, payload)

    # Canary: this deterministic form path bypasses the LLM graph, so the fc-side signals
    # never apply — the record carries the process constants + arch and the fc fields default.
    # Tagged as the search_direct endpoint so the gate can exclude it: this path is
    # deterministic and LLM-free, so folding its latency/zero fc-signals into the
    # agent A/B would dilute both sides.
    _emit_canary_turn(
        endpoint=ENDPOINT_SEARCH_DIRECT,
        conversation_id=conversation_id, user_id=user_id, request_id=request_id,
        http_status=response.status_code,
        turn_outcome=(OUTCOME_SERVER_ERROR if _serialization_failed else
                      OUTCOME_AGENT_ERROR if payload.get("response_type") == "error"
                      else OUTCOME_OK),
        turn_latency_ms=(time.perf_counter() - _turn_started_ms) * 1000.0,
        fc_signals=search_direct_signals())

    return response


# ============================================================================
# Conversations CRUD (all state scoped to the resolved user_id)
# ============================================================================

@app.route('/api/conversations', methods=['GET'])
def list_conversations():
    """List the resolved user's conversations, newest-updated first."""
    user_id, _ = _identity_from_request()
    return jsonify({"conversations": conversation_store.list_conversations(user_id)})


@app.route('/api/conversations', methods=['POST'])
def create_conversation():
    """Create a new conversation. Body: {user_id, title?}."""
    data = get_json_or_400()
    user_id, _ = resolve_identity(data)
    # Stamp architecture provenance at creation (this process's constants).
    conv = conversation_store.create_conversation(
        user_id, title=data.get('title'),
        agent_arch=AGENT_ARCH, agent_version=APP_CANDIDATE_SHA, strict=DEEPSEEK_STRICT)
    return jsonify({"conversation": conv}), 201


@app.route('/api/conversations/<cid>', methods=['PATCH'])
def rename_conversation(cid):
    """Rename a conversation. Body: {user_id, title}. 404 if not owned by this user."""
    data = get_json_or_400()
    user_id, _ = resolve_identity(data)
    title = data.get('title')
    if not isinstance(title, str) or not title.strip():
        raise ApiError(400, "title is required")
    conv = conversation_store.rename_conversation(user_id, cid, title.strip())
    if conv is None:
        raise ApiError(404, "conversation not found")
    return jsonify(conv)


@app.route('/api/conversations/<cid>', methods=['DELETE'])
def delete_conversation(cid):
    """Delete a conversation + its messages, hot-cache slice, and checkpointer thread.
    Does NOT touch long-term (SQLite) memory. 404 if not owned."""
    user_id, _ = _identity_from_request()
    if not conversation_store.delete_conversation(user_id, cid):
        raise ApiError(404, "conversation not found")
    _session_store.clear(user_id, cid)
    _delete_checkpoint_thread(user_id, cid)
    return jsonify({"deleted": True})


@app.route('/api/conversations/<cid>/messages', methods=['GET'])
def get_conversation_messages(cid):
    """Full persisted transcript in chronological order. Each message carries
    role/content/timestamp/turn_id[/response_type/recommendations]; turn_id is present on
    BOTH user and assistant rows (null for legacy pre-turns rows) so the frontend can branch
    off a user message's turn. 404 if the conversation isn't owned by this user."""
    user_id, _ = _identity_from_request()
    if conversation_store.get_conversation(user_id, cid) is None:
        raise ApiError(404, "conversation not found")
    return jsonify({"messages": conversation_store.get_messages(user_id, cid)})


# fork_conversation errors → (http status, stable error code, client message). Returned
# directly (not via ApiError) so the fork response can carry a stable "code" field without
# altering the global ApiError JSON shape used by every other route.
_FORK_ERROR_MAP = (
    (ConversationNotFound, 404, "conversation_not_found", "conversation not found"),
    (NoCompletedTurn, 400, "no_completed_turn", "no completed turn to fork from"),
    (TurnNotFound, 400, "turn_not_found", "turn not found"),
    (TurnNotInConversation, 400, "turn_not_in_conversation",
     "turn does not belong to this conversation"),
    (TurnNotCompleted, 400, "turn_not_completed", "turn is not completed"),
)


@app.route('/api/conversations/<cid>/fork', methods=['POST'])
def fork_conversation(cid):
    """Branch a NEW conversation from a completed turn of <cid>. It inherits all context
    up to and including that turn; afterwards parent and child are fully independent.

    Body (all optional): {after_turn_id?, title?, idempotency_key?}. Header 'Idempotency-Key'
    takes precedence over the body key. after_turn_id omitted → the latest completed turn.
    Returns {"conversation": {...}, "idempotent": bool}: 201 on create, 200 on an idempotent
    replay. Fork validation failures return {"error", "code"} at 404/400 (see _FORK_ERROR_MAP).
    """
    data = _request_body() or {}
    user_id, _ = resolve_identity(data)

    after_turn_id = data.get('after_turn_id')
    if after_turn_id is not None and (not isinstance(after_turn_id, str) or not after_turn_id.strip()):
        raise ApiError(400, "after_turn_id must be a string")
    title = data.get('title')
    if title is not None and not isinstance(title, str):
        raise ApiError(400, "title must be a string")
    try:
        idem = request.headers.get('Idempotency-Key')
    except Exception:
        idem = None
    if not idem:
        _body_idem = data.get('idempotency_key')
        idem = _body_idem if isinstance(_body_idem, str) and _body_idem.strip() else None

    try:
        child = conversation_store.fork_conversation(
            user_id, cid, after_turn_id=(after_turn_id.strip() if after_turn_id else None),
            title=title, idempotency_key=idem)
    except Exception as e:
        for exc_type, status, code, msg in _FORK_ERROR_MAP:
            if isinstance(e, exc_type):
                return jsonify({"error": msg, "code": code}), status
        raise  # non-fork error → global handler (500)

    idempotent = bool(child.pop("idempotent", False))
    # The store also mirrors forked_from_turn_id at the top level of the returned dict; it is
    # already part of the conversation dict shape, so nothing extra to strip.
    return jsonify({"conversation": child, "idempotent": idempotent}), (200 if idempotent else 201)


@app.route('/api/conversations/<cid>/edit_turn', methods=['POST'])
def edit_turn(cid):
    """Branch a NEW conversation for a ChatGPT-style message edit: the user wants to rewrite
    the user message of turn ``turn_id`` and resend it. The branch inherits everything BEFORE
    ``turn_id`` (exclusive); the source conversation is untouched. Editing the first turn
    yields a zero-inheritance branch (lineage preserved, no turns copied).

    Body: {user_id?, turn_id}. Header 'Idempotency-Key' (or body 'idempotency_key') makes a
    retried request return the same branch. This endpoint ONLY creates the branch — the client
    then POSTs the rewritten message to /api/alex against the returned conversation_id.

    Returns {"conversation": {...full dict incl. parent/root/branch_depth + fork_reason +
    edited_slot_turn_id...}, "idempotent": bool}: 201 on create, 200 on idempotent replay.
    Validation failures return {"error", "code"} at 404/400 (shared _FORK_ERROR_MAP)."""
    data = _request_body() or {}
    user_id, _ = resolve_identity(data)

    turn_id = data.get('turn_id')
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ApiError(400, "turn_id is required")
    title = data.get('title')
    if title is not None and not isinstance(title, str):
        raise ApiError(400, "title must be a string")
    try:
        idem = request.headers.get('Idempotency-Key')
    except Exception:
        idem = None
    if not idem:
        _body_idem = data.get('idempotency_key')
        idem = _body_idem if isinstance(_body_idem, str) and _body_idem.strip() else None

    try:
        child = conversation_store.branch_for_edit(
            user_id, cid, turn_id.strip(), title=title, idempotency_key=idem)
    except Exception as e:
        for exc_type, status, code, msg in _FORK_ERROR_MAP:
            if isinstance(e, exc_type):
                return jsonify({"error": msg, "code": code}), status
        raise  # non-fork error → global handler (500)

    idempotent = bool(child.pop("idempotent", False))
    return jsonify({"conversation": child, "idempotent": idempotent}), (200 if idempotent else 201)


@app.route('/api/conversations/<cid>/version_map', methods=['GET'])
def get_version_map(cid):
    """Version groups for the branch family (same root) that <cid> belongs to — the frontend
    uses this to decide which user bubbles show a `< k/n >` version switcher and where each
    version lives. Shape: {"version_groups": {"<slot_turn_id>": [{"conversation_id",
    "created_at", "title"} ... created_at ASC]}}. Only groups with >=2 versions are returned;
    no edits → {"version_groups": {}}. 404 if <cid> isn't owned by this user."""
    user_id, _ = _identity_from_request()
    vm = conversation_store.version_map(user_id, cid)
    if vm is None:
        raise ApiError(404, "conversation not found")
    return jsonify(vm)


@app.route('/api/conversations/<cid>/turns', methods=['GET'])
def list_conversation_turns(cid):
    """Turn history for a conversation (started_at ASC). Additive helper — the frontend
    forks off message turn_id, but this makes the lifecycle inspectable. 404 if not owned."""
    user_id, _ = _identity_from_request()
    if conversation_store.get_conversation(user_id, cid) is None:
        raise ApiError(404, "conversation not found")
    return jsonify({"turns": conversation_store.list_turns(user_id, cid)})


@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    """Conversation-scoped reset (NEVER touches SQLite long-term memory).
    Body {user_id, conversation_id?}: with a conversation_id clears just that conversation;
    without one clears ALL of the user's conversations. The frontend routes clearing through
    DELETE /api/conversations/<cid> instead, but this stays for API completeness."""
    data = get_json_or_400()
    cid = _validate_conversation_id(data)  # reject list/dict/non-string cid before sqlite
    user_id, _ = resolve_identity(data)
    if cid:
        # Verify ownership first — mirrors DELETE /api/conversations/<cid>. Clearing an
        # unowned/bogus cid used to return a misleading 200 {"success": true}; a
        # conversation the caller doesn't own is a 404, not a silent no-op success.
        if conversation_store.get_conversation(user_id, cid) is None:
            raise ApiError(404, "conversation not found")
        conversation_store.clear_conversation_messages(user_id, cid)
        _session_store.clear(user_id, cid)
        _delete_checkpoint_thread(user_id, cid)
    else:
        for c in conversation_store.delete_all_conversations(user_id):
            _delete_checkpoint_thread(user_id, c)
        _session_store.clear_user(user_id)
    logger.info(
        "conversation.history_cleared",
        extra={
            "user_id_hash": hash_user_id(user_id)[0],
            "scope": "conversation" if cid else "user",
        },
    )
    return jsonify({"success": True, "message": "Conversation history cleared"})


@app.route('/api/forget_me', methods=['POST'])
def forget_me():
    """PRIVACY: the ONLY route that wipes long-term memory. Body {user_id}.
    Erases the user's durable AgentMemory AND all conversations, messages, favorites,
    checkpointer threads, and hot-cache slices."""
    data = get_json_or_400()
    user_id, _ = resolve_identity(data)
    try:
        conversation_store.begin_privacy_erasure(user_id)
    except ConversationBusy as exc:
        response = jsonify({
            "forgotten": False,
            "status": "busy",
            "error": "a conversation turn is still running; retry after it finishes",
            "running_turn_id": exc.turn_id,
        })
        response.status_code = 409
        response.headers["Retry-After"] = str(exc.retry_after)
        return response
    except PrivacyErasureInProgress:
        response = jsonify({
            "forgotten": False,
            "status": "busy",
            "error": "privacy erasure is already in progress",
        })
        response.status_code = 409
        response.headers["Retry-After"] = "1"
        return response

    layers: dict[str, dict] = {}
    try:
        # Capture identifiers only; no message/favorite/memory content is read or logged.
        conversation_ids = [
            item["id"] for item in conversation_store.list_conversations(user_id)
        ]

        try:
            from rag.agent_memory import get_agent_memory
            memory = get_agent_memory()
            before_memory = memory.privacy_inventory(user_id)
            deleted_memory = memory.forget(user_id)
            after_memory = memory.privacy_inventory(user_id)
            memory_ok = after_memory["total"] == 0
            layers["memory"] = {
                "status": "deleted" if memory_ok else "failed",
                "deleted": int(deleted_memory),
                "before": before_memory,
                "after": after_memory,
            }
        except Exception as exc:
            memory_ok = False
            layers["memory"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "after": None,
            }

        checkpoint_results = [
            _delete_checkpoint_thread(user_id, cid)
            for cid in conversation_ids
        ]
        checkpoints_ok = all(
            result.get("status") in {"deleted", "disabled"}
            and result.get("residual") is False
            for result in checkpoint_results
        )
        layers["checkpoints"] = {
            "status": "deleted" if checkpoints_ok else "failed",
            "threads": len(conversation_ids),
            "results": checkpoint_results,
        }

        if memory_ok and checkpoints_ok:
            try:
                relational = conversation_store.delete_all_user_data(user_id)
                relational_ok = relational["after"]["total"] == 0
                layers["relational"] = {
                    "status": "deleted" if relational_ok else "failed",
                    "before": relational["before"],
                    "after": relational["after"],
                }
            except Exception as exc:
                relational_ok = False
                layers["relational"] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "after": None,
                }
        else:
            relational_ok = False
            retained = conversation_store.privacy_inventory(user_id)
            layers["relational"] = {
                "status": "retained_for_retry",
                "after": retained,
            }

        if memory_ok and checkpoints_ok and relational_ok:
            try:
                before_credentials = auth_store.privacy_inventory(user_id)
                deleted_credentials = auth_store.delete_user_id(user_id)
                after_credentials = auth_store.privacy_inventory(user_id)
                credentials_ok = after_credentials["total"] == 0
                layers["credentials"] = {
                    "status": "deleted" if credentials_ok else "failed",
                    "deleted": int(deleted_credentials),
                    "before": before_credentials,
                    "after": after_credentials,
                }
            except Exception as exc:
                credentials_ok = False
                layers["credentials"] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "after": None,
                }
        else:
            credentials_ok = False
            layers["credentials"] = {"status": "retained_for_retry"}

        if memory_ok and checkpoints_ok and relational_ok and credentials_ok:
            _session_store.clear_user(user_id)
            hot_after = _session_store.privacy_inventory(user_id)
            hot_ok = hot_after["session_slices"] == 0
            layers["hot_cache"] = {
                "status": "deleted" if hot_ok else "failed",
                "after": hot_after,
            }
        else:
            hot_ok = False
            layers["hot_cache"] = {"status": "retained_for_retry"}

        complete = (
            memory_ok and checkpoints_ok and relational_ok
            and credentials_ok and hot_ok
        )
        if complete:
            for key in (
                "authenticated", "auth_user_id", "username", "display_name", "user_id"
            ):
                session.pop(key, None)
        logger.info(
            "privacy.erasure.completed" if complete else "privacy.erasure.partial",
            extra={
                "user_id_hash": hash_user_id(user_id)[0],
                "layer_status": {
                    name: value.get("status") for name, value in layers.items()
                },
            },
        )
        response = jsonify({
            "forgotten": bool(complete),
            "status": "complete" if complete else "partial",
            "layers": layers,
        })
        response.status_code = 200 if complete else 503
        if not complete:
            response.headers["Retry-After"] = "5"
        return response
    finally:
        conversation_store.end_privacy_erasure(user_id)


# ============================================================================
# Favorites — per-USER, persisted to sqlite (survives restart), keyed on lowercase url
# ============================================================================

@app.route('/api/favorites', methods=['POST'])
def add_favorite():
    """Add/replace a favorite. Body is the full property dict (lowercase canonical keys)
    plus user_id. Stored VERBATIM (incl. geo_location) — no fields stripped."""
    data = get_json_or_400()
    user_id, _ = resolve_identity(data)
    # New frontend sends lowercase `url`; keep `URL` as a legacy fallback.
    url = data.get('url') or data.get('URL')
    if not url:
        raise ApiError(400, "Property URL is required")
    conversation_store.add_favorite(user_id, str(url), data)
    return jsonify({"success": True, "message": "Added to favorites"})


@app.route('/api/favorites', methods=['GET'])
def get_favorites_list():
    """Return all of the resolved user's saved properties (full stored dicts)."""
    user_id, _ = _identity_from_request()
    return jsonify({"favorites": conversation_store.list_favorites(user_id)})


@app.route('/api/favorites/<path:url>', methods=['DELETE'])
def remove_favorite(url):
    """Remove a favorite by (percent-decoded) url. Identity via header + ?user_id=."""
    user_id, _ = _identity_from_request()
    if conversation_store.remove_favorite(user_id, url):
        return jsonify({"success": True, "message": "Removed from favorites"})
    raise ApiError(404, "Property not found")


@app.route('/api/generate_map', methods=['POST'])
def generate_property_map():
    """
    Generate an interactive amenity map for a property
    
    Expected JSON body:
    {
        "address": "property address",
        "geo_location": "lat, lon" or {"lat": X, "lng": Y},
        "price": "£X pcm",
        "travel_time": "X min" or X (minutes)
    }
    
    Returns:
    HTML content of the interactive map or error
    """
    data = request.get_json()
    if not data or not data.get('address'):
        return jsonify({"error": "Property address is required"}), 400
    
    try:
        from core.amenity_map_generator import PropertyAmenityMapGenerator
        from core.maps_service import OverpassError

        print(f"\n{'='*60}")
        print(f"[MAP GEN] Generating amenity map; address_chars={len(str(data['address']))}")
        print(f"{'='*60}\n")

        # Initialize map generator
        generator = PropertyAmenityMapGenerator(radius_km=1.5)

        # Prepare property data
        property_data = {
            'Address': data['address'],
            'address': data['address'],
            'Price': data.get('price', 'N/A'),
            'price': data.get('price', 'N/A'),
            'travel_time_minutes': data.get('travel_time', 'N/A'),
            'travel_time': data.get('travel_time', 'N/A'),
            'geo_location': data.get('geo_location'),
            'coordinates': data.get('coordinates') or data.get('geo_location')
        }

        # Parse coordinates once
        coords = generator.parse_geo_location(data.get('geo_location'))
        if not coords:
            return jsonify({"error": "Invalid coordinates"}), 400

        lat, lon = coords

        # Fetch every amenity category in ONE cached, batched Overpass query.
        # OverpassError means the provider is down (all mirrors failed) -> render
        # the map with a visible "data unavailable" banner rather than a
        # silently-empty map. An empty dict of results is a legitimate "nothing
        # nearby" and is shown without a banner.
        print(f"  [MAP GEN] Querying nearby amenities (batched)...")
        amenities_unavailable = False
        try:
            amenities_data = generator.fetch_all_amenities(lat, lon)
        except OverpassError as e:
            print(f"  [WARN] Amenity provider unavailable; error_type={type(e).__name__}")
            amenities_data = {}
            amenities_unavailable = True

        # Generate map HTML
        print(f"\n  [MAP GEN] Generating interactive map...")
        map_html = generator.generate_map_html(
            property_data, amenities_data,
            amenities_unavailable=amenities_unavailable,
        )

        if map_html:
            print(f"  ✓ [MAP GEN] Map generated successfully\n")
            return map_html, 200, {'Content-Type': 'text/html; charset=utf-8'}
        else:
            return jsonify({"error": "Failed to generate map"}), 500

    except Exception as e:
        print(f"❌ Error generating map; error_type={type(e).__name__}")
        return jsonify({"error": "Map generation is temporarily unavailable"}), 500


if __name__ == '__main__':
    # 允许所有来源访问(用于公网访问)。端口可用 PORT 环境变量覆盖（默认 5001）。
    port = int(os.getenv("PORT", "5001"))
    app.run(debug=False, host='127.0.0.1', port=port, use_reloader=False)
