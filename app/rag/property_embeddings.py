# property_embeddings.py
from sentence_transformers import SentenceTransformer
import numpy as np
import faiss

class PropertyEmbeddingStore:
    def __init__(self):
        print("    -> [DEBUG] Initializing PropertyEmbeddingStore...")
        # This line downloads or loads the model, which can be a point of failure
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.index = None
        self.properties = []
        self.property_name_index = {}  # 🆕 按名称索引，用于直接查找
        print("    -> [DEBUG] PropertyEmbeddingStore initialized successfully.")
    
    def build_index(self, properties: list[dict]):
        """Create FAISS index from property descriptions"""
        if not properties:
            print("    -> [WARNING] No properties provided for indexing!")
            return
        
        texts = []
        for prop in properties:
            # Combine relevant fields into rich text - 🆕 使用增强描述
            address = prop.get('Address', '')
            price = prop.get('Price', '')
            description = prop.get('Enhanced_Description', '') or prop.get('Description', '')
            amenities = prop.get('Detailed_Amenities', '')
            guest_policy = prop.get('Guest_Policy', '')
            
            # 🆕 组合更多字段以提高检索质量
            text = f"{address} {price} {description} {amenities} {guest_policy}"
            texts.append(text.strip())
            
            # 🆕 建立名称索引（用于直接查找特定房产）
            # 提取房产名称（如 "Scape Bloomsbury", "Spring Mews" 等）
            name_parts = address.split(',')[0].strip()
            self.property_name_index[name_parts.lower()] = prop
            
            # 也存储简短版本 - 使用安全的方式
            for word in ['Scape', 'Vega', 'City', 'Spring', 'Fusion', 'Tufnell']:
                if word.lower() in address.lower():
                    # 简单存储：关键词 -> 房产
                    self.property_name_index[word.lower()] = prop
                    # 也尝试存储完整名称（如 "scape bloomsbury"）
                    try:
                        if word in address:
                            parts_after = address.split(word)[-1].strip().split()[0] if address.split(word)[-1].strip() else ''
                            if parts_after:
                                full_key = f"{word.lower()} {parts_after.lower()}"
                                self.property_name_index[full_key] = prop
                    except (IndexError, ValueError):
                        pass  # 跳过解析错误
        
        if not texts:
            print("    -> [WARNING] No valid text to embed!")
            return
        
        print(f"    -> [DEBUG] Encoding {len(texts)} property texts...")
        embeddings = self.model.encode(texts, show_progress_bar=False)
        
        # FAISS index for fast similarity search
        dimension = embeddings.shape[1]
        self.index = faiss.IndexFlatIP(dimension)  # Inner product (cosine)
        faiss.normalize_L2(embeddings)
        self.index.add(embeddings)
        self.properties = properties
        print(f"    -> [DEBUG] FAISS index created with {len(properties)} properties")
        print(f"    -> [DEBUG] Property name index: {list(self.property_name_index.keys())}")
    
    def get_property_by_name(self, name: str) -> dict | None:
        """🆕 直接按名称查找房产（用于对比查询）"""
        name_lower = name.lower().strip()
        
        # 直接匹配
        if name_lower in self.property_name_index:
            return self.property_name_index[name_lower]
        
        # 模糊匹配
        for key, prop in self.property_name_index.items():
            if name_lower in key or key in name_lower:
                return prop
        
        # 在所有房产中搜索
        for prop in self.properties:
            if name_lower in prop.get('Address', '').lower():
                return prop
        
        return None
    
    def search_by_names(self, property_names: list[str], top_k_per_name: int = 1) -> list[dict]:
        """🆕 按名称列表查找多个房产（用于对比查询）
        
        Args:
            property_names: 要查找的房产名称列表
            top_k_per_name: 每个名称返回的最大匹配数
            
        Returns:
            匹配到的房产列表，带有相似度分数
        """
        results = []
        seen_addresses = set()  # 避免重复
        
        for name in property_names:
            if not name or not isinstance(name, str):
                continue
                
            # 首先尝试精确匹配
            prop = self.get_property_by_name(name)
            if prop:
                address = prop.get('Address', '')
                if address not in seen_addresses:
                    prop_copy = prop.copy()
                    prop_copy['match_type'] = 'exact'
                    prop_copy['search_query'] = name
                    results.append(prop_copy)
                    seen_addresses.add(address)
                    print(f"    -> [DEBUG] 精确匹配房产: {address[:50]}...")
                continue
            
            # 尝试语义搜索
            if self.index is not None:
                query_embedding = self.model.encode([name])
                faiss.normalize_L2(query_embedding)
                scores, indices = self.index.search(query_embedding, min(top_k_per_name * 2, len(self.properties)))
                
                count = 0
                for idx, score in zip(indices[0], scores[0]):
                    if idx < len(self.properties) and count < top_k_per_name:
                        prop = self.properties[idx]
                        address = prop.get('Address', '')
                        if address not in seen_addresses:
                            prop_copy = prop.copy()
                            prop_copy['similarity_score'] = float(score)
                            prop_copy['match_type'] = 'semantic'
                            prop_copy['search_query'] = name
                            results.append(prop_copy)
                            seen_addresses.add(address)
                            count += 1
                            print(f"    -> [DEBUG] 语义匹配房产: {address[:50]}... (score: {score:.3f})")
        
        print(f"    -> [DEBUG] search_by_names 找到 {len(results)} 个房产")
        return results
    
    def search(self, query: str, top_k: int = 10):
        """Semantic search for properties"""
        if self.index is None:
            print("    -> [WARNING] FAISS index not initialized. Returning empty results.")
            return []
        
        if not query or not isinstance(query, str):
            print("    -> [WARNING] Invalid query provided to search.")
            return []
        
        print(f"    -> [DEBUG] Searching for: {query[:50]}...")
        
        query_embedding = self.model.encode([query])
        faiss.normalize_L2(query_embedding)
        
        scores, indices = self.index.search(query_embedding, min(top_k, len(self.properties)))
        
        results = []
        for idx, score in zip(indices[0], scores[0]):
            if idx < len(self.properties):
                prop = self.properties[idx].copy()
                prop['similarity_score'] = float(score)
                results.append(prop)
        
        print(f"    -> [DEBUG] Found {len(results)} similar properties")
        return results
    
    def search_by_amenities(self, amenities: list[str]) -> list[dict]:
        """
        🆕 按设施关键词搜索所有房产
        
        这是一个硬匹配搜索 - 在 Detailed_Amenities 和 Enhanced_Description 中查找关键词
        
        Args:
            amenities: 设施关键词列表，如 ['karaoke', 'basketball', 'pool', 'gym']
            
        Returns:
            包含任何指定设施的房产列表
        """
        results = []
        
        if not amenities or not self.properties:
            return results
        
        # 标准化搜索词
        search_terms = [a.lower().strip() for a in amenities if a]
        print(f"    -> [DEBUG] Searching for amenities: {search_terms}")
        
        for prop in self.properties:
            # 获取可能包含设施信息的所有字段
            amenities_text = prop.get('Detailed_Amenities', '').lower()
            description = prop.get('Enhanced_Description', '').lower()
            description2 = prop.get('Description', '').lower()
            
            # 组合搜索文本
            combined_text = f"{amenities_text} {description} {description2}"
            
            # 检查是否包含任何搜索词
            matched_amenities = []
            for term in search_terms:
                # 支持一些常见变体
                variants = [term]
                if term == 'pool':
                    variants.extend(['swimming', 'swimming pool'])
                elif term == 'karaoke':
                    variants.extend(['ktv', 'karaoke room'])
                elif term == 'basketball':
                    variants.extend(['basketball court', 'sports court'])
                elif term == 'games':
                    variants.extend(['game room', 'games room', 'games area', 'gaming'])
                elif term == 'gym':
                    variants.extend(['fitness', 'fitness center', 'workout'])
                
                for variant in variants:
                    if variant in combined_text:
                        matched_amenities.append(term)
                        break
            
            if matched_amenities:
                prop_copy = prop.copy()
                prop_copy['matched_amenities'] = matched_amenities
                prop_copy['match_count'] = len(matched_amenities)
                results.append(prop_copy)
                print(f"    -> [DEBUG] ✅ Found: {prop.get('Address', '')[:40]} - Matched: {matched_amenities}")
        
        # 按匹配数量排序
        results.sort(key=lambda x: x.get('match_count', 0), reverse=True)
        print(f"    -> [DEBUG] search_by_amenities found {len(results)} properties with requested amenities")
        return results