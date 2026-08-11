# cache_service.py

import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from uk_rent_agent.data.cache import CacheEntry, PersistentCache

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


def get_cache_entry(
    key: str,
    *,
    ttl_seconds: float | None = None,
    version: str | None = None,
) -> CacheEntry:
    """Return a structured cache result (fresh/stale/miss/corrupt)."""
    try:
        entry = _cache.read(key, ttl_seconds=ttl_seconds, version=version)
    except (OSError, sqlite3.Error) as exc:
        entry = _switch_to_fallback(exc).read(
            key, ttl_seconds=ttl_seconds, version=version,
        )
    if entry.status == "corrupt":
        # Parseable message plus structured extra for handlers that retain
        # arbitrary LogRecord fields. A bad row is a cache miss, not an outage.
        logger.warning(
            "cache_read status=corrupt key=%s reason=%s",
            key,
            entry.reason,
            extra={
                "cache_status": "corrupt",
                "cache_key": key,
                "cache_reason": entry.reason,
            },
        )
    return entry


def get_from_cache(
    key: str,
    *,
    ttl_seconds: float | None = None,
    version: str | None = None,
    allow_stale: bool = False,
    with_status: bool = False,
):
    """Read cached data while preserving the historical value-or-None API.

    New callers can opt into the full CacheEntry contract with with_status.
    Stale values are returned only when explicitly asked.
    """
    entry = get_cache_entry(key, ttl_seconds=ttl_seconds, version=version)
    if with_status:
        return entry
    if entry.is_fresh or (allow_stale and entry.is_stale):
        return entry.value
    return None


def set_to_cache(
    key: str,
    value,
    *,
    ttl_seconds: float | None = None,
    version: str = "1",
    provenance: dict | None = None,
):
    """Store data with an envelope containing TTL, version and provenance."""
    try:
        _cache.set(
            key,
            value,
            ttl_seconds=ttl_seconds,
            version=version,
            provenance=provenance,
        )
    except (OSError, sqlite3.Error) as exc:
        _switch_to_fallback(exc).set(
            key,
            value,
            ttl_seconds=ttl_seconds,
            version=version,
            provenance=provenance,
        )


def create_cache_key(func_name: str, *args, **kwargs) -> str:
    """根据函数名和参数创建一个唯一的缓存键"""
    return PersistentCache.make_key(func_name, *args, **kwargs)
