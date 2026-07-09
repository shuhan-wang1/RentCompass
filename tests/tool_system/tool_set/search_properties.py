# tools/search_properties.py

"""
房源搜索工具
从 Rightmove 搜索租房信息
"""

from tool_system.base import Tool


async def search_properties_impl(
    location: str,
    max_budget: int,
    radius: float = 5.0,
    limit: int = 25
) -> dict:
    """
    实际执行函数
    这里复用你原来的代码
    """
    # 导入你原来的代码
    from data_loader import get_live_properties
    from location_resolver import get_best_location_id
    
    print(f"   🔍 搜索参数:")
    print(f"      - 位置: {location}")
    print(f"      - 预算: £{max_budget}")
    print(f"      - 半径: {radius} 英里")
    print(f"      - 限制: {limit} 个")
    
    # 解析位置
    location_id, actual_radius = get_best_location_id([location])
    
    # 搜索房源（复用你的代码）
    properties = get_live_properties(
        location_id=location_id,
        radius=actual_radius if actual_radius else radius,
        min_price=500,
        max_price=max_budget,
        limit=limit
    )
    
    return {
        'properties': properties,
        'count': len(properties),
        'search_metadata': {
            'location': location,
            'location_id': location_id,
            'radius_used': actual_radius or radius,
            'max_budget': max_budget
        }
    }


# 创建工具实例
search_properties_tool = Tool(
    name="search_properties",
    
    description="""
搜索英国租房房源。

**功能:**
- 在指定位置搜索可租房源
- 从 Rightmove 获取实时数据
- 支持按预算、位置、半径过滤

**何时使用:**
- 用户要找房子
- 需要获取房源列表
- 开始新的搜索任务

**何时不用:**
- 只是询问价格范围（不需要实际搜索）
- 已经有房源列表（不需要重复搜索）

**返回内容:**
- properties: 房源列表（包含地址、价格、URL、图片）
- count: 找到的房源数量
- search_metadata: 搜索元数据
""",
    
    func=search_properties_impl,
    
    # OpenAI 格式（但不调用 OpenAI API，完全免费）
    parameters={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "搜索位置（可以是城市、区域、地标）。例如: 'London', 'UCL', 'Bloomsbury', 'Manchester'"
            },
            "max_budget": {
                "type": "integer",
                "description": "最高月租金（英镑）。例如: 1500 表示 £1500/月"
            },
            "radius": {
                "type": "number",
                "description": "搜索半径（英里）。默认 5.0。如果结果太少，可以增大到 7-10",
                "default": 5.0
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少个房源。默认 25",
                "default": 25
            }
        },
        "required": ["location", "max_budget"]
    },
    
    max_retries=2,
    retry_on_error=True
)


'''
初始化工具：定义工具名字，用途，参数和执行逻辑
生成tool说明：把工具信息转成LLM能读懂的自然语言说明书
模型调用工具：LLM阅读说明书后，判断是否调用、生成JSON函数
'''