# user_session.py

import json
from datetime import datetime

_session_data = {
    'search_history': [],
    'favorites': {}, # 使用字典以避免重复添加，用URL作为key
    'pending_criteria': None,  # ✅ 存储待处理的搜索条件（用于澄清流程）
    'clarification_state': None,  # ✅ 存储澄清问题的状态
}

def add_to_history(criteria: dict, results_count: int):
    """将一次成功的搜索记录添加到历史中。"""
    history_entry = {
        'timestamp': datetime.now().isoformat(),
        'criteria': criteria,
        'results_found': results_count
    }
    _session_data['search_history'].append(history_entry)
    print("\n[SESSION] Search criteria saved to history.")

def add_to_favorites(property_data: dict):
    """将一个房源添加到收藏夹。"""
    url = property_data.get('URL')
    if not url:
        print("\n[SESSION] Cannot favorite property without a URL.")
        return
    if url in _session_data['favorites']:
        print(f"\n[SESSION] '{property_data.get('Address')}' is already in favorites.")
    else:
        _session_data['favorites'][url] = property_data
        print(f"\n[SESSION] '{property_data.get('Address')}' added to favorites.")

def get_favorites() -> list:
    """获取所有收藏的房源。"""
    return list(_session_data['favorites'].values())

def print_favorites():
    """打印收藏夹中的房源以供对比。"""
    favorites = get_favorites()
    if not favorites:
        print("\n--- Your Favorites List is empty ---")
        return
        
    print("\n==============================================")
    print("         YOUR FAVORITES COMPARISON")
    print("==============================================")
    for i, prop in enumerate(favorites):
        print(f"\n--- Favorite #{i+1} ---")
        print(f"Address: {prop.get('Address', 'N/A')}")
        print(f"Price: {prop.get('Price', 'N/A')}")
        print(f"Travel Time: {prop.get('travel_time_minutes', 'N/A')} mins")
        print(f"Platform: {prop.get('Platform', 'N/A')}")
        print(f"URL: {prop.get('URL', 'N/A')}")
    print("==============================================")


# ✅ 新增: 澄清流程管理函数
def set_pending_criteria(criteria: dict):
    """
    存储待处理的搜索条件（当状态为 clarification_needed 时）
    """
    _session_data['pending_criteria'] = criteria
    print(f"[SESSION] Pending criteria saved: destination={criteria.get('destination')}")


def get_pending_criteria() -> dict | None:
    """
    获取待处理的搜索条件
    """
    return _session_data.get('pending_criteria')


def clear_pending_criteria():
    """
    清除待处理的搜索条件（澄清完成后）
    """
    _session_data['pending_criteria'] = None
    _session_data['clarification_state'] = None
    print("[SESSION] Pending criteria cleared")


def has_pending_clarification() -> bool:
    """
    检查是否有待处理的澄清
    """
    return _session_data.get('pending_criteria') is not None


def is_clarification_response(user_query: str) -> bool:
    """
    检查用户输入是否是对澄清问题的回复
    （而不是新的搜索查询）
    """
    if not has_pending_clarification():
        return False
    
    # 澄清回复通常很短且不包含新的搜索词
    query_lower = user_query.lower().strip()
    
    # 排除明确的新查询标志
    new_query_keywords = ['find me', 'search', 'looking for', 'want', 'need', 'price', 'budget', 'area', 'near']
    if any(keyword in query_lower for keyword in new_query_keywords):
        return False
    
    # 澄清回复通常很短
    if len(query_lower) > 150:
        return False
    
    return True