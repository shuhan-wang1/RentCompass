from __future__ import annotations

import sqlite3

from uk_rent_agent.data.cache import PersistentCache


def test_cache_envelope_reports_fresh_stale_version_and_provenance(tmp_path):
    cache = PersistentCache(tmp_path / "cache.sqlite3")
    cache.set(
        "key",
        {"answer": 42},
        ttl_seconds=60,
        version="payload-v2",
        provenance={"provider": "example"},
        now=100.0,
    )

    fresh = cache.read("key", version="payload-v2", now=159.9)
    assert fresh.status == "fresh"
    assert fresh.value == {"answer": 42}
    assert fresh.stored_at == 100.0
    assert fresh.expires_at == 160.0
    assert fresh.provenance == {"provider": "example"}

    stale = cache.read("key", version="payload-v2", now=160.0)
    assert stale.status == "stale"
    assert stale.reason == "expired"
    assert stale.value == {"answer": 42}

    incompatible = cache.read("key", version="payload-v3", now=101.0)
    assert incompatible.status == "stale"
    assert incompatible.reason == "version_mismatch"


def test_caller_ttl_can_shorten_but_not_extend_writer_expiry(tmp_path):
    cache = PersistentCache(tmp_path / "cache.sqlite3")
    cache.set("key", "value", ttl_seconds=60, now=100.0)

    assert cache.read("key", ttl_seconds=10, now=110.0).status == "stale"
    assert cache.read("key", ttl_seconds=600, now=160.0).status == "stale"


def test_legacy_rows_are_usable_without_contract_but_stale_with_one(tmp_path):
    cache = PersistentCache(tmp_path / "cache.sqlite3")
    with sqlite3.connect(cache.path) as db:
        db.execute(
            "INSERT INTO cache(key, value, accessed) VALUES (?, ?, ?)",
            ("legacy", '{"old": true}', 1.0),
        )

    assert cache.get("legacy") == {"old": True}
    contracted = cache.read("legacy", ttl_seconds=60, version="v2")
    assert contracted.status == "stale"
    assert contracted.legacy is True
    assert contracted.reason == "legacy_entry_has_no_freshness_metadata"


def test_corrupt_json_degrades_to_structured_signal_and_value_miss(
    tmp_path, monkeypatch, caplog,
):
    from core import cache_service

    cache = PersistentCache(tmp_path / "cache.sqlite3")
    with sqlite3.connect(cache.path) as db:
        db.execute(
            "INSERT INTO cache(key, value, accessed) VALUES (?, ?, ?)",
            ("broken", "{not-json", 1.0),
        )
    monkeypatch.setattr(cache_service, "_cache", cache)

    entry = cache_service.get_cache_entry("broken")
    assert entry.status == "corrupt"
    assert entry.value is None
    assert entry.reason.startswith("invalid_json:")
    assert cache_service.get_from_cache("broken") is None
    records = [r for r in caplog.records if getattr(r, "cache_status", None) == "corrupt"]
    assert records
    assert records[-1].cache_key == "broken"


def test_value_api_never_serves_stale_unless_explicit(tmp_path, monkeypatch):
    from core import cache_service

    cache = PersistentCache(tmp_path / "cache.sqlite3")
    cache.set("expired", "old", ttl_seconds=1, now=1.0)
    monkeypatch.setattr(cache_service, "_cache", cache)

    assert cache_service.get_from_cache("expired") is None
    assert cache_service.get_from_cache("expired", allow_stale=True) == "old"
