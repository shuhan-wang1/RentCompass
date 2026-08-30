"""
Tool 3: Check Safety Tool
检查地区的安全指数
"""

from core.tool_system import Tool
from core.maps_service import get_crime_data_by_location
from typing import Optional

from core.safety_reference import (
    CAVEAT_EN, CAVEAT_ZH, reference_note, score_from_monthly_count,
)


def check_safety_impl(
    address: str = None,
    area: str = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    user_query: str = ""
) -> dict:
    """
    检查地址附近的犯罪数据和安全指数

    NOTE: this is a PLAIN SYNC function on purpose. get_crime_data_by_location performs
    SYNCHRONOUS network I/O (data.police.uk HTTP + geocoding). Registering it as sync means
    Tool.execute offloads it to an executor thread (tool_system.py :279-284), keeping the
    asyncio event loop responsive so the fc-loop's per-tool timeout / batch budget can fire.
    """
    # 兼容 address 和 area 参数
    location = address or area
    if not location:
        raise ValueError("必须提供 address 或 area 参数")
    
    # 检测用户语言
    is_chinese = _detect_chinese(user_query or location)
    
    try:
        print(
            "   🔒 检查安全性 "
            f"location_chars={len(location)} language={'zh' if is_chinese else 'en'}"
        )
        
        # 使用地址调用 get_crime_data_by_location
        crime_data = get_crime_data_by_location(location)
        
        # Score against a reference distribution, never against a bare count. The old formula
        # was `max(0, 100 - total_crimes // 2)`: no normalisation, so it measured how many
        # rows the API returned rather than anything about the area.
        band = None
        if crime_data and not crime_data.get('error'):
            total_crimes = crime_data.get('total_crimes_6m', 0)
            if isinstance(total_crimes, str):
                try:
                    total_crimes = int(total_crimes)
                except Exception:
                    total_crimes = 0

            per_month = crime_data.get('crimes_per_month')
            if per_month is None and total_crimes:
                per_month = total_crimes / max(1, crime_data.get('months_covered', 1))

            safety_score, band = score_from_monthly_count(per_month)

            if safety_score is None:
                # A zero or missing rate is an ABSENT ANSWER, not a quiet neighbourhood.
                # Presenting it as a score is exactly how "9 crimes, 96/100, very safe" got
                # in front of a user.
                scoring_explanation = (
                    "犯罪数据不足，无法给出评分。" if is_chinese
                    else "Not enough crime data to produce a score.")
                safety_analysis = (
                    "该地点在 data.police.uk 上没有返回足够的犯罪记录，因此无法评估治安。"
                    "这不代表该区域没有犯罪。" if is_chinese
                    else "data.police.uk returned too few records for this point to assess "
                         "safety. That is not evidence of low crime.")
            else:
                scoring_explanation = _generate_scoring_explanation(
                    total_crimes, safety_score, crime_data, is_chinese, band=band,
                    per_month=per_month)
                safety_analysis = _generate_safety_analysis(crime_data, location, is_chinese)
        else:
            # No data must NOT become a middling score that reads as a measurement.
            safety_score = None
            total_crimes = 0
            crime_data = crime_data or {}
            if is_chinese:
                scoring_explanation = "无法获取犯罪数据，因此不提供评分。"
                safety_analysis = "由于数据不可用，无法进行治安分析。建议实地考察或咨询当地居民。"
            else:
                scoring_explanation = "Crime data unavailable, so no score is given."
                safety_analysis = ("Safety could not be assessed because the data was "
                                   "unavailable. Consider visiting in person or asking local residents.")
        
        safety_level = (
            None if safety_score is None
            else 'Very Safe' if safety_score >= 80
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
            'crimes_per_month': (crime_data or {}).get('crimes_per_month'),
            'months_covered': (crime_data or {}).get('months_covered'),
            'radius_miles': (crime_data or {}).get('radius_miles'),
            'reference_basis': reference_note(),
            'caveat': CAVEAT_ZH if is_chinese else CAVEAT_EN,
            'recommendation': (
                f"This area has a safety level of {safety_level} with a safety score of "
                f"{safety_score}/100 ({reference_note()}). {CAVEAT_EN}"
                if safety_score is not None else
                "Safety could not be scored for this location: data.police.uk returned too "
                "few records. That is not evidence of low crime."),
            'next_action_hint': 'NOW use Final Answer to summarize this safety information for the user. Do NOT call search_properties again - the user already has property recommendations.'
        }
    
    except Exception as exc:
        print(f"   ❌ 安全检查失败 exception_type={type(exc).__name__}")
        return {
            'address': location,
            # An API exception contains no crime observation. A neutral-looking
            # number (formerly 50) is still a fabricated measurement and may be
            # presented downstream as real evidence.
            'safety_score': None,
            'safety_level': 'Unknown',
            'crime_data': {},
            'error': "Safety data lookup failed",
            'scoring_explanation': "Safety data lookup failed; no score is available.",
            'safety_analysis': "Unable to perform safety analysis due to an error.",
            'retryable': True,
        }


def _detect_chinese(text: str) -> bool:
    """检测文本是否包含中文"""
    if not text:
        return False
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False


def _generate_scoring_explanation(total_crimes: int, safety_score: int, crime_data: dict,
                                  is_chinese: bool, band: str | None = None,
                                  per_month: float | None = None) -> str:
    """生成评分计算方法的详细解释（根据语言生成）"""
    
    if is_chinese:
        _mo = crime_data.get("months_covered", 3)
        _pm = per_month if per_month is not None else 0
        explanation = f"""
**评分依据:**

1. **数据来源**: data.police.uk（英国警方官方犯罪数据）
2. **统计范围**: 该地点周边约 1 英里半径，最近 {_mo} 个月
3. **记录总数**: {total_crimes} 起（平均每月约 {_pm:.0f} 起）
4. **评分方式**: 与 13 个已采样英国区域的分布对比得出百分位，而不是按犯罪数直接扣分。
   本区域属于「{band}」，对应 **{safety_score}/100**。
5. **重要限制**: {CAVEAT_ZH}
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
        _mo = crime_data.get("months_covered", 3)
        _pm = per_month if per_month is not None else 0
        explanation = f"""
**How this score was produced:**

1. **Source**: data.police.uk (official UK police crime data)
2. **Coverage**: about a 1 mile radius around this point, last {_mo} months
3. **Records**: {total_crimes} incidents (about {_pm:.0f} per month)
4. **Method**: ranked against a sample of 13 UK areas rather than deducted from a
   flat count. This area is **{band}**, which maps to **{safety_score}/100**.
5. **Important limitation**: {CAVEAT_EN}
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
    
    description="""Check crime/safety data near a place using data.police.uk (last 6 months): safety score (0-100), level and a night-walk assessment. Prefer a FULL address (street + postcode) from the context's shown properties, but a bare area/postcode works too — use it to answer a safety question about an area under discussion ("晚上安全吗"/"犯罪率高吗"). For a generic "is London safe" use web_search.
检查某地址/区域的治安数据（数据来源 data.police.uk）。""",
    
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
