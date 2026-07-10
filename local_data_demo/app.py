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
from uk_rent_agent.web.session_store import SessionStore
from uk_rent_agent.web.conversation_store import ConversationStore
from uk_rent_agent.web.identity import (
    resolve_user_id, normalize_message, valid_user_id, InvalidUserId, InvalidMessage,
)
from uk_rent_agent.web.auth_store import (
    AuthStore, AuthError, InvalidUsername, WeakPassword, UsernameTaken,
)
from uk_rent_agent.config import Config
from uk_rent_agent.agent.persistence import get_sqlite_checkpointer, graph_config
from uk_rent_agent.observability import new_request_id, request_context
from core.data_loader import load_mock_properties_from_csv, load_properties
from rag.rag_coordinator import RAGCoordinator
from core.tool_system import create_tool_registry
from core.langgraph_agent import build_agent_graph, create_initial_state
from core.tools.search_properties import search_properties_impl

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
    SESSION_COOKIE_SECURE=_bool_env("SESSION_COOKIE_SECURE", False),
    PERMANENT_SESSION_LIFETIME=_runtime_config.session_ttl_seconds,
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
        try:
            if not sess.history:
                sess.history = conversation_store.rehydrate_history(
                    user_id, conversation_id, MAX_HISTORY_LENGTH)
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
    body_uid = data.get('user_id') if isinstance(data, dict) else None
    try:
        header_uid = request.headers.get('X-User-Id')
    except Exception:
        header_uid = None
    try:
        query_uid = request.args.get('user_id')
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
try:
    rag_coordinator = RAGCoordinator()
    print("✓ [STARTUP] RAGCoordinator initialized successfully")
except Exception as e:
    print(f"❌ FATAL ERROR during RAG initialization:")
    print(f"   Error type: {type(e).__name__}")
    print(f"   Error message: {str(e)}")
    import traceback
    traceback.print_exc()
    raise  # Re-raise to see full stack trace

print("[STARTUP] Loading properties (PROPERTY_SOURCE=%s)..." % _os.getenv("PROPERTY_SOURCE", "auto"))
all_properties = load_properties()
print(f"✓ [STARTUP] Loaded {len(all_properties)} properties")

# ✅ FIXED: 确保在建立索引前处理所有属性，添加 parsed_price
if all_properties:
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
        raise
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
    user_id, _session_id = resolve_identity(data)
    try:
        user_message = normalize_message(data.get('message'))
    except InvalidMessage as e:
        raise ApiError(400, str(e))

    context = data.get('context', {}) or {}
    is_continuation = data.get('is_continuation', False)

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

    # Persist the user turn up-front (survives a crash mid-generation).
    conversation_store.add_message(user_id, conversation_id, "user", user_message)

    request_id = new_request_id(request.headers.get("X-Request-Id"))
    try:
        # 所有请求都通过 ReAct Agent 处理
        with request_context(request_id, user_id):
            payload = await handle_with_react_agent(
                user_message, context, is_continuation, user_id, conversation_id, request_id
            )
    except Exception as e:
        print(f"❌ [ALEX] 错误: {e}")
        traceback.print_exc()
        payload = {
            "response_type": "error",
            "message": "抱歉，处理您的请求时出错了。请稍后再试。",
        }

    # conversation_id is echoed in EVERY response (incl. errors + implicit creation).
    payload["conversation_id"] = conversation_id

    # Persist the assistant reply (recommendations preserved verbatim for re-render).
    try:
        conversation_store.add_message(
            user_id, conversation_id, "assistant",
            payload.get("message", ""),
            response_type=payload.get("response_type"),
            recommendations=payload.get("recommendations"),
        )
    except Exception as e:
        print(f"[persist] assistant message failed: {e}")

    # Always HTTP 200: an agent-side "error" is a normal response_type the client renders,
    # and returning 200 lets the frontend adopt conversation_id + persist the turn even when
    # generation failed (a 500 would orphan the freshly-created conversation).
    response = jsonify(payload)
    response.headers["X-Request-Id"] = request_id
    return response


def _derive_title(message: str) -> str:
    """Human-friendly conversation title from the first user message (implicit creation)."""
    text = " ".join((message or "").split())
    if not text:
        return "New chat"
    return text[:40] + ("…" if len(text) > 40 else "")


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
        })

        prev_results_context += f"{i}. **{property_name}**\n"
        prev_results_context += f"   - Full Address: {addr}\n"
        prev_results_context += f"   - Price: {price}\n"
        prev_results_context += f"   - Commute: {travel}\n"
        if rec.get('bedrooms') not in (None, '', 'N/A'):
            prev_results_context += f"   - Bedrooms: {rec.get('bedrooms')}\n"
        if rec.get('property_type'):
            prev_results_context += f"   - Type: {rec.get('property_type')}\n"
        if rec.get('budget_status'):
            prev_results_context += f"   - Budget: {rec.get('budget_status')}\n"
        if rec.get('url'):
            prev_results_context += f"   - URL: {rec.get('url')}\n"
        prev_results_context += "\n"
    return prev_results_context, structured_results


def _write_back_turn(user_id, conversation_id, user_message, assistant_text,
                     recommendations, *, user_preferences=_UNSET,
                     accumulated_search_criteria=_UNSET, criteria_overwrite=None):
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
            del _sess.history[:-MAX_HISTORY_LENGTH]
        if recommendations:
            _sess.last_results = recommendations
            _sess.persistent_state.setdefault('extracted_context', {})
            _sess.persistent_state['extracted_context']['previous_search_results'] = prev_results_context
            _sess.persistent_state['extracted_context']['last_results'] = structured_results
            print(f"[state] 💾 已保存 {len(recommendations)} 个搜索结果到上下文")
    return prev_results_context, structured_results


async def handle_with_react_agent(user_message: str, context: dict, is_continuation: bool,
                                  user_id: str = "default", conversation_id: str = "default",
                                  request_id: str | None = None):
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
        agent_graph = build_agent_graph(agent_tool_provider, checkpointer=checkpointer)
        print("[LangGraph] ✓ LangGraph agent 编译完成")

    # ── 构建本轮 extracted_context ──────────────────────────────
    extracted_context = dict(persistent_snapshot.get('extracted_context', {}))

    # 如果有 property context，设置到 extracted_context 中并从数据库获取详细信息
    if context and context.get('property'):
        property_info = context['property']
        property_address = property_info.get('address', '')

        extracted_context['property_address'] = property_address
        extracted_context['property_price'] = property_info.get('price')
        extracted_context['property_travel_time'] = property_info.get('travel_time')

        print(f"[LangGraph] 📍 已设置 property context: {property_address}")
        print(f"[LangGraph] 🔍 正在从数据库获取房产详细信息...")

        # 在 all_properties 中查找匹配的房产
        matched_property = None
        for prop in all_properties:
            if prop.get('Address', '').lower().strip() == property_address.lower().strip():
                matched_property = prop
                break
            if property_address.lower() in prop.get('Address', '').lower() or prop.get('Address', '').lower() in property_address.lower():
                matched_property = prop
                break

        if matched_property:
            print(f"[LangGraph] ✅ 找到匹配房产，加载详细信息")
            extracted_context['room_type'] = matched_property.get('Room_Type_Category', '')
            extracted_context['amenities'] = matched_property.get('Detailed_Amenities', '')
            extracted_context['guest_policy'] = matched_property.get('Guest_Policy', '')
            extracted_context['payment_rules'] = matched_property.get('Payment_Rules', '')
            extracted_context['excluded_features'] = matched_property.get('Excluded_Features', '')
            extracted_context['description'] = matched_property.get('Description', '')
            extracted_context['enhanced_description'] = matched_property.get('Enhanced_Description', '')
            extracted_context['property_url'] = matched_property.get('URL', '')
            print(f"[LangGraph] 🔗 房产 URL: {matched_property.get('URL', 'N/A')}")
        else:
            print(f"[LangGraph] ⚠️ 未在数据库中找到匹配房产: {property_address}")

    # ── 检测对比查询 ─────────────────────────────────────────────
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

    # ── 构建包含历史的查询 ───────────────────────────────────────
    query_with_history = user_message
    has_property_context = bool(extracted_context.get('property_address'))

    if has_property_context:
        print(f"[LangGraph] 📍 用户正在询问关于特定房产的问题，将使用房产上下文回答")
    elif history_snapshot:
        last_response = history_snapshot[-1].get('assistant', '') if history_snapshot else ''
        is_clarification_answer = any(q in last_response.lower() for q in [
            'what is your', 'could you tell me', 'what\'s the maximum',
            'please provide', 'how many', 'which area', '?'
        ])

        if is_clarification_answer and len(user_message.split()) <= 5:
            print(f"[LangGraph] 🔄 检测到澄清回复，保持完整上下文")
            history_text = "\n".join([
                f"User: {h['user']}\nAlex: {h['assistant']}"
                for h in history_snapshot[-5:]
            ])
            query_with_history = f"""Previous conversation (IMPORTANT - user is answering a clarification question):
{history_text}

User's answer to the clarification question: {user_message}

INSTRUCTIONS: The user just answered your clarification question. Use their answer to complete the ORIGINAL request. Do NOT ask more questions about the same thing. Do NOT treat their answer as a confusing new command."""
        else:
            history_text = "\n".join([
                f"User: {h['user']}\nAlex: {h['assistant']}"
                for h in history_snapshot[-3:]
            ])
            query_with_history = f"""Previous conversation:
{history_text}

Current user message: {user_message}"""

    # ── 注入长期记忆（Generative-Agents 评分检索: relevance+recency+importance）──
    try:
        from rag.agent_memory import get_agent_memory
        _am = get_agent_memory()
        # Long-term memory is per-USER (shared across the user's conversations), so it is
        # namespaced by user_id — NOT by conversation_id.
        _mems = _am.retrieve(user_message, session_id=user_id, user_id=user_id, n=6)
        _mem_block = _am.format_for_prompt(_mems)
        if _mem_block:
            query_with_history = f"{_mem_block}\n\n{query_with_history}"
            print(f"[Memory] 🧠 注入 {len(_mems)} 条相关记忆")
    except Exception as _e:
        print(f"[Memory] retrieve skipped: {_e}")

    # 原始当前消息（不含记忆/历史前缀）——供工具做"仅基于本条消息"的解析
    # (预算/通勤正则、postcode/序数解析)，避免误抓注入记忆里的旧值。
    extracted_context['current_message'] = user_message

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
    final_state = await agent_graph.ainvoke(
        initial_state,
        config=graph_config(user_id, conversation_id, request_id=request_id),
    )
    print(f"[LangGraph] ✓ 完成!")

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

def _coerce_optional_int(value, field_name):
    """Coerce an optional numeric criterion to a non-negative int, or None when absent.
    Rejects non-numeric / negative values with ApiError(400)."""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field_name} must be an integer")
    if n < 0:
        raise ApiError(400, f"{field_name} must not be negative")
    return n


def _coerce_bool(value) -> bool:
    """Coerce a JSON bool (or a common truthy string) to a real bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _compose_search_line(area, max_budget, budget_period, bedrooms,
                         no_commute, commute_destination, max_commute_time) -> str:
    """A compact, language-neutral one-liner describing a direct search — reused as the
    conversation title, the persisted user turn, and the tool's user_query."""
    parts = [f"🔍 Search: {area}"]
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
    return " | ".join(parts)


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
    user_id, _session_id = resolve_identity(data)

    criteria = data.get('criteria')
    if not isinstance(criteria, dict):
        raise ApiError(400, "criteria must be an object")

    area = criteria.get('area')
    if not isinstance(area, str) or not area.strip():
        raise ApiError(400, "area is required")
    area = area.strip()

    max_budget = _coerce_optional_int(criteria.get('max_budget'), "max_budget")
    bedrooms = _coerce_optional_int(criteria.get('bedrooms'), "bedrooms")
    max_commute_time = _coerce_optional_int(criteria.get('max_commute_time'), "max_commute_time")
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

    # no_commute is authoritative: drop any commute constraint from the TOOL call (the
    # raw commute_destination is still mirrored into the accumulated criteria below).
    if no_commute:
        max_commute_time = None
    tool_commute_destination = None if no_commute else commute_destination

    readable = _compose_search_line(
        area, max_budget, budget_period, bedrooms,
        no_commute, commute_destination, max_commute_time)

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

    # Persist the user turn up-front (survives a crash mid-search).
    conversation_store.add_message(user_id, conversation_id, "user", readable)

    request_id = new_request_id(request.headers.get("X-Request-Id"))
    # --- call the search tool DIRECTLY (no LangGraph / critic / memory) ---------
    try:
        with request_context(request_id, user_id):
            result = await search_properties_impl(
                user_query=readable,
                area=area,
                commute_destination=tool_commute_destination,
                max_budget=max_budget,
                max_commute_time=max_commute_time,
                no_commute=no_commute,
                bedrooms=bedrooms,
                budget_period=budget_period,
                room_type=room_type,
                # The panel Search button is an explicit user confirmation, so this path
                # BYPASSES the soft criteria gate (never returns a soft clarification).
                confirmed=True,
            )
        recommendations = result.get('recommendations') or []
        message = (result.get('summary') or result.get('message')
                   or (f"Found {len(recommendations)} matching properties." if recommendations
                       else "No matching properties found. Try widening your criteria."))
        payload = {
            "response_type": "search",
            "message": message,
            "recommendations": recommendations,
            "search_criteria": result.get('search_criteria') or {},
        }
    except Exception as e:
        # Same convention as /api/alex: a tool-side error is a normal response_type the
        # client renders, returned at HTTP 200 so the freshly-created conversation isn't
        # orphaned and the frontend can still adopt conversation_id.
        print(f"❌ [SEARCH_DIRECT] 错误: {e}")
        traceback.print_exc()
        recommendations = []
        message = "抱歉，搜索房源时出错了。请稍后再试。"
        payload = {
            "response_type": "error",
            "message": message,
            "recommendations": [],
            "search_criteria": {},
        }

    # conversation_id echoed in EVERY response (incl. errors + implicit creation).
    payload["conversation_id"] = conversation_id

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
            'commute_destination': commute_destination,
            'destination': commute_destination,   # legacy mirror consumed by older paths
            'max_budget': max_budget,
            'max_travel_time': max_commute_time,
            'no_commute': no_commute,
            'bedrooms': bedrooms,
            'budget_period': budget_period,
            'room_type': room_type,
        },
    )

    # Persist the assistant reply (recommendations preserved verbatim for re-render).
    try:
        conversation_store.add_message(
            user_id, conversation_id, "assistant", message,
            response_type=payload.get("response_type"),
            recommendations=recommendations,
        )
    except Exception as e:
        print(f"[persist] assistant message failed: {e}")

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


@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    """Conversation-scoped reset (NEVER touches ChromaDB long-term memory).
    Body {user_id, conversation_id?}: with a conversation_id clears just that conversation;
    without one clears ALL of the user's conversations. The frontend routes clearing through
    DELETE /api/conversations/<cid> instead, but this stays for API completeness."""
    data = get_json_or_400()
    user_id, _ = resolve_identity(data)
    cid = data.get('conversation_id')
    if cid:
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
        return jsonify({"error": f"Map generation failed: {str(e)}"}), 500


if __name__ == '__main__':
    # 允许所有来源访问(用于公网访问)。端口可用 PORT 环境变量覆盖（默认 5001）。
    port = int(os.getenv("PORT", "5001"))
    app.run(debug=False, host='127.0.0.1', port=port, use_reloader=False)
