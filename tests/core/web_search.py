# web_search.py

from ddgs import DDGS
from .cache_service import get_from_cache, set_to_cache, create_cache_key

def get_search_snippets(query: str, max_results: int = 3) -> str:
    """
    执行DuckDuckGo搜索并返回格式化的摘要字符串。
    使用缓存。
    """
    cache_key = create_cache_key('get_search_snippets', query, max_results)
    cached_result = get_from_cache(cache_key)
    if cached_result:
        print(f"  -> [Cache HIT] Web search for: '{query}'")
        return cached_result
    
    print(f"  -> [API Call] Web search for: '{query}'")
    snippets = []
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=max_results)
            if results:
                for result in results:
                    snippets.append(result.get('body', ''))
        
        full_snippet = " ".join(snippets)
        set_to_cache(cache_key, full_snippet)
        return full_snippet
    except Exception as e:
        print(f"DuckDuckGo search failed: {e}")
        return "Could not retrieve search information."

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