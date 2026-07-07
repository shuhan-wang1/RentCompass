# cache_service.py

from pathlib import Path

from uk_rent_agent.data.cache import PersistentCache

_cache = PersistentCache(Path(__file__).resolve().parents[1] / "data" / "runtime_cache.sqlite3")

def get_from_cache(key: str):
    """从缓存中获取数据"""
    return _cache.get(key)

def set_to_cache(key: str, value):
    """将数据存入缓存"""
    _cache.set(key, value)

def create_cache_key(func_name: str, *args, **kwargs) -> str:
    """根据函数名和参数创建一个唯一的缓存键"""
    return PersistentCache.make_key(func_name, *args, **kwargs)
