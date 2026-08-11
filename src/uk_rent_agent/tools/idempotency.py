from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InvocationRecord:
    key: str
    tool: str
    status: str
    result: Any | None
    error: str | None
    updated: float
    lease_expires: float | None


class IdempotencyStore:
    """Durable write ledger with explicit terminal and uncertain outcomes.

    A timed-out write is never silently released for retry: ``running`` leases that
    expire become ``unknown`` and require reconciliation or a new user-authorised
    logical invocation.  This favours one missing write over an accidental duplicate.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS tool_invocations ("
                "key TEXT PRIMARY KEY, tool TEXT NOT NULL, status TEXT NOT NULL, "
                "result TEXT, updated REAL NOT NULL, error TEXT, lease_expires REAL)"
            )
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(tool_invocations)")
            }
            if "error" not in columns:
                db.execute("ALTER TABLE tool_invocations ADD COLUMN error TEXT")
            if "lease_expires" not in columns:
                db.execute("ALTER TABLE tool_invocations ADD COLUMN lease_expires REAL")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def claim(self, key: str, tool: str, *, lease_seconds: float = 120.0) -> bool:
        """Atomically claim a logical write. False means it was already claimed."""
        with self._lock, self._connect() as db:
            now = time.time()
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE tool_invocations SET status='unknown', error=COALESCE(error, ?), "
                "updated=?, lease_expires=NULL "
                "WHERE key=? AND status='running' AND lease_expires IS NOT NULL "
                "AND lease_expires<=?",
                ("write lease expired before completion; outcome is unknown", now, key, now),
            )
            cursor = db.execute(
                "INSERT OR IGNORE INTO tool_invocations("
                "key, tool, status, updated, lease_expires) "
                "VALUES (?, ?, 'running', ?, ?)",
                (key, tool, now, now + max(1.0, float(lease_seconds))),
            )
            return cursor.rowcount == 1

    def complete(self, key: str, result: Any) -> None:
        payload = json.dumps(result, ensure_ascii=False, default=str)
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE tool_invocations SET status='complete', result=?, error=NULL, "
                "updated=?, lease_expires=NULL WHERE key=? AND status IN ('running', 'unknown')",
                (payload, time.time(), key),
            )

    def fail(self, key: str, error: str, result: Any | None = None) -> None:
        """Record a known terminal failure; the same logical key replays it."""
        payload = None if result is None else json.dumps(
            result, ensure_ascii=False, default=str
        )
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE tool_invocations SET status='failed', result=?, error=?, "
                "updated=?, lease_expires=NULL WHERE key=? AND status='running'",
                (payload, str(error), time.time(), key),
            )

    def mark_unknown(self, key: str, error: str, *, tool: str | None = None) -> None:
        """Freeze an ambiguous write so automatic retries cannot duplicate it."""
        with self._lock, self._connect() as db:
            if tool:
                db.execute(
                    "INSERT OR IGNORE INTO tool_invocations("
                    "key, tool, status, updated, error, lease_expires) "
                    "VALUES (?, ?, 'unknown', ?, ?, NULL)",
                    (key, tool, time.time(), str(error)),
                )
            db.execute(
                "UPDATE tool_invocations SET status='unknown', error=?, updated=?, "
                "lease_expires=NULL WHERE key=? AND status IN ('running', 'unknown')",
                (str(error), time.time(), key),
            )

    def release(self, key: str) -> None:
        """Backward-compatible safety shim: running writes become unknown, not retryable."""
        self.mark_unknown(key, "write claim released without a confirmed outcome")

    def get_record(self, key: str) -> InvocationRecord | None:
        with self._lock, self._connect() as db:
            now = time.time()
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE tool_invocations SET status='unknown', error=COALESCE(error, ?), "
                "updated=?, lease_expires=NULL WHERE key=? AND status='running' "
                "AND lease_expires IS NOT NULL AND lease_expires<=?",
                ("write lease expired before completion; outcome is unknown", now, key, now),
            )
            row = db.execute(
                "SELECT key, tool, status, result, error, updated, lease_expires "
                "FROM tool_invocations WHERE key=?", (key,),
            ).fetchone()
        if row is None:
            return None
        parsed = None
        if row[3] is not None:
            try:
                parsed = json.loads(row[3])
            except (TypeError, json.JSONDecodeError):
                parsed = None
        return InvocationRecord(
            key=row[0], tool=row[1], status=row[2], result=parsed,
            error=row[4], updated=float(row[5]), lease_expires=row[6],
        )

    def get(self, key: str) -> Any | None:
        record = self.get_record(key)
        if record is None or record.status != "complete":
            return None
        return record.result
