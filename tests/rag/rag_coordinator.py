# rag_coordinator.py
from .property_embeddings import PropertyEmbeddingStore
from .conversation_memory import ConversationMemory
from .area_knowledge import AreaKnowledgeBase

class RAGCoordinator:
    def __init__(self):
        self.property_store = PropertyEmbeddingStore()
        self.conversation_memory = ConversationMemory()
        self.area_knowledge = AreaKnowledgeBase()  # New component
    
    def enhanced_search(self, user_query: str, criteria: dict):
        """
        ✅ FIXED: Multi-source retrieval with reranking
        现在支持两种搜索模式：
        1. 新查询模式 - 使用用户查询的语义搜索
        2. 澄清回复模式 - 使用完整条件而不是澄清回复文本
        """
        
        # ✅ FIXED: 当是澄清回复时，不使用澄清文本作为查询
        # 而是使用目标位置和其他条件来构造更有意义的搜索查询
        if user_query and len(user_query) < 30 and ('do not' in user_query.lower() or 'nope' in user_query.lower() or 'nothing' in user_query.lower()):
            # 这看起来像是澄清回复，使用目标位置作为查询
            search_query = criteria.get('destination', 'London')
            print(f"    -> [RAG] Detected clarification reply, using location-based search: '{search_query}'")
        else:
            search_query = user_query
        
        # 1. Semantic property search
        print(f"    -> [RAG] Starting semantic search for: {search_query[:50]}...")
        semantic_results = self.property_store.search(search_query, top_k=20)
        print(f"    -> [RAG] Got {len(semantic_results)} semantic results")
        
        # 2. Get relevant past conversations
        past_context = []
        try:
            past_context = self.conversation_memory.retrieve_relevant_history(
                user_query, n_results=3
            )
        except Exception as e:
            print(f"    -> [RAG] Warning: Could not retrieve conversation history: {e}")
        
        # 3. Retrieve area-specific knowledge
        area_info = []
        try:
            location = criteria.get('destination')
            area_info = self.area_knowledge.get_context(location) if location else []
        except Exception as e:
            print(f"    -> [RAG] Warning: Could not retrieve area knowledge: {e}")
        
        # 4. Hybrid scoring (semantic + rules)
        scored_results = self._hybrid_rank(
            semantic_results, criteria, area_info
        )
        
        print(f"    -> [RAG] Returning {len(scored_results)} ranked results")
        return scored_results, past_context, area_info
    
    def _hybrid_rank(self, properties, criteria, area_info):
        """
        ✅ FIXED: 组合语义相似性和硬约束
        现在分为三个类别:
        1. 在预算内 (perfect_match) - 优先推荐
        2. 微超预算 (soft_violation) - 仅在有充分理由时推荐
        3. 超出预算太多 (reject) - 不推荐
        """
        max_budget = criteria.get('max_budget', 999999)
        print(f"    -> [DEBUG] _hybrid_rank: Input {len(properties)} properties, max_budget: {max_budget}")
        
        perfect_match = []
        soft_violation = []  # 超预算但可考虑（最多+15%）
        rejected = 0
        
        for i, prop in enumerate(properties):
            # ✅ FIXED: 处理 parsed_price 和 Price 两种字段
            prop_price = prop.get('parsed_price')
            if prop_price is None:
                # 如果没有 parsed_price，尝试从 Price 字段解析
                price_str = prop.get('Price', '')
                try:
                    # 例如: "£1,600 pcm" -> 1600
                    prop_price = float(price_str.replace('£', '').replace(',', '').replace(' pcm', '').strip())
                    prop['parsed_price'] = prop_price
                except (ValueError, AttributeError):
                    print(f"    ⚠️ Could not parse price for {prop.get('Address', 'Unknown')}: {price_str}")
                    continue  # 跳过无法解析价格的房源
            
            # ✅ 硬过滤1: 如果超预算 > 15%，直接排除
            if prop_price > max_budget * 1.15:
                rejected += 1
                print(f"    -> [DEBUG] Prop {i}: {prop.get('Address', 'Unknown')[:40]} REJECTED - Price £{prop_price} > budget limit £{max_budget * 1.15}")
                continue
            
            score = prop.get('similarity_score', 0) * 0.4  # Semantic weight
            
            # Rule-based boosting from criteria
            if 'max_travel_time' in criteria and prop.get('travel_time_minutes', 999) <= criteria['max_travel_time']:
                score += 0.3
            
            if 'max_budget' in criteria and prop_price <= max_budget:
                score += 0.2  # 预算内加分
            else:
                score += 0.05  # 微超预算只加少量分
            
            # Safety concerns boost from soft preferences
            soft_prefs = criteria.get('soft_preferences', '')
            crime_trend = prop.get('crime_data_summary', {}).get('crime_trend')
            if soft_prefs and 'safe' in soft_prefs.lower():
                if crime_trend == 'decreasing':
                    score += 0.1
            
            prop['final_score'] = score
            
            # 分类
            if prop_price <= max_budget:
                perfect_match.append(prop)
            else:
                soft_violation.append(prop)
        
        # ✅ 合并并排序: 优先返回预算内的，然后是微超预算的
        sorted_perfect = sorted(perfect_match, key=lambda x: x['final_score'], reverse=True)
        sorted_soft = sorted(soft_violation, key=lambda x: x['final_score'], reverse=True)
        
        # 限制超预算房源数量（最多展示2个，且必须在完全符合的房源之后）
        # 这样可以给用户建议，但不会让超预算房源成为主要选择
        result = sorted_perfect + sorted_soft[:2]
        print(f"    -> [DEBUG] _hybrid_rank result: {len(sorted_perfect)} perfect + {min(2, len(sorted_soft))} soft_violation ({rejected} rejected) = {len(result)} total")
        return result
