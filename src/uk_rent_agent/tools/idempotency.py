from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class IdempotencyStore:
    """Durable result ledger used before retrying write tools."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS tool_invocations ("
                "key TEXT PRIMARY KEY, tool TEXT NOT NULL, status TEXT NOT NULL, "
                "result TEXT, updated REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def claim(self, key: str, tool: str) -> bool:
        """Atomically claim a logical write. False means it was already claimed."""
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO tool_invocations(key, tool, status, updated) "
                "VALUES (?, ?, 'running', ?)",
                (key, tool, time.time()),
            )
            return cursor.rowcount == 1

    def complete(self, key: str, result: Any) -> None:
        payload = json.dumps(result, ensure_ascii=False, default=str)
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE tool_invocations SET status='complete', result=?, updated=? WHERE key=?",
                (payload, time.time(), key),
            )

    def release(self, key: str) -> None:
        """Release a failed claim so an explicitly requested retry can run."""
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM tool_invocations WHERE key=? AND status='running'", (key,))

    def get(self, key: str) -> Any | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT status, result FROM tool_invocations WHERE key=?", (key,)
            ).fetchone()
        if not row or row[0] != "complete" or row[1] is None:
            return None
        return json.loads(row[1])
