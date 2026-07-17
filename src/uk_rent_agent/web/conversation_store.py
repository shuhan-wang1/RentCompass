"""Durable multi-conversation + favorites store (sqlite, survives restart).

New sqlite DB (separate file from the LangGraph checkpointer) holding three tables:
  conversations(user_id, id, title, created_at, updated_at)      PK (user_id, id)
  messages(id, user_id, conversation_id, role, content,
           response_type, recommendations_json, timestamp)        autoincrement id
  favorites(user_id, url, property_json, created_at)              PK (user_id, url)

All state is keyed by (user_id[, conversation_id]); favorites are per-USER. The store
is the source of truth — SessionStore is only a hot cache rehydrated from here on miss.

Thread-safety: one connection guarded by an RLock. Every op is short (no LLM calls), so a
single lock is simpler and correct under Flask's per-request worker threads.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import threading
import uuid
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    user_id    TEXT NOT NULL,
    id         TEXT NOT NULL,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE TABLE IF NOT EXISTS messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              TEXT NOT NULL,
    conversation_id      TEXT NOT NULL,
    role                 TEXT NOT NULL,
    content              TEXT NOT NULL,
    response_type        TEXT,
    recommendations_json TEXT,
    timestamp            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv
    ON messages (user_id, conversation_id, id);
CREATE TABLE IF NOT EXISTS favorites (
    user_id       TEXT NOT NULL,
    url           TEXT NOT NULL,
    property_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, url)
);
"""


_NOW_LOCK = threading.Lock()
_LAST_NOW = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _now_iso() -> str:
    # ISO-8601 UTC with microseconds → lexicographically sortable for updated_at DESC.
    # Strictly monotonic within the process: Windows clock granularity can return the
    # same instant for consecutive calls, which would make ORDER BY updated_at ties
    # (and thus list_conversations order) nondeterministic.
    global _LAST_NOW
    with _NOW_LOCK:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now <= _LAST_NOW:
            now = _LAST_NOW + datetime.timedelta(microseconds=1)
        _LAST_NOW = now
        return now.isoformat()


class ConversationStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------ conversations
    def create_conversation(self, user_id: str, title: str | None = None) -> dict:
        cid = uuid.uuid4().hex
        now = _now_iso()
        title = (title or "").strip() or "New chat"
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations(user_id,id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (user_id, cid, title, now, now),
            )
            self._conn.commit()
        return {"id": cid, "title": title, "created_at": now,
                "updated_at": now, "message_count": 0}

    def get_conversation(self, user_id: str, cid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, title, created_at, updated_at,
                          (SELECT COUNT(*) FROM messages m
                             WHERE m.user_id=? AND m.conversation_id=?) AS message_count
                   FROM conversations WHERE user_id=? AND id=?""",
                (user_id, cid, user_id, cid),
            ).fetchone()
        return self._conv_dict(row) if row else None

    def list_conversations(self, user_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.id, c.title, c.created_at, c.updated_at,
                          (SELECT COUNT(*) FROM messages m
                             WHERE m.user_id=c.user_id AND m.conversation_id=c.id) AS message_count
                   FROM conversations c WHERE c.user_id=?
                   ORDER BY c.updated_at DESC""",
                (user_id,),
            ).fetchall()
        return [self._conv_dict(r) for r in rows]

    def rename_conversation(self, user_id: str, cid: str, title: str) -> dict | None:
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE user_id=? AND id=?",
                (title, now, user_id, cid),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_conversation(user_id, cid)

    def delete_conversation(self, user_id: str, cid: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conversations WHERE user_id=? AND id=?", (user_id, cid)
            )
            self._conn.execute(
                "DELETE FROM messages WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_all_conversations(self, user_id: str) -> list[str]:
        """Delete every conversation + message for a user; return the deleted ids
        (so the caller can drop the matching LangGraph checkpointer threads)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM conversations WHERE user_id=?", (user_id,)
            ).fetchall()
            cids = [r["id"] for r in rows]
            self._conn.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))
            self._conn.commit()
        return cids

    def clear_conversation_messages(self, user_id: str, cid: str) -> bool:
        """Empty a conversation's transcript but keep the (renamed) row."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM conversations WHERE user_id=? AND id=?", (user_id, cid)
            ).fetchone()
            if not exists:
                return False
            self._conn.execute(
                "DELETE FROM messages WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.execute(
                "UPDATE conversations SET updated_at=? WHERE user_id=? AND id=?",
                (_now_iso(), user_id, cid),
            )
            self._conn.commit()
        return True

    # ----------------------------------------------------------------- messages
    def add_message(self, user_id: str, cid: str, role: str, content: str,
                    response_type: str | None = None, recommendations=None,
                    timestamp: str | None = None) -> str:
        ts = timestamp or _now_iso()
        rec_json = (json.dumps(recommendations, ensure_ascii=False)
                    if recommendations is not None else None)
        with self._lock:
            self._conn.execute(
                """INSERT INTO messages
                   (user_id, conversation_id, role, content, response_type,
                    recommendations_json, timestamp)
                   VALUES(?,?,?,?,?,?,?)""",
                (user_id, cid, role, content or "", response_type, rec_json, ts),
            )
            # bump updated_at per turn
            self._conn.execute(
                "UPDATE conversations SET updated_at=? WHERE user_id=? AND id=?",
                (ts, user_id, cid),
            )
            self._conn.commit()
        return ts

    def get_messages(self, user_id: str, cid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT role, content, response_type, recommendations_json, timestamp
                   FROM messages WHERE user_id=? AND conversation_id=? ORDER BY id ASC""",
                (user_id, cid),
            ).fetchall()
        out = []
        for r in rows:
            msg = {"role": r["role"], "content": r["content"], "timestamp": r["timestamp"]}
            if r["response_type"]:
                msg["response_type"] = r["response_type"]
            if r["recommendations_json"]:
                try:
                    msg["recommendations"] = json.loads(r["recommendations_json"])
                except Exception:
                    pass
            out.append(msg)
        return out

    def rehydrate_history(self, user_id: str, cid: str, max_len: int = 10) -> list[dict]:
        """Rebuild the SessionStore [{'user','assistant'}] history from persisted rows
        (used on a cache miss / after a restart)."""
        history: list[dict] = []
        pending_user = None
        for msg in self.get_messages(user_id, cid):
            if msg["role"] == "user":
                pending_user = msg["content"]
            elif msg["role"] == "assistant":
                history.append({"user": pending_user or "",
                                "assistant": (msg["content"] or "")[:500]})
                pending_user = None
        if max_len and len(history) > max_len:
            history = history[-max_len:]
        return history

    # ---------------------------------------------------------------- favorites
    def add_favorite(self, user_id: str, url: str, property_dict: dict) -> None:
        """Upsert a favorite. Stores the FULL client dict verbatim (incl. geo_location)."""
        now = _now_iso()
        payload = json.dumps(property_dict, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """INSERT INTO favorites(user_id, url, property_json, created_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id, url)
                   DO UPDATE SET property_json=excluded.property_json""",
                (user_id, url, payload, now),
            )
            self._conn.commit()

    def list_favorites(self, user_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT property_json FROM favorites WHERE user_id=? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["property_json"]))
            except Exception:
                pass
        return out

    def remove_favorite(self, user_id: str, url: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM favorites WHERE user_id=? AND url=?", (user_id, url)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_all_favorites(self, user_id: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM favorites WHERE user_id=?", (user_id,))
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _conv_dict(row) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": row["message_count"],
        }
