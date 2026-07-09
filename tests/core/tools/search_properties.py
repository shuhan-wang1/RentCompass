"""
Tool 1: Search Properties Tool
搜索符合条件的房源 - 包含智能过滤
"""

from core.tool_system import Tool
from typing import Optional, List, Dict
import pandas as pd
import asyncio
from pathlib import Path


class PropertyFilter:
    """严格的过滤器 - 用户必须满足的条件"""

    @staticmethod
    def apply_hard_filters(
        properties: List[Dict],
        budget: int,
        max_commute: int,
        location_keywords: str,
        care_about_safety: bool = False
    ) -> tuple[List[Dict], List[Dict]]:
        """
        应用硬过滤器和软过滤器
        
        返回: (完全符合, 轻微超预算的)
        
        硬过滤规则（必须满足）:
        - 通勤时间 ≤ max_commute (不能违反)
        - 价格 ≤ budget (基准)
        
        软过滤规则（可以违反但需说明）:
        - 价格 ≤ budget × 1.15 (允许超预算15%)
        """
        perfect_match = []
        soft_violation = []  # 超预算但通勤符合

        for prop in properties:
            try:
                # 获取属性，处理异常情况
                price = float(prop.get('price', float('inf')))
                commute = float(prop.get('travel_time', float('inf')))
                
                # ⚠️ 硬过滤：通勤时间是绝对要求
                if commute > max_commute:
                    continue  # 过滤掉，不考虑
                
                # 价格检查
                if price <= budget:
                    # ✅ 完全符合
                    perfect_match.append({
                        **prop,
                        'match_type': 'perfect',
                        'budget_status': '✅ 在预算内',
                        'price_diff': 0,
                        'price_diff_percentage': 0.0,
                        'commute_status': '✅ 通勤符合',
                        'recommendation_score': PropertyFilter.calculate_score(price, commute, budget, max_commute)
                    })
                elif price <= budget * 1.15:  # 允许超预算最多15%
                    # ⚠️ 轻微超预算（可推荐但需说明）
                    price_diff = price - budget
                    price_diff_percentage = round((price_diff / budget) * 100, 1)
                    
                    soft_violation.append({
                        **prop,
                        'match_type': 'soft_violation',
                        'budget_status': f'⚠️ 超预算 £{int(price_diff)}',
                        'price_diff': price_diff,
                        'price_diff_percentage': price_diff_percentage,
                        'commute_status': '✅ 通勤符合',
                        'recommendation_score': PropertyFilter.calculate_score(price, commute, budget, max_commute)
                    })
                # else: 超过软过滤阈值，完全排除
                
            except (ValueError, TypeError) as e:
                print(f"   ⚠️ 跳过房源 {prop.get('address', 'Unknown')}: 数据格式错误")
                continue

        return perfect_match, soft_violation
    
    @staticmethod
    def calculate_score(price: float, commute: float, budget: int, max_commute: int) -> float:
        """
        计算推荐分数 (0-100)
        
        分数 = 价格匹配度(50%) + 通勤匹配度(50%)
        
        - 价格匹配度: 越接近预算越高
        - 通勤匹配度: 通勤时间越短越高
        """
        # 价格匹配度：0-50分
        price_match = max(0, 50 * (1 - (price - budget) / budget)) if price >= budget else 50
        
        # 通勤匹配度：0-50分
        commute_match = max(0, 50 * (1 - commute / max_commute))
        
        total_score = price_match + commute_match
        return round(total_score, 1)


async def search_properties_impl(
    location: str,
    max_budget: int,
    max_commute_time: int = 50,
    min_budget: int = 500,
    radius_miles: float = 2.0,
    limit: int = 25,
    care_about_safety: bool = False,
    sort_by: str = "value"  # "price", "commute", "value"
) -> dict:
    """
    实际执行的搜索房源函数 - 现在包含过滤和排序
    
    Args:
        location: 目标地点
        max_budget: 最大预算
        max_commute_time: 最大通勤时间（硬过滤）
        min_budget: 最小预算
        radius_miles: 搜索半径
        limit: 结果限制
        care_about_safety: 是否关心安全性
        sort_by: 排序方式
    
    Returns:
        包含分类房源的字典
    """
    try:
        from core.data_loader import load_mock_properties_from_csv
        from core.location_service import resolve_location

        print(f"\n   🔍 搜索参数:")
        print(f"      📍 位置: {location}")
        print(f"      💰 预算: £{min_budget} - £{max_budget}")
        print(f"      ⏱️ 通勤: ≤{max_commute_time} 分钟")
        print(f"      🔄 排序: {sort_by}")

        # 加载所有房源
        all_properties = load_mock_properties_from_csv()
        print(f"      📊 总房源数: {len(all_properties)}")

        # 预处理：确保数据一致性
        for prop in all_properties:
            # 清理价格数据
            if isinstance(prop.get('price'), str):
                prop['price'] = float(prop['price'].replace('£', '').replace(',', ''))
            
            # 清理通勤时间
            if isinstance(prop.get('travel_time'), str):
                prop['travel_time'] = float(prop['travel_time'].split()[0])
        
        # 基础过滤：预算范围
        budget_filtered = [
            p for p in all_properties
            if min_budget <= float(p.get('price', 0)) <= max_budget * 1.2  # 包括超预算的供后续过滤
        ]
        print(f"      ✅ 预算过滤后: {len(budget_filtered)} 个房源")

        # 应用硬过滤和软过滤
        perfect_match, soft_violation = PropertyFilter.apply_hard_filters(
            properties=budget_filtered,
            budget=max_budget,
            max_commute=max_commute_time,
            location_keywords=location,
            care_about_safety=care_about_safety
        )
        
        print(f"      ✅ 完全符合: {len(perfect_match)} 个")
        print(f"      ⚠️ 超预算但可考虑: {len(soft_violation)} 个")

        # 排序逻辑
        def sort_by_value(prop):
            """综合评分排序 - 更好的选择"""
            return -prop.get('recommendation_score', 0)
        
        def sort_by_price(prop):
            """按价格排序"""
            return prop.get('price', float('inf'))
        
        def sort_by_commute(prop):
            """按通勤时间排序"""
            return prop.get('travel_time', float('inf'))

        # 排序完全符合的房源
        if sort_by == "value":
            perfect_match.sort(key=sort_by_value)
        elif sort_by == "price":
            perfect_match.sort(key=sort_by_price)
        elif sort_by == "commute":
            perfect_match.sort(key=sort_by_commute)
        
        # 排序软违规房源（同样的逻辑）
        if sort_by == "value":
            soft_violation.sort(key=sort_by_value)
        elif sort_by == "price":
            soft_violation.sort(key=sort_by_price)
        elif sort_by == "commute":
            soft_violation.sort(key=sort_by_commute)

        # 限制数量
        perfect_match_limited = perfect_match[:limit]
        soft_violation_limited = soft_violation[:max(3, limit // 5)]  # 软违规最多总数的1/5

        return {
            'success': True,
            'perfect_match': perfect_match_limited,
            'soft_violation': soft_violation_limited,
            'total_perfect': len(perfect_match),
            'total_soft_violation': len(soft_violation),
            'search_metadata': {
                'location': location,
                'budget': max_budget,
                'max_commute': max_commute_time,
                'sort_by': sort_by,
                'results_returned': len(perfect_match_limited) + len(soft_violation_limited),
                'filter_explanation': {
                    'hard_filters': [
                        f'通勤时间 ≤ {max_commute_time} 分钟（不可违反）',
                        f'价格基准: £{max_budget}'
                    ],
                    'soft_filters': [
                        f'允许超预算最多 15%（£{int(max_budget * 0.15)}）'
                    ]
                }
            }
        }

    except Exception as e:
        print(f"   ❌ 搜索房源出错: {e}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e),
            'perfect_match': [],
            'soft_violation': []
        }


# 创建工具实例
search_properties_tool = Tool(
    name="search_properties",

    description="""
🏠 **搜索英国租房房源 - 智能过滤版本**

**核心功能:**
✅ 在指定位置搜索可租房源
✅ 应用硬过滤器（通勤时间是绝对要求）
✅ 智能分类房源（完全符合 vs 超预算）
✅ 综合评分排序

**硬过滤规则（必须满足）:**
- 通勤时间 ≤ max_commute_time（关键要求，不能违反）
- 价格用作基准（但可灵活考虑）

**软过滤规则（可灵活违反）:**
- 允许超预算最多 15%

**返回内容:**
- perfect_match: 完全符合条件的房源（TOP推荐）
- soft_violation: 轻微超预算但值得考虑的房源
- 每个房源包含推荐分数和详细说明

**使用示例:**
"Find me a flat near UCL, under £2200, travel time within 50 minutes"
→ 搜索会自动过滤和排序

**重要:**
用户说"不关心安全"时，不要让安全性影响过滤逻辑
""",

    func=search_properties_impl,

    parameters={
        'type': 'object',
        'properties': {
            'location': {
                'type': 'string',
                'description': '目标位置（如 UCL、King\'s College、Bloomsbury、Camden 等）'
            },
            'max_budget': {
                'type': 'integer',
                'description': '最大预算（英镑/月）- 这是硬限制'
            },
            'max_commute_time': {
                'type': 'integer',
                'description': '最大通勤时间（分钟）- 这是硬过滤，不能违反',
                'default': 50
            },
            'min_budget': {
                'type': 'integer',
                'description': '最小预算（英镑/月），默认 500',
                'default': 500
            },
            'care_about_safety': {
                'type': 'boolean',
                'description': '是否关心安全性（犯罪率）。如果用户明确说不关心，设为 false',
                'default': True
            },
            'sort_by': {
                'type': 'string',
                'description': '排序方式：value（综合评分，推荐）| price（按价格） | commute（按通勤时间）',
                'default': 'value',
                'enum': ['value', 'price', 'commute']
            },
            'limit': {
                'type': 'integer',
                'description': '返回结果数限制，默认 25',
                'default': 25
            }
        },
        'required': ['location', 'max_budget', 'max_commute_time']
    },

    max_retries=2
)
