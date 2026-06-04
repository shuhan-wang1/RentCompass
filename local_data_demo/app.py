# app.py - Enhanced with RAG and LangGraph Agent Framework

import asyncio
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import json
import traceback
import re
from core.user_session import add_to_favorites, get_favorites, _session_data
from core.data_loader import load_mock_properties_from_csv
from rag.rag_coordinator import RAGCoordinator
from core.tool_system import create_tool_registry
from core.langgraph_agent import build_agent_graph, create_initial_state

app = Flask(__name__, template_folder='.')
CORS(app)

# 统一 UI 模式标志
USE_UNIFIED_UI = True  # 设置为 True 使用新的统一 Alex 界面

# LangGraph Agent — compiled graph (lazy-initialized)
agent_graph = None

# Persistent cross-turn state (preferences & accumulated search criteria)
agent_persistent_state = {
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

# 对话历史存储 - 用于保持上下文记忆
conversation_history = []
MAX_HISTORY_LENGTH = 10  # 保留最近10轮对话

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

print("[STARTUP] Loading mock properties from CSV...")
all_properties = load_mock_properties_from_csv()
print(f"✓ [STARTUP] Loaded {len(all_properties)} properties from CSV")

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

# Store last search results for chat context
last_search_results = []

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
    global last_search_results

    data = request.get_json()
    if not data or not data.get('message'):
        return jsonify({"error": "Message is required"}), 400
    
    user_message = data.get('message')
    context = data.get('context', {})
    is_continuation = data.get('is_continuation', False)
    
    print(f"\n{'='*60}")
    print(f"🤖 [ALEX - LangGraph Agent] 收到消息: {user_message}")
    print(f"📋 [ALEX] is_continuation: {is_continuation}")
    print(f"📋 [ALEX] context: {context}")
    print(f"{'='*60}")
    
    try:
        # 所有请求都通过 ReAct Agent 处理
        return await handle_with_react_agent(user_message, context, is_continuation)
    
    except Exception as e:
        print(f"❌ [ALEX] 错误: {e}")
        traceback.print_exc()
        return jsonify({
            "response_type": "error",
            "message": "抱歉，处理您的请求时出错了。请稍后再试。"
        }), 500


async def handle_with_react_agent(user_message: str, context: dict, is_continuation: bool):
    """
    使用 LangGraph Agent 处理所有用户请求 - 纯 LLM 驱动

    LangGraph Agent 会自主决定：
    1. 是否需要调用 search_properties 工具搜索房源
    2. 是否需要调用其他工具（安全检查、通勤计算等）
    3. 或者直接回答用户问题

    没有任何关键词匹配 - 完全由 LLM 决策
    """
    global agent_graph, tool_registry, agent_tool_provider, last_search_results, conversation_history, agent_persistent_state

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

    # ── 检查是否有房源搜索结果 ──────────────────────────────────
    if tool_data.get('recommendations'):
        last_search_results = tool_data['recommendations']

        # 保存搜索结果到 persistent extracted_context 供后续安全/设施问题使用
        prev_results_context = "\n"
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

            prev_results_context += f"{i}. **{property_name}**\n"
            prev_results_context += f"   - Full Address: {full_address}\n"
            prev_results_context += f"   - Price: {price}\n"
            prev_results_context += f"   - Commute: {travel}\n"
            if full_prop:
                prev_results_context += f"   - Amenities: {full_prop.get('Detailed_Amenities', 'N/A')}\n"
                prev_results_context += f"   - URL: {full_prop.get('URL', 'N/A')}\n"
            prev_results_context += "\n"

        agent_persistent_state['extracted_context']['previous_search_results'] = prev_results_context
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
    """清除对话历史，开始新对话"""
    global conversation_history, agent_persistent_state
    conversation_history = []
    # 重置跨轮持久状态
    agent_persistent_state = {
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
    print("[ALEX] 对话历史已清除")
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