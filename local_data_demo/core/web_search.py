# web_search.py
"""
Web Search Module - 使用 SearXNG 本地实例进行网络搜索
替代 DuckDuckGo，提供更稳定和可控的搜索能力
"""

import os
import requests
from typing import List, Dict, Optional, Union
from .cache_service import get_from_cache, set_to_cache, create_cache_key


class SearXNGSearch:
    """
    SearXNG 搜索客户端类
    用于与本地或远程 SearXNG 实例进行交互
    """
    
    def __init__(
        self, 
        instance_url: str = "http://localhost:8080",
        timeout: int = 10,
        default_max_results: int = 10,
        verbose: bool = False
    ):
        """
        初始化 SearXNG 搜索客户端
        
        Args:
            instance_url: SearXNG 实例的 URL
            timeout: 请求超时时间（秒）
            default_max_results: 默认返回的最大结果数
            verbose: 是否输出详细日志
        """
        self.instance_url = instance_url.rstrip('/')
        self.search_endpoint = f"{self.instance_url}/search"
        self.timeout = timeout
        self.default_max_results = default_max_results
        self.verbose = verbose
        
        # 🆕 数据源策略：根据查询类型使用不同的搜索引擎
        # 对于事实性查询（租房信息、政策、学校等），只使用Google以确保结果质量
        self.authoritative_engines = "google"  # 只使用Google，确保最高质量的官方来源
        
        # 对于评价、反馈类查询，可以包含论坛
        self.forum_engines = "google,reddit"
    
    def _detect_query_intent(self, query: str) -> str:
        """
        检测查询意图，判断是事实查询、评价/反馈查询还是 POI/设施查询

        Args:
            query: 搜索查询字符串

        Returns:
            str: 'factual' (需要过滤) 或 'opinion' (不过滤) 或 'poi' (不过滤)
        """
        query_lower = query.lower()

        # POI/设施类关键词 - 这些查询允许所有来源（包括论坛、博客、评论）
        poi_keywords = [
            'chinese supermarket', 'chinese restaurant', 'chinese food', 'asian supermarket',
            'supermarket', 'tesco', 'sainsbury', 'waitrose', 'aldi', 'lidl',
            'restaurant', 'cafe', 'coffee', 'pub', 'bar',
            'gym', 'fitness', 'sports', 'swimming pool',
            'park', 'garden', 'playground',
            'tube station', 'bus stop', 'metro', 'underground',
            'pharmacy', 'hospital', 'clinic', 'gp',
            'school', 'library', 'museum',
            'shop', 'store', 'mall', 'shopping',
            'nearby', 'near', 'around', 'close to',
            'facilities', 'amenities', 'convenience'
        ]

        # 评价/反馈类关键词 - 这些查询也允许论坛等来源
        opinion_keywords = [
            'review', 'reviews', 'rating', 'experience', 'feedback',
            'opinion', 'recommend', 'worth', 'good or bad',
            'how is', 'what do people think', 'comments',
            'forum', 'discussion', 'reddit', 'community',
            'student experience', 'living experience'
        ]

        # 优先检测 POI 查询（因为 POI 查询可能也包含 'experience' 等词）
        if any(keyword in query_lower for keyword in poi_keywords):
            return 'poi'

        # 其次检测评价类查询
        if any(keyword in query_lower for keyword in opinion_keywords):
            return 'opinion'

        # 默认为事实查询（需要过滤）
        return 'factual'
    
    def search(
        self, 
        query: str, 
        max_results: Optional[int] = None,
        categories: str = "general",
        language: str = "en-GB"
    ) -> List[Dict[str, str]]:
        """
        执行搜索并返回结构化结果
        
        Args:
            query: 搜索查询字符串
            max_results: 返回的最大结果数
            categories: 搜索类别 (general, images, news, etc.)
            language: 搜索语言
            
        Returns:
            List[Dict]: 包含 title, url, content 的字典列表
        """
        if max_results is None:
            max_results = self.default_max_results
        
        # 🆕 根据查询意图选择搜索引擎
        intent = self._detect_query_intent(query)
        # POI 和 opinion 查询都允许使用论坛引擎
        engines = self.forum_engines if intent in ['opinion', 'poi'] else self.authoritative_engines

        print(f"  -> [SearXNG] Query intent: {intent}, using engines: {engines}")
        
        params = {
            "q": query,
            "format": "json",
            "categories": categories,
            "language": language,
            "engines": engines  # 🆕 根据意图选择引擎
        }
        
        try:
            print(f"  -> [SearXNG] Searching: '{query}'")
            response = requests.get(
                self.search_endpoint,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            data = response.json()
            results = data.get("results", [])
            
            # 🆕 诊断日志：显示原始结果
            print(f"  -> [SearXNG] Raw results: {len(results)} items")
            if len(results) > 0:
                engines_used = set(r.get('engine', 'unknown') for r in results)
                print(f"  -> [SearXNG] Engines returned data: {', '.join(engines_used)}")
            
            # 检查是否有不响应的引擎（只记录简要信息）
            # 这些备选引擎（brave/duckduckgo/startpage）失败不影响结果
            # 因为我们只使用 Google 作为主引擎
            unresponsive = data.get("unresponsive_engines", [])
            if unresponsive and len(results) == 0:
                # 只在主引擎也失败时才警告
                print(f"  ⚠️ [SearXNG] All engines unresponsive, no results")
            
            # 🆕 对于事实性查询，过滤结果，只保留权威来源
            # POI 和 opinion 查询不过滤
            pre_filter_count = len(results)
            if intent == 'factual':
                results = self._filter_authoritative_sources(results)
                print(f"  -> [Filter] {pre_filter_count} → {len(results)} (kept authoritative sources)")
            elif intent in ['opinion', 'poi']:
                print(f"  -> [Filter] Skipping filter for {intent} query (allowing all sources)")
            
            # 提取并格式化结果
            formatted_results = []
            for result in results[:max_results]:
                formatted_results.append({
                    "title": result.get("title", "No title"),
                    "url": result.get("url", ""),
                    "content": result.get("content", "No content available")
                })
            
            print(f"  ✅ Found {len(formatted_results)} results")
            return formatted_results
            
        except requests.exceptions.ConnectionError:
            print(f"  ❌ Connection Error: Cannot connect to SearXNG at {self.instance_url}")
            print("     Make sure the SearXNG Docker container is running.")
            return []
        except requests.exceptions.Timeout:
            print(f"  ❌ Timeout: SearXNG request timed out after {self.timeout}s")
            return []
        except requests.exceptions.HTTPError as e:
            print(f"  ❌ HTTP Error: {e}")
            return []
        except Exception as e:
            print(f"  ❌ SearXNG search failed: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def _filter_authoritative_sources(self, results: List[Dict]) -> List[Dict]:
        """
        智能三层过滤系统：
        1. 绝对黑名单 (Banned): 投资、B2B、垃圾农场 -> 永远丢弃
        2. 权威白名单 (Authoritative): 官网、学校 -> 优先保留
        3. 灰色名单 (Grey): 博客、论坛、新闻 -> 兜底使用
        
        核心原则：绝不返回投资类内容给学生
        
        Args:
            results: 原始搜索结果列表
            
        Returns:
            List[Dict]: 过滤后的结果列表（优先白名单，兜底灰名单）
        """
        # 1. 🚫 黑名单：绝对不要给学生看的内容
        banned_domains = [
            # 五大行（商业地产投资机构）
            'knightfrank', 'savills', 'cushmanwakefield', 'cbre', 'jll',
            # 行业媒体（B2B内容）
            'propertyweek', 'constructionmaguk', 'housingtoday', 'costar',
            'propertyinvestor', 'estateagenttoday', 'landlordtoday',
            # 社交媒体噪音
            'linkedin.com', 'facebook.com', 'instagram.com',
            # 垃圾内容农场
            'quora.com', 'answers.yahoo.com'
        ]
        
        # 投资相关关键词（出现在标题或摘要中的话直接拉黑）
        investment_keywords = [
            'transaction volume', 'cap rate', 'investor', 'asset management',
            'yield', 'institutional investment', 'portfolio', 'capital value',
            'investment volume', 'market transaction', 'commercial property'
        ]
        
        # 2. ✅ 白名单：最信任的权威来源
        authoritative_domains = [
            # 英国政府和官方机构
            '.gov.uk', 'nhs.uk', 'police.uk',
            # 教育机构
            '.ac.uk', '.edu', 'university',
            # 正规租房平台（学生导向）
            'rightmove.co.uk', 'zoopla.co.uk', 'spareroom.co.uk',
            'uhomes.com', 'uhomes.co.uk',
            'accommodation.london', 'studentcrowd.com', 'uniplaces.com',
            # 主流新闻媒体
            'bbc.co.uk', 'theguardian.com', 'thetimes.co.uk',
            'telegraph.co.uk', 'independent.co.uk', 'standard.co.uk',
            # 交通官方网站
            'tfl.gov.uk', 'nationalrail.co.uk',
            # 金融和法律权威（学生公益组织）
            'moneyhelper.org.uk', 'citizensadvice.org.uk', 'shelter.org.uk',
            'nus.org.uk', 'savethestudent.org', 'ukcisa.org.uk'
        ]
        
        whitelist_results = []  # 存权威结果
        greylist_results = []   # 存普通结果（备胎）
        
        for result in results:
            url = result.get("url", "").lower()
            title = result.get("title", "").lower()
            snippet = result.get("content", "").lower()
            
            # --- 🛑 第一道防线：黑名单熔断 ---
            is_banned = False
            
            # 检查域名黑名单
            for ban in banned_domains:
                if ban in url or ban in title:
                    print(f"  🗑️ [Filter] HARD BLOCK (domain): {ban} found in {url}")
                    is_banned = True
                    break
            
            # 检查投资类关键词
            if not is_banned:
                for keyword in investment_keywords:
                    if keyword in snippet or keyword in title:
                        print(f"  🗑️ [Filter] HARD BLOCK (keyword): '{keyword}' found in content")
                        is_banned = True
                        break
            
            if is_banned:
                continue  # 跳过当前循环，彻底丢弃该结果

            # --- ✅ 第二道防线：权威白名单 ---
            if any(domain in url for domain in authoritative_domains):
                whitelist_results.append(result)
                print(f"  ✅ [Filter] Found Authoritative: {url}")
                continue
                
            # --- 🆗 第三道防线：灰色名单（软性过滤） ---
            # 必须包含学生相关内容才能进入灰名单
            if 'student' in title or 'rent' in title or 'guide' in title or 'accommodation' in title:
                greylist_results.append(result)
                print(f"  🆗 [Filter] Added to Greylist: {url}")
            else:
                print(f"  ⛔ [Filter] Rejected (not student-relevant): {url}")

        # --- 🔄 最终返回逻辑 ---
        
        # 1. 如果有足够的权威结果，优先返回权威结果
        if len(whitelist_results) >= 2:
            print(f"  ✅ [Filter] Returning {len(whitelist_results)} authoritative results")
            return whitelist_results
            
        # 2. 如果权威结果太少，用灰色名单补充
        # 关键点：灰色名单里已经剔除了黑名单内容！
        combined = whitelist_results + greylist_results
        
        if len(combined) > 0:
            print(f"  ⚠️ [Filter] Low authoritative count ({len(whitelist_results)}). Mixing with {len(greylist_results)} greylist results.")
            return combined[:5]  # 只取前5个，避免太长
            
        # 3. 如果连灰色名单都没有（全是被Block的），返回空
        print("  ❌ [Filter] All results were blocked or irrelevant. No student-friendly content found.")
        return []
    
    def format_for_llm(self, results: List[Dict[str, str]]) -> str:
        """
        将搜索结果格式化为 LLM 友好的字符串
        
        Args:
            results: 搜索结果列表
            
        Returns:
            str: 格式化的字符串，适合 LLM 阅读
        """
        if not results:
            return "No search results found for this query."
        
        formatted_parts = []
        for i, result in enumerate(results, 1):
            formatted_part = (
                f"[{i}] Title: {result['title']}\n"
                f"    Link: {result['url']}\n"
                f"    Summary: {result['content']}"
            )
            formatted_parts.append(formatted_part)
        
        return "\n\n".join(formatted_parts)


# 全局搜索实例（使用本地 SearXNG）
# instance_url 通过 SEARXNG_URL 环境变量配置：
#   - 主机直接运行时默认 http://localhost:8080
#   - 在 docker-compose 中运行时设为 http://searxng:8080（服务名解析）
_searxng_client = SearXNGSearch(
    instance_url=os.getenv("SEARXNG_URL", "http://localhost:8080"),
    timeout=10,
    default_max_results=10
)


def get_search_snippets(query: str, max_results: int = 5) -> str:
    """
    执行 SearXNG 搜索并返回格式化的摘要字符串。
    使用缓存。返回详细的结果，包括标题、链接和摘要。
    
    Args:
        query: 搜索查询字符串
        max_results: 返回的最大结果数
        
    Returns:
        str: 格式化的搜索结果字符串
    """
    cache_key = create_cache_key('get_search_snippets', query, max_results)
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"  -> [Cache HIT] Web search for: '{query}'")
        return cached_result
    
    print(f"  -> [API Call] Web search for: '{query}'")
    
    # 使用 SearXNG 执行搜索
    results = _searxng_client.search(query, max_results=max_results)
    
    if not results:
        print(f"  ⚠️ SearXNG returned no results for: {query}")
        return "No search results found for this query."
    
    # 格式化为 LLM 友好的输出
    full_result = _searxng_client.format_for_llm(results)
    set_to_cache(cache_key, full_result)
    
    return full_result


def search_web(query: str, max_results: int = 10) -> Union[str, List[Dict[str, str]]]:
    """
    主要的网络搜索函数 - 作为 LLM 工具使用
    
    Args:
        query: 搜索查询字符串
        max_results: 返回的最大结果数 (5-10)
        
    Returns:
        str: 格式化的搜索结果，适合 LLM 阅读
        
    Example:
        >>> result = search_web("python docker tutorial")
        >>> print(result)
        [1] Title: Docker Python Tutorial
            Link: https://example.com/...
            Summary: Learn how to use Docker with Python...
    """
    return get_search_snippets(query, max_results=max_results)


def search_rent_prices(property_type: str, area: str = "London") -> str:
    """专门搜索租金价格信息，使用更精准的查询"""
    queries = [
        f"{property_type} rent price {area} UK 2024",
        f"average {property_type} rental cost {area}",
    ]
    all_results = []
    for q in queries:
        result = get_search_snippets(q, max_results=3)
        if result and "No search results" not in result:
            all_results.append(result)
    return "\n\n---\n\n".join(all_results) if all_results else "No rent price information found."


def search_crime_data(area: str) -> str:
    """使用更精确的查询搜索特定英国地区的犯罪数据。"""
    if not area:
        return "No area provided for crime search."
    return get_search_snippets(f'"{area} crime rate" official statistics')


def search_cost_of_living(area: str) -> str:
    """使用更精确的查询搜索生活成本信息。"""
    if not area:
        return "No area provided for cost of living search."
    return get_search_snippets(f'"cost of living in {area} London"')


# 便捷函数：检查 SearXNG 是否可用
def check_searxng_health() -> bool:
    """
    检查 SearXNG 实例是否正常运行
    
    Returns:
        bool: True 如果实例可用，否则 False
    """
    try:
        response = requests.get(
            f"{_searxng_client.instance_url}/healthz",
            timeout=5
        )
        return response.status_code == 200
    except:
        # 尝试基本连接
        try:
            response = requests.get(
                _searxng_client.instance_url,
                timeout=5
            )
            return response.status_code == 200
        except:
            return False


if __name__ == "__main__":
    # 测试代码
    print("=== SearXNG Web Search Test ===\n")
    
    # 检查健康状态
    print("Checking SearXNG health...")
    if check_searxng_health():
        print("✅ SearXNG is running!\n")
    else:
        print("❌ SearXNG is not available. Make sure Docker container is running.\n")
        exit(1)
    
    # 测试搜索
    test_query = "London rent prices 2024"
    print(f"Testing search: '{test_query}'\n")
    result = search_web(test_query, max_results=5)
    print(result)