# RAG使用具体介绍


本项目使用三个独立的 RAG 知识源来增强 LLM 的推荐能力，都围绕着**向量化 (Embedding)** 和**余弦相似度**（Cosine Similarity）进行操作, 三者仅有的不同是**数据源**。在最后模型会结合这三个独立的RAG来生成最终的推荐结果。

### 一、 房源嵌入存储 (`PropertyEmbeddingStore`)

这个组件负责将所有房源的详细信息转化为向量，用于**语义检索**，以找到与用户查询意图最匹配的房源。

#### 1\. 读取输入与富文本构建

`PropertyEmbeddingStore.build_index` 函数首先遍历所有房源字典（这些房源是从 CSV 文件加载并经过初步处理的），将关键字段拼接成一段**富文本**。

Python

    # local_data_demo/property_embeddings.py (build_index 方法)
    
    def build_index(self, properties: list[dict]):
        """Create FAISS index from property descriptions"""
        texts = []
        for prop in properties:
            # 核心：将结构化和非结构化数据组合成一个用于嵌入的文本
            text = f"""
                {prop.get('Address', '')}
                Price: {prop.get('Price', '')}
                {prop.get('Description', '')}
                Travel time: {prop.get('travel_time_minutes', '')} minutes
                Crime: {prop.get('crime_data_summary', {}).get('crime_trend', '')}
                """
            texts.append(text.strip())
        # ...

#### 2\. 向量化操作（Embedding）

使用 `SentenceTransformer` 模型将这些富文本批量转换成高维向量：

Python

    # local_data_demo/property_embeddings.py (build_index 方法)
    # ...
            
        # SentenceTransformer 将文本列表编码为 NumPy 数组 (embeddings)
        embeddings = self.model.encode(texts, show_progress_bar=True)
        
        # 关键：对嵌入向量进行 L2 归一化
        faiss.normalize_L2(embeddings) 
        
        # ...

#### 3\. 相似度计算与检索

系统使用 **FAISS** 库加速检索。将 L2 归一化后的向量导入 **IndexFlatIP**（内积索引），内积操作即等效于余弦相似度。

Python

    # local_data_demo/property_embeddings.py (search 方法)
    
    def search(self, query: str, top_k: int = 10):
        """Semantic search for properties"""
        # 对查询文本进行向量化和 L2 归一化
        query_embedding = self.model.encode([query])
        faiss.normalize_L2(query_embedding)
        
        # 在内积（IP）索引上执行搜索
        scores, indices = self.index.search(query_embedding, top_k)
        # ...

* * *

### 二、历史记忆 (`ConversationMemory`)

这个组件负责存储和检索用户与助手之间的历史交互，以便在后续查询中提供上下文。

#### 1\. 读取输入与文档构建

`ConversationMemory.add_interaction` 方法负责写入数据。它接收用户消息、助手回复和元数据，并将它们组合成一个**单一文档**进行存储。

Python

    # local_data_demo/conversation_memory.py (add_interaction 方法)
    
    def add_interaction(self, user_msg: str, bot_response: str, 
                       metadata: dict = None):
        """Store conversation turn with metadata"""
        turn_id = f"turn_{self.collection.count()}"
        
        # 组合用户消息和助手回复作为文档内容
        self.collection.add(
            documents=[f"User: {user_msg}\nAssistant: {bot_response}"],
            metadatas=[clean_metadata],
            ids=[turn_id]
        )

#### 2\. 向量化与相似度指定

向量化由 ChromaDB 内部处理。关键在于在初始化时指定了检索空间：

Python

    # local_data_demo/conversation_memory.py (__init__ 方法)
    
    def __init__(self):
        # ...
        self.collection = self.client.get_or_create_collection(
            name="conversations",
            metadata={"hnsw:space": "cosine"} # <-- 明确要求使用余弦相似度
        )
        # ...

#### 3\. 相似度检索

检索时，只需提供查询文本和结果数量，ChromaDB 会自动执行向量相似度搜索：

Python

    # local_data_demo/conversation_memory.py (retrieve_relevant_history 方法)
    
    def retrieve_relevant_history(self, query: str, n_results: int = 3):
        """Get relevant past conversations"""
        results = self.collection.query(
            query_texts=[query], # 将查询文本向量化，并在向量空间内检索
            n_results=n_results
        )
        return results['documents'][0] if results['documents'] else []

* * *

### 三、区域知识库 (`AreaKnowledgeBase`)

这个组件用于存储和检索关于特定地区的预策划的、宏观的背景知识，以丰富 LLM 的推荐叙事。

#### 1\. 读取输入与文档构建

`AreaKnowledgeBase._populate_initial_data` 方法在初始化时加载人工策划的区域数据（如 Camden 的 vibe、safety 等）。

Python

    # local_data_demo/area_knowledge.py (_populate_initial_data 方法)
    
        # ...
        # 示例区域数据
        areas = [
            {
                "name": "Camden",
                "vibe": "Alternative, vibrant, music scene",
                "safety": "Generally safe, some late-night concerns",
                "prices": "£1800-2500 for 1-bed"
            },
        # ...
        
        if self.collection.count() == 0:
            for area in areas:
                # 拼接文本用于向量化
                doc = f"{area['name']}: {area['vibe']}. {area['demographics']}. {area['transport']}. Safety: {area['safety']}."
                
                self.collection.add(
                    documents=[doc],
                    metadatas=[area], # 存储原始结构化数据作为元数据
                    ids=[area['name']]
                )

#### 2\. 向量化与相似度检索

与 `ConversationMemory` 类似，向量化由 ChromaDB 内部处理。

Python

    # local_data_demo/area_knowledge.py (get_context 方法)
    
    def get_context(self, location: str, n_results: int = 2):
        """Retrieve relevant area information"""
        results = self.collection.query(
            query_texts=[location], # 将地点名称向量化
            n_results=n_results
        )
        # 返回原始的结构化元数据（例如 Camden 的字典）
        return results['metadatas'][0] if results['metadatas'] else []

**增强点：** 在这个模块中，即使查询只是一个简单的地名（如 `"Camden"`），系统也会将其向量化，然后搜索与该地名**最相似**的知识文档（例如，如果用户问 `"King's Cross"` 的信息，它可能会检索到与之相近的 `"Camden"` 知识）。最终它会返回存储在 `metadatas` 中的**结构化数据**，供 LLM 在生成推荐时作为事实依据。

---


### RAG 结果的整合与混合评分

三个 RAG 知识源的输出首先在 `RAGCoordinator.enhanced_search` 中被收集，并通过**混合评分**机制进行整合：

1.  **三源检索 (Retrieval)**:
    
    *   **房源索引** (`PropertyEmbeddingStore`)：返回包含 `similarity_score` 的房源列表 (`semantic_results`)。
        
    *   **历史记忆** (`ConversationMemory`)：返回用户的历史偏好对话文本 (`past_context`)。
        
    *   **区域知识库** (`AreaKnowledgeBase`)：返回目标区域的结构化信息（如 "Camden" 的 `vibe`、`safety` 等） (`area_info`)。
        
2.  **混合评分 (Hybrid Scoring)**： 混合打分用于给庞大的数据库检索出的房源进行二次排序。将RAG语义相似度分数与房源价格，通勤时间，以及软性偏好等价格匹配得到一个混合的打分。**注意，这个打分不用于最终的评估，最终的评估是由后续生成阶段的 LLM 完成的。这里的混合评分只是为了筛查掉不符合用户需求的大部分房源。** 具体打分公式如下：

$$\text{Final Score}= 0.4 \times \text{Semantic Score} + 0.3 \times \text{travel time} + 0.2 \times \text{Budget Match} + 0.1 \times \text{Soft Preference Match}$$

其中 `Semantic Score` 是 `PropertyEmbeddingStore` 返回的相似度分数，`travel time` 是根据用户的通勤需求计算的一个分数（通勤时间越短分数越高），`Budget Match` 是根据用户预算计算的一个分数（价格越接近预算上限分数越高），Soft Preference Match 是根据用户的软性偏好（如喜欢安静、热闹等）计算的一个分数。 `ConversationMeomory` 和 `AreaKnowledgeBase` 的结果主要用于后续的LLM生成阶段提供客观的数据基础, 不参与这里的打分

* * *

### 最终推荐的生成过程（Augmented Generation）

在通过精确的通勤时间筛选（硬性过滤）和深度数据富集（Phase II）后，程序进入最终的生成阶段。

#### 1\. 构建增强 Prompt

`app.py` 中的 `generate_recommendations_with_rag` 函数负责将所有检索和富集到的信息打包，构建一个**增强的 Prompt**：

Python

    # local_data_demo/app.py (generate_recommendations_with_rag 方法)
    
    # 将 RAG 上下文 (历史对话和区域知识) 组合成一个更丰富的软性偏好描述
    contextual_prompt = f"""
    User's original query: "{user_query}"
    Relevant information from past conversations:
    - {" ".join(past_conversations)}  # <-- 来自 ConversationMemory
    
    Additional context about the target search area:
    - {json.dumps(area_knowledge, indent=2)} # <-- 来自 AreaKnowledgeBase
    """
    
    # 调用 LLM 生成推荐
    return generate_recommendations(properties=enriched_candidates, user_query=user_query, soft_preferences=contextual_prompt)

#### 2\. LLM 的增强生成指令

增强后的 Prompt 被发送给 `ollama_interface.generate_recommendations`。LLM 接收：

*   **完整房源数据** (`properties_data`)：这是经过**深度富集**（加入了实时通勤时间、犯罪统计、周边设施等事实数据）的最终房源列表。
    
*   **富文本上下文** (`soft_preferences`)：包含用户的历史偏好和区域背景知识。
    

LLM 被赋予严格的 **系统 Prompt** 和 **CRITICAL RULES**，以确保其生成的内容具有事实依据：

Python

    # local_data_demo/ollama_interface.py (generate_recommendations 中的 Prompt 节选)
    
    system_prompt = """You are an expert London rental assistant. 
    CRITICAL RULES:
    1. You MUST use the EXACT crime numbers from the data
    2. NEVER say "no crimes" or "0 crimes" unless the crimes field is actually 0
    3. NEVER make up statistics - only use provided data
    4. Be honest about safety - high crime numbers deserve honest mention"""
    
    prompt = f"""
    Properties (with REAL crime statistics):
    {json.dumps(simple_props, indent=2)}  # <-- 注入富集后的房源事实
    
    CRITICAL: For EACH property, you MUST mention the crime statistics using the EXACT numbers provided.
    
    Example of CORRECT explanation:
    "This property offers a 20-minute commute. The area has 170 reported crimes over the past 6 months (stable trend). At £2,500 pcm..."
    """

**最终结合方式：**

1.  **事实锚定**：LLM 接收到**富集了外部事实**（犯罪数据、通勤时间等）的房源列表。
    
2.  **上下文引导**：LLM 接收到**RAG检索到的历史和区域知识**。
    
3.  **约束生成**：LLM 严格遵循指令，综合所有信息（通勤、价格、**确切的犯罪数字和趋势**、区域氛围），生成个性化、数据驱动的推荐理由，并给出最终排名。


---


