"""
Web Search Tool - 智能搜索协调器
可以调用其他本地工具（check_safety, get_weather, search_nearby_pois等）+ 网络搜索
用于回答综合性问题
"""

from core.tool_system import Tool
from core.web_search import get_search_snippets
from uk_rent_agent.agent.critic import evidence_usable as _evidence_usable
from typing import Optional, List, Dict
import asyncio
import json
import re

# 🆕 全局 tool_registry（通过 set_tool_registry 设置）
_tool_registry = None

def set_tool_registry(registry):
    """设置全局 tool_registry，供 web_search 使用"""
    global _tool_registry
    _tool_registry = registry
    print("[WEB_SEARCH] ✅ Tool registry 已设置，可以调用本地工具")


async def web_search_func(query: str, sub_queries: Optional[List[Dict]] = None) -> dict:
    """
    智能搜索协调器 - 可以调用本地工具 + 网络搜索
    
    Args:
        query: 主查询语句
        sub_queries: 子查询列表（可选），格式:
            [
                {"tool": "check_safety", "params": {"address": "..."}},
                {"tool": "get_weather", "params": {"location": "..."}},
                {"tool": "web_search_only", "params": {"query": "..."}}
            ]
    
    Returns:
        dict: 合并的搜索结果；由 Tool.execute 统一包装。

    NOTE: this stays an `async def` (it AWAITS sibling tools via _tool_registry.execute_tool
    for the sub_queries path, so it cannot be offloaded wholesale to a thread). Every BLOCKING
    call it makes directly — get_search_snippets performs SYNCHRONOUS SearXNG HTTP — is pushed
    through asyncio.to_thread so it never blocks the event loop while running inline on the loop.
    """
    try:
        print(f"[WEB_SEARCH] 主查询: {query}")
        
        results_parts = []
        all_data = {}
        
        # 🆕 如果有 sub_queries，执行本地工具调用
        if sub_queries and _tool_registry:
            print(f"[WEB_SEARCH] 🔧 执行 {len(sub_queries)} 个子查询...")
            
            for i, sub_query in enumerate(sub_queries, 1):
                tool_name = sub_query.get('tool', 'web_search_only')
                params = sub_query.get('params', {})
                
                print(f"  [{i}/{len(sub_queries)}] 调用: {tool_name}")
                print(f"       参数: {json.dumps(params, ensure_ascii=False)}")
                
                if tool_name == 'web_search_only':
                    # 执行网络搜索（阻塞 HTTP -> to_thread，避免阻塞事件循环）
                    search_query = params.get('query', query)
                    web_result = await asyncio.to_thread(get_search_snippets, search_query, 5)
                    
                    results_parts.append(f"### Web Search: {search_query}")
                    results_parts.append(web_result)
                    all_data[f'web_search_{i}'] = web_result
                    
                else:
                    # 调用本地工具
                    try:
                        tool_result = await _tool_registry.execute_tool(tool_name, **params)
                        
                        if tool_result.success:
                            results_parts.append(f"### {tool_name}: {json.dumps(params, ensure_ascii=False)}")
                            results_parts.append(json.dumps(tool_result.data, ensure_ascii=False, indent=2))
                            all_data[f'{tool_name}_{i}'] = tool_result.data
                            print(f"       ✅ 成功")
                        else:
                            results_parts.append(f"### {tool_name}: FAILED")
                            results_parts.append(f"Error: {tool_result.error}")
                            print(f"       ❌ 失败: {tool_result.error}")
                    
                    except Exception as e:
                        results_parts.append(f"### {tool_name}: ERROR")
                        results_parts.append(f"Error: {str(e)}")
                        print(f"       ❌ 异常: {e}")
                
                results_parts.append("")  # 空行分隔
        
        else:
            # 🆕 没有 sub_queries，只执行简单的网络搜索（阻塞 HTTP -> to_thread）
            print(f"[WEB_SEARCH] 执行简单网络搜索...")
            web_result = await asyncio.to_thread(get_search_snippets, query, 5)

            # H3: report success=FALSE truthfully when the search backend returned
            # nothing usable. get_search_snippets emits a placeholder string
            # ("No search results found for this query.") — NOT the old
            # "Could not retrieve search information." — when SearXNG is unreachable or a
            # query yields nothing, so the previous exact-string check let placeholders
            # through as success=True. evidence_usable is the single source of truth for
            # what counts as an empty/placeholder result.
            if not _evidence_usable(web_result):
                print(f"[WEB_SEARCH] ⚠️ 无可用搜索结果（后端为空/占位）: {query}")
                return {"success": False, "error": "No search results found",
                        "query": query, "results": "", "detailed_data": {}}

            results_parts.append(web_result)
            all_data['web_search'] = web_result

        # 合并所有结果
        combined_results = "\n---\n".join(results_parts)

        # H3: if NOTHING usable came back across all sub-queries (every web result was a
        # placeholder and no local tool produced data), report success=False truthfully
        # rather than handing the model a page of "No search results found" it might
        # paper over with a fabricated answer.
        if not _evidence_usable(all_data):
            print(f"[WEB_SEARCH] ⚠️ 所有子查询均无可用结果: {query}")
            return {"success": False, "error": "No usable search results",
                    "query": query, "results": combined_results, "detailed_data": all_data}

        print(f"[WEB_SEARCH] ✅ 完成! 共 {len(results_parts)} 个结果片段")

        return {
            "success": True,
            "query": query,
            "results": combined_results,
            "detailed_data": all_data,
        }
        
    except Exception as e:
        print(f"[WEB_SEARCH] ❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e), "query": query, "results": ""}


# 工具定义
web_search_tool = Tool(
    name="web_search",
    
    description="""Smart search coordinator for general/open-ended questions (UK areas, neighbourhoods, universities, living costs) and anything needing web information or a mix of web + local data. Put the main query (in English) in `query`. Optionally pass `sub_queries` to run local tools in the same call (check_safety, get_weather, search_nearby_pois, get_property_details, calculate_commute, web_search_only); each is {tool, params}. Omit sub_queries for a plain web search.
综合搜索协调器：一般性/开放性问题与需要联网的信息查询。""",
    
    func=web_search_func,
    
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "主查询语句（英文）。示例: 'Scape Bloomsbury safety and amenities'"
            },
            "sub_queries": {
                "type": "array",
                "description": "子查询列表（可选）。每个子查询包含 tool 和 params",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "工具名称: check_safety, get_weather, search_nearby_pois, get_property_details, calculate_commute, web_search_only"
                        },
                        "params": {
                            "type": "object",
                            "description": "工具参数（JSON object）"
                        }
                    },
                    "required": ["tool", "params"]
                }
            }
        },
        "required": ["query"]
    }
)
