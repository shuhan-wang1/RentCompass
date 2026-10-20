"""
Tool 3: Check Safety Tool
检查地区的安全指数
"""

from core.tool_system import Tool
from core.maps_service import get_crime_data_by_location
from core.tools._argtext import clean_arg_text
from typing import Optional
import re

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
    # 兼容 address 和 area 参数. Both are free text from a model/planner, so they are
    # normalised at the boundary (None / a number / a lone UTF-16 surrogate all arrive here).
    location = clean_arg_text(address) or clean_arg_text(area)
    user_query = clean_arg_text(user_query)
    if not location and (latitude is not None or longitude is not None):
        location = 'Supplied coordinates'
    if not location:
        # A missing/blank/non-string location is a CALLER error, not a crash. Raising here
        # (outside the try below) turned an empty string into an unhandled exception that
        # every caller had to catch separately; a structured refusal is what the rest of
        # this tool returns and what the agent loop can render honestly.
        return {
            'address': "",
            'safety_score': None,
            'safety_level': 'Unknown',
            'crime_data': {},
            'error': "check_safety needs an address, postcode or area name",
            'scoring_explanation': "No location was supplied, so no score is available.",
            'safety_analysis': "Safety cannot be assessed without a location.",
            'retryable': False,
        }
    
    # 检测用户语言
    is_chinese = _detect_chinese(user_query or location)
    
    try:
        print(
            "   🔒 检查安全性 "
            f"location_chars={len(location)} language={'zh' if is_chinese else 'en'}"
        )
        
        if latitude is not None or longitude is not None:
            crime_data = get_crime_data_by_location(
                location, latitude=latitude, longitude=longitude,
            )
        else:
            crime_data = get_crime_data_by_location(location)
            if _geocode_failed(crime_data):
                crime_data = _coarser_reference(location, clean_arg_text(area), crime_data)
        
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
            # Every queried month FAILED (e.g. HTTP 503): an outage, not "too few records"
            # (R2-32 — the answer said the police data "returned no records").
            police_outage = bool(crime_data.get('failed_months')) and not crime_data.get('successful_months')

            if police_outage:
                safety_score, band = None, None
                scoring_explanation = (
                    "警方数据服务本次未响应，无法给出评分。" if is_chinese
                    else "The police data service did not respond, so no score is given.")
                safety_analysis = (
                    "data.police.uk 本次对所有查询月份都没有响应（服务故障），因此没有检查犯罪情况；"
                    "这不能说明该区域安全或不安全。" if is_chinese
                    else "data.police.uk did not respond for any queried month (a service "
                         "outage), so crime levels were not checked this time. This is not "
                         "evidence either way.")
            elif safety_score is None:
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
                safety_analysis = _generate_safety_analysis(
                    crime_data, location, is_chinese, per_month=per_month)
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
            'radius_m': crime_data.get('radius_m'),
            'radius_basis': crime_data.get('radius_basis'),
            'reference_point': crime_data.get('reference_point'),
            'source': crime_data.get('source'),
            'source_url': crime_data.get('source_url'),
            'data_months': crime_data.get('data_months', []),
            'queried_months': crime_data.get('queried_months', []),
            'successful_months': crime_data.get('successful_months', []),
            'failed_months': crime_data.get('failed_months', []),
            'partial': crime_data.get('partial', False),
            'from_cache': crime_data.get('from_cache', False),
            'cache_status': crime_data.get('cache_status', 'unknown'),
            'possibly_outdated': crime_data.get('possibly_outdated', False),
            'fetched_at': crime_data.get('fetched_at'),
            'reference_basis': reference_note(),
            'caveat': CAVEAT_ZH if is_chinese else CAVEAT_EN,
            'service_status': ('unavailable' if (crime_data or {}).get('failed_months')
                               and not (crime_data or {}).get('successful_months') else 'ok'),
            'recommendation': (
                f"This area has a safety level of {safety_level} with a safety score of "
                f"{safety_score}/100 ({reference_note()}). {CAVEAT_EN}"
                if safety_score is not None else
                "Safety could not be scored: data.police.uk did not respond (service outage). "
                "Say the check failed; do not say it returned no records."
                if (crime_data or {}).get('failed_months') and not (crime_data or {}).get('successful_months') else
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
            'reference_point': None,
            'source': 'data.police.uk',
            'queried_months': [],
            'error': "Safety data lookup failed",
            'scoring_explanation': "Safety data lookup failed; no score is available.",
            'safety_analysis': "Unable to perform safety analysis due to an error.",
            'retryable': True,
        }


# UK outward code (postcode district) at the end of an address: "…, London, N6", "… E1 6AN".
_POSTCODE_DISTRICT = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?)(?:\s+\d[A-Z]{2})?\s*$", re.I)


def _geocode_failed(crime_data) -> bool:
    return isinstance(crime_data, dict) and crime_data.get('error') == "Could not geocode address."


def _coarser_reference(location: str, area: str, failed: dict) -> dict:
    """Retry an ungeocodable address at its postcode district, then at its area (R3-08).

    "Highgate West Hill, London, N6" could not be geocoded and the turn got no safety data at
    all, although the district (N6) and the area (Highgate) resolve. The coarser point is
    used with HONEST precision: reference_point says which fallback it is and that it is not
    the listing's own location."""
    london = 'london' in location.lower()
    candidates = []
    district = _POSTCODE_DISTRICT.search(location.strip().rstrip('.,'))
    if district:
        code = district.group(1).upper()
        candidates.append(('postcode_district', f"{code}, London" if london else code))
    if area and area.strip().lower() != location.strip().lower():
        name = area.strip()
        candidates.append(('area', f"{name}, London" if london and 'london' not in name.lower() else name))
    for kind, query in candidates:
        try:
            data = get_crime_data_by_location(query)
        except Exception:
            continue
        if not isinstance(data, dict) or data.get('error'):
            continue
        ref = dict(data.get('reference_point') or {})
        ref.update({
            'fallback_from': location,
            'fallback_kind': kind,
            'queried_location': query,
            'is_specific_address': False,
            'measured_from': (
                f"the {kind.replace('_', ' ')} {query!r}, because {location!r} could not be "
                "geocoded; this is not the listing's own location"),
        })
        return {**data, 'reference_point': ref}
    return failed


def _detect_chinese(text: str) -> bool:
    """检测文本是否包含中文"""
    if not text:
        return False
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False


def crime_window(crime_data: dict, per_month: float | None = None) -> tuple[int, float]:
    """(months_covered, crimes_per_month) for ONE crime payload — the single place the
    monthly average is computed.

    There used to be two: the scoring block divided by ``months_covered`` (3) while the
    detail block divided the SAME total by a hardcoded 6 and labelled it "6-month total",
    so one reply said "7681 incidents (about 2560 per month)" and, four lines later,
    "Monthly average: 1280 incidents". ``total_crimes_6m`` keeps its historical key name
    but holds MONTHS_OF_CRIME_DATA months of radius data (see maps_service), so 6 was
    simply wrong. Both renderers now read this. Never raises."""
    data = crime_data if isinstance(crime_data, dict) else {}
    try:
        months = int(data.get("months_covered") or 0)
    except (TypeError, ValueError):
        months = 0
    if months <= 0:
        months = 3
    total = data.get("total_crimes_6m", 0)
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = 0.0
    if per_month is None:
        per_month = data.get("crimes_per_month")
    try:
        pm = float(per_month)
    except (TypeError, ValueError):
        pm = None
    if pm is None or pm != pm:            # missing or NaN
        pm = total / months
    return months, pm


def _generate_scoring_explanation(total_crimes: int, safety_score: int, crime_data: dict,
                                  is_chinese: bool, band: str | None = None,
                                  per_month: float | None = None) -> str:
    """生成评分计算方法的详细解释（根据语言生成）"""
    
    _mo, _pm = crime_window(crime_data, per_month)
    if is_chinese:
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


def _generate_safety_analysis(crime_data: dict, location: str, is_chinese: bool,
                              per_month: float | None = None) -> str:
    """Describe the actual observation period without inventing personal night-walk risk.

    The window and the monthly average come from ``crime_window`` — the SAME values the
    scoring block prints — so one reply can no longer state two different averages for
    one total."""
    total = crime_data.get('total_crimes_6m', 0)
    months, per_month = crime_window(crime_data, per_month)
    ref = crime_data.get('reference_point') or {}
    period = ', '.join(crime_data.get('data_months') or []) or 'not supplied'
    origin = ref.get('measured_from') or 'query-point precision was not supplied'
    categories = crime_data.get('category_breakdown') or {}
    if not isinstance(categories, dict):
        categories = {}
    if is_chinese:
        lines = [f'地点：{location}；实际查询点：{origin}',
                 f'统计月份：{period}；{months}个月总计{total}条记录，月均{per_month:g}条。',
                 '覆盖范围：警方接口默认约1英里半径，并非某条步行路线或某套房的个人风险。']
        lines.extend(f'{name}: {count}条' for name, count in list(categories.items())[:3])
        lines.append(CAVEAT_ZH)
    else:
        lines = [f'Location: {location}; actual query point: {origin}',
                 f'Data months: {period}; {months}-month total: {total} incidents; '
                 f'monthly average: {per_month:g}.',
                 'Coverage: the police endpoint defaults to approximately a 1 mile radius; '
                 'it does not establish the safety of a walking route or an individual home.']
        lines.extend(f'{name}: {count} incidents' for name, count in list(categories.items())[:3])
        lines.append(CAVEAT_EN)
    return '\n'.join(lines)


# 创建工具实例
check_safety_tool = Tool(
    name="check_safety",
    
    description="""Check crime/safety data near a place using data.police.uk (the returned data_months and months_covered specify the observation period). Prefer a FULL address (street + postcode), or supply both latitude and longitude to query that exact point. A bare area queries its geocoded point, not an individual property. Only this result's reference_point establishes the actual coordinates/precision; never use another tool's coordinates to claim this safety query checked a property. Quote the returned source, approximate provider radius, data_months and cache freshness; counts do not establish a safe walking route. For a generic "is London safe" use web_search.
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
