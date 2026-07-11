"""
Tool 3: Check Safety Tool
检查地区的安全指数
"""

from core.tool_system import Tool
from core.maps_service import get_crime_data_by_location
from typing import Optional

async def check_safety_impl(
    address: str = None,
    area: str = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    user_query: str = ""
) -> dict:
    """
    检查地址附近的犯罪数据和安全指数
    """
    # 兼容 address 和 area 参数
    location = address or area
    if not location:
        raise ValueError("必须提供 address 或 area 参数")
    
    # 检测用户语言
    is_chinese = _detect_chinese(user_query or location)
    
    try:
        print(f"   🔒 检查安全性:")
        print(f"      地址: {location}")
        print(f"      语言: {'中文' if is_chinese else 'English'}")
        
        # 使用地址调用 get_crime_data_by_location
        crime_data = get_crime_data_by_location(location)
        
        # 计算安全指数（0-100，越高越安全）
        if crime_data and not crime_data.get('error'):
            # 从 crime_data 获取总犯罪数
            total_crimes = crime_data.get('total_crimes_6m', 0)
            if isinstance(total_crimes, str):
                try:
                    total_crimes = int(total_crimes)
                except:
                    total_crimes = 0
            
            # 计算安全分数: 基准分 100，每 2 起犯罪扣 1 分
            safety_score = max(0, 100 - total_crimes // 2)
            
            # 生成评分解释
            scoring_explanation = _generate_scoring_explanation(total_crimes, safety_score, crime_data, is_chinese)
            
            # 生成详细的安全分析
            safety_analysis = _generate_safety_analysis(crime_data, location, is_chinese)
            
        else:
            safety_score = 50  # 默认值
            total_crimes = 0
            crime_data = crime_data or {}
            if is_chinese:
                scoring_explanation = "无法获取犯罪数据，使用默认评分 50/100"
                safety_analysis = "由于数据不可用，无法进行详细的安全分析。建议实地考察或咨询当地居民。"
            else:
                scoring_explanation = "Unable to retrieve crime data, using default score 50/100"
                safety_analysis = "Unable to perform detailed safety analysis due to data unavailability. Recommend visiting in person or consulting local residents."
        
        safety_level = (
            'Very Safe' if safety_score >= 80
            else 'Safe' if safety_score >= 60
            else 'Moderate' if safety_score >= 40
            else 'Concerning'
        )
        
        return {
            'address': location,
            'safety_score': safety_score,
            'safety_level': safety_level,
            'crime_data': crime_data,
            'scoring_explanation': scoring_explanation,
            'safety_analysis': safety_analysis,
            'recommendation': f"This area has a safety level of {safety_level} with a safety score of {safety_score}/100",
            'next_action_hint': 'NOW use Final Answer to summarize this safety information for the user. Do NOT call search_properties again - the user already has property recommendations.'
        }
    
    except Exception as e:
        print(f"   ❌ 安全检查失败: {e}")
        return {
            'address': location,
            'safety_score': 50,
            'safety_level': 'Unknown',
            'crime_data': {},
            'error': str(e),
            'scoring_explanation': f"Error occurred: {e}",
            'safety_analysis': "Unable to perform safety analysis due to an error."
        }


def _detect_chinese(text: str) -> bool:
    """检测文本是否包含中文"""
    if not text:
        return False
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False


def _generate_scoring_explanation(total_crimes: int, safety_score: int, crime_data: dict, is_chinese: bool) -> str:
    """生成评分计算方法的详细解释（根据语言生成）"""
    
    if is_chinese:
        explanation = f"""
**评分计算方法:**

1. **数据来源**: UK Police API（英国警方官方犯罪数据）
2. **统计周期**: 最近 6 个月
3. **总犯罪数**: {total_crimes} 起

4. **评分算法**:
   - 基准分: 100 分（理想状态，零犯罪）
   - 扣分规则: 每 2 起犯罪扣 1 分
   - 计算公式: 安全分 = max(0, 100 - 总犯罪数 ÷ 2)
   - 本区域计算: 100 - {total_crimes} ÷ 2 = **{safety_score} 分**

5. **评级标准**:
   - 80-100分: Very Safe（非常安全）
   - 60-79分: Safe（安全）
   - 40-59分: Moderate（中等）
   - 0-39分: Concerning（需要注意）
"""
        
        # 添加趋势分析
        trend = crime_data.get('crime_trend', 'unknown')
        if trend == 'increasing':
            explanation += "\n⚠️ **趋势警示**: 近期犯罪呈上升趋势，需额外注意。"
        elif trend == 'decreasing':
            explanation += "\n✅ **积极趋势**: 犯罪率正在下降，治安改善中。"
        else:
            explanation += "\n📊 **趋势**: 犯罪率相对稳定。"
    
    else:  # English
        explanation = f"""
**Scoring Method:**

1. **Data Source**: UK Police API (Official UK Police Crime Data)
2. **Period**: Last 6 months
3. **Total Crimes**: {total_crimes} incidents

4. **Algorithm**:
   - Base Score: 100 points (ideal state, zero crime)
   - Deduction Rule: -1 point per 2 crimes
   - Formula: Safety Score = max(0, 100 - Total Crimes ÷ 2)
   - Calculation: 100 - {total_crimes} ÷ 2 = **{safety_score} points**

5. **Rating Criteria**:
   - 80-100: Very Safe
   - 60-79: Safe
   - 40-59: Moderate
   - 0-39: Concerning
"""
        
        # Add trend analysis
        trend = crime_data.get('crime_trend', 'unknown')
        if trend == 'increasing':
            explanation += "\n⚠️ **Trend Alert**: Crime rate is increasing, extra caution needed."
        elif trend == 'decreasing':
            explanation += "\n✅ **Positive Trend**: Crime rate is decreasing, safety improving."
        else:
            explanation += "\n📊 **Trend**: Crime rate is relatively stable."
    
    return explanation.strip()


def _generate_safety_analysis(crime_data: dict, location: str, is_chinese: bool) -> str:
    """生成详细的安全分析和建议（根据语言生成）"""
    total_crimes = crime_data.get('total_crimes_6m', 0)
    category_breakdown = crime_data.get('category_breakdown', {})
    most_recent_count = crime_data.get('most_recent_month_count', 0)
    
    if is_chinese:
        analysis = f"""
**详细安全分析:**

📍 **地点**: {location}

📊 **犯罪统计**:
- 6个月总计: {total_crimes} 起
- 最近一个月: {most_recent_count} 起  
- 月均犯罪: {total_crimes // 6 if total_crimes > 0 else 0} 起

🔍 **主要犯罪类型**:
"""
        
        if category_breakdown:
            for category, count in list(category_breakdown.items())[:3]:
                percentage = (count / total_crimes * 100) if total_crimes > 0 else 0
                analysis += f"\n- {category}: {count} 起 ({percentage:.1f}%)"
        else:
            analysis += "\n- 数据不可用"
        
        # 夜间安全建议
        analysis += "\n\n🌙 **夜间安全建议**:"
        
        if total_crimes < 20:
            analysis += """
- ✅ 该区域整体犯罪率较低
- ✅ 从地铁站步行回家相对安全
- 💡 仍建议: 走人流较多的主路，避免抄小道
- 💡 保持警觉，注意周围环境
"""
        elif total_crimes < 50:
            analysis += """
- ⚠️ 该区域有一定犯罪记录，需保持警惕
- 💡 建议: 晚上10点后尽量结伴而行
- 💡 选择光线明亮、有监控的主路
- 💡 避免在深夜独自行走偏僻街道
- 💡 考虑使用打车软件（短途也可以）
"""
        else:
            analysis += """
- ⚠️ 该区域犯罪率较高，需格外注意
- 🚨 强烈建议: 晚上避免独自步行
- 🚨 优先选择: 打车/Uber回家
- 🚨 如必须步行: 走繁华大街，避开小巷
- 🚨 随时保持警觉，手机充满电备用
- 💡 考虑选择治安更好的区域居住
"""
        
        # 对比参考
        analysis += "\n\n📈 **参考对比**:"
        if total_crimes < 30:
            analysis += "\n- 该区域安全性 **优于伦敦平均水平**"
        elif total_crimes < 60:
            analysis += "\n- 该区域安全性 **接近伦敦平均水平**"
        else:
            analysis += "\n- 该区域安全性 **低于伦敦平均水平**，建议谨慎选择"
    
    else:  # English
        analysis = f"""
**Detailed Safety Analysis:**

📍 **Location**: {location}

📊 **Crime Statistics**:
- 6-month total: {total_crimes} incidents
- Most recent month: {most_recent_count} incidents
- Monthly average: {total_crimes // 6 if total_crimes > 0 else 0} incidents

🔍 **Main Crime Categories**:
"""
        
        if category_breakdown:
            for category, count in list(category_breakdown.items())[:3]:
                percentage = (count / total_crimes * 100) if total_crimes > 0 else 0
                analysis += f"\n- {category}: {count} ({percentage:.1f}%)"
        else:
            analysis += "\n- Data not available"
        
        # 夜间安全建议
        analysis += "\n\n🌙 **Night Safety Advice**:"
        
        if total_crimes < 20:
            analysis += """
- ✅ Overall low crime rate in this area
- ✅ Walking from tube station is relatively safe
- 💡 Still recommended: Use main roads with foot traffic, avoid shortcuts
- 💡 Stay alert and aware of surroundings
"""
        elif total_crimes < 50:
            analysis += """
- ⚠️ Area has some crime records, stay vigilant
- 💡 Recommended: Travel with others after 10 PM when possible
- 💡 Choose well-lit main roads with CCTV
- 💡 Avoid walking alone on quiet streets late at night
- 💡 Consider using taxi apps even for short distances
"""
        else:
            analysis += """
- ⚠️ Higher crime rate area, extra caution required
- 🚨 Strongly recommended: Avoid walking alone at night
- 🚨 Priority option: Use taxi/Uber to get home
- 🚨 If must walk: Use busy main streets, avoid alleys
- 🚨 Stay alert, keep phone fully charged
- 💡 Consider choosing a safer neighborhood
"""
        
        # 对比参考
        analysis += "\n\n📈 **Comparison**:"
        if total_crimes < 30:
            analysis += "\n- Safety is **better than London average**"
        elif total_crimes < 60:
            analysis += "\n- Safety is **close to London average**"
        else:
            analysis += "\n- Safety is **below London average**, consider carefully"
    
    return analysis.strip()


# 创建工具实例
check_safety_tool = Tool(
    name="check_safety",
    
    description="""
检查地址附近的安全情况和犯罪数据。

**CRITICAL: 地址参数要求**
- 必须使用 **完整的详细地址**（包括街道、邮编）
- ❌ 错误示例: "London", "Stratford", "Bloomsbury"
- ✅ 正确示例: "Scape Bloomsbury, 19-29 Woburn Place, London WC1H 0AQ, UK"
- ✅ 正确示例: "Spring Mews, 10 Tinworth Street, London SE11 5AL, UK"

**如何获取完整地址:**
1. 如果用户提到房产名称（如 "Scape Bloomsbury"），从上下文的 PREVIOUSLY SHOWN PROPERTIES 中查找完整地址
2. 提取 "Address" 字段的完整值
3. 将完整地址传给本工具

**功能:**
- 获取具体地点的犯罪统计数据（6个月内）
- 计算安全指数（0-100）
- 提供安全等级评估
- 分析夜间步行安全性

**何时使用:**
- 用户关心某个具体地点/房产的治安
- 用户询问 "晚上安全吗"、"犯罪率高吗"
- 需要对房源进行安全评估

**何时不用:**
- 用户没有提到安全问题
- 用户只是一般性询问（如 "伦敦安全吗" - 应该用 web_search）

**返回内容:**
- safety_score: 安全指数（0-100）
- safety_level: 安全等级（Very Safe/Safe/Moderate/Concerning）
- crime_data: 详细的犯罪统计数据
- total_crimes_6m: 6个月内总犯罪数
- recommendation: 建议
""",
    
    func=check_safety_impl,
    
    parameters={
        'type': 'object',
        'properties': {
            'address': {
                'type': 'string',
                'description': '要检查的完整地址或区域名称（如 "Stratford, London" 或 "Scape Bloomsbury, 19-29 Woburn Place, London"）'
            },
            'area': {
                'type': 'string',
                'description': '区域名称（address 的别名，可以使用 address 或 area 任意一个）'
            },
            'latitude': {
                'type': 'number',
                'description': '纬度（可选，如果不提供会自动解析地址）'
            },
            'longitude': {
                'type': 'number',
                'description': '经度（可选，如果不提供会自动解析地址）'
            },
            'user_query': {
                'type': 'string',
                'description': '用户原始查询（用于检测语言）'
            }
        },
        'required': []
    },
    
    max_retries=2
)
