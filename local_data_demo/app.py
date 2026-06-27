# app.py - Enhanced with RAG and LangGraph Agent Framework

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import asyncio
import uuid
import copy
import threading
from flask import Flask, request, jsonify, render_template, session
from flask_cors import CORS
import json
import traceback
import re
from core.user_session import add_to_favorites, get_favorites, _session_data
from core.data_loader import load_mock_properties_from_csv, load_properties
from rag.rag_coordinator import RAGCoordinator
from core.tool_system import create_tool_registry
from core.langgraph_agent import build_agent_graph, create_initial_state

app = Flask(__name__, template_folder='.')
CORS(app)

# Secret key — needed for the server-side `session` cookie used as a per-browser
# identity fallback (priority (c) in resolve_identity). Read from env first so a real
# deployment secret is never clobbered; otherwise use a stable dev secret so cookies
# survive across requests (a random per-boot key would break single-user continuity).
if not app.secret_key:
    import os as _os_secret
    app.secret_key = _os_secret.environ.get(
        "FLASK_SECRET_KEY", "uk-rent-dev-secret-key-do-not-use-in-prod"
    )

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
_user_states = {}        # user_id -> persistent cross-turn state dict
_user_histories = {}     # user_id -> conversation history list
_user_last_results = {}  # user_id -> last search results list
_user_stores_lock = threading.Lock()  # guards lazy slice creation only

MAX_HISTORY_LENGTH = 10  # 保留最近10轮对话


def _get_user_state(user_id):
    """Lazily initialise and return this user's persistent state slice."""
    s = _user_states.get(user_id)
    if s is None:
        with _user_stores_lock:
            s = _user_states.setdefault(user_id, _default_persistent_state())
    return s


def _get_user_history(user_id):
    """Lazily initialise and return this user's conversation history list."""
    h = _user_histories.get(user_id)
    if h is None:
        with _user_stores_lock:
            h = _user_histories.setdefault(user_id, [])
    return h


def _get_user_last_results(user_id):
    """Lazily initialise and return this user's last search results list."""
    r = _user_last_results.get(user_id)
    if r is None:
        with _user_stores_lock:
            r = _user_last_results.setdefault(user_id, [])
    return r


def resolve_identity(data=None):
    """Resolve a per-request (user_id, session_id) for L2 + L3 isolation.

    Priority:
      (a) explicit 'user_id' in the POST JSON body
      (b) 'X-User-Id' request header
      (c) a Flask server-side session cookie holding a generated UUID
          (auto-isolates different browsers without any client changes)
      (d) 'default'  (final fallback — preserves legacy single-user behaviour)

    session_id mirrors user_id (the app has no separate per-conversation id);
    user_id is the isolation axis for both conversational state and ChromaDB memory.
    """
    uid = None
    if isinstance(data, dict):
        uid = data.get('user_id')
    if not uid:
        try:
            uid = request.headers.get('X-User-Id')
        except Exception:
            uid = None
    if not uid:
        try:
            uid = session.get('user_id')
            if not uid:
                uid = uuid.uuid4().hex
                session['user_id'] = uid
        except Exception:
            uid = None
    uid = (str(uid).strip() if uid else '') or 'default'
    return uid, uid

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
if _os.environ.get("USE_MCP_TOOLS", "1").lower() not in ("0", "false", "no"):
    try:
        import sys as _sys
        from core.mcp_client import MCPToolClient
        _mcp_client = MCPToolClient(
            command=_sys.executable,
            args=["mcp_server.py"],
            cwd=_os.path.dirname(_os.path.abspath(__file__)),
            fallback_registry=tool_registry,
        ).start()
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
    data = request.get_json()
    if not data or not data.get('message'):
        return jsonify({"error": "Message is required"}), 400

    user_message = data.get('message')
    context = data.get('context', {})
    is_continuation = data.get('is_continuation', False)

    # Resolve per-request identity (body user_id > X-User-Id header > session cookie > "default")
    user_id, session_id = resolve_identity(data)

    print(f"\n{'='*60}")
    print(f"🤖 [ALEX - LangGraph Agent] 收到消息: {user_message}")
    print(f"👤 [ALEX] user_id: {user_id}")
    print(f"📋 [ALEX] is_continuation: {is_continuation}")
    print(f"📋 [ALEX] context: {context}")
    print(f"{'='*60}")

    try:
        # 所有请求都通过 ReAct Agent 处理
        return await handle_with_react_agent(user_message, context, is_continuation, user_id, session_id)
    
    except Exception as e:
        print(f"❌ [ALEX] 错误: {e}")
        traceback.print_exc()
        return jsonify({
            "response_type": "error",
            "message": "抱歉，处理您的请求时出错了。请稍后再试。"
        }), 500


async def handle_with_react_agent(user_message: str, context: dict, is_continuation: bool,
                                  user_id: str = "default", session_id: str = "default"):
    """
    使用 LangGraph Agent 处理所有用户请求 - 纯 LLM 驱动

    LangGraph Agent 会自主决定：
    1. 是否需要调用 search_properties 工具搜索房源
    2. 是否需要调用其他工具（安全检查、通勤计算等）
    3. 或者直接回答用户问题

    没有任何关键词匹配 - 完全由 LLM 决策
    """
    global agent_graph, tool_registry, agent_tool_provider

    # ── Load THIS user's isolated L2 slices (no cross-user sharing) ──────────
    # Locals keep the legacy names so the existing in-place dict mutations of
    # `agent_persistent_state` continue to update the stored slice directly.
    agent_persistent_state = _get_user_state(user_id)
    conversation_history = _get_user_history(user_id)

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
        agent_graph = build_agent_graph(agent_tool_provider)
        print("[LangGraph] ✓ LangGraph agent 编译完成")

    # ── 构建本轮 extracted_context ──────────────────────────────
    extracted_context = dict(agent_persistent_state.get('extracted_context', {}))

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
    elif conversation_history:
        last_response = conversation_history[-1].get('assistant', '') if conversation_history else ''
        is_clarification_answer = any(q in last_response.lower() for q in [
            'what is your', 'could you tell me', 'what\'s the maximum',
            'please provide', 'how many', 'which area', '?'
        ])

        if is_clarification_answer and len(user_message.split()) <= 5:
            print(f"[LangGraph] 🔄 检测到澄清回复，保持完整上下文")
            history_text = "\n".join([
                f"User: {h['user']}\nAlex: {h['assistant']}"
                for h in conversation_history[-5:]
            ])
            query_with_history = f"""Previous conversation (IMPORTANT - user is answering a clarification question):
{history_text}

User's answer to the clarification question: {user_message}

INSTRUCTIONS: The user just answered your clarification question. Use their answer to complete the ORIGINAL request. Do NOT ask more questions about the same thing. Do NOT treat their answer as a confusing new command."""
        else:
            history_text = "\n".join([
                f"User: {h['user']}\nAlex: {h['assistant']}"
                for h in conversation_history[-3:]
            ])
            query_with_history = f"""Previous conversation:
{history_text}

Current user message: {user_message}"""

    # ── 注入长期记忆（Generative-Agents 评分检索: relevance+recency+importance）──
    try:
        from rag.agent_memory import get_agent_memory
        _am = get_agent_memory()
        _mems = _am.retrieve(user_message, session_id=session_id, user_id=user_id, n=6)
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
    initial_state = create_initial_state(
        user_query=query_with_history,
        extracted_context=extracted_context,
        user_preferences=agent_persistent_state['user_preferences'],
        accumulated_search_criteria=agent_persistent_state['accumulated_search_criteria'],
    )

    print(f"[LangGraph] ▶ 开始执行 graph.ainvoke() ...")
    final_state = await agent_graph.ainvoke(initial_state)
    print(f"[LangGraph] ✓ 完成!")

    # ── 持久化跨轮状态 ──────────────────────────────────────────
    agent_persistent_state['user_preferences'] = final_state.get('user_preferences', agent_persistent_state['user_preferences'])
    agent_persistent_state['accumulated_search_criteria'] = final_state.get('accumulated_search_criteria', agent_persistent_state['accumulated_search_criteria'])

    response_text = final_state.get('final_response', '')
    response_type = final_state.get('response_type', 'answer')
    tool_data = final_state.get('tool_data', {})

    print(f"[LangGraph] Response Type: {response_type}")

    # ── 保存对话历史 ────────────────────────────────────────────
    conversation_history.append({
        'user': user_message,
        'assistant': response_text[:500]
    })
    if len(conversation_history) > MAX_HISTORY_LENGTH:
        conversation_history = conversation_history[-MAX_HISTORY_LENGTH:]
    # Windowing may rebind the local to a new list — persist it back to this user's slice.
    _user_histories[user_id] = conversation_history

    # ── 写入长期记忆（后台线程: Mem0 式抽取+整合 / GA 反思，不阻塞响应）──
    try:
        from rag.agent_memory import get_agent_memory
        _td = final_state.get('tool_decision')
        _tool_used = _td.get('tool') if isinstance(_td, dict) else None
        get_agent_memory().remember_turn_async(
            user_message, response_text,
            session_id=session_id, user_id=user_id, tool_used=_tool_used,
        )
    except Exception as _e:
        print(f"[Memory] write skipped: {_e}")

    # ── 检查是否有房源搜索结果 ──────────────────────────────────
    if tool_data.get('recommendations'):
        _user_last_results[user_id] = tool_data['recommendations']

        # 保存搜索结果到 persistent extracted_context 供后续安全/设施问题使用
        prev_results_context = "\n"
        structured_results = []  # 结构化，供 _resolve_target_address 解析 "the first one"/postcode
        for i, rec in enumerate(tool_data['recommendations'][:6], 1):
            addr = rec.get('address', 'Unknown')
            price = rec.get('price', 'N/A')
            travel = rec.get('travel_time', 'N/A')
            full_prop = None
            for prop in all_properties:
                if prop.get('Address', '').startswith(addr.split(',')[0]):
                    full_prop = prop
                    break

            property_name = addr.split(',')[0].strip()
            full_address = full_prop.get('Address', addr) if full_prop else addr

            structured_results.append({
                'name': property_name,
                'address': full_address,
                'price': price,
            })

            prev_results_context += f"{i}. **{property_name}**\n"
            prev_results_context += f"   - Full Address: {full_address}\n"
            prev_results_context += f"   - Price: {price}\n"
            prev_results_context += f"   - Commute: {travel}\n"
            if full_prop:
                prev_results_context += f"   - Amenities: {full_prop.get('Detailed_Amenities', 'N/A')}\n"
                prev_results_context += f"   - URL: {full_prop.get('URL', 'N/A')}\n"
            prev_results_context += "\n"

        agent_persistent_state['extracted_context']['previous_search_results'] = prev_results_context
        agent_persistent_state['extracted_context']['last_results'] = structured_results
        print(f"[LangGraph] 💾 已保存 {len(tool_data['recommendations'])} 个搜索结果到上下文")

        return jsonify({
            "response_type": "search",
            "message": response_text,
            "recommendations": tool_data['recommendations']
        })

    # ── 根据结果类型返回响应 ─────────────────────────────────────
    if response_type == 'question' or response_type == 'clarification':
        return jsonify({
            "response_type": "clarification",
            "message": response_text,
            "agent_state": "waiting_for_input",
            "extracted_context": extracted_context
        })

    elif response_type == 'answer':
        return jsonify({
            "response_type": "chat",
            "message": response_text,
        })

    else:
        return jsonify({
            "response_type": "chat",
            "message": response_text or "I'm here to help! What would you like to know?"
        })


@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    """清除对话历史，开始新对话 —— 只重置当前用户自己的 L2 状态，不影响其他用户。"""
    data = request.get_json(silent=True)
    user_id, _session_id = resolve_identity(data)

    # Reset ONLY this user's slices (fresh default-shaped state + empty history/results).
    _user_states[user_id] = _default_persistent_state()
    _user_histories[user_id] = []
    _user_last_results[user_id] = []

    print(f"[ALEX] 对话历史已清除 (user_id={user_id})")
    return jsonify({"success": True, "message": "Conversation history cleared"})


@app.route('/api/favorites', methods=['POST'])
def add_favorite():
    """Add a property to favorites"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    
    try:
        add_to_favorites(data)
        return jsonify({"success": True, "message": "Added to favorites"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/favorites', methods=['GET'])
def get_favorites_list():
    """Get all favorited properties"""
    try:
        favorites = get_favorites()
        return jsonify({"favorites": favorites})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/favorites/<path:url>', methods=['DELETE'])
def remove_favorite(url):
    """Remove a property from favorites"""
    try:
        if url in _session_data['favorites']:
            del _session_data['favorites'][url]
            return jsonify({"success": True, "message": "Removed from favorites"})
        return jsonify({"error": "Property not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/history', methods=['GET'])
def get_search_history():
    """Get search history"""
    try:
        history = _session_data.get('search_history', [])
        return jsonify({"history": history})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        from core.maps_service import get_nearby_places_osm
        
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
        
        # Query amenities from OpenStreetMap
        print(f"  [MAP GEN] Querying nearby amenities...")
        amenities_data = {}
        
        # Parse coordinates once
        coords = generator.parse_geo_location(data.get('geo_location'))
        if not coords:
            return jsonify({"error": "Invalid coordinates"}), 400
        
        lat, lon = coords
        
        # Use the new query method that supports cuisine filtering
        for amenity_key in generator.amenity_config.keys():
            try:
                config = generator.amenity_config[amenity_key]
                cuisine_filter = config.get('cuisine_filter', None)
                
                # Use the specialized query method that handles cuisine filtering
                places = generator.query_osm_amenities_with_filter(
                    lat, lon,
                    amenity_key,
                    cuisine_filter
                )
                amenities_data[amenity_key] = places
                print(f"    ✓ Found {len(places)} {config['name']}")
            except Exception as e:
                print(f"    ✗ Error querying {amenity_key}: {e}")
                import traceback as tb
                tb.print_exc()
                amenities_data[amenity_key] = []
        
        # Generate map HTML
        print(f"\n  [MAP GEN] Generating interactive map...")
        map_html = generator.generate_map_html(property_data, amenities_data)
        
        if map_html:
            print(f"  ✓ [MAP GEN] Map generated successfully\n")
            return map_html, 200, {'Content-Type': 'text/html; charset=utf-8'}
        else:
            return jsonify({"error": "Failed to generate map"}), 500
            
    except Exception as e:
        print(f"❌ Error generating map: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Map generation failed: {str(e)}"}), 500


@app.route('/api/cached_pois', methods=['GET'])
def get_cached_pois():
    """
    获取缓存的 POI 数据
    
    Query params:
    - address: 要查询的地址
    
    Returns:
    缓存的 POI 数据或错误
    """
    address = request.args.get('address')
    if not address:
        return jsonify({"error": "Address is required"}), 400
    
    try:
        import os
        cache_file = "data/osm_poi_cache.json"
        
        if not os.path.exists(cache_file):
            return jsonify({"error": "No cache file found", "cached": False}), 404
        
        with open(cache_file, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        
        cache_key = address.lower().strip()
        
        if cache_key in cache:
            return jsonify({
                "cached": True,
                "data": cache[cache_key]
            })
        else:
            return jsonify({
                "cached": False,
                "message": "Address not in cache"
            }), 404
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    
if __name__ == '__main__':
    # 允许所有来源访问(用于公网访问)
    app.run(debug=True, host='0.0.0.0', port=5001)