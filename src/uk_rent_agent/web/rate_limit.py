from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path


class SlidingWindowRateLimiter:
    """Sliding-window limiter with an optional shared SQLite ledger.

    SQLite mode is shared by both blue/green processes and survives restarts. Keys are
    SHA-256 digests, so raw user/IP subjects are not added to another durable store.
    The in-memory mode remains available for isolated unit tests.
    """

    def __init__(self, clock=None, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else None
        self._clock = clock or (time.time if self.db_path else time.monotonic)
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    """CREATE TABLE IF NOT EXISTS rate_limit_events (
                           subject_hash TEXT NOT NULL,
                           occurred REAL NOT NULL
                       )"""
                )
                db.execute(
                    """CREATE INDEX IF NOT EXISTS idx_rate_limit_subject_time
                       ON rate_limit_events(subject_hash, occurred)"""
                )

    def _connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            raise RuntimeError("shared rate-limit database is not configured")
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def allow(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        if limit < 1 or window_seconds < 1:
            raise ValueError("limit and window_seconds must be positive")
        if self.db_path is not None:
            return self._allow_shared(key, limit=limit, window_seconds=window_seconds)
        now = self._clock()
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            cutoff = now - window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
                return False, retry_after
            bucket.append(now)
            return True, 0

    def _allow_shared(
        self, key: str, *, limit: int, window_seconds: int
    ) -> tuple[bool, int]:
        now = float(self._clock())
        cutoff = now - window_seconds
        subject_hash = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Bound the database globally as requests advance the window.
            db.execute("DELETE FROM rate_limit_events WHERE occurred<=?", (cutoff,))
            row = db.execute(
                """SELECT COUNT(*) AS n, MIN(occurred) AS oldest
                   FROM rate_limit_events
                   WHERE subject_hash=? AND occurred>?""",
                (subject_hash, cutoff),
            ).fetchone()
            count = int(row[0] or 0)
            if count >= limit:
                oldest = float(row[1] or now)
                retry_after = max(1, int(oldest + window_seconds - now) + 1)
                return False, retry_after
            db.execute(
                "INSERT INTO rate_limit_events(subject_hash, occurred) VALUES(?,?)",
                (subject_hash, now),
            )
        return True, 0
