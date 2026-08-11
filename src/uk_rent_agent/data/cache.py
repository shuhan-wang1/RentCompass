from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


CACHE_ENVELOPE_SCHEMA = 1
_CACHE_ENVELOPE_MARKER = "uk_rent_agent_cache"

CacheStatus = Literal["miss", "fresh", "stale", "corrupt"]


@dataclass(frozen=True)
class CacheEntry:
    """The result of a cache read, including freshness and provenance.

    The value is deliberately retained for a stale entry so callers can make an
    explicit stale-if-error decision. Corrupt entries never expose a value.
    """

    status: CacheStatus
    value: Any = None
    stored_at: float | None = None
    expires_at: float | None = None
    version: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    legacy: bool = False

    @property
    def is_fresh(self) -> bool:
        return self.status == "fresh"

    @property
    def is_stale(self) -> bool:
        return self.status == "stale"


class PersistentCache:
    def __init__(self, path: Path, max_entries: int = 5_000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cache "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, accessed REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def read(
        self,
        key: str,
        *,
        ttl_seconds: float | None = None,
        version: str | None = None,
        now: float | None = None,
    ) -> CacheEntry:
        """Read an entry without hiding miss/stale/corrupt states.

        ttl_seconds is a caller-side maximum age. It can make an entry expire
        sooner than the TTL declared when it was written, but never extends the
        writer's expiry. version invalidates incompatible payload shapes without
        deleting the old value, allowing an explicitly labelled stale fallback.
        """
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return CacheEntry(status="miss")
            db.execute("UPDATE cache SET accessed = ? WHERE key = ?", (time.time(), key))
            try:
                payload = json.loads(row[0])
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                return CacheEntry(
                    status="corrupt",
                    reason=f"invalid_json:{type(exc).__name__}",
                )

        # Old databases stored the user value directly. Keep them readable for
        # callers without a freshness contract, but do not pretend they satisfy
        # a requested TTL or schema version: their write time/provenance is lost.
        if not (
            isinstance(payload, dict)
            and payload.get("_cache_envelope") == _CACHE_ENVELOPE_MARKER
        ):
            if ttl_seconds is not None or version is not None:
                return CacheEntry(
                    status="stale",
                    value=payload,
                    reason="legacy_entry_has_no_freshness_metadata",
                    legacy=True,
                )
            return CacheEntry(status="fresh", value=payload, legacy=True)

        if payload.get("schema") != CACHE_ENVELOPE_SCHEMA:
            return CacheEntry(status="corrupt", reason="unsupported_envelope_schema")

        stored_at = payload.get("stored_at")
        expires_at = payload.get("expires_at")
        stored_version = payload.get("version")
        provenance = payload.get("provenance", {})
        if (
            not isinstance(stored_at, (int, float))
            or (expires_at is not None and not isinstance(expires_at, (int, float)))
            or not isinstance(stored_version, str)
            or not isinstance(provenance, dict)
            or "value" not in payload
        ):
            return CacheEntry(status="corrupt", reason="invalid_envelope_fields")

        common = {
            "value": payload["value"],
            "stored_at": float(stored_at),
            "expires_at": float(expires_at) if expires_at is not None else None,
            "version": stored_version,
            "provenance": provenance,
        }
        if version is not None and stored_version != str(version):
            return CacheEntry(status="stale", reason="version_mismatch", **common)

        effective_expiry = common["expires_at"]
        if ttl_seconds is not None:
            caller_expiry = common["stored_at"] + max(0.0, float(ttl_seconds))
            effective_expiry = (
                caller_expiry if effective_expiry is None
                else min(effective_expiry, caller_expiry)
            )
        read_at = time.time() if now is None else now
        if effective_expiry is not None and read_at >= effective_expiry:
            return CacheEntry(status="stale", reason="expired", **common)
        return CacheEntry(status="fresh", **common)

    def get(self, key: str) -> Any:
        """Compatibility read: return only a usable value, else None."""
        entry = self.read(key)
        return entry.value if entry.is_fresh else None

    def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float | None = None,
        version: str = "1",
        provenance: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        """Store a versioned cache envelope with freshness metadata."""
        stored_at = time.time() if now is None else float(now)
        expires_at = (
            None if ttl_seconds is None
            else stored_at + max(0.0, float(ttl_seconds))
        )
        envelope = {
            "_cache_envelope": _CACHE_ENVELOPE_MARKER,
            "schema": CACHE_ENVELOPE_SCHEMA,
            "version": str(version),
            "stored_at": stored_at,
            "expires_at": expires_at,
            "provenance": dict(provenance or {}),
            "value": value,
        }
        payload = json.dumps(envelope, ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO cache(key, value, accessed) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, accessed=excluded.accessed",
                (key, payload, time.time()),
            )
            excess = db.execute("SELECT MAX(COUNT(*) - ?, 0) FROM cache", (self.max_entries,)).fetchone()[0]
            if excess:
                db.execute(
                    "DELETE FROM cache WHERE key IN "
                    "(SELECT key FROM cache ORDER BY accessed ASC LIMIT ?)",
                    (excess,),
                )

    @staticmethod
    def make_key(func_name: str, *args: object, **kwargs: object) -> str:
        data = json.dumps(
            {"func": func_name, "args": args, "kwargs": sorted(kwargs.items())},
            sort_keys=True,
            default=str,
        )
        return hashlib.md5(data.encode("utf-8")).hexdigest()
