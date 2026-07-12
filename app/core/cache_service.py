# cache_service.py

import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from uk_rent_agent.data.cache import PersistentCache

logger = logging.getLogger(__name__)


def _assert_writable(path: Path) -> None:
    """Check write access without changing the cache contents."""
    with sqlite3.connect(path, timeout=1) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _build_cache() -> PersistentCache:
    configured_path = os.getenv("RUNTIME_CACHE_PATH")
    primary_path = Path(configured_path) if configured_path else (
        Path(__file__).resolve().parents[1] / "data" / "runtime_cache.sqlite3"
    )
    try:
        cache = PersistentCache(primary_path)
        _assert_writable(primary_path)
        return cache
    except (OSError, sqlite3.Error) as exc:
        fallback_path = Path(tempfile.gettempdir()) / "uk-rent-agent" / "runtime_cache.sqlite3"
        logger.warning(
            "Runtime cache %s is not writable (%s); using %s",
            primary_path,
            exc,
            fallback_path,
        )
        return PersistentCache(fallback_path)


_cache = _build_cache()

def _switch_to_fallback(exc: Exception) -> PersistentCache:
    global _cache
    fallback_path = Path(tempfile.gettempdir()) / "uk-rent-agent" / "runtime_cache.sqlite3"
    if _cache.path != fallback_path:
        logger.warning(
            "Runtime cache %s failed during use (%s); switching to %s",
            _cache.path,
            exc,
            fallback_path,
        )
        _cache = PersistentCache(fallback_path)
    return _cache


def get_from_cache(key: str):
    """从缓存中获取数据"""
    try:
        return _cache.get(key)
    except (OSError, sqlite3.Error) as exc:
        return _switch_to_fallback(exc).get(key)

def set_to_cache(key: str, value):
    """将数据存入缓存"""
    try:
        _cache.set(key, value)
    except (OSError, sqlite3.Error) as exc:
        _switch_to_fallback(exc).set(key, value)

def create_cache_key(func_name: str, *args, **kwargs) -> str:
    """根据函数名和参数创建一个唯一的缓存键"""
    return PersistentCache.make_key(func_name, *args, **kwargs)
