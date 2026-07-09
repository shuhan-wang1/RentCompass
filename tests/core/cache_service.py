# cache_service.py

import json
import hashlib

# 使用简单的字典作为内存缓存
_cache = {}

def get_from_cache(key: str):
    """从缓存中获取数据"""
    return _cache.get(key)

def set_to_cache(key: str, value):
    """将数据存入缓存"""
    _cache[key] = value

def create_cache_key(func_name: str, *args, **kwargs) -> str:
    """根据函数名和参数创建一个唯一的缓存键"""
    # 将所有参数转换为稳定的JSON字符串
    s = json.dumps({
        'func': func_name,
        'args': args,
        'kwargs': sorted(kwargs.items())
    }, sort_keys=True)
    
    # 使用哈希算法确保键的长度一致且安全
    return hashlib.md5(s.encode('utf-8')).hexdigest()