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
from flask import Flask, request, jsonify, render_template, session
from flask_cors import CORS
from werkzeug.exceptions import HTTPException, BadRequest, UnsupportedMediaType
import json
import traceback
import re
from datetime import datetime
from uk_rent_agent.web.session_store import SessionStore
from uk_rent_agent.web.conversation_store import (
    ConversationStore, ConversationNotFound, NoCompletedTurn, TurnNotFound,
    TurnNotInConversation, TurnNotCompleted,
)
from uk_rent_agent.web.identity import (
    resolve_user_id, normalize_message, valid_user_id, InvalidUserId, InvalidMessage,
)
from uk_rent_agent.web.auth_store import (
    AuthStore, AuthError, InvalidUsername, WeakPassword, UsernameTaken,
)
from uk_rent_agent.config import Config
from uk_rent_agent.web.rate_limit import SlidingWindowRateLimiter
from uk_rent_agent.agent.persistence import get_sqlite_checkpointer, get_prefs_store, graph_config
from uk_rent_agent.observability import new_request_id, request_context
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
_runtime_config = Config.from_env()
# supports_credentials=True so the signed session cookie (which now carries the
# authenticated identity) survives cross-origin requests when the UI is opened over a
# different origin (e.g. file://). Same-origin (render_template at :5001) works regardless.
CORS(app, origins=list(_runtime_config.cors_origins), supports_credentials=True)
_api_rate_limiter = SlidingWindowRateLimiter()

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
print(f"[STARTUP] Auth store: {auth_store.path} "
      f"(require_auth={_runtime_config.require_auth})")

# 统一 UI 模式标志
USE_UNIFIED_UI = True  # 设置为 True 使用新的统一 Alex 界面

# LangGraph Agent — compiled graph (lazy-initialized)
agent_graph = None

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
print(f"[STARTUP] Conversation store: {conversation_store.db_path}")

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
            print(f"[rehydrate] snapshot skipped ({user_id}:{conversation_id}): {e}")
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
            print(f"[rehydrate] failed ({user_id}:{conversation_id}): {e}")
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
    traceback.print_exc()
    if _is_api_path():
        # Generic message only — never leak a traceback to the client.
        return jsonify({"error": "internal server error"}), 500
    raise e


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


def _delete_checkpoint_thread(user_id: str, conversation_id: str) -> None:
    """Drop a conversation's LangGraph checkpointer thread (best-effort)."""
    try:
        if _runtime_config.enable_checkpointer and _runtime_config.checkpoint_path:
            cp = get_sqlite_checkpointer(_runtime_config.checkpoint_path)
            if cp is not None:
                cp.delete_thread(f"{user_id}:{conversation_id}")
    except Exception as e:
        print(f"[checkpoint] delete_thread failed ({user_id}:{conversation_id}): {e}")

# --- Tool System Setup (从 fengyuan-agent 迁移) ---
print("[STARTUP] Initializing Tool System...")
try:
    tool_registry = create_tool_registry()
    print(f"✓ [STARTUP] Tool System initialized with {len(tool_registry.tools)} tools")
    
    # 🆕 设置 tool_registry 到 web_search，让它可以调用其他工具
    from core.tools.web_search import set_tool_registry
    set_tool_registry(tool_registry)
    
except Exception as e:
    print(f"⚠️  [STARTUP] Warning: Tool System initialization failed: {e}")
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
        print(f"⚠️  [STARTUP] MCP init failed ({_e}); using in-process tool registry")

# --- RAG Setup as per markdown ---
# Initialize the coordinator and build the index at startup
print("[STARTUP] Initializing RAG Coordinator...")
rag_coordinator = None
try:
    # Import lazily: optional embedding dependencies must not prevent the
    # deterministic listing search from serving real results.
    from rag.rag_coordinator import RAGCoordinator
    rag_coordinator = RAGCoordinator()
    print("✓ [STARTUP] RAGCoordinator initialized successfully")
except Exception as e:
    print(f"❌ FATAL ERROR during RAG initialization:")
    print(f"   Error type: {type(e).__name__}")
    print(f"   Error message: {str(e)}")
    import traceback
    traceback.print_exc()
    # RAG is optional. Search falls back to deterministic ranking.
    rag_coordinator = None

print("[STARTUP] Loading properties (PROPERTY_SOURCE=%s)..." % _os.getenv("PROPERTY_SOURCE", "auto"))
all_properties = load_properties()
print(f"✓ [STARTUP] Loaded {len(all_properties)} properties")

# ✅ FIXED: 确保在建立索引前处理所有属性，添加 parsed_price
if all_properties and rag_coordinator is not None:
    from core.data_loader import parse_price
    for prop in all_properties:
        if 'parsed_price' not in prop:
            prop['parsed_price'] = parse_price(prop.get('Price'))

if all_properties:
    print("[STARTUP] Building FAISS index for property embeddings... (This may take a moment)")
    try:
        rag_coordinator.property_store.build_index(all_properties)
        from core.tools.search_properties import set_rag_coordinator
        set_rag_coordinator(rag_coordinator)
        print("✓ [STARTUP] FAISS index built successfully. Starting server...")
    except Exception as e:
        print(f"❌ ERROR building FAISS index: {e}")
        import traceback
        traceback.print_exc()
        rag_coordinator = None
else:
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
        # Behind our own nginx (which binds the app to loopback and APPENDS the
        # real client IP as the last X-Forwarded-For entry). Trust exactly one
        # proxy hop: take the RIGHTMOST XFF value, not the leftmost. A guest can
        # forge leading XFF entries to rotate their rate-limit bucket, but cannot
        # forge the final entry our own nginx writes.
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
    print(f"[AUTH] registered + logged in: {view['username']} -> {view['user_id']}")
    return jsonify({"authenticated": True, **view})


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """Verify credentials and start a session. Body {username, password}."""
    data = get_json_or_400()
    view = auth_store.verify(data.get('username'), data.get('password'))
    if not view:
        raise ApiError(401, "invalid username or password")
    _establish_session(view)
    print(f"[AUTH] login: {view['username']} -> {view['user_id']}")
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

    # --- resolve / implicitly create the conversation --------------------------
    conversation_id = data.get('conversation_id')
    conv = conversation_store.get_conversation(user_id, conversation_id) if conversation_id else None
    if not conv:
        conv = conversation_store.create_conversation(user_id, title=_derive_title(user_message))
        conversation_id = conv['id']
        print(f"🆕 [ALEX] implicitly created conversation {conversation_id}")

    print(f"\n{'='*60}")
    print(f"🤖 [ALEX - LangGraph Agent] 收到消息: {user_message}")
    print(f"👤 [ALEX] user_id: {user_id} | 🧵 conversation_id: {conversation_id}")
    print(f"📋 [ALEX] is_continuation: {is_continuation}")
    print(f"📋 [ALEX] context: {context}")
    print(f"{'='*60}")

    request_id = new_request_id(request.headers.get("X-Request-Id"))

    # Persist the user turn up-front (survives a crash mid-generation) and open a turn.
    # begin_turn needs the user message's rowid, so the user row is written first; the turn
    # then spans this request. (The user row's turn_id column stays NULL — the frozen store
    # exposes no message-turn_id update and the turns table records user_message_id, which
    # is what fork/lineage rely on; the assistant row below carries the turn_id.)
    _user_msg = conversation_store.add_message(user_id, conversation_id, "user", user_message)
    turn = conversation_store.begin_turn(
        user_id, conversation_id, request_id=request_id,
        user_message_id=_user_msg.get("id"))
    turn_id = turn["id"]

    _turn_crashed = False
    try:
        # 所有请求都通过 ReAct Agent 处理
        with request_context(request_id, user_id):
            payload = await handle_with_react_agent(
                user_message, context, is_continuation, user_id, conversation_id, request_id,
                ui_language=ui_language, turn=turn,
            )
    except Exception as e:
        print(f"❌ [ALEX] 错误: {e}")
        traceback.print_exc()
        _turn_crashed = True
        # 错误文案也遵循回复语言策略（本条消息含中文→中文，否则跟随 UI 语言）。
        _err_zh = _resolve_reply_language(user_message, ui_language) == "zh"
        payload = {
            "response_type": "error",
            "message": ("抱歉，处理您的请求时出错了。请稍后再试。" if _err_zh
                        else "Sorry, something went wrong while handling your request. Please try again."),
        }

    # conversation_id + turn_id are echoed in EVERY response (incl. errors + implicit creation).
    payload["conversation_id"] = conversation_id
    payload["turn_id"] = turn_id

    # Persist the assistant reply (tagged with turn_id; recommendations preserved verbatim).
    _asst_msg_id = None
    try:
        _asst = conversation_store.add_message(
            user_id, conversation_id, "assistant",
            payload.get("message", ""),
            response_type=payload.get("response_type"),
            recommendations=payload.get("recommendations"),
            turn_id=turn_id,
        )
        _asst_msg_id = _asst.get("id")
    except Exception as e:
        print(f"[persist] assistant message failed: {e}")

    # Finalize the turn: an agent-side error (or a crash) fails the turn; a real answer
    # completes it and snapshots the post-turn context (built AFTER _write_back_turn ran
    # inside handle_with_react_agent). A failed turn is never a valid fork target.
    if _turn_crashed or payload.get("response_type") == "error":
        conversation_store.fail_turn(user_id, turn_id)
    else:
        conversation_store.complete_turn(user_id, turn_id, assistant_message_id=_asst_msg_id)
        _save_turn_snapshot_after_turn(user_id, conversation_id, turn_id)

    # Always HTTP 200: an agent-side "error" is a normal response_type the client renders,
    # and returning 200 lets the frontend adopt conversation_id + persist the turn even when
    # generation failed (a 500 would orphan the freshly-created conversation).
    response = jsonify(payload)
    response.headers["X-Request-Id"] = request_id
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
    url_key = payload_url.lower().strip()
    url_key_norm = url_key.rstrip('/')

    # ① URL 精确匹配 → ② 地址精确匹配（都对照完整 last_results 快照）
    session_hit = None
    if url_key:
        for rec in (last_results or []):
            if isinstance(rec, dict) and str(rec.get('url') or '').lower().strip() == url_key:
                session_hit = rec
                break
    if session_hit is None and addr_key:
        for rec in (last_results or []):
            if isinstance(rec, dict) and str(rec.get('address') or '').lower().strip() == addr_key:
                session_hit = rec
                break

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
        return ctx, 'session'

    # ②.5 累计推荐注册表命中（历史所有轮次的推荐，不只最近一轮）。URL 优先、地址次之精确匹配；
    #     命中后用注入的 cache_lookup 按 URL 取 sqlite 缓存里的完整房源（描述/设施/政策等大字段），
    #     缓存未命中则退回注册表轻量字段（地址/价格/通勤/区域/可入住日）。
    reg_hit = None
    if registry:
        if url_key_norm:
            for e in registry:
                if isinstance(e, dict) and str(e.get('url') or '').lower().strip().rstrip('/') == url_key_norm:
                    reg_hit = e
                    break
        if reg_hit is None and addr_key:
            for e in registry:
                if isinstance(e, dict) and str(e.get('address') or '').lower().strip() == addr_key:
                    reg_hit = e
                    break
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

    # ③ demo CSV 精确地址匹配（仅 ==；子串/模糊分支已删除）。
    if addr_key:
        for prop in (csv_properties or []):
            if str(prop.get('Address') or '').lower().strip() == addr_key:
                ctx['room_type'] = prop.get('Room_Type_Category', '')
                ctx['amenities'] = prop.get('Detailed_Amenities', '')
                ctx['guest_policy'] = prop.get('Guest_Policy', '')
                ctx['payment_rules'] = prop.get('Payment_Rules', '')
                ctx['excluded_features'] = prop.get('Excluded_Features', '')
                ctx['description'] = prop.get('Description', '')
                ctx['enhanced_description'] = prop.get('Enhanced_Description', '')
                ctx['property_url'] = prop.get('URL', '')
                return ctx, 'csv'

    return ctx, 'scalar'


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
        key = ('url', url.lower()) if url else ('address', address.lower())
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


def _registry_entry_key(url, address):
    """去重键：优先规范化后的 url（小写/去首尾空白/去尾斜杠），无 url 用规范化地址；都空 → None。"""
    u = str(url or '').strip().lower().rstrip('/')
    if u:
        return ('url', u)
    a = str(address or '').strip().lower()
    return ('address', a) if a else None


def _merge_recommended_registry(existing, recommendations, max_items=_REGISTRY_MAX_ENTRIES):
    """把本轮 recommendations merge 进累计注册表（纯函数，返回新列表，不改动入参）。

    去重：按 _registry_entry_key（url 优先、地址次之）；已存在的条目原样保留（首见 index 稳定）；
    新条目 index = 现有最大 index + 1（单调递增，不复用/不冲突）。达到 max_items 后不再追加新条目。"""
    registry = [dict(e) for e in (existing or []) if isinstance(e, dict)]
    seen = {}
    max_index = 0
    for e in registry:
        key = _registry_entry_key(e.get('url'), e.get('address'))
        if key is not None:
            seen[key] = e
        try:
            max_index = max(max_index, int(e.get('index', 0)))
        except (TypeError, ValueError):
            pass
    for rec in (recommendations or []):
        if not isinstance(rec, dict):
            continue
        key = _registry_entry_key(rec.get('url'), rec.get('address'))
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


def _save_turn_snapshot_after_turn(user_id, conversation_id, turn_id):
    """Build + persist the post-turn context snapshot AFTER _write_back_turn ran.

    Runs under the per-conversation turn lock so the read of _sess.persistent_state is
    consistent with the just-completed write-back. Best-effort — a snapshot failure must
    never turn a successful turn into an error (the durable transcript still persisted).

    context_revision: a monotonic per-conversation counter = previous snapshot's
    context_revision + 1 (starting at 1). complete_turn has already marked THIS turn
    completed but its snapshot row does not exist yet, so latest_snapshot() returns the
    PREVIOUS turn's snapshot — the revision keeps climbing across turns and across a fork
    (the child inherits the copied snapshots and continues from their revision).
    """
    try:
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
            snapshot = build_turn_snapshot(
                turn_id=turn_id,
                persistent_state=_sess.persistent_state,
                context_revision=context_revision,
            )
        conversation_store.save_turn_snapshot(user_id, conversation_id, turn_id, snapshot)
    except Exception as e:
        print(f"[snapshot] save skipped ({user_id}:{conversation_id}): {e}")


def _spawn_rolling_summary_update(user_id, conversation_id, dropped_turns,
                                  through_turn_id, reply_language):
    """Fold the turns just trimmed out of the hot history into the rolling summary on a
    daemon background thread (an LLM call — must never block or break the turn). The
    result is written under the turn lock into extracted_context so the NEXT turn's
    snapshot captures it. Any failure is swallowed (update_rolling_summary itself keeps
    the prior summary on error)."""
    def _run():
        try:
            lock = _session_store.turn_lock(user_id, conversation_id)
            with lock:
                _s = _get_session(user_id, conversation_id)
                prior = (_s.persistent_state.get("extracted_context") or {}).get("rolling_summary")
            new_summary = update_rolling_summary(
                _llm_complete, prior, dropped_turns, reply_language)
            with lock:
                _s = _get_session(user_id, conversation_id)
                ec = _s.persistent_state.setdefault("extracted_context", {})
                ec["rolling_summary"] = new_summary
                ec["rolling_summary_through_turn_id"] = through_turn_id
        except Exception as e:
            print(f"[summary] rolling update skipped ({user_id}:{conversation_id}): {e}")
    threading.Thread(target=_run, daemon=True).start()


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

    # 确保 tool_registry 已初始化
    if tool_registry is None:
        print("[LangGraph] tool_registry 为空，重新初始化...")
        tool_registry = create_tool_registry()

    # 选择工具提供方：优先 MCP 客户端，否则进程内 registry
    if agent_tool_provider is None:
        agent_tool_provider = tool_registry

    # 懒加载编译 LangGraph
    if agent_graph is None:
        print("[LangGraph] 首次请求，编译 LangGraph StateGraph...")
        checkpointer = None
        if _runtime_config.enable_checkpointer and _runtime_config.checkpoint_path:
            checkpointer = get_sqlite_checkpointer(_runtime_config.checkpoint_path)
        # Cross-thread Store + HITL are opt-in (default off): identical topology when disabled.
        store = get_prefs_store() if _runtime_config.enable_store else None
        agent_graph = build_agent_graph(
            agent_tool_provider,
            checkpointer=checkpointer,
            store=store,
            enable_hitl=_runtime_config.enable_hitl,
        )
        print("[LangGraph] ✓ LangGraph agent 编译完成")

    # ── 构建本轮 extracted_context ──────────────────────────────
    extracted_context = dict(persistent_snapshot.get('extracted_context', {}))

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
        print(f"[LangGraph] 📍 Ask-AI 聚焦栈 {len(focus_records)} 项，当前聚焦 [{focus_source}]: "
              f"{extracted_context.get('property_address')}")

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

    comparison_keywords = ['compare', 'vs', 'versus', 'between', 'or', 'better', 'which one', 'deciding between']
    is_comparison_query = any(kw in user_message.lower() for kw in comparison_keywords)

    if is_comparison_query:
        print(f"[LangGraph] 🔄 检测到对比查询，正在加载房产数据...")
        mentioned_properties = []
        for prop in all_properties:
            prop_name = prop.get('Address', '').split(',')[0].strip().lower()
            name_words = prop_name.split()
            for word in name_words:
                if len(word) > 3 and word.lower() in user_message.lower():
                    mentioned_properties.append(prop)
                    print(f"[LangGraph] ✅ 找到提及的房产: {prop.get('Address', '')[:50]}")
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
        print(f"[Memory] retrieve skipped: {_e}")

    # 装配最终查询：历史分支选择 / 记忆前缀 / 滚动摘要插入 / token 预算裁剪都由 assemble 负责，
    # 无裁剪时与旧的手拼字符串逐字节一致。滚动摘要取自本会话 extracted_context（后台线程写入）。
    query_with_history = assemble_context(
        user_message=user_message,
        history=history_snapshot,
        memory_block=_mem_block,
        has_property_context=has_property_context,
        rolling_summary=(persistent_snapshot.get('extracted_context') or {}).get('rolling_summary'),
    )

    # 原始当前消息（不含记忆/历史前缀）——供工具做"仅基于本条消息"的解析
    # (预算/通勤正则、postcode/序数解析)，避免误抓注入记忆里的旧值。
    extracted_context['current_message'] = user_message
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
    )

    # ── Phase 2: the slow LLM call — NO turn lock held here ──────
    print(f"[LangGraph] ▶ 开始执行 graph.ainvoke() ...")
    import time as _eval_time
    _eval_turn_started = _eval_time.perf_counter()
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
            _snap = await agent_graph.aget_state(_graph_cfg)
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
    final_state = await agent_graph.ainvoke(
        graph_input,
        config=_graph_cfg,
    )
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

    print(f"[LangGraph] Response Type: {response_type}")

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

    # ── 写入长期记忆（后台线程: Mem0 式抽取+整合 / GA 反思，不阻塞响应）──
    # 记忆按 user_id 命名空间共享（跨会话），故 session_id 传 user_id。
    try:
        from rag.agent_memory import get_agent_memory
        _td = final_state.get('tool_decision')
        _tool_used = _td.get('tool') if isinstance(_td, dict) else None
        get_agent_memory().remember_turn_async(
            user_message, response_text,
            session_id=user_id, user_id=user_id, tool_used=_tool_used,
            idempotency_key=f"turn:{request_id}" if request_id else None,
            conversation_id=conversation_id,
            turn_id=(turn.get("id") if isinstance(turn, dict) else None),
            turn_started_at=(turn.get("started_at") if isinstance(turn, dict) else None),
        )
    except Exception as _e:
        print(f"[Memory] write skipped: {_e}")

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

    # --- resolve / implicitly create the conversation (mirrors /api/alex) -------
    conversation_id = data.get('conversation_id')
    conv = conversation_store.get_conversation(user_id, conversation_id) if conversation_id else None
    if not conv:
        conv = conversation_store.create_conversation(user_id, title=_derive_title(readable))
        conversation_id = conv['id']
        print(f"🆕 [SEARCH_DIRECT] implicitly created conversation {conversation_id}")

    print(f"\n{'='*60}")
    print(f"🔍 [SEARCH_DIRECT] {readable}")
    print(f"👤 [SEARCH_DIRECT] user_id: {user_id} | 🧵 conversation_id: {conversation_id}")
    print(f"{'='*60}")

    request_id = new_request_id(request.headers.get("X-Request-Id"))

    # Persist the user turn up-front (survives a crash mid-search) and open a turn spanning
    # this request. (See /api/alex: the user row's turn_id stays NULL; the turns table holds
    # user_message_id and the assistant row below carries the turn_id.)
    _user_msg = conversation_store.add_message(user_id, conversation_id, "user", readable)
    turn = conversation_store.begin_turn(
        user_id, conversation_id, request_id=request_id,
        user_message_id=_user_msg.get("id"))
    turn_id = turn["id"]

    # --- call the search tool DIRECTLY (no LangGraph / critic / memory) ---------
    try:
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
            )
        if _search_result_failed(result):
            # The tool returns structured failures instead of raising. Do not turn a
            # provider/RAG failure into the misleading "no matching properties" state.
            raise RuntimeError((result or {}).get('error', 'property search failed'))
        recommendations = result.get('recommendations') or []
        # 工具已按 reply_language 本地化 summary；仅兜底文案由本端点自己本地化。
        if recommendations:
            _fallback = (f"为你找到 {len(recommendations)} 套匹配房源。" if reply_language == 'zh'
                         else f"Found {len(recommendations)} matching properties.")
        else:
            _fallback = ("没有找到符合条件的房源，试着放宽搜索条件。" if reply_language == 'zh'
                         else "No matching properties found. Try widening your criteria.")
        message = result.get('summary') or result.get('message') or _fallback
        payload = {
            "response_type": "search",
            "message": message,
            "recommendations": recommendations,
            "search_criteria": result.get('search_criteria') or {},
            # 🆕 目的地附近"已验证的推荐居住区"，前端渲染为可点击 chips → 一键多区域再搜。
            "area_recommendations": result.get('area_recommendations') or [],
        }
    except Exception as e:
        # Same convention as /api/alex: a tool-side error is a normal response_type the
        # client renders, returned at HTTP 200 so the freshly-created conversation isn't
        # orphaned and the frontend can still adopt conversation_id.
        print(f"❌ [SEARCH_DIRECT] 错误: {e}")
        traceback.print_exc()
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

    # --- L2 write-back — SAME helper the ReAct path uses (phase 3) --------------
    # A form submit is authoritative, so OVERWRITE the scalar accumulated criteria (the
    # list fields are kept as-is by _write_back_turn). user_preferences is left untouched
    # (no LLM preference extraction on this path). Deliberately NO remember_turn_async:
    # deterministic form input is not a conversational signal worth writing to long-term
    # memory (unlike a chat turn on /api/alex).
    _write_back_turn(
        user_id, conversation_id, readable, message, recommendations,
        criteria_overwrite={
            'area': area,
            'areas': areas,   # 🆕 多区域：持久化提交的区域集合
            'commute_destination': commute_destination,
            'destination': commute_destination,   # legacy mirror consumed by older paths
            'max_budget': max_budget,
            'max_travel_time': max_commute_time,
            'no_commute': no_commute,
            'bedrooms': bedrooms,
            'budget_period': budget_period,
            'room_type': room_type,
            'move_in_date': move_in_date,   # 🆕 期望入住日持久化（表单值跨轮保留）
        },
        turn_id=turn_id,
        reply_language=reply_language,
    )

    # Persist the assistant reply (tagged with turn_id; recommendations preserved verbatim).
    _asst_msg_id = None
    try:
        _asst = conversation_store.add_message(
            user_id, conversation_id, "assistant", message,
            response_type=payload.get("response_type"),
            recommendations=recommendations,
            turn_id=turn_id,
        )
        _asst_msg_id = _asst.get("id")
    except Exception as e:
        print(f"[persist] assistant message failed: {e}")

    # Finalize the turn: a tool-side error fails it; a real search completes it and
    # snapshots the post-turn context (built AFTER _write_back_turn cached the criteria).
    if payload.get("response_type") == "error":
        conversation_store.fail_turn(user_id, turn_id)
    else:
        conversation_store.complete_turn(user_id, turn_id, assistant_message_id=_asst_msg_id)
        _save_turn_snapshot_after_turn(user_id, conversation_id, turn_id)

    response = jsonify(payload)
    response.headers["X-Request-Id"] = request_id
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
    conv = conversation_store.create_conversation(user_id, title=data.get('title'))
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
    Does NOT touch long-term (ChromaDB) memory. 404 if not owned."""
    user_id, _ = _identity_from_request()
    if not conversation_store.delete_conversation(user_id, cid):
        raise ApiError(404, "conversation not found")
    _session_store.clear(user_id, cid)
    _delete_checkpoint_thread(user_id, cid)
    return jsonify({"deleted": True})


@app.route('/api/conversations/<cid>/messages', methods=['GET'])
def get_conversation_messages(cid):
    """Full persisted transcript (role/content/timestamp[/response_type/recommendations])
    in chronological order. 404 if the conversation isn't owned by this user."""
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
    """Conversation-scoped reset (NEVER touches ChromaDB long-term memory).
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
    print(f"[ALEX] 对话历史已清除 (user_id={user_id}, conversation_id={cid})")
    return jsonify({"success": True, "message": "Conversation history cleared"})


@app.route('/api/forget_me', methods=['POST'])
def forget_me():
    """PRIVACY: the ONLY route that wipes long-term memory. Body {user_id}.
    Erases the user's ChromaDB/Mem0 memory AND all conversations, messages, favorites,
    checkpointer threads, and hot-cache slices."""
    data = get_json_or_400()
    user_id, _ = resolve_identity(data)

    # 1) long-term memory (ChromaDB, namespaced by user_id)
    try:
        from rag.agent_memory import get_agent_memory
        wiped = get_agent_memory().forget(user_id)
        print(f"[forget_me] wiped {wiped} memory records for user_id={user_id}")
    except Exception as e:
        print(f"[forget_me] memory wipe skipped: {e}")

    # 2) conversations + messages (+ checkpointer threads) + favorites + hot cache
    for c in conversation_store.delete_all_conversations(user_id):
        _delete_checkpoint_thread(user_id, c)
    conversation_store.delete_all_favorites(user_id)
    _session_store.clear_user(user_id)

    return jsonify({"forgotten": True})


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
        print(f"[MAP GEN] Generating amenity map for: {data['address']}")
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
            print(f"  [WARN] Amenity provider unavailable: {e}")
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
        print(f"❌ Error generating map: {e}")
        traceback.print_exc()
        return jsonify({"error": "Map generation is temporarily unavailable"}), 500


if __name__ == '__main__':
    # 允许所有来源访问(用于公网访问)。端口可用 PORT 环境变量覆盖（默认 5001）。
    port = int(os.getenv("PORT", "5001"))
    app.run(debug=False, host='127.0.0.1', port=port, use_reloader=False)
