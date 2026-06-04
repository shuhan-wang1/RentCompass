"""
Tool 1: Search Properties Tool
搜索符合条件的房源 - 完整的 ReAct Agent 工具

这个工具整合了：
1. Fine-tuned Model：从用户自然语言中提取搜索条件
2. RAG 检索：向量搜索找到相关房源  
3. 智能过滤：硬过滤 + 软过滤
4. 通勤时间计算：真实 API 计算

核心原则：
- 作为 ReAct Agent 的一个工具被调用
- LLM 决定何时调用此工具
- 工具内部处理所有房源搜索逻辑
"""

from core.tool_system import Tool
from typing import Optional, List, Dict
import pandas as pd
import asyncio
from pathlib import Path
import json
import re


def _clean_explanation(desc, travel_min, location):
    """Single-source the commute time in the explanation: strip the static CSV
    Bus/Walk/Cycle/Drive minutes (which disagree with the live TfL time) and append
    the real TfL transit time so the card never shows two conflicting commute times."""
    desc = desc or ''
    cleaned = re.sub(r'(Bus|Walk|Cycle|Drive)[ ]*[0-9]+[ ]*min[,. ]*', '', desc, flags=re.IGNORECASE)
    cleaned = ' '.join(cleaned.split()).strip().rstrip('.,').strip()[:120]
    try:
        suffix = f" (TfL transit: {int(travel_min)} min to {location})"
    except (TypeError, ValueError):
        suffix = ''
    return (cleaned + suffix).strip()


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
    user_query: str = "",
    location: str = None,
    max_budget: int = None,
    max_commute_time: int = None,
    min_budget: int = 500,
    radius_miles: float = 2.0,
    limit: int = 10,
    care_about_safety: bool = False,
    sort_by: str = "value",
    property_features: list = None,  # 🆕 累积的房产特征（如 studio, private）
    accumulated_preferences: list = None,  # 🆕 累积的软性偏好
    budget_period: str = "month",  # 🆕 预算周期：'week' 或 'month'
    **kwargs  # 接受 LLM 可能传递的任何额外参数（如 property_type）
) -> dict:
    """
    完整的房源搜索工具 - 整合 Ollama + RAG + 过滤器
    
    这是 ReAct Agent 调用的主要工具。
    
    流程：
    1. 如果参数不完整，使用 Ollama 从 user_query 提取
    2. 如果仍有缺失参数，返回需要澄清的问题
    3. 参数完整后，执行 RAG 搜索 + 过滤 + 排序
    4. 返回格式化的结果供 Agent 回复
    
    Args:
        user_query: 用户的原始自然语言查询（用于提取参数）
        location: 目标地点（可选，如果缺失会从 user_query 提取）
        max_budget: 最大预算（可选）- 会根据 budget_period 自动转换为月租
        max_commute_time: 最大通勤时间（可选）
        budget_period: 预算周期 'week' 或 'month'，如果是 week 会自动转换为月租
        property_features: 累积的房产特征（如 studio, private, en-suite）
        accumulated_preferences: 累积的软性偏好
        其他参数...
        **kwargs: 接受任何额外参数（LLM 可能会传递 property_type 等）
    
    Returns:
        包含搜索结果或澄清问题的字典
    """
    # 🆕 初始化累积的特征和偏好
    # 修复：确保输入是列表，不是字符串
    if isinstance(property_features, list):
        all_property_features = list(property_features)
    elif isinstance(property_features, str):
        all_property_features = [property_features] if property_features else []
    else:
        all_property_features = []
    
    # 🔧 修复：正确处理 accumulated_preferences，避免 list(string) 变成字符列表
    if isinstance(accumulated_preferences, list):
        # 确保列表中的元素不是单个字符（被错误拆分的情况）
        all_soft_preferences = []
        for item in accumulated_preferences:
            if isinstance(item, str) and len(item) > 1:  # 排除单个字符
                all_soft_preferences.append(item)
            elif isinstance(item, str) and len(item) == 1:
                # 跳过单个字符（可能是之前错误拆分的）
                continue
    elif isinstance(accumulated_preferences, str) and accumulated_preferences:
        all_soft_preferences = [accumulated_preferences]
    else:
        all_soft_preferences = []
    
    # 记录任何额外传递的参数（用于调试）
    if kwargs:
        print(f"   ℹ️ 收到额外参数: {kwargs}")
    
    try:
        print(f"\n{'='*60}")
        print(f"🏠 [SEARCH TOOL] 开始执行房源搜索")
        print(f"   user_query: {user_query}")
        print(f"   location: {location}")
        print(f"   max_budget: {max_budget}")
        print(f"   max_commute_time: {max_commute_time}")
        print(f"   property_features (累积): {all_property_features}")
        print(f"   soft_preferences (累积): {all_soft_preferences}")
        print(f"{'='*60}")
        
        # ================================================================
        # 步骤 1: 检查参数完整性，必要时用 Fine-tuned Model 提取
        # ================================================================
        
        # 如果核心参数缺失，尝试从 user_query 提取
        if not all([location, max_budget, max_commute_time]) and user_query:
            print(f"\n📝 [SEARCH] 参数不完整，使用 Ollama 提取搜索条件...")
            
            # 🆕 构建包含累积信息的查询，帮助模型理解上下文
            enhanced_query = user_query
            if all_property_features:
                enhanced_query = f"Looking for {', '.join(all_property_features)} property. {user_query}"
            
            from core.llm_interface import clarify_and_extract_criteria
            
            criteria_response = clarify_and_extract_criteria(enhanced_query)
            print(f"   Fine-tuned 返回: {json.dumps(criteria_response, ensure_ascii=False, indent=2)}")
            
            # 🆕 累积 Fine-tuned model 提取的特征
            new_features = criteria_response.get('property_tags', []) or []
            # 确保 new_features 是列表，不是字符串
            if isinstance(new_features, str):
                new_features = [new_features] if new_features else []
            for feat in new_features:
                if feat and feat not in all_property_features:
                    all_property_features.append(feat)
            
            # 🔧 修复：soft_preferences 可能是字符串，需要正确处理
            new_prefs = criteria_response.get('soft_preferences', '')
            if isinstance(new_prefs, str) and new_prefs:
                # 如果是字符串，作为整体添加
                if new_prefs not in all_soft_preferences:
                    all_soft_preferences.append(new_prefs)
            elif isinstance(new_prefs, list):
                # 如果是列表，遍历添加
                for pref in new_prefs:
                    if pref and pref not in all_soft_preferences:
                        all_soft_preferences.append(pref)
            
            # 合并提取的参数（优先使用显式传入的参数）
            if not location:
                location = criteria_response.get('destination')
            if not max_budget:
                max_budget = criteria_response.get('max_budget')
            if not max_commute_time:
                max_commute_time = criteria_response.get('max_travel_time')
            
            # 🆕 获取预算周期（周/月）
            extracted_budget_period = criteria_response.get('budget_period', 'month')
            if extracted_budget_period:
                budget_period = extracted_budget_period
            
            # 如果 Ollama 需要澄清
            if criteria_response.get('status') != 'success':
                missing_fields = []
                if not location:
                    missing_fields.append("destination/location (e.g., UCL, King's College, Central London)")
                if not max_budget:
                    missing_fields.append("budget (e.g., £1500/month, £280/week)")
                if not max_commute_time:
                    missing_fields.append("max commute time (e.g., 30 minutes, 45 min)")
                
                question = criteria_response.get('data', {}).get('question', '')
                if not question:
                    question = f"To search for properties, I need: {', '.join(missing_fields)}. Could you please provide this information?"
                
                # 🆕 包含所有已累积的信息
                return {
                    'success': False,
                    'status': 'need_clarification',
                    'question': question,
                    'missing_fields': missing_fields,
                    'extracted_so_far': {
                        'destination': location,
                        'max_budget': max_budget,
                        'max_travel_time': max_commute_time,
                        'property_features': all_property_features,  # 🆕 保留特征
                        'soft_preferences': all_soft_preferences,    # 🆕 保留偏好
                    }
                }
        
        # 最终参数检查
        if not location:
            return {
                'success': False,
                'status': 'need_clarification',
                'question': "Where would you like to commute to? (e.g., UCL, King's College, London Bridge)",
                'missing_fields': ['location'],
                'extracted_so_far': {
                    'destination': None,
                    'max_budget': max_budget,
                    'max_travel_time': max_commute_time,
                    'property_features': all_property_features,
                    'soft_preferences': all_soft_preferences,
                }
            }
        
        # 🔧 修复：正确检查 max_budget，0 是无效值
        if max_budget is None or max_budget == 0:
            return {
                'success': False,
                'status': 'need_clarification', 
                'question': f"What's your monthly budget for rent near {location}?",
                'missing_fields': ['max_budget'],
                'extracted_so_far': {
                    'destination': location,
                    'max_budget': None,
                    'max_travel_time': max_commute_time,
                    'property_features': all_property_features,
                    'soft_preferences': all_soft_preferences,
                }
            }
        
        # 如果没有指定通勤时间，应该询问用户而不是自动假设
        # 🔧 修复：正确检查 max_commute_time，0 是无效值但 None 也需要询问
        if max_commute_time is None or max_commute_time == 0:
            return {
                'success': False,
                'status': 'need_clarification',
                'question': f"What's the maximum commute time you're willing to accept? (e.g., 30 minutes, 45 minutes, 1 hour)",
                'missing_fields': ['max_commute_time'],
                'extracted_so_far': {
                    'destination': location,
                    'max_budget': max_budget,
                    'max_travel_time': None,
                    'property_features': all_property_features,
                    'soft_preferences': all_soft_preferences,
                }
            }
        
        # ================================================================
        # 步骤 1.5: 🆕 周租转月租转换
        # ================================================================
        original_budget = max_budget
        budget_converted = False
        
        if budget_period and budget_period.lower() == 'week':
            # 周租 × 4.33 ≈ 月租（一个月平均 4.33 周）
            max_budget = int(max_budget * 4.33)
            budget_converted = True
            print(f"\n💱 [BUDGET] 周租转月租: £{original_budget}/week → £{max_budget}/month")
        
        # ================================================================
        # 步骤 2: 执行 RAG 搜索（带房产特征过滤）
        # ================================================================
        print(f"\n🔍 [SEARCH] 执行 RAG 搜索...")
        print(f"   📍 位置: {location}")
        print(f"   💰 预算: £{max_budget}/month" + (f" (原始: £{original_budget}/week)" if budget_converted else ""))
        print(f"   ⏱️ 最大通勤: {max_commute_time} 分钟")
        print(f"   🏠 房产特征: {all_property_features}")
        print(f"   💭 软性偏好: {all_soft_preferences}")
        
        from rag.rag_coordinator import RAGCoordinator
        from core.data_loader import load_mock_properties_from_csv, parse_price
        
        # 获取 RAG Coordinator
        rag_coordinator = RAGCoordinator()
        
        # 确保属性有 parsed_price
        all_properties = load_mock_properties_from_csv()
        for prop in all_properties:
            if 'parsed_price' not in prop:
                prop['parsed_price'] = parse_price(prop.get('Price'))
        
        # 重建索引（如果需要）
        if not hasattr(rag_coordinator.property_store, 'index') or rag_coordinator.property_store.index is None:
            rag_coordinator.property_store.build_index(all_properties)
        
        # 🆕 构建包含房产特征的搜索查询
        search_query = f"Find flat near {location} under £{max_budget}"
        if all_property_features:
            search_query = f"Find {', '.join(all_property_features)} flat near {location} under £{max_budget}"
        
        criteria = {
            'destination': location,
            'max_budget': max_budget,
            'max_travel_time': max_commute_time,
            'property_features': all_property_features,  # 🆕 传递房产特征
            'soft_preferences': all_soft_preferences,     # 🆕 传递软性偏好
        }
        
        ranked_properties, past_context, area_info = rag_coordinator.enhanced_search(
            search_query,  # 🆕 使用增强的查询
            criteria
        )
        print(f"   ✅ RAG 返回 {len(ranked_properties)} 个候选房源")
        
        # 🆕 根据房产特征过滤结果
        if all_property_features:
            print(f"\n🔍 [SEARCH] 根据房产特征过滤: {all_property_features}")
            filtered_by_features = []
            for prop in ranked_properties:
                room_type = prop.get('Room_Type_Category', '').lower()
                description = prop.get('Description', '').lower()
                amenities = prop.get('Detailed_Amenities', '').lower()
                
                # 检查是否匹配任何特征
                matches = True
                for feature in all_property_features:
                    feature_lower = feature.lower()
                    if feature_lower in ['studio', 'private', 'en-suite', 'ensuite']:
                        # 检查房型匹配
                        if feature_lower == 'studio' and 'studio' not in room_type:
                            matches = False
                            break
                        if feature_lower == 'private' and 'private' not in room_type and 'private' not in description:
                            matches = False
                            break
                        if feature_lower in ['en-suite', 'ensuite'] and 'en-suite' not in room_type and 'en-suite' not in amenities:
                            matches = False
                            break
                
                if matches:
                    filtered_by_features.append(prop)
            
            if filtered_by_features:
                print(f"   ✅ 特征过滤后剩余 {len(filtered_by_features)} 个房源")
                ranked_properties = filtered_by_features
            else:
                print(f"   ⚠️ 特征过滤后无结果，保留原始结果并在说明中提及")
        
        # ================================================================
        # 步骤 3: 计算通勤时间并过滤
        # ================================================================
        print(f"\n⏱️ [SEARCH] 计算通勤时间...")
        
        from core.maps_service import calculate_travel_time
        import asyncio
        
        top_candidates = ranked_properties[:15]
        loop = asyncio.get_event_loop()
        
        travel_time_tasks = [
            loop.run_in_executor(
                None,
                calculate_travel_time,
                prop.get('Address', ''),
                location
            )
            for prop in top_candidates
        ]
        
        travel_times = await asyncio.gather(*travel_time_tasks, return_exceptions=True)
        
        # 添加通勤时间并过滤
        candidates_with_travel = []
        for prop, travel_time in zip(top_candidates, travel_times):
            if isinstance(travel_time, Exception):
                continue
            if travel_time and travel_time <= max_commute_time:
                prop['travel_time_minutes'] = travel_time
                prop['travel_time'] = travel_time
                candidates_with_travel.append(prop)
        
        print(f"   ✅ 通勤过滤后: {len(candidates_with_travel)} 个房源")
        
        # ================================================================
        # 步骤 4: 应用价格过滤和评分
        # ================================================================
        print(f"\n💰 [SEARCH] 应用价格过滤...")
        
        # 初始化结果列表
        perfect_match = []
        soft_violation = []
        
        if candidates_with_travel:
            # 预处理价格数据
            for prop in candidates_with_travel:
                if 'price' not in prop or not prop['price']:
                    prop['price'] = prop.get('parsed_price', parse_price(prop.get('Price', '')))
            
            perfect_match, soft_violation = PropertyFilter.apply_hard_filters(
                properties=candidates_with_travel,
                budget=max_budget,
                max_commute=max_commute_time,
                location_keywords=location,
                care_about_safety=care_about_safety
            )
            
            print(f"   ✅ 完全符合: {len(perfect_match)} 个")
            print(f"   ⚠️ 超预算可考虑: {len(soft_violation)} 个")
        else:
            print(f"   ⚠️ 没有符合通勤时间的房源，跳过价格过滤")
        
        # 排序
        def sort_key(prop):
            return -prop.get('recommendation_score', 0)
        
        perfect_match.sort(key=sort_key)
        soft_violation.sort(key=sort_key)
        
        # ================================================================
        # 步骤 4.5: 如果没有完全符合的结果，使用 RAG 找最相似的
        # ================================================================
        if not perfect_match and not soft_violation:
            print(f"\n⚠️ [SEARCH] 没有找到符合预算和通勤的房源，尝试 RAG 语义搜索最相似的...")
            
            # 使用 RAG 的语义搜索找到最相似的房源
            similar_properties = rag_coordinator.property_store.search(
                f"flat apartment near {location} budget {max_budget}",
                top_k=10
            )
            
            if similar_properties:
                # 计算这些房源的通勤时间
                similar_with_commute = []
                for prop in similar_properties[:6]:  # 只处理前6个
                    try:
                        travel_time = calculate_travel_time(
                            prop.get('Address', ''),
                            location
                        )
                        if travel_time and travel_time <= max_commute_time * 1.5:  # 允许通勤时间超50%
                            prop['travel_time'] = travel_time
                            prop['price'] = prop.get('parsed_price', parse_price(prop.get('Price', '')))
                            similar_with_commute.append(prop)
                    except:
                        continue
                
                if similar_with_commute:
                    # 按价格排序，找最接近预算的
                    similar_with_commute.sort(key=lambda x: x.get('price', float('inf')))
                    
                    # 取前3个最接近的
                    closest_3 = similar_with_commute[:3]
                    
                    # 计算建议的预算
                    min_price_needed = min(p.get('price', 0) for p in closest_3)
                    suggested_budget = int(min_price_needed * 1.05)  # 加5%余量
                    budget_increase = suggested_budget - max_budget
                    
                    # 格式化这3个推荐
                    similar_formatted = []
                    for i, prop in enumerate(closest_3, 1):
                        price = int(prop.get('price', 0))
                        over_budget = price - max_budget
                        over_percentage = round((over_budget / max_budget) * 100, 1)
                        
                        # 🆕 获取图片和地理位置
                        images = prop.get('Images', prop.get('images', []))
                        if isinstance(images, str):
                            images = [images] if images else []
                        geo_location = prop.get('Geo_Location', prop.get('geo_location', ''))
                        
                        similar_formatted.append({
                            'rank': i,
                            'address': prop.get('Address', prop.get('address', 'Unknown')),
                            'price': f"£{price}/month",
                            'travel_time': f"{int(prop.get('travel_time', 0))} min to {location}",
                            'budget_status': f"⚠️ Over budget by £{over_budget} ({over_percentage}%)",
                            'price_raw': price,
                            'over_budget': over_budget,
                            'similarity_score': round(prop.get('similarity_score', 0) * 100, 1),
                            'property_type': prop.get('Type', prop.get('type', 'Flat')),
                            'bedrooms': prop.get('Bedrooms', prop.get('bedrooms', 'N/A')),
                            'match_type': 'similar_suggestion',
                            'url': prop.get('URL', prop.get('url', '')),
                            'images': images,  # 🆕 添加图片
                            'geo_location': geo_location,  # 🆕 添加地理位置
                            'explanation': _clean_explanation(prop.get('Description', ''), prop.get('travel_time'), location),
                        })
                    
                    return {
                        'success': True,
                        'status': 'no_exact_match_but_similar',
                        'message': f"Based on our database, no properties were found within your budget of £{max_budget}/month and {max_commute_time} min commute to {location}.",
                        'suggestion': f"However, I found {len(closest_3)} similar properties. The closest match is £{int(closest_3[0].get('price', 0))}/month. Would you consider increasing your budget by approximately £{budget_increase} (to £{suggested_budget}/month)?",
                        'similar_properties': similar_formatted,
                        'suggested_budget': suggested_budget,
                        'budget_increase_needed': budget_increase,
                        'search_criteria': {
                            'destination': location,
                            'max_budget': max_budget,
                            'max_commute_time': max_commute_time
                        },
                        'recommendations': similar_formatted  # 也放在 recommendations 中让前端可以展示
                    }
            
            # 如果 RAG 也没有找到
            return {
                'success': True,
                'status': 'no_results',
                'message': f"Unfortunately, no properties were found in our database near {location} within {max_commute_time} minutes. This might be because:\n\n1. Your budget of £{max_budget}/month might be too low for the {location} area\n2. The commute time of {max_commute_time} minutes might be too restrictive\n\n**Suggestions:**\n- Try increasing your budget to £{int(max_budget * 1.3)}/month\n- Or extend your commute time to {int(max_commute_time * 1.5)} minutes\n- Or consider different nearby areas",
                'recommendations': []
            }
        
        # 限制结果数量
        perfect_limited = perfect_match[:limit]
        soft_limited = soft_violation[:3]
        
        # ================================================================
        # 步骤 5: 格式化结果
        # ================================================================
        all_results = perfect_limited + soft_limited
        
        formatted_results = []
        for i, prop in enumerate(all_results[:limit], 1):
            # 🆕 获取图片列表（如果有的话）
            images = prop.get('Images', prop.get('images', []))
            if isinstance(images, str):
                images = [images] if images else []
            
            # 🆕 获取地理位置
            geo_location = prop.get('Geo_Location', prop.get('geo_location', ''))
            if not geo_location:
                # 尝试从地址生成一个默认的
                address = prop.get('Address', prop.get('address', ''))
                if 'London' in address:
                    geo_location = '51.5074,-0.1278'  # London center fallback
            
            formatted_results.append({
                'rank': i,
                'address': prop.get('Address', prop.get('address', 'Unknown')),
                'price': f"£{int(prop.get('price', 0))}/month",
                'travel_time': f"{int(prop.get('travel_time', 0))} min to {location}",
                'budget_status': prop.get('budget_status', ''),
                'score': prop.get('recommendation_score', 0),
                'property_type': prop.get('Type', prop.get('type', 'Flat')),
                'bedrooms': prop.get('Bedrooms', prop.get('bedrooms', 'N/A')),
                'match_type': prop.get('match_type', 'perfect'),
                'url': prop.get('URL', prop.get('url', '')),
                'images': images,  # 🆕 添加图片列表
                'geo_location': geo_location,  # 🆕 添加地理位置
                'explanation': _clean_explanation(prop.get('Description', prop.get('description', '')), prop.get('travel_time'), location),  # 单一来源：真实 TfL 时间
            })
        
        return {
            'success': True,
            'status': 'found',
            'total_found': len(all_results),
            'search_criteria': {
                'destination': location,
                'max_budget': max_budget,
                'max_travel_time': max_commute_time,
                'property_features': all_property_features,  # 🆕 包含房产特征
                'soft_preferences': all_soft_preferences,     # 🆕 包含软性偏好
            },
            'recommendations': formatted_results,
            'summary': f"Found {len(perfect_match)} properties within budget (£{max_budget}/month) and {len(soft_violation)} slightly over budget, all within {max_commute_time} min of {location}."
        }

    except Exception as e:
        print(f"   ❌ 搜索房源出错: {e}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'status': 'error',
            'error': str(e)
        }


# 创建工具实例
search_properties_tool = Tool(
    name="search_properties",

    description="""Search for SPECIFIC rental properties in the UK database.

⚠️ USE THIS TOOL ONLY WHEN:
- User explicitly wants to FIND/SEARCH for a specific property they can rent
- User provides search criteria like budget, location, commute time
- User says things like "帮我找房", "I want to find a flat", "search for apartments", "找房子"

❌ DO NOT USE THIS TOOL FOR:
- General questions about rent prices or averages ("租房价格多少")
- Questions about living costs, transport costs, food costs
- Questions about areas, neighborhoods, or safety
- "租房价格怎么样" = asking about rent prices → use web_search
- "介绍租房信息" = asking about renting info → use web_search

WORKFLOW:
1. Call this tool with the user's query
2. If parameters are missing, the tool returns a clarification question
3. Once complete, returns property recommendations

For GENERAL INFORMATION questions about rent, use web_search instead.""",

    func=search_properties_impl,

    parameters={
        'type': 'object',
        'properties': {
            'user_query': {
                'type': 'string',
                'description': 'The user\'s natural language query about finding properties. The tool will extract location, budget, and commute time from this.'
            },
            'location': {
                'type': 'string',
                'description': 'Target commute destination (e.g., UCL, King\'s College, London Bridge). Optional if user_query contains this.'
            },
            'max_budget': {
                'type': 'integer',
                'description': 'Maximum monthly budget in GBP (e.g., 1500, 2000). Optional if user_query contains this.'
            },
            'max_commute_time': {
                'type': 'integer',
                'description': 'Maximum commute time in minutes. ONLY provide if user explicitly mentions a time limit. Do NOT assume a default.'
            },
            'care_about_safety': {
                'type': 'boolean',
                'description': 'Whether user cares about area safety/crime rates.',
                'default': False
            },
            'limit': {
                'type': 'integer',
                'description': 'Maximum number of results to return.',
                'default': 10
            }
        },
        'required': []  # 没有必须参数 - 工具内部会处理
    },

    max_retries=2
)
