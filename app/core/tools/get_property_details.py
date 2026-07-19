"""
Tool: Get Property Details
获取数据库中特定房产的详细信息

当用户询问特定房产的详情（如房型、设施、价格等）时，
应该直接查询本地数据库，而不是进行网络搜索。

使用场景：
1. 用户点击前端 "Ask AI" 按钮询问某个房产
2. 用户提到特定房产名称/地址并询问详情
3. 用户对之前推荐的房产提出具体问题
"""

import pandas as pd
from typing import Optional, Dict, List
from core.tool_system import Tool, ToolResult
import re


def load_property_database() -> pd.DataFrame:
    """加载房产数据库。

    直接读取 on-demand 抓取缓存（listing_cache.sqlite3）—— 与列表/搜索路径
    （core.scraping.on_demand.get_listings）完全相同的数据源，因此用户询问
    "介绍一下某套房" 时看到的是同一批真实房源，而不是离线批处理 CSV / 假数据。
    缓存为空或不可用时返回空 DataFrame（诚实地表示"暂无可查数据"）。"""
    try:
        from core.scraping.on_demand import iter_cached_listings
        rows = iter_cached_listings()
    except Exception as e:
        print(f"❌ 加载房产数据失败: {e}")
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def normalize_text(text: str) -> str:
    """标准化文本用于匹配"""
    if not text:
        return ""
    # 转小写，移除多余空格和特殊字符
    text = text.lower().strip()
    text = re.sub(r'[,\.\-\'\"]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def find_property_by_name_or_address(
    query: str,
    df: pd.DataFrame
) -> List[Dict]:
    """
    根据名称或地址查找房产
    
    Args:
        query: 用户查询的房产名称或地址（部分匹配）
        df: 房产数据库 DataFrame
        
    Returns:
        匹配的房产列表
    """
    if df.empty:
        return []
    
    query_normalized = normalize_text(query)
    matches = []
    
    for _, row in df.iterrows():
        address = row.get('Address', '')
        address_normalized = normalize_text(address)
        
        # 检查查询是否包含在地址中，或地址是否包含查询的关键词
        # 提取查询中的关键词（如 "Scape Bloomsbury", "Woburn Place"）
        query_words = query_normalized.split()
        
        # 计算匹配分数
        match_score = 0
        for word in query_words:
            if len(word) > 2 and word in address_normalized:  # 忽略太短的词
                match_score += 1
        
        # 如果至少有2个关键词匹配，或者整个查询包含在地址中
        if match_score >= 2 or query_normalized in address_normalized:
            matches.append({
                'score': match_score,
                'data': row.to_dict()
            })
    
    # 按匹配分数排序
    matches.sort(key=lambda x: x['score'], reverse=True)
    
    return [m['data'] for m in matches]


def format_property_details(property_data: Dict) -> str:
    """
    格式化房产详细信息
    
    Args:
        property_data: 房产数据字典
        
    Returns:
        格式化的房产详情字符串
    """
    address = property_data.get('Address', 'Unknown')
    price = property_data.get('Price', 'Unknown')
    room_type = property_data.get('Room_Type_Category', 'Unknown')
    description = property_data.get('Description', '')
    amenities = property_data.get('Detailed_Amenities', '')
    guest_policy = property_data.get('Guest_Policy', '')
    payment_rules = property_data.get('Payment_Rules', '')
    excluded_features = property_data.get('Excluded_Features', '')
    url = property_data.get('URL', '')
    available_from = property_data.get('Available From', 'Now')
    
    # 构建详细信息
    details = f"""
📍 **{address}**

💰 **价格**: {price}
🏠 **房型**: {room_type}
📅 **可入住日期**: {available_from}

📝 **描述**: 
{description}

✨ **设施与配置**:
{amenities}

👥 **访客政策**:
{guest_policy}

💳 **付款规则**:
{payment_rules}

⛔ **不包含的设施**:
{excluded_features}

🔗 **链接**: {url}
"""
    return details.strip()


async def get_property_details_impl(
    property_name: str = "",
    property_address: str = "",
    property_url: str = "",
    question: Optional[str] = None,
    **kwargs
) -> dict:
    """
    获取特定房产的详细信息

    当用户询问数据库中某个房产的具体信息时使用此工具。
    优先通过 URL 精确命中 sqlite 缓存；否则通过房产名称或地址进行模糊匹配。

    Args:
        property_name: 房产名称（如 "Scape Bloomsbury"）
        property_address: 房产地址或部分地址（如 "Woburn Place"）
        property_url: 房产 URL（推荐！推荐索引/结果里每条都带 URL，直接传它可精确命中缓存里那一条）
        question: 用户关于这个房产的具体问题（可选）

    Returns:
        包含房产详细信息的字典
    """
    print(f"\n{'='*60}")
    print(f"🏠 [PROPERTY DETAILS] 查询房产详情")
    print(f"   property_name: {property_name}")
    print(f"   property_address: {property_address}")
    print(f"   property_url: {property_url}")
    print(f"   question: {question}")
    print(f"{'='*60}")
    
    # 加载数据库
    df = load_property_database()
    if df.empty:
        return {
            "success": False,
            "error": "无法加载房产数据库",
            "message": "抱歉，无法访问房产数据库。请稍后重试。"
        }
    
    # 构建查询字符串（URL 放最前，触发下方按 URL 精确命中缓存的直查分支）。
    search_query = ""
    if property_url:
        search_query = property_url
    if property_name:
        search_query = f"{search_query} {property_name}".strip()
    if property_address:
        search_query = f"{search_query} {property_address}".strip()

    if not search_query:
        return {
            "success": False,
            "error": "需要提供房产名称、地址或 URL",
            "message": "请提供您想查询的房产名称、地址或 URL。"
        }
    
    # 精确优先：若查询本身就是一条房源 URL（如前端 "Ask AI" 传入的 focus URL），
    # 直接按 URL 命中缓存中的那一条，避免模糊匹配歧义。
    matches: List[Dict] = []
    if re.search(r"https?://|onthemarket\.com", search_query, re.I):
        try:
            from core.scraping.on_demand import find_cached_listing_by_url
            for token in search_query.split():
                if re.search(r"https?://|onthemarket\.com", token, re.I):
                    hit = find_cached_listing_by_url(token)
                    if hit:
                        matches = [hit]
                        break
        except Exception as e:
            print(f"  [PROPERTY DETAILS] URL 直查失败: {e}")

    # 查找匹配的房产（模糊名称/地址匹配）
    if not matches:
        matches = find_property_by_name_or_address(search_query, df)
    
    if not matches:
        # 尝试更宽松的搜索
        # 提取查询中的主要关键词再试一次
        keywords = search_query.split()
        for keyword in keywords:
            if len(keyword) > 3:  # 只用长度大于3的词
                matches = find_property_by_name_or_address(keyword, df)
                if matches:
                    break
    
    if not matches:
        return {
            "success": False,
            "found": False,
            "search_query": search_query,
            "message": f"在数据库中未找到与 '{search_query}' 匹配的房产。",
            "suggestion": "请检查房产名称或地址是否正确，或尝试使用更短的关键词搜索。"
        }
    
    # 找到匹配的房产
    primary_match = matches[0]  # 最佳匹配
    
    # 格式化详细信息
    formatted_details = format_property_details(primary_match)
    
    # 提取关键信息用于回答特定问题
    room_type = primary_match.get('Room_Type_Category', '')
    
    # 判断房型相关信息
    is_studio = 'studio' in room_type.lower()
    is_shared = 'shared' in room_type.lower() or 'twin' in room_type.lower()
    is_ensuite = 'en-suite' in room_type.lower() or 'ensuite' in room_type.lower()
    has_private_kitchen = 'own kitchen' in room_type.lower() or 'private kitchen' in room_type.lower()
    
    result = {
        "success": True,
        "found": True,
        "search_query": search_query,
        "property": {
            "address": primary_match.get('Address', ''),
            "price": primary_match.get('Price', ''),
            "room_type": room_type,
            "description": primary_match.get('Description', ''),
            "amenities": primary_match.get('Detailed_Amenities', ''),
            "guest_policy": primary_match.get('Guest_Policy', ''),
            "payment_rules": primary_match.get('Payment_Rules', ''),
            "excluded_features": primary_match.get('Excluded_Features', ''),
            "url": primary_match.get('URL', ''),
            "available_from": primary_match.get('Available From', ''),
            "geo_location": primary_match.get('geo_location', ''),
        },
        "room_type_analysis": {
            "is_studio": is_studio,
            "is_shared_room": is_shared,
            "is_ensuite": is_ensuite,
            "has_private_kitchen": has_private_kitchen,
            "room_type_category": room_type
        },
        "formatted_details": formatted_details,
        "total_matches": len(matches),
        "message": f"找到房产: {primary_match.get('Address', '')}",
    }
    
    # 如果有其他匹配的房产，也列出来
    if len(matches) > 1:
        result["other_matches"] = [
            {
                "address": m.get('Address', ''),
                "price": m.get('Price', ''),
                "room_type": m.get('Room_Type_Category', '')
            }
            for m in matches[1:5]  # 最多显示其他4个
        ]
    
    print(f"\n✅ [PROPERTY DETAILS] 找到 {len(matches)} 个匹配房产")
    print(f"   最佳匹配: {primary_match.get('Address', '')}")
    print(f"   房型: {room_type}")
    print(f"   是否Studio: {is_studio}")
    
    return result


# 创建工具实例
get_property_details_tool = Tool(
    name="get_property_details",
    description="""Get a specific property's full details from the local cache (description, amenities, visitor/payment policy, room type) — same source as search, more accurate than the web. Use when the user asks about a specific listing, clicks "Ask AI" on one, or asks about any previously recommended listing. The RECOMMENDED LISTINGS INDEX in context holds only summaries; pass a listing's URL to property_url for an exact cache hit (avoids same-name ambiguity), else use its name or address.
获取某房源的完整详情；优先用该房源 URL 精确命中缓存。""",
    parameters={
        "type": "object",
        "properties": {
            "property_url": {
                "type": "string",
                "description": "房产 URL（首选）。推荐索引/搜索结果里每条都带 URL，直接传它可精确命中缓存里那一条房源。"
            },
            "property_name": {
                "type": "string",
                "description": "房产名称，如 'Scape Bloomsbury', 'iQ Bloomsbury', 'Tufnell House' 等"
            },
            "property_address": {
                "type": "string",
                "description": "房产地址或部分地址，如 '19-29 Woburn Place' 或 'London WC1H'"
            },
            "question": {
                "type": "string",
                "description": "用户关于这个房产的具体问题（可选），如 '是不是studio？' 或 '访客政策是什么？'"
            }
        },
        "required": []  # 至少需要 property_url / property_name / property_address 之一
    },
    func=get_property_details_impl
)
