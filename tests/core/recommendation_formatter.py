# 在 format_search_results 方法中添加

class RecommendationFormatter:
    
    @staticmethod
    def format_search_results(search_result: Dict, care_about_safety: bool = True) -> List[PropertyRecommendation]:
        """
        将搜索结果转换为格式化推荐
        
        Args:
            care_about_safety: 用户是否关心安全性
        """
        recommendations = []
        rank = 1
        
        # 第一部分：完全符合的房源
        for prop in search_result.get('perfect_match', []):
            rec = RecommendationFormatter._format_perfect_match(prop, rank, care_about_safety)
            recommendations.append(rec)
            rank += 1
        
        # 第二部分：超预算但值得考虑的房源
        for prop in search_result.get('soft_violation', []):
            rec = RecommendationFormatter._format_soft_violation(
                prop, 
                rank, 
                search_result['search_metadata']['budget'],
                care_about_safety  # 传递用户偏好
            )
            recommendations.append(rec)
            rank += 1
        
        return recommendations
    
    @staticmethod
    def _format_perfect_match(prop: Dict, rank: int, care_about_safety: bool = True) -> PropertyRecommendation:
        """格式化完全符合的房源"""
        why_recommended = [
            f"✅ 价格 £{int(prop.get('price', 0))} 在预算内",
            f"✅ 通勤仅 {int(prop.get('travel_time', 0))} 分钟",
            "🏆 这是完全符合您需求的选项"
        ]
        
        # ✨ 只有用户关心安全时，才添加安全信息
        if care_about_safety and prop.get('safety_info'):
            why_recommended.append(f"🔒 安全等级: {prop.get('safety_info', 'Unknown')}")
        
        return PropertyRecommendation(
            rank=rank,
            address=prop.get('address', 'Unknown'),
            price=int(prop.get('price', 0)),
            price_status='✅ 在预算内',
            travel_time=int(prop.get('travel_time', 0)),
            commute_status='✅ 通勤符合',
            recommendation_score=prop.get('recommendation_score', 0),
            bedrooms=int(prop.get('bedrooms', 0)),
            description=prop.get('description', '') if care_about_safety else PropertyRecommendation._clean_description(prop.get('description', ''), care_about_safety),
            why_recommended=why_recommended
        )
    
    @staticmethod
    def _format_soft_violation(
        prop: Dict, 
        rank: int, 
        budget: int,
        care_about_safety: bool = True
    ) -> PropertyRecommendation:
        """格式化超预算但值得考虑的房源"""
        price = int(prop.get('price', 0))
        price_diff = price - budget
        price_diff_percentage = prop.get('price_diff_percentage', 0)
        commute = int(prop.get('travel_time', 0))
        
        # 生成推荐理由
        why_recommended = []
        
        # 理由1：通勤时间特别短
        if commute < 20:
            why_recommended.append(f"⏱️ 通勤时间超短：仅 {commute} 分钟")
        elif commute < 30:
            why_recommended.append(f"⏱️ 通勤时间很好：仅 {commute} 分钟")
        
        # 理由2：超预算比例小
        if price_diff_percentage <= 10:
            why_recommended.append(f"💷 超预算比例极小：仅 {price_diff_percentage}%（£{int(price_diff)}）")
        elif price_diff_percentage <= 15:
            why_recommended.append(f"💷 超预算可控：仅 {price_diff_percentage}%（£{int(price_diff)}）")
        
        # 理由3：房间数
        bedrooms = int(prop.get('bedrooms', 0))
        if bedrooms >= 2:
            why_recommended.append(f"🏠 提供 {bedrooms} 间卧室，空间充足")
        
        # ✨ 只有用户关心安全时，才添加安全理由
        if care_about_safety and prop.get('safety_info'):
            why_recommended.append(f"🔒 安全等级: {prop.get('safety_info', 'Unknown')}")
        
        # 理由4：综合评分
        score = prop.get('recommendation_score', 0)
        if score >= 75:
            why_recommended.append(f"🏆 综合评分 {score}/100（价格+通勤平衡很好）")
        
        # 最后的总结
        why_recommended.append(
            f"📝 虽然超预算 £{int(price_diff)}，但综合考虑通勤便利性和整体性价比，这是很值得考虑的选项"
        )
        
        return PropertyRecommendation(
            rank=rank,
            address=prop.get('address', 'Unknown'),
            price=price,
            price_status=f'⚠️ 超预算 £{int(price_diff)} ({price_diff_percentage}%)',
            travel_time=commute,
            commute_status='✅ 通勤符合',
            recommendation_score=score,
            bedrooms=bedrooms,
            description=PropertyRecommendation._clean_description(prop.get('description', ''), care_about_safety),
            why_recommended=why_recommended
        )
    
    @staticmethod
    def _clean_description(description: str, care_about_safety: bool) -> str:
        """
        清理描述中的不相关信息
        如果用户不关心安全，移除所有犯罪相关的内容
        """
        if not care_about_safety and description:
            # 移除犯罪相关的信息
            import re
            # 移除包含 crime, crimes, criminal, safety 等词的句子
            patterns = [
                r'[^.!?]*(?:crime|crimes|criminal|safety|report|reported)[^.!?]*[.!?]',
                r'[^.!?]*(?:has seen|witnessed|area has)[^.!?]*(?:crime|crimes)[^.!?]*[.!?]',
            ]
            
            cleaned = description
            for pattern in patterns:
                cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
            
            # 清理多余空格
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            return cleaned
        
        return description