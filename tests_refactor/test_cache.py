from uk_rent_agent.data.cache import PersistentCache


def test_cache_key_matches_legacy_md5():
    from core.cache_service import create_cache_key

    assert PersistentCache.make_key("f", 1, b=2) == create_cache_key("f", 1, b=2)


def test_cache_persists_across_instances(tmp_path):
    path = tmp_path / "cache.sqlite3"
    PersistentCache(path).set("key", {"value": 3})
    assert PersistentCache(path).get("key") == {"value": 3}
