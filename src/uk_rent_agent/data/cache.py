from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


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

    def get(self, key: str) -> Any:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE cache SET accessed = ? WHERE key = ?", (time.time(), key))
            return json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
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
