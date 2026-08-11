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
    user_id                TEXT NOT NULL,
    id                     TEXT NOT NULL,
    title                  TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    parent_conversation_id TEXT,
    forked_from_turn_id    TEXT,
    root_conversation_id   TEXT,
    branch_depth           INTEGER NOT NULL DEFAULT 0,
    context_schema_version INTEGER NOT NULL DEFAULT 1,
    fork_reason            TEXT,
    edited_slot_turn_id    TEXT,
    -- Architecture provenance for the process that last served this conversation
    -- (see create_conversation / set_agent_assignment). agent_arch is 'legacy'|'fc_loop';
    -- agent_version is the candidate SHA (APP_CANDIDATE_SHA); strict mirrors DEEPSEEK_STRICT.
    agent_arch             TEXT NOT NULL DEFAULT 'legacy',
    agent_version          TEXT NOT NULL DEFAULT 'unknown',
    strict                 INTEGER NOT NULL DEFAULT 0,
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
    timestamp            TEXT NOT NULL,
    turn_id              TEXT
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
CREATE TABLE IF NOT EXISTS turns (
    id                   TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL,
    conversation_id      TEXT NOT NULL,
    request_id           TEXT,
    user_message_id      INTEGER,
    assistant_message_id INTEGER,
    status               TEXT NOT NULL,
    started_at           TEXT NOT NULL,
    completed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns (user_id, conversation_id, started_at);
CREATE TABLE IF NOT EXISTS turn_requests (
    user_id         TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_turn_requests_conv
    ON turn_requests (user_id, conversation_id, created_at);
CREATE TABLE IF NOT EXISTS conversation_turn_leases (
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL UNIQUE,
    acquired_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS privacy_erasure_locks (
    user_id    TEXT PRIMARY KEY,
    started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS background_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    result_json     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    available_at    TEXT NOT NULL,
    claimed_by      TEXT,
    lease_expires   TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (user_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_ready
    ON background_jobs (status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_background_jobs_conversation
    ON background_jobs (user_id, conversation_id, status, id);
CREATE TABLE IF NOT EXISTS turn_snapshots (
    turn_id         TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    snapshot_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_conv ON turn_snapshots (user_id, conversation_id);
CREATE TABLE IF NOT EXISTS fork_requests (
    user_id         TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, idempotency_key)
);
CREATE TRIGGER IF NOT EXISTS turns_completed_requires_assistant_insert
BEFORE INSERT ON turns
WHEN NEW.status = 'completed' AND NEW.assistant_message_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'completed turn requires assistant_message_id');
END;
CREATE TRIGGER IF NOT EXISTS turns_completed_requires_assistant_update
BEFORE UPDATE OF status, assistant_message_id ON turns
WHEN NEW.status = 'completed' AND NEW.assistant_message_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'completed turn requires assistant_message_id');
END;
"""


class ForkError(Exception):
    """Base class for all fork_conversation validation failures."""


class ConversationNotFound(ForkError):
    """Source conversation does not exist for this user."""


class NoCompletedTurn(ForkError):
    """No completed turn is available to fork from (after_turn_id omitted)."""


class TurnNotFound(ForkError):
    """The requested after_turn_id does not exist."""


class TurnNotInConversation(ForkError):
    """The requested turn exists but belongs to a different conversation."""


class TurnNotCompleted(ForkError):
    """The requested turn is not in status 'completed'."""


class ConversationBusy(RuntimeError):
    """A different request currently owns the conversation turn lease."""

    def __init__(self, turn_id: str, retry_after: int):
        super().__init__("conversation already has a running turn")
        self.turn_id = turn_id
        self.retry_after = max(1, int(retry_after))


class PrivacyErasureInProgress(RuntimeError):
    """New turns are blocked while the user's cross-store deletion is running."""


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
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate()

    def _migrate(self) -> None:
        """Bring an existing DB up to the current schema in place. Idempotent:
        PRAGMA table_info → ALTER TABLE ADD COLUMN for anything missing. New tables
        are already created by executescript(_SCHEMA) (all IF NOT EXISTS)."""
        def _cols(table: str) -> set[str]:
            return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}

        conv_cols = _cols("conversations")
        for name, decl in (
            ("parent_conversation_id", "TEXT"),
            ("forked_from_turn_id", "TEXT"),
            ("root_conversation_id", "TEXT"),
            ("branch_depth", "INTEGER NOT NULL DEFAULT 0"),
            ("context_schema_version", "INTEGER NOT NULL DEFAULT 1"),
            ("fork_reason", "TEXT"),
            ("edited_slot_turn_id", "TEXT"),
            # Durable architecture provenance for rollout/rollback auditing.
            ("agent_arch", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("agent_version", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("strict", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in conv_cols:
                self._conn.execute(f"ALTER TABLE conversations ADD COLUMN {name} {decl}")
        if "turn_id" not in _cols("messages"):
            self._conn.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT")
        # Backfill: pre-fork rows are their own root.
        self._conn.execute(
            "UPDATE conversations SET root_conversation_id=id WHERE root_conversation_id IS NULL"
        )
        self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------ conversations
    def create_conversation(self, user_id: str, title: str | None = None, *,
                            agent_arch: str = "legacy", agent_version: str = "unknown",
                            strict: bool = False) -> dict:
        """Create a conversation with the serving process's provenance triple.

        ``set_agent_assignment`` retains its historical name for schema/API compatibility;
        it updates this provenance after an explicit pool switch or rollback.
        """
        cid = uuid.uuid4().hex
        now = _now_iso()
        title = (title or "").strip() or "New chat"
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversations
                   (user_id, id, title, created_at, updated_at,
                    parent_conversation_id, forked_from_turn_id,
                    root_conversation_id, branch_depth, context_schema_version,
                    agent_arch, agent_version, strict)
                   VALUES(?,?,?,?,?,NULL,NULL,?,0,1,?,?,?)""",
                (user_id, cid, title, now, now, cid,
                 agent_arch, agent_version, 1 if strict else 0),
            )
            self._conn.commit()
        return {"id": cid, "title": title, "created_at": now,
                "updated_at": now, "message_count": 0,
                "parent_conversation_id": None, "forked_from_turn_id": None,
                "root_conversation_id": cid, "branch_depth": 0,
                "fork_reason": None, "edited_slot_turn_id": None,
                "agent_arch": agent_arch, "agent_version": agent_version,
                "strict": bool(strict)}

    def set_agent_assignment(self, user_id: str, cid: str, agent_arch: str,
                             agent_version: str, strict: bool) -> None:
        """Update the stored (agent_arch, agent_version, strict) provenance triple.

        The historical method name is retained for compatibility. A process calls it when
        it serves a conversation stamped by the other pool after an explicit cutover. It
        does not touch ``updated_at`` because reconciliation is operational metadata, not a
        user-visible edit that should reorder the conversation list.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET agent_arch=?, agent_version=?, strict=? "
                "WHERE user_id=? AND id=?",
                (agent_arch, agent_version, 1 if strict else 0, user_id, cid),
            )
            self._conn.commit()

    def get_conversation(self, user_id: str, cid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, title, created_at, updated_at,
                          parent_conversation_id, forked_from_turn_id,
                          root_conversation_id, branch_depth,
                          fork_reason, edited_slot_turn_id,
                          agent_arch, agent_version, strict,
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
                          c.parent_conversation_id, c.forked_from_turn_id,
                          c.root_conversation_id, c.branch_depth,
                          c.fork_reason, c.edited_slot_turn_id,
                          c.agent_arch, c.agent_version, c.strict,
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
            self._conn.execute(
                "DELETE FROM turn_snapshots WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.execute(
                "DELETE FROM turns WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.execute(
                "DELETE FROM turn_requests WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.execute(
                "DELETE FROM conversation_turn_leases WHERE user_id=? AND conversation_id=?",
                (user_id, cid),
            )
            self._conn.execute(
                "DELETE FROM background_jobs WHERE user_id=? AND conversation_id=?",
                (user_id, cid),
            )
            self._conn.commit()
            # Children of a deleted parent keep their (now dangling) lineage pointers.
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
            self._conn.execute("DELETE FROM turn_snapshots WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM turns WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM turn_requests WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM conversation_turn_leases WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM background_jobs WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))
            self._conn.commit()
        return cids

    def _privacy_inventory_unlocked(self, user_id: str) -> dict[str, int]:
        """Count every user-owned row in this database; caller holds self._lock."""
        tables = (
            "conversations",
            "messages",
            "favorites",
            "turns",
            "turn_snapshots",
            "turn_requests",
            "conversation_turn_leases",
            "background_jobs",
            "fork_requests",
        )
        inventory: dict[str, int] = {}
        for table in tables:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=?",
                (user_id,),
            ).fetchone()
            inventory[table] = int(row["n"] if row else 0)
        inventory["total"] = sum(inventory.values())
        return inventory

    def privacy_inventory(self, user_id: str) -> dict[str, int]:
        """Return a non-content count of residual user data for erasure verification."""
        with self._lock:
            return self._privacy_inventory_unlocked(user_id)

    def delete_all_user_data(self, user_id: str) -> dict:
        """Transactionally delete all user-owned data in the conversation database.

        The caller still owns external stores (LangGraph checkpoints, AgentMemory and
        process-local hot state). Returning the conversation ids lets that caller
        delete and verify those layers without first querying content.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                before = self._privacy_inventory_unlocked(user_id)
                rows = self._conn.execute(
                    "SELECT id FROM conversations WHERE user_id=?",
                    (user_id,),
                ).fetchall()
                cids = [str(row["id"]) for row in rows]
                for table in (
                    "messages",
                    "turn_snapshots",
                    "turns",
                    "turn_requests",
                    "conversation_turn_leases",
                    "background_jobs",
                    "fork_requests",
                    "favorites",
                    "conversations",
                ):
                    self._conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
                after = self._privacy_inventory_unlocked(user_id)
                if after["total"] != 0:
                    raise RuntimeError(f"relational erasure left {after['total']} rows")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"conversation_ids": cids, "before": before, "after": after}

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
            # Resetting the transcript also drops its turns + snapshots.
            self._conn.execute(
                "DELETE FROM turn_snapshots WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.execute(
                "DELETE FROM turns WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.execute(
                "DELETE FROM turn_requests WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.execute(
                "DELETE FROM conversation_turn_leases WHERE user_id=? AND conversation_id=?",
                (user_id, cid),
            )
            self._conn.execute(
                "DELETE FROM background_jobs WHERE user_id=? AND conversation_id=?",
                (user_id, cid),
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
                    timestamp: str | None = None, turn_id: str | None = None) -> dict:
        """Persist a message; returns {"id": <int rowid>, "timestamp": <ts>}."""
        ts = timestamp or _now_iso()
        rec_json = (json.dumps(recommendations, ensure_ascii=False)
                    if recommendations is not None else None)
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO messages
                   (user_id, conversation_id, role, content, response_type,
                    recommendations_json, timestamp, turn_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (user_id, cid, role, content or "", response_type, rec_json, ts, turn_id),
            )
            row_id = cur.lastrowid
            # bump updated_at per turn
            self._conn.execute(
                "UPDATE conversations SET updated_at=? WHERE user_id=? AND id=?",
                (ts, user_id, cid),
            )
            self._conn.commit()
        return {"id": row_id, "timestamp": ts}

    def set_message_turn(self, user_id: str, message_id: int, turn_id: str) -> None:
        """Tag an already-persisted message row with its turn_id. Used by the live app to
        stamp the USER message after begin_turn mints the turn id (the row is written first
        to obtain its rowid for turns.user_message_id). Idempotent; no-op if the row is gone."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET turn_id=? WHERE user_id=? AND id=?",
                (turn_id, user_id, message_id),
            )
            self._conn.commit()

    def get_messages(self, user_id: str, cid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, role, content, response_type, recommendations_json,
                          timestamp, turn_id
                   FROM messages WHERE user_id=? AND conversation_id=? ORDER BY id ASC""",
                (user_id, cid),
            ).fetchall()
        out = []
        for r in rows:
            msg = {"id": r["id"], "role": r["role"], "content": r["content"],
                   "timestamp": r["timestamp"], "turn_id": r["turn_id"]}
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

    # --------------------------------------------------------------------- turns
    def start_request_turn(
        self,
        user_id: str,
        cid: str | None,
        request_id: str,
        user_content: str,
        *,
        lease_seconds: int = 15 * 60,
        create_title: str | None = None,
        agent_arch: str = "legacy",
        agent_version: str = "unknown",
        strict: bool = False,
    ) -> dict:
        """Atomically persist the user message and claim a single-flight turn lease.

        This is the production HTTP boundary. It intentionally co-exists with the
        lower-level begin_turn method used by import/fork tooling and historical
        tests. The durable turn_requests row makes retries idempotent across
        processes; the lease prevents two different requests from executing the same
        conversation concurrently. Expired leases are reclaimed and their abandoned
        turns are marked failed.
        """
        request_id = str(request_id or "").strip()
        if not request_id:
            raise ValueError("request_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")

        now = _now_iso()
        expires = (
            datetime.datetime.fromisoformat(now)
            + datetime.timedelta(seconds=int(lease_seconds))
        ).isoformat()
        tid = uuid.uuid4().hex
        conversation_created = False

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                erasing = self._conn.execute(
                    "SELECT 1 FROM privacy_erasure_locks WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                if erasing is not None:
                    raise PrivacyErasureInProgress("privacy erasure in progress")

                replay = self._conn.execute(
                    """SELECT t.* FROM turn_requests r
                       JOIN turns t ON t.id=r.turn_id AND t.user_id=r.user_id
                       WHERE r.user_id=? AND r.request_id=?""",
                    (user_id, request_id),
                ).fetchone()
                if replay is not None:
                    self._conn.commit()
                    out = self._turn_dict(replay)
                    out["replayed"] = True
                    return out

                if cid:
                    exists = self._conn.execute(
                        "SELECT 1 FROM conversations WHERE user_id=? AND id=?",
                        (user_id, cid),
                    ).fetchone()
                    if not exists:
                        raise ConversationNotFound(cid)
                else:
                    cid = uuid.uuid4().hex
                    title = (create_title or "").strip() or "New chat"
                    self._conn.execute(
                        """INSERT INTO conversations
                           (user_id, id, title, created_at, updated_at,
                            parent_conversation_id, forked_from_turn_id,
                            root_conversation_id, branch_depth, context_schema_version,
                            agent_arch, agent_version, strict)
                           VALUES(?,?,?,?,?,NULL,NULL,?,0,1,?,?,?)""",
                        (
                            user_id,
                            cid,
                            title,
                            now,
                            now,
                            cid,
                            agent_arch,
                            agent_version,
                            1 if strict else 0,
                        ),
                    )
                    conversation_created = True

                lease = self._conn.execute(
                    """SELECT l.turn_id, l.expires_at, t.status
                       FROM conversation_turn_leases l
                       LEFT JOIN turns t ON t.id=l.turn_id
                       WHERE l.user_id=? AND l.conversation_id=?""",
                    (user_id, cid),
                ).fetchone()
                if lease is not None:
                    active = lease["status"] == "running" and lease["expires_at"] > now
                    if active:
                        remaining = (
                            datetime.datetime.fromisoformat(lease["expires_at"])
                            - datetime.datetime.fromisoformat(now)
                        ).total_seconds()
                        raise ConversationBusy(lease["turn_id"], max(1, int(remaining)))
                    self._conn.execute(
                        """UPDATE turns SET status='failed', completed_at=?
                           WHERE user_id=? AND id=? AND status='running'""",
                        (now, user_id, lease["turn_id"]),
                    )
                    self._conn.execute(
                        """DELETE FROM conversation_turn_leases
                           WHERE user_id=? AND conversation_id=?""",
                        (user_id, cid),
                    )

                cur = self._conn.execute(
                    """INSERT INTO messages
                       (user_id, conversation_id, role, content, response_type,
                        recommendations_json, timestamp, turn_id)
                       VALUES(?,?, 'user', ?, NULL, NULL, ?, ?)""",
                    (user_id, cid, user_content or "", now, tid),
                )
                user_message_id = int(cur.lastrowid)
                self._conn.execute(
                    """INSERT INTO turns
                       (id, user_id, conversation_id, request_id, user_message_id,
                        assistant_message_id, status, started_at, completed_at)
                       VALUES(?,?,?,?,?,NULL,'running',?,NULL)""",
                    (tid, user_id, cid, request_id, user_message_id, now),
                )
                self._conn.execute(
                    """INSERT INTO turn_requests
                       (user_id, request_id, conversation_id, turn_id, created_at)
                       VALUES(?,?,?,?,?)""",
                    (user_id, request_id, cid, tid, now),
                )
                self._conn.execute(
                    """INSERT INTO conversation_turn_leases
                       (user_id, conversation_id, turn_id, acquired_at, expires_at)
                       VALUES(?,?,?,?,?)""",
                    (user_id, cid, tid, now, expires),
                )
                self._conn.execute(
                    "UPDATE conversations SET updated_at=? WHERE user_id=? AND id=?",
                    (now, user_id, cid),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return {
            "id": tid,
            "user_id": user_id,
            "conversation_id": cid,
            "request_id": request_id,
            "user_message_id": user_message_id,
            "assistant_message_id": None,
            "status": "running",
            "started_at": now,
            "completed_at": None,
            "replayed": False,
            "conversation_created": conversation_created,
        }

    def begin_privacy_erasure(self, user_id: str) -> None:
        """Block new turns after proving no active conversation turn can write back."""
        now = _now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT 1 FROM privacy_erasure_locks WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                if existing:
                    raise PrivacyErasureInProgress("privacy erasure already in progress")

                leases = self._conn.execute(
                    """SELECT l.turn_id, l.expires_at, t.status
                       FROM conversation_turn_leases l
                       LEFT JOIN turns t ON t.id=l.turn_id
                       WHERE l.user_id=?""",
                    (user_id,),
                ).fetchall()
                for lease in leases:
                    if lease["status"] == "running" and lease["expires_at"] > now:
                        remaining = (
                            datetime.datetime.fromisoformat(lease["expires_at"])
                            - datetime.datetime.fromisoformat(now)
                        ).total_seconds()
                        raise ConversationBusy(lease["turn_id"], max(1, int(remaining)))
                    self._conn.execute(
                        """UPDATE turns SET status='failed', completed_at=?
                           WHERE user_id=? AND id=? AND status='running'""",
                        (now, user_id, lease["turn_id"]),
                    )
                running_job = self._conn.execute(
                    """SELECT id, lease_expires FROM background_jobs
                       WHERE user_id=? AND status='running'
                       ORDER BY id LIMIT 1""",
                    (user_id,),
                ).fetchone()
                if running_job is not None and (
                    running_job["lease_expires"] is None
                    or running_job["lease_expires"] > now
                ):
                    retry_after = 1
                    if running_job["lease_expires"]:
                        retry_after = max(
                            1,
                            int((
                                datetime.datetime.fromisoformat(running_job["lease_expires"])
                                - datetime.datetime.fromisoformat(now)
                            ).total_seconds()),
                        )
                    raise ConversationBusy(
                        f"background-job:{running_job['id']}", retry_after
                    )
                self._conn.execute(
                    """UPDATE background_jobs
                       SET status='pending', claimed_by=NULL, lease_expires=NULL,
                           updated_at=?
                       WHERE user_id=? AND status='running'""",
                    (now, user_id),
                )
                self._conn.execute(
                    "DELETE FROM conversation_turn_leases WHERE user_id=?",
                    (user_id,),
                )
                self._conn.execute(
                    "INSERT INTO privacy_erasure_locks(user_id, started_at) VALUES(?,?)",
                    (user_id, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def end_privacy_erasure(self, user_id: str) -> None:
        """Release the cross-store erasure barrier; safe and idempotent."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM privacy_erasure_locks WHERE user_id=?",
                (user_id,),
            )
            self._conn.commit()

    def get_request_turn(self, user_id: str, request_id: str) -> dict | None:
        """Return the turn previously claimed for a request id, if any."""
        with self._lock:
            row = self._conn.execute(
                """SELECT t.* FROM turn_requests r
                   JOIN turns t ON t.id=r.turn_id AND t.user_id=r.user_id
                   WHERE r.user_id=? AND r.request_id=?""",
                (user_id, request_id),
            ).fetchone()
        return self._turn_dict(row) if row else None

    def get_turn_response(self, user_id: str, turn_id: str) -> dict | None:
        """Rebuild the persisted HTTP payload for an idempotent completed retry."""
        with self._lock:
            row = self._conn.execute(
                """SELECT t.status, t.conversation_id, t.id AS turn_id,
                          m.content, m.response_type, m.recommendations_json
                   FROM turns t
                   LEFT JOIN messages m ON m.id=t.assistant_message_id
                   WHERE t.user_id=? AND t.id=?""",
                (user_id, turn_id),
            ).fetchone()
        if row is None or row["content"] is None:
            return None
        payload = {
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
            "response_type": row["response_type"] or (
                "error" if row["status"] == "failed" else "chat"
            ),
            "message": row["content"],
            "replayed": True,
        }
        if row["recommendations_json"]:
            try:
                payload["recommendations"] = json.loads(row["recommendations_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return payload

    def finalize_request_turn(
        self,
        user_id: str,
        turn_id: str,
        *,
        status: str,
        assistant_content: str,
        response_type: str | None = None,
        recommendations=None,
        snapshot: dict | None = None,
        snapshot_schema_version: int = 1,
        background_jobs: list[dict] | None = None,
    ) -> dict:
        """Atomically persist the assistant, terminal state, snapshot and outbox jobs."""
        if status not in {"completed", "failed"}:
            raise ValueError("status must be completed or failed")
        if status == "completed" and snapshot is None:
            raise ValueError("completed request turns require a snapshot")

        now = _now_iso()
        rec_json = (
            json.dumps(recommendations, ensure_ascii=False)
            if recommendations is not None
            else None
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                turn = self._conn.execute(
                    "SELECT * FROM turns WHERE user_id=? AND id=?",
                    (user_id, turn_id),
                ).fetchone()
                if turn is None:
                    raise TurnNotFound(turn_id)
                if turn["status"] != "running":
                    self._conn.commit()
                    return self.get_turn(user_id, turn_id)

                cur = self._conn.execute(
                    """INSERT INTO messages
                       (user_id, conversation_id, role, content, response_type,
                        recommendations_json, timestamp, turn_id)
                       VALUES(?,?, 'assistant', ?, ?, ?, ?, ?)""",
                    (
                        user_id,
                        turn["conversation_id"],
                        assistant_content or "",
                        response_type,
                        rec_json,
                        now,
                        turn_id,
                    ),
                )
                assistant_id = int(cur.lastrowid)
                updated = self._conn.execute(
                    """UPDATE turns SET status=?, completed_at=?, assistant_message_id=?
                       WHERE user_id=? AND id=? AND status='running'""",
                    (status, now, assistant_id, user_id, turn_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("turn lost its running state during finalize")

                if status == "completed":
                    payload = json.dumps(snapshot, ensure_ascii=False)
                    self._conn.execute(
                        """INSERT OR REPLACE INTO turn_snapshots
                           (turn_id, user_id, conversation_id, schema_version,
                            snapshot_json, created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (
                            turn_id,
                            user_id,
                            turn["conversation_id"],
                            int(snapshot_schema_version),
                            payload,
                            now,
                        ),
                    )
                    for job in background_jobs or []:
                        if not isinstance(job, dict):
                            raise ValueError("background job must be an object")
                        kind = str(job.get("kind") or "").strip()
                        if not kind:
                            raise ValueError("background job kind is required")
                        job_payload = job.get("payload") or {}
                        if not isinstance(job_payload, dict):
                            raise ValueError("background job payload must be an object")
                        dedupe_key = str(
                            job.get("dedupe_key") or f"turn:{turn_id}:{kind}"
                        )
                        self._conn.execute(
                            """INSERT OR IGNORE INTO background_jobs
                               (user_id, conversation_id, turn_id, kind, dedupe_key,
                                payload_json, result_json, status, attempts,
                                available_at, claimed_by, lease_expires, last_error,
                                created_at, updated_at)
                               VALUES(?,?,?,?,?,?,NULL,'pending',0,?,NULL,NULL,NULL,?,?)""",
                            (
                                user_id,
                                turn["conversation_id"],
                                turn_id,
                                kind,
                                dedupe_key,
                                json.dumps(job_payload, ensure_ascii=False),
                                now,
                                now,
                                now,
                            ),
                        )
                self._conn.execute(
                    "DELETE FROM conversation_turn_leases WHERE user_id=? AND turn_id=?",
                    (user_id, turn_id),
                )
                self._conn.execute(
                    "UPDATE conversations SET updated_at=? WHERE user_id=? AND id=?",
                    (now, user_id, turn["conversation_id"]),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_turn(user_id, turn_id)

    # -------------------------------------------------------- background outbox
    @staticmethod
    def _background_job_dict(row) -> dict | None:
        if row is None:
            return None
        job = dict(row)
        for key in ("payload_json", "result_json"):
            raw = job.pop(key, None)
            value = None
            if raw is not None:
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    value = None
            job["payload" if key == "payload_json" else "result"] = value
        return job

    def claim_background_job(
        self, worker_id: str, *, lease_seconds: int = 5 * 60
    ) -> dict | None:
        """Claim one ready job, serialising jobs within each conversation."""
        now = _now_iso()
        expires = (
            datetime.datetime.fromisoformat(now)
            + datetime.timedelta(seconds=max(1, int(lease_seconds)))
        ).isoformat()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """UPDATE background_jobs
                       SET status='pending', claimed_by=NULL, lease_expires=NULL,
                           last_error=COALESCE(last_error, 'worker lease expired'),
                           updated_at=?
                       WHERE status='running' AND lease_expires IS NOT NULL
                         AND lease_expires<=?""",
                    (now, now),
                )
                row = self._conn.execute(
                    """SELECT j.* FROM background_jobs j
                       WHERE j.status='pending' AND j.available_at<=?
                         AND NOT EXISTS (
                           SELECT 1 FROM privacy_erasure_locks p
                           WHERE p.user_id=j.user_id
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM background_jobs active
                           WHERE active.user_id=j.user_id
                             AND active.conversation_id=j.conversation_id
                             AND active.status='running'
                         )
                       ORDER BY j.id ASC LIMIT 1""",
                    (now,),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                updated = self._conn.execute(
                    """UPDATE background_jobs
                       SET status='running', attempts=attempts+1, claimed_by=?,
                           lease_expires=?, updated_at=?
                       WHERE id=? AND status='pending'""",
                    (worker_id, expires, now, row["id"]),
                )
                if updated.rowcount != 1:
                    self._conn.rollback()
                    return None
                claimed = self._conn.execute(
                    "SELECT * FROM background_jobs WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self._background_job_dict(claimed)

    def save_background_job_result(
        self, job_id: int, worker_id: str, result: dict
    ) -> None:
        payload = json.dumps(result, ensure_ascii=False)
        with self._lock:
            updated = self._conn.execute(
                """UPDATE background_jobs SET result_json=?, updated_at=?
                   WHERE id=? AND status='running' AND claimed_by=?""",
                (payload, _now_iso(), int(job_id), worker_id),
            )
            self._conn.commit()
        if updated.rowcount != 1:
            raise RuntimeError("background job lease was lost before saving result")

    def complete_background_job(self, job_id: int, worker_id: str) -> None:
        """Keep a content-free tombstone so the dedupe key remains durable."""
        with self._lock:
            updated = self._conn.execute(
                """UPDATE background_jobs
                   SET status='completed', payload_json='{}', result_json=NULL,
                       claimed_by=NULL, lease_expires=NULL, last_error=NULL, updated_at=?
                   WHERE id=? AND status='running' AND claimed_by=?""",
                (_now_iso(), int(job_id), worker_id),
            )
            self._conn.commit()
        if updated.rowcount != 1:
            raise RuntimeError("background job lease was lost before completion")

    def retry_background_job(
        self, job_id: int, worker_id: str, error: str, *, max_attempts: int = 5
    ) -> str:
        """Retry with bounded backoff, or quarantine as dead after max_attempts."""
        now = _now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM background_jobs WHERE id=? AND claimed_by=?",
                (int(job_id), worker_id),
            ).fetchone()
            if row is None:
                return "lost"
            attempts = int(row["attempts"] or 0)
            status = "dead" if attempts >= max(1, int(max_attempts)) else "pending"
            available = (
                datetime.datetime.fromisoformat(now)
                + datetime.timedelta(seconds=min(300, 2 ** max(0, attempts - 1)))
            ).isoformat()
            self._conn.execute(
                """UPDATE background_jobs
                   SET status=?, available_at=?, claimed_by=NULL, lease_expires=NULL,
                       last_error=?, updated_at=? WHERE id=? AND claimed_by=?""",
                (status, available, str(error)[:1000], now, int(job_id), worker_id),
            )
            self._conn.commit()
        return status

    def patch_latest_snapshot_summary(
        self, user_id: str, cid: str, summary: str, through_turn_id: str
    ) -> bool:
        """Idempotently persist an outbox-produced summary into the latest snapshot."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """SELECT s.turn_id, s.snapshot_json FROM turn_snapshots s
                       JOIN turns t ON t.id=s.turn_id AND t.user_id=s.user_id
                       WHERE s.user_id=? AND s.conversation_id=? AND t.status='completed'
                       ORDER BY t.started_at DESC LIMIT 1""",
                    (user_id, cid),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return False
                snapshot = json.loads(row["snapshot_json"])
                snapshot["summary"] = str(summary or "") or None
                snapshot["summary_through_turn_id"] = through_turn_id
                snapshot["context_revision"] = int(
                    snapshot.get("context_revision") or 0
                ) + 1
                self._conn.execute(
                    """UPDATE turn_snapshots SET snapshot_json=?, created_at=?
                       WHERE user_id=? AND turn_id=?""",
                    (
                        json.dumps(snapshot, ensure_ascii=False),
                        _now_iso(),
                        user_id,
                        row["turn_id"],
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return True

    def background_job_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM background_jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts

    def begin_turn(self, user_id: str, cid: str, request_id: str | None = None,
                   user_message_id: int | None = None) -> dict:
        """Open a 'running' turn. Returns the turn dict."""
        tid = uuid.uuid4().hex
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO turns
                   (id, user_id, conversation_id, request_id, user_message_id,
                    assistant_message_id, status, started_at, completed_at)
                   VALUES(?,?,?,?,?,NULL,'running',?,NULL)""",
                (tid, user_id, cid, request_id, user_message_id, now),
            )
            self._conn.commit()
        return {"id": tid, "user_id": user_id, "conversation_id": cid,
                "request_id": request_id, "user_message_id": user_message_id,
                "assistant_message_id": None, "status": "running",
                "started_at": now, "completed_at": None}

    def complete_turn(self, user_id: str, turn_id: str,
                      assistant_message_id: int | None = None) -> dict | None:
        """Mark a turn 'completed' (sets completed_at). Optionally record the
        assistant message id. Returns the updated turn dict, or None if not found."""
        now = _now_iso()
        with self._lock:
            if assistant_message_id is not None:
                cur = self._conn.execute(
                    """UPDATE turns SET status='completed', completed_at=?,
                              assistant_message_id=? WHERE user_id=? AND id=?""",
                    (now, assistant_message_id, user_id, turn_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE turns SET status='completed', completed_at=? WHERE user_id=? AND id=?",
                    (now, user_id, turn_id),
                )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            self._conn.execute(
                "DELETE FROM conversation_turn_leases WHERE user_id=? AND turn_id=?",
                (user_id, turn_id),
            )
            self._conn.commit()
        return self.get_turn(user_id, turn_id)

    def fail_turn(self, user_id: str, turn_id: str) -> None:
        """Mark a turn 'failed' (sets completed_at)."""
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE turns SET status='failed', completed_at=? WHERE user_id=? AND id=?",
                (now, user_id, turn_id),
            )
            self._conn.execute(
                "DELETE FROM conversation_turn_leases WHERE user_id=? AND turn_id=?",
                (user_id, turn_id),
            )
            self._conn.commit()

    def get_turn(self, user_id: str, turn_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE user_id=? AND id=?", (user_id, turn_id)
            ).fetchone()
        return self._turn_dict(row) if row else None

    def list_turns(self, user_id: str, cid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM turns WHERE user_id=? AND conversation_id=?
                   ORDER BY started_at ASC""",
                (user_id, cid),
            ).fetchall()
        return [self._turn_dict(r) for r in rows]

    def latest_completed_turn(self, user_id: str, cid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM turns WHERE user_id=? AND conversation_id=?
                   AND status='completed' ORDER BY started_at DESC LIMIT 1""",
                (user_id, cid),
            ).fetchone()
        return self._turn_dict(row) if row else None

    # ----------------------------------------------------------- turn snapshots
    def save_turn_snapshot(self, user_id: str, cid: str, turn_id: str,
                           snapshot: dict, schema_version: int = 1) -> None:
        """Store (or replace) a turn's context snapshot as JSON."""
        now = _now_iso()
        payload = json.dumps(snapshot, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO turn_snapshots
                   (turn_id, user_id, conversation_id, schema_version, snapshot_json, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (turn_id, user_id, cid, schema_version, payload, now),
            )
            self._conn.commit()

    def get_turn_snapshot(self, user_id: str, turn_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json FROM turn_snapshots WHERE user_id=? AND turn_id=?",
                (user_id, turn_id),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["snapshot_json"])
        except Exception:
            return None

    def latest_snapshot(self, user_id: str, cid: str) -> dict | None:
        """Snapshot of the latest COMPLETED turn that has one."""
        with self._lock:
            row = self._conn.execute(
                """SELECT s.snapshot_json FROM turn_snapshots s
                   JOIN turns t ON t.id = s.turn_id AND t.user_id = s.user_id
                   WHERE s.user_id=? AND s.conversation_id=? AND t.status='completed'
                   ORDER BY t.started_at DESC LIMIT 1""",
                (user_id, cid),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["snapshot_json"])
        except Exception:
            return None

    # ---------------------------------------------------------------- lineage
    def get_branch_lineage(self, user_id: str, cid: str) -> list[dict]:
        """Walk the parent chain (cycle-guarded, max depth 50). First entry is the
        conversation itself with before=None; each ancestor entry's cutoff is the
        started_at of the fork turn (inclusive). Missing fork turn → child's
        created_at fallback; missing parent row terminates the walk."""
        lineage: list[dict] = []
        current = cid
        before = None
        seen: set[str] = set()
        depth = 0
        while current and depth < 50:
            if current in seen:
                break  # cycle guard
            seen.add(current)
            with self._lock:
                row = self._conn.execute(
                    """SELECT parent_conversation_id, forked_from_turn_id, created_at
                       FROM conversations WHERE user_id=? AND id=?""",
                    (user_id, current),
                ).fetchone()
            if row is None:
                break  # missing conversation terminates the walk
            lineage.append({"conversation_id": current, "before": before})
            parent = row["parent_conversation_id"]
            if not parent:
                break
            fork_turn_id = row["forked_from_turn_id"]
            if not fork_turn_id:
                # A branch with a parent but NO fork turn is a deliberate zero-inheritance
                # branch (edit of the conversation's first turn): it inherits nothing, so the
                # walk stops here — no ancestor context is visible to it. (Ordinary
                # fork/edit branches always carry a fork turn, so this never fires for them.)
                break
            turn_row = None
            if fork_turn_id:
                with self._lock:
                    turn_row = self._conn.execute(
                        "SELECT started_at FROM turns WHERE user_id=? AND id=?",
                        (user_id, fork_turn_id),
                    ).fetchone()
            before = turn_row["started_at"] if turn_row else row["created_at"]
            current = parent
            depth += 1
        return lineage

    # ------------------------------------------------------------------- fork
    def fork_conversation(self, user_id: str, source_cid: str,
                          after_turn_id: str | None = None, title: str | None = None,
                          idempotency_key: str | None = None) -> dict:
        """Create a new conversation inheriting all context up to and including a chosen
        completed turn of the source. Entirely atomic (one transaction, rollback on
        error). See FORK_CONTRACT.md §1.2.

        Message inheritance is turn-membership based, NOT raw rowid <= cutoff: a fork
        never copies half a turn even when concurrent same-conversation requests
        interleave message rowids. A source message is copied iff it belongs to an
        inherited completed turn (started_at <= the fork turn's), OR it is a legacy
        pre-turns row (referenced by no turn) with id <= the fork turn's message id.
        Messages belonging to a running, failed, or completed-after-fork turn are
        excluded — in-flight and failed half-turns are not inheritable context by
        design (failed turns also produce no snapshot). Assistant rows carry the
        remapped copied turn id; user and legacy rows keep turn_id NULL, matching the
        live app's tagging."""
        with self._lock:
            # 1. Idempotency replay (race-safe: serialized by the lock, PK backstop).
            if idempotency_key:
                existing = self._conn.execute(
                    """SELECT conversation_id FROM fork_requests
                       WHERE user_id=? AND idempotency_key=?""",
                    (user_id, idempotency_key),
                ).fetchone()
                if existing:
                    child = self.get_conversation(user_id, existing["conversation_id"])
                    if child is not None:
                        child = dict(child)
                        child["idempotent"] = True
                        return child
                    # Recorded child was deleted → stale key, fall through and re-create.

            # 2. Validate the source conversation.
            src = self._conn.execute(
                """SELECT id, title, root_conversation_id, branch_depth,
                          agent_arch, agent_version, strict
                   FROM conversations WHERE user_id=? AND id=?""",
                (user_id, source_cid),
            ).fetchone()
            if src is None:
                raise ConversationNotFound(source_cid)

            # 3. Resolve the fork turn.
            if after_turn_id is None:
                fork_turn = self._conn.execute(
                    """SELECT * FROM turns WHERE user_id=? AND conversation_id=?
                       AND status='completed' ORDER BY started_at DESC LIMIT 1""",
                    (user_id, source_cid),
                ).fetchone()
                if fork_turn is None:
                    raise NoCompletedTurn(source_cid)
            else:
                fork_turn = self._conn.execute(
                    "SELECT * FROM turns WHERE user_id=? AND id=?",
                    (user_id, after_turn_id),
                ).fetchone()
                if fork_turn is None:
                    raise TurnNotFound(after_turn_id)
                if fork_turn["conversation_id"] != source_cid:
                    raise TurnNotInConversation(after_turn_id)
                if fork_turn["status"] != "completed":
                    raise TurnNotCompleted(after_turn_id)

            fork_turn_id = fork_turn["id"]
            fork_started_at = fork_turn["started_at"]

            try:
                self._conn.execute("BEGIN")

                # 4. Create the child conversation row.
                child_cid = uuid.uuid4().hex
                now = _now_iso()
                child_title = (title or "").strip() or f"{src['title']} (branch)"
                root = src["root_conversation_id"] or source_cid
                depth = int(src["branch_depth"] or 0) + 1
                # A branch starts with the source conversation's architecture provenance;
                # the serving pool reconciles it after a later cutover if necessary.
                self._conn.execute(
                    """INSERT INTO conversations
                       (user_id, id, title, created_at, updated_at,
                        parent_conversation_id, forked_from_turn_id,
                        root_conversation_id, branch_depth, context_schema_version,
                        agent_arch, agent_version, strict)
                       VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                    (user_id, child_cid, child_title, now, now,
                     source_cid, fork_turn_id, root, depth,
                     src["agent_arch"], src["agent_version"], src["strict"]),
                )

                # 5-7. Copy the inherited COMPLETED turns (started_at <= fork turn's), their
                #      messages (whole turns only) and snapshots into the child. Legacy no-turn
                #      rows are bounded by the fork turn's message id so we never grab a row
                #      after the fork point.
                src_turns = self._conn.execute(
                    """SELECT * FROM turns WHERE user_id=? AND conversation_id=?
                       AND status='completed' AND started_at<=? ORDER BY started_at ASC""",
                    (user_id, source_cid, fork_started_at),
                ).fetchall()
                cutoff_msg_id = fork_turn["assistant_message_id"]
                if cutoff_msg_id is None:
                    cutoff_msg_id = fork_turn["user_message_id"]
                self._materialize_branch(user_id, source_cid, child_cid, src_turns,
                                         cutoff_msg_id)

                # 8. Record the idempotency key (overwrites a stale row).
                if idempotency_key:
                    self._conn.execute(
                        """INSERT OR REPLACE INTO fork_requests
                           (user_id, idempotency_key, conversation_id, created_at)
                           VALUES(?,?,?,?)""",
                        (user_id, idempotency_key, child_cid, _now_iso()),
                    )

                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        # 9. Return the child conversation dict.
        child = self.get_conversation(user_id, child_cid)
        child = dict(child)
        child["idempotent"] = False
        child["forked_from_turn_id"] = fork_turn_id
        return child

    def _materialize_branch(self, user_id: str, source_cid: str, child_cid: str,
                            src_turns: list, cutoff_msg_id) -> None:
        """Copy inherited turns/messages/snapshots from ``source_cid`` into the ALREADY-created
        child conversation row ``child_cid``. MUST run inside the caller's lock and open
        transaction (it neither BEGINs nor commits). Shared by fork_conversation and
        branch_for_edit — the ONLY difference between those two is which ``src_turns`` are
        inherited and the legacy ``cutoff_msg_id``; the copy semantics are identical.

        ``src_turns`` are the source completed-turn rows to inherit (whole turns only, each
        assigned a fresh child turn id). Message membership is derived from the TURNS table,
        not messages.turn_id, so it is robust whether or not the live app tagged user rows:
        a source message is copied iff it belongs to an inherited turn, or it is a legacy
        (no-turn) row with id <= cutoff_msg_id. Messages owned by a non-inherited turn
        (running / failed / after the cut) are excluded. Copied rows carry the remapped
        turn id when they had one, else NULL."""
        turn_map: dict[str, str] = {t["id"]: uuid.uuid4().hex for t in src_turns}
        copied_turn_ids = set(turn_map)

        copied_turn_msg_ids: set[int] = set()
        for t in src_turns:
            for col in ("user_message_id", "assistant_message_id"):
                if t[col] is not None:
                    copied_turn_msg_ids.add(t[col])
        # Any message referenced by a NON-inherited turn is an in-flight / failed / excluded
        # half-turn → never copied even if its rowid falls under the legacy cutoff.
        other_turn_msg_ids: set[int] = set()
        for t in self._conn.execute(
            """SELECT id, user_message_id, assistant_message_id FROM turns
               WHERE user_id=? AND conversation_id=?""",
            (user_id, source_cid),
        ).fetchall():
            if t["id"] in copied_turn_ids:
                continue
            for col in ("user_message_id", "assistant_message_id"):
                if t[col] is not None:
                    other_turn_msg_ids.add(t[col])

        msg_map: dict[int, int] = {}
        src_msgs = self._conn.execute(
            """SELECT id, role, content, response_type, recommendations_json,
                      timestamp, turn_id
               FROM messages WHERE user_id=? AND conversation_id=? ORDER BY id ASC""",
            (user_id, source_cid),
        ).fetchall()
        for m in src_msgs:
            mid = m["id"]
            mtid = m["turn_id"]
            if mid in copied_turn_msg_ids or (mtid and mtid in copied_turn_ids):
                pass  # belongs to an inherited turn → copy
            elif mtid is not None:
                continue  # tagged to a non-inherited turn → exclude
            elif mid in other_turn_msg_ids:
                continue  # untagged row owned by an in-flight / excluded turn
            elif cutoff_msg_id is not None and mid <= cutoff_msg_id:
                pass  # legacy no-turn prefix → copy
            else:
                continue
            new_turn = turn_map.get(m["turn_id"]) if m["turn_id"] else None
            cur = self._conn.execute(
                """INSERT INTO messages
                   (user_id, conversation_id, role, content, response_type,
                    recommendations_json, timestamp, turn_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (user_id, child_cid, m["role"], m["content"],
                 m["response_type"], m["recommendations_json"], m["timestamp"],
                 new_turn),
            )
            msg_map[mid] = cur.lastrowid

        for t in src_turns:
            new_umid = msg_map.get(t["user_message_id"]) if t["user_message_id"] is not None else None
            new_amid = msg_map.get(t["assistant_message_id"]) if t["assistant_message_id"] is not None else None
            self._conn.execute(
                """INSERT INTO turns
                   (id, user_id, conversation_id, request_id, user_message_id,
                    assistant_message_id, status, started_at, completed_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (turn_map[t["id"]], user_id, child_cid, t["request_id"], new_umid,
                 new_amid, t["status"], t["started_at"], t["completed_at"]),
            )

        for old_tid, new_tid in turn_map.items():
            snap = self._conn.execute(
                """SELECT schema_version, snapshot_json FROM turn_snapshots
                   WHERE user_id=? AND turn_id=?""",
                (user_id, old_tid),
            ).fetchone()
            if snap is None:
                continue
            snapshot_json = snap["snapshot_json"]
            try:
                parsed = json.loads(snapshot_json)
                if isinstance(parsed, dict) and "turn_id" in parsed:
                    parsed["turn_id"] = new_tid
                    snapshot_json = json.dumps(parsed, ensure_ascii=False)
            except Exception:
                pass  # store verbatim if unparseable
            self._conn.execute(
                """INSERT OR REPLACE INTO turn_snapshots
                   (turn_id, user_id, conversation_id, schema_version,
                    snapshot_json, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (new_tid, user_id, child_cid, snap["schema_version"],
                 snapshot_json, _now_iso()),
            )

    # -------------------------------------------------------------- edit / branch
    def _resolve_slot_anchor(self, user_id: str, source_cid: str, src_row,
                             edited_turn_id: str) -> str:
        """Family-stable version-group slot key for editing ``edited_turn_id`` in
        ``source_cid`` (see :meth:`version_map` and :meth:`branch_for_edit`).

        A slot is a logical message position across a branch family. The key is the turn_id
        of the edited turn AS IT LIVES IN THE CONVERSATION WHERE THE SLOT ORIGINATED. To keep
        repeated edits of the *same* position in one group (transitivity), when the edited
        turn is the source branch's OWN first fresh turn — the very slot the source branch was
        created to fill — and the source is itself an edit branch, we reuse the source's slot
        key instead of minting a new one. Any other edit (a later turn, or the first edit off
        a non-edit conversation) originates a new slot keyed by the edited turn's own id.

        Turn ids are regenerated when a branch copies turns, so a slot key deliberately points
        at a turn in a *specific* conversation (the origin), never a copied turn."""
        boundary = ""
        fork_turn_id = src_row["forked_from_turn_id"]
        if fork_turn_id:
            r = self._conn.execute(
                "SELECT started_at FROM turns WHERE user_id=? AND id=?",
                (user_id, fork_turn_id),
            ).fetchone()
            if r is not None:
                boundary = r["started_at"]
        first_fresh = self._conn.execute(
            """SELECT id FROM turns WHERE user_id=? AND conversation_id=? AND started_at>?
               ORDER BY started_at ASC LIMIT 1""",
            (user_id, source_cid, boundary),
        ).fetchone()
        src_anchor = src_row["edited_slot_turn_id"]
        if src_anchor and first_fresh is not None and first_fresh["id"] == edited_turn_id:
            return src_anchor
        return edited_turn_id

    def branch_for_edit(self, user_id: str, source_cid: str, turn_id: str,
                        title: str | None = None,
                        idempotency_key: str | None = None) -> dict:
        """Create a NEW branch that inherits everything STRICTLY BEFORE ``turn_id`` so the
        caller can re-send a rewritten version of that turn's user message (ChatGPT-style
        edit-and-resend). The source conversation is never modified. Entirely atomic (one
        transaction, rollback on error). This endpoint only builds the branch; the caller
        drives the rewritten message through the normal chat path afterwards.

        Contrast with :meth:`fork_conversation`, which inherits up to and INCLUDING a chosen
        completed turn:
          * The edited turn itself is NEVER inherited, and its status is irrelevant — a
            running, failed, or completed turn are all valid edit targets (we only ever
            inherit COMPLETED turns that STARTED before it, so a concurrent in-flight turn or
            the edited turn's own failed attempt are naturally excluded).
          * When ``turn_id`` is the conversation's first turn there is nothing before it, so
            the branch inherits ZERO turns (a "zero-inheritance branch"). Lineage is still
            recorded (parent / root / branch_depth), and forked_from_turn_id is left NULL,
            which get_branch_lineage reads as "inherits no ancestor context".

        Version-group metadata is recorded on the child: ``fork_reason='edit'`` and
        ``edited_slot_turn_id`` (the family-stable slot key from :meth:`_resolve_slot_anchor`).

        Raises ConversationNotFound (source missing / not owned), TurnNotFound (unknown
        turn_id), TurnNotInConversation (turn belongs to another conversation). Returns the
        child conversation dict with an extra ``idempotent`` flag."""
        with self._lock:
            # 1. Idempotency replay (shares the fork_requests table; serialized by the lock).
            if idempotency_key:
                existing = self._conn.execute(
                    """SELECT conversation_id FROM fork_requests
                       WHERE user_id=? AND idempotency_key=?""",
                    (user_id, idempotency_key),
                ).fetchone()
                if existing:
                    child = self.get_conversation(user_id, existing["conversation_id"])
                    if child is not None:
                        child = dict(child)
                        child["idempotent"] = True
                        return child
                    # Recorded child was deleted → stale key, fall through and re-create.

            # 2. Validate source + the edited turn (completion status is NOT required).
            src = self._conn.execute(
                """SELECT id, title, root_conversation_id, branch_depth,
                          forked_from_turn_id, edited_slot_turn_id,
                          agent_arch, agent_version, strict
                   FROM conversations WHERE user_id=? AND id=?""",
                (user_id, source_cid),
            ).fetchone()
            if src is None:
                raise ConversationNotFound(source_cid)
            edited = self._conn.execute(
                "SELECT * FROM turns WHERE user_id=? AND id=?", (user_id, turn_id),
            ).fetchone()
            if edited is None:
                raise TurnNotFound(turn_id)
            if edited["conversation_id"] != source_cid:
                raise TurnNotInConversation(turn_id)

            edited_started_at = edited["started_at"]

            # 3. Inherited = COMPLETED turns that STARTED before the edited turn (exclusive).
            src_turns = self._conn.execute(
                """SELECT * FROM turns WHERE user_id=? AND conversation_id=?
                   AND status='completed' AND started_at<? ORDER BY started_at ASC""",
                (user_id, source_cid, edited_started_at),
            ).fetchall()

            # Fork point = last inherited turn (inclusive cutoff in get_branch_lineage);
            # None → zero-inheritance branch.
            forked_from = src_turns[-1]["id"] if src_turns else None

            # Legacy (no-turn) rows are bounded to just before the edited turn's first message.
            edited_msg_ids = [x for x in (edited["user_message_id"],
                                          edited["assistant_message_id"]) if x is not None]
            if edited_msg_ids:
                cutoff_msg_id = min(edited_msg_ids) - 1
            elif src_turns:
                inh = [x for t in src_turns
                       for x in (t["user_message_id"], t["assistant_message_id"])
                       if x is not None]
                cutoff_msg_id = max(inh) if inh else None
            else:
                cutoff_msg_id = None

            anchor = self._resolve_slot_anchor(user_id, source_cid, src, turn_id)

            try:
                self._conn.execute("BEGIN")
                child_cid = uuid.uuid4().hex
                now = _now_iso()
                child_title = (title or "").strip() or f"{src['title']} (edit)"
                root = src["root_conversation_id"] or source_cid
                depth = int(src["branch_depth"] or 0) + 1
                # Edit branches inherit the source's architecture provenance.
                self._conn.execute(
                    """INSERT INTO conversations
                       (user_id, id, title, created_at, updated_at,
                        parent_conversation_id, forked_from_turn_id, root_conversation_id,
                        branch_depth, context_schema_version, fork_reason, edited_slot_turn_id,
                        agent_arch, agent_version, strict)
                       VALUES(?,?,?,?,?,?,?,?,?,1,'edit',?,?,?,?)""",
                    (user_id, child_cid, child_title, now, now,
                     source_cid, forked_from, root, depth, anchor,
                     src["agent_arch"], src["agent_version"], src["strict"]),
                )
                self._materialize_branch(user_id, source_cid, child_cid, src_turns,
                                         cutoff_msg_id)
                if idempotency_key:
                    self._conn.execute(
                        """INSERT OR REPLACE INTO fork_requests
                           (user_id, idempotency_key, conversation_id, created_at)
                           VALUES(?,?,?,?)""",
                        (user_id, idempotency_key, child_cid, _now_iso()),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        child = dict(self.get_conversation(user_id, child_cid))
        child["idempotent"] = False
        return child

    def version_map(self, user_id: str, cid: str) -> dict | None:
        """Version-group map for the whole branch FAMILY (same root_conversation_id) that
        ``cid`` belongs to. Returns ``None`` when ``cid`` is not owned by ``user_id`` (the
        route maps that to 404).

        Shape::

            {"version_groups": {"<slot_turn_id>": [
                {"conversation_id", "created_at", "title"},  # created_at ASC
                ...]}}

        A group is the set of alternative versions of one logical user-message slot. Its
        members are every edit branch tagged with that slot key PLUS each such branch's parent
        (the "original, un-edited continuation" the branch diverged from). Because a slot key
        is family-stable (see :meth:`_resolve_slot_anchor`), editing the same position on an
        edit branch lands in the SAME group as the previous edit (transitivity): the branch
        and its parent chain are all pulled in.

        Only groups with >=2 members are emitted — a single version has nothing to switch
        between. Every emitted group therefore has >=2 members by construction (an edit branch
        always contributes at least itself + its parent). No edits anywhere in the family →
        ``{"version_groups": {}}``."""
        with self._lock:
            base = self._conn.execute(
                "SELECT root_conversation_id FROM conversations WHERE user_id=? AND id=?",
                (user_id, cid),
            ).fetchone()
            if base is None:
                return None
            root = base["root_conversation_id"] or cid
            fam = self._conn.execute(
                """SELECT id, created_at, title, parent_conversation_id, edited_slot_turn_id
                   FROM conversations WHERE user_id=? AND root_conversation_id=?""",
                (user_id, root),
            ).fetchall()
        by_id = {r["id"]: r for r in fam}
        groups: dict[str, set] = {}
        for r in fam:
            slot = r["edited_slot_turn_id"]
            if not slot:
                continue
            members = groups.setdefault(slot, set())
            members.add(r["id"])
            parent = r["parent_conversation_id"]
            if parent and parent in by_id:
                members.add(parent)
        out: dict[str, list] = {}
        for slot, ids in groups.items():
            if len(ids) < 2:
                continue
            rows = sorted((by_id[i] for i in ids),
                          key=lambda r: (r["created_at"], r["id"]))
            out[slot] = [{"conversation_id": r["id"], "created_at": r["created_at"],
                          "title": r["title"]} for r in rows]
        return {"version_groups": out}

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
            "parent_conversation_id": row["parent_conversation_id"],
            "forked_from_turn_id": row["forked_from_turn_id"],
            "root_conversation_id": row["root_conversation_id"],
            "branch_depth": row["branch_depth"],
            "fork_reason": row["fork_reason"],
            "edited_slot_turn_id": row["edited_slot_turn_id"],
            # Architecture provenance used by rollout telemetry and reconciliation.
            "agent_arch": row["agent_arch"],
            "agent_version": row["agent_version"],
            "strict": bool(row["strict"]),
        }

    @staticmethod
    def _turn_dict(row) -> dict:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "conversation_id": row["conversation_id"],
            "request_id": row["request_id"],
            "user_message_id": row["user_message_id"],
            "assistant_message_id": row["assistant_message_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }
