# tools/check_safety.py

"""
区域安全检查工具
"""

from tool_system import Tool


async def check_safety_impl(address: str) -> dict:
    """
    检查区域安全性
    """
    from free_maps_service import get_crime_data_by_location
    
    print(f"   🛡️  检查安全: {address[:50]}...")
    
    crime_data = get_crime_data_by_location(address)
    
    if not crime_data:
        return {
            'address': address,
            'safety_rating': 'Unknown',
            'error': '该区域无犯罪数据'
        }
    
    # 计算安全评分
    crime_count = crime_data.get('total_crimes_6m', 0)
    
    if crime_count < 50:
        safety_rating = "Very Safe"
        safety_score = 90
    elif crime_count < 100:
        safety_rating = "Moderately Safe"
        safety_score = 70
    elif crime_count < 200:
        safety_rating = "Average"
        safety_score = 50
    else:
        safety_rating = "Higher Crime"
        safety_score = 30
    
    return {
        'address': address,
        'safety_rating': safety_rating,
        'safety_score': safety_score,
        'total_crimes_6m': crime_count,
        'crime_trend': crime_data.get('crime_trend', 'unknown'),
        'is_safe': crime_count < 100
    }


check_safety_tool = Tool(
    name="check_area_safety",
    
    description="""
检查英国地址的区域安全性。

**功能:**
- 使用英国警察局官方数据（UK Police API）
- 查询过去 6 个月的犯罪统计
- 返回安全评级和趋势

**何时使用:**
- 用户明确提到"安全"、"治安"、"犯罪"
- 用户说"care about safety"
- 对比房源时，安全是考虑因素

**何时不用:**
- 用户没提安全问题
- 用户只关心价格和位置

**返回内容:**
- safety_rating: Very Safe / Moderately Safe / Average / Higher Crime
- safety_score: 0-100 分（越高越安全）
- total_crimes_6m: 过去6个月犯罪总数
- crime_trend: increasing / stable / decreasing
""",
    
    func=check_safety_impl,
    
    parameters={
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "要检查的英国地址（可以是完整地址、邮编或区域名）"
            }
        },
        "required": ["address"]
    },
    
    max_retries=1,
    retry_on_error=False
)