"""
Web Search Tool - 智能搜索协调器
可以调用其他本地工具（check_safety, get_weather, search_nearby_pois等）+ 网络搜索
用于回答综合性问题
"""

from core.tool_system import Tool
from core.web_search import get_search_snippets
from typing import Optional, List, Dict
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
                    # 执行网络搜索
                    search_query = params.get('query', query)
                    web_result = get_search_snippets(search_query, max_results=5)
                    
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
            # 🆕 没有 sub_queries，只执行简单的网络搜索
            print(f"[WEB_SEARCH] 执行简单网络搜索...")
            web_result = get_search_snippets(query, max_results=5)
            
            if not web_result or web_result == "Could not retrieve search information.":
                return {"success": False, "error": "No search results found", "query": query, "results": ""}
            
            results_parts.append(web_result)
            all_data['web_search'] = web_result
        
        # 合并所有结果
        combined_results = "\n---\n".join(results_parts)
        
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
    
    description="""
智能搜索协调器 - 可以调用本地工具 + 网络搜索

**USE THIS TOOL FOR:**
- 综合性问题（涉及多个主题）
- 一般性信息查询（UK areas, neighborhoods, universities, living costs等）
- 需要结合本地数据和网络信息的问题

**🆕 ADVANCED: 可以调用其他本地工具**
如果用户的问题需要多个数据源，可以使用 sub_queries 参数：

示例1: 用户问 "Scape Bloomsbury 的治安和天气怎么样？"
```json
{
  "query": "Scape Bloomsbury safety and weather",
  "sub_queries": [
    {
      "tool": "check_safety",
      "params": {"address": "Scape Bloomsbury, 19-29 Woburn Place, London WC1H 0AQ, UK"}
    },
    {
      "tool": "get_weather",
      "params": {"location": "London"}
    }
  ]
}
```

示例2: 用户问 "这个区域安全吗？附近有什么设施？"
```json
{
  "query": "area safety and nearby amenities",
  "sub_queries": [
    {
      "tool": "check_safety",
      "params": {"address": "完整地址"}
    },
    {
      "tool": "search_nearby_pois",
      "params": {"address": "完整地址", "poi_type": "all"}
    }
  ]
}
```

示例3: 用户问 "伦敦租房政策是什么？"（纯网络搜索）
```json
{
  "query": "London rental policy UK 2024",
  "sub_queries": [
    {
      "tool": "web_search_only",
      "params": {"query": "London tenant rights UK gov.uk 2024"}
    }
  ]
}
```

**可用的本地工具:**
- check_safety: 检查治安数据
- get_weather: 获取天气信息
- search_nearby_pois: 搜索附近设施
- get_property_details: 获取房产详情
- calculate_commute: 计算通勤时间

**简单模式（不使用 sub_queries）:**
只传 query 参数，系统会执行简单的网络搜索。
""",
    
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
