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
    def create_conversation(self, user_id: str, title: str | None = None) -> dict:
        cid = uuid.uuid4().hex
        now = _now_iso()
        title = (title or "").strip() or "New chat"
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversations
                   (user_id, id, title, created_at, updated_at,
                    parent_conversation_id, forked_from_turn_id,
                    root_conversation_id, branch_depth, context_schema_version)
                   VALUES(?,?,?,?,?,NULL,NULL,?,0,1)""",
                (user_id, cid, title, now, now, cid),
            )
            self._conn.commit()
        return {"id": cid, "title": title, "created_at": now,
                "updated_at": now, "message_count": 0,
                "parent_conversation_id": None, "forked_from_turn_id": None,
                "root_conversation_id": cid, "branch_depth": 0}

    def get_conversation(self, user_id: str, cid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, title, created_at, updated_at,
                          parent_conversation_id, forked_from_turn_id,
                          root_conversation_id, branch_depth,
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
            # Resetting the transcript also drops its turns + snapshots.
            self._conn.execute(
                "DELETE FROM turn_snapshots WHERE user_id=? AND conversation_id=?", (user_id, cid)
            )
            self._conn.execute(
                "DELETE FROM turns WHERE user_id=? AND conversation_id=?", (user_id, cid)
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
        return self.get_turn(user_id, turn_id)

    def fail_turn(self, user_id: str, turn_id: str) -> None:
        """Mark a turn 'failed' (sets completed_at)."""
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE turns SET status='failed', completed_at=? WHERE user_id=? AND id=?",
                (now, user_id, turn_id),
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
                """SELECT id, title, root_conversation_id, branch_depth
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
                self._conn.execute(
                    """INSERT INTO conversations
                       (user_id, id, title, created_at, updated_at,
                        parent_conversation_id, forked_from_turn_id,
                        root_conversation_id, branch_depth, context_schema_version)
                       VALUES(?,?,?,?,?,?,?,?,?,1)""",
                    (user_id, child_cid, child_title, now, now,
                     source_cid, fork_turn_id, root, depth),
                )

                # 5. Determine which COMPLETED turns are inherited (started_at <= fork
                #    turn's) and assign each a fresh child turn id up front.
                src_turns = self._conn.execute(
                    """SELECT * FROM turns WHERE user_id=? AND conversation_id=?
                       AND status='completed' AND started_at<=? ORDER BY started_at ASC""",
                    (user_id, source_cid, fork_started_at),
                ).fetchall()
                turn_map: dict[str, str] = {t["id"]: uuid.uuid4().hex for t in src_turns}
                copied_turn_ids = set(turn_map)

                # Message membership is derived from the TURNS table, NOT messages.turn_id
                # (the live app tags only assistant rows; user rows keep turn_id NULL).
                copied_turn_msg_ids: set[int] = set()
                for t in src_turns:
                    for col in ("user_message_id", "assistant_message_id"):
                        if t[col] is not None:
                            copied_turn_msg_ids.add(t[col])
                # Any message referenced by a NON-inherited turn (running / failed /
                # completed-after-fork) is an in-flight or failed half-turn → excluded.
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

                # Legacy (pre-turns) rows are bounded by the fork turn's message id so we
                # never grab a no-turn row that lives after the fork point.
                cutoff_msg_id = fork_turn["assistant_message_id"]
                if cutoff_msg_id is None:
                    cutoff_msg_id = fork_turn["user_message_id"]

                # 6. Copy messages by turn membership (whole turns only, never half a
                #    turn). Copied-turn messages are copied in full regardless of rowid;
                #    no-turn rows up to the cutoff are copied as legacy; everything else
                #    (other-turn rows, post-fork no-turn rows) is excluded.
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
                        continue  # tagged to a non-inherited turn (e.g. failed-turn
                                  # error row) → exclude
                    elif mid in other_turn_msg_ids:
                        continue  # untagged row owned by an in-flight/failed turn
                    elif cutoff_msg_id is not None and mid <= cutoff_msg_id:
                        pass  # legacy no-turn prefix → copy with NULL turn_id
                    else:
                        continue
                    # Assistant rows carry the (remapped) copied turn id; user & legacy
                    # rows keep turn_id NULL, matching the live app's tagging.
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

                # Copy the inherited turns, remapping message ids to the copied rows.
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

                # 7. Copy turn snapshots for every copied turn (rewrite embedded turn_id).
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
