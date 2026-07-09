# tools/__init__.py

"""
Tool System 模块
提供所有可用工具和工具注册中心
"""

from tool_system.base import Tool, ToolRegistry, ToolResult
from tool_system.tool_set.search_properties import search_properties_tool
from tool_system.tool_set.calculate_commute import calculate_commute_tool
from tool_system.tool_set.check_safety import check_safety_tool


def create_tool_registry() -> ToolRegistry:
    """
    创建并初始化工具注册中心
    这是项目的工具入口
    """
    registry = ToolRegistry()
    
    # 注册所有工具
    registry.register(search_properties_tool)
    registry.register(calculate_commute_tool)
    registry.register(check_safety_tool)
    
    print(f"\n✅ Tool System 初始化完成")
    print(f"📦 已注册 {len(registry.tools)} 个工具: {', '.join(registry.list_tool_names())}\n")
    
    return registry


# 导出
__all__ = [
    'Tool',
    'ToolRegistry',
    'ToolResult',
    'create_tool_registry',
    'search_properties_tool',
    'calculate_commute_tool',
    'check_safety_tool'
]

