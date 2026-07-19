"""Memory-write gate for taint policy A+ (design §2.8c).

The loop executor codes against this module. It provides four things:

  * ``user_authorizes_memory`` — does the CURRENT user message explicitly ask to
    remember something (zh+en)? An explicit request is authorization: the current
    user message is untainted by construction, so a model-initiated ``remember``
    may go through directly, no extra confirmation (A+ rule 2). Recall questions
    ("do you remember ...", "还记得吗") are NOT authorization.
  * ``memory_write_allowed`` — the A+ truth table: a tainted, unauthorized write is
    denied; everything else is allowed.
  * ``freeze_pending_write`` / ``consume_pending_write`` — durable, per-session,
    single-consumption ledger for the frozen candidate. On a deny the executor
    freezes the exact content (returns its sha256 digest); after the user confirms,
    only that frozen candidate is replayed, exactly once (digest check + atomic
    single consumption), so the model cannot swap content after confirmation
    (A+ rule 4).
  * ``pending_confirmation_message`` — the bilingual "should I remember: X?" text
    the deny response shows the user (A+ rule 3).

Storage mirrors the app's existing sqlite conventions (see
``uk_rent_agent.tools.idempotency.IdempotencyStore`` and the ``.runtime`` cache
files): a tiny table under ``.runtime/`` with a process lock and atomic
DELETE-based single consumption.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path


# --------------------------------------------------------------------- authorization

# Unambiguous "please save this" cues. Matching one of these (and no recall-question
# veto) means the current user message authorizes a memory write.
_AUTHORIZE_EN = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bplease\s+remember\b",
        r"\bremember\s+(?:that|this|to|my|our|i\b|we\b|the\s+following)",
        r"\bnote\s+that\b",
        r"\bmake\s+a\s+note\b",
        r"\btake\s+note\b",
        r"\bsave\s+this\b",
        r"\bkeep\s+in\s+mind\b",
        r"\bdon'?t\s+forget\b",
    )
)
_AUTHORIZE_ZH = (
    "记住", "记一下", "记下", "记录一下", "帮我记", "帮我记住", "记录下",
    "存一下", "保存这个", "保存一下", "别忘了", "别忘记", "不要忘记", "标记一下",
)

# Recall QUESTIONS — the user is asking what we already know, NOT asking to save.
# These veto authorization even when a positive substring incidentally matches
# (e.g. "do you remember my budget").
_RECALL_VETO_EN = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:do|does|did|can|could|would|will)\s+you\s+remember\b",
        r"\byou\s+remember\b",
        r"\bremember\s+(?:when|what|who|where|why|how|the\s+time|that\s+time|if)\b",
    )
)
_RECALL_VETO_ZH = ("记得吗", "记不记得", "还记得", "是否记得", "你记得")


def user_authorizes_memory(current_message: str) -> bool:
    """True when the CURRENT user message explicitly asks us to remember something.

    zh + en. Recall questions ("do you remember ...", "还记得吗") return False.
    """
    text = (current_message or "").strip()
    if not text:
        return False
    if any(pat.search(text) for pat in _RECALL_VETO_EN):
        return False
    if any(phrase in text for phrase in _RECALL_VETO_ZH):
        return False
    if any(pat.search(text) for pat in _AUTHORIZE_EN):
        return True
    if any(phrase in text for phrase in _AUTHORIZE_ZH):
        return True
    return False


def memory_write_allowed(*, context_tainted: bool, user_authorized: bool) -> bool:
    """A+ truth table: only a tainted AND unauthorized write is denied."""
    if not context_tainted:
        return True
    return user_authorized


# Bare confirmations/declines for the turn AFTER an ask_user memory confirmation.
# Deliberately conservative: these are only consulted when a pending frozen candidate
# exists for the session, so an unrelated "好的" cannot trigger a write by itself.
_CONFIRM_YES_ZH = ("好的", "好", "是的", "是", "可以", "嗯", "行", "确认", "记吧", "保存吧", "存吧", "要")
_CONFIRM_YES_EN = ("yes", "yep", "yeah", "ok", "okay", "sure", "confirm", "please do",
                   "go ahead", "do it", "save it", "remember it")
_CONFIRM_NO_ZH = ("不用", "不要", "别", "算了", "不必", "取消", "先不要", "不")
_CONFIRM_NO_EN = ("no", "nope", "don't", "do not", "cancel", "never mind", "not now")


def confirmation_intent(current_message: str) -> str:
    """Classify a (short) reply to the pending-memory confirmation: 'yes' | 'no' | 'none'.

    Only meaningful when the caller has verified a pending candidate exists. Long
    messages that merely contain a cue are 'none' — a new substantive message should
    flow through the normal loop, not silently consume the frozen candidate.
    """
    text = (current_message or "").strip().lower().rstrip("。.!！~")
    if not text or len(text) > 20:
        return "none"
    for cue in _CONFIRM_NO_ZH + _CONFIRM_NO_EN:
        if text == cue or text.startswith(cue):
            return "no"
    for cue in _CONFIRM_YES_ZH + _CONFIRM_YES_EN:
        if text == cue or text.startswith(cue):
            return "yes"
    return "none"


# ---------------------------------------------------------------- confirmation text

def pending_confirmation_message(content: str, lang: str) -> str:
    """Bilingual "should I remember: <content>?" text for the deny response."""
    body = (content or "").strip()
    if (lang or "").lower().startswith("zh"):
        return f"我要为你记住这条信息吗：{body}？请回复确认。"
    return f"Should I remember this: {body}? Please confirm."


# ------------------------------------------------------------------- pending ledger

def _default_db_path() -> Path:
    override = os.getenv("MEMORY_GATE_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / ".runtime" / "memory_gate.sqlite3"


class _PendingWriteStore:
    """Durable, single-consumption ledger of frozen memory-write candidates."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ensure_schema(self.path)

    def _ensure_schema(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=10) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending_memory_writes ("
                "session_id TEXT NOT NULL, digest TEXT NOT NULL, content TEXT NOT NULL, "
                "kind TEXT NOT NULL, created REAL NOT NULL, "
                "PRIMARY KEY(session_id, digest))"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def freeze(self, session_id: str, content: str, kind: str) -> str:
        digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO pending_memory_writes"
                "(session_id, digest, content, kind, created) VALUES (?, ?, ?, ?, ?)",
                (session_id, digest, content, kind, time.time()),
            )
        return digest

    def latest_digest(self, session_id: str) -> str | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT digest FROM pending_memory_writes WHERE session_id=? "
                "ORDER BY created DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return row[0] if row else None

    def consume(self, session_id: str, digest: str) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT content, kind FROM pending_memory_writes "
                "WHERE session_id=? AND digest=?",
                (session_id, digest),
            ).fetchone()
            if not row:
                return None
            # Atomic single-consumption: the DELETE rowcount is the gate. A racing
            # consumer that deleted first leaves rowcount 0 here → None, so the
            # frozen candidate is replayed exactly once.
            cursor = db.execute(
                "DELETE FROM pending_memory_writes WHERE session_id=? AND digest=?",
                (session_id, digest),
            )
            if cursor.rowcount != 1:
                return None
            return {"content": row[0], "kind": row[1]}


_STORE: _PendingWriteStore | None = None
_STORE_LOCK = threading.Lock()


def _store() -> _PendingWriteStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                path = _default_db_path()
                try:
                    _STORE = _PendingWriteStore(path)
                except (OSError, sqlite3.Error) as exc:
                    fallback = Path(tempfile.gettempdir()) / "uk-rent-agent" / "memory_gate.sqlite3"
                    print(f"[memory_gate] store {path} not writable ({exc}); using {fallback}")
                    _STORE = _PendingWriteStore(fallback)
    return _STORE


def freeze_pending_write(session_id: str, content: str, kind: str) -> str:
    """Freeze a denied memory-write candidate; return its sha256 content digest."""
    return _store().freeze(session_id, content, kind)


def consume_pending_write(session_id: str, digest: str) -> dict | None:
    """Replay a frozen candidate exactly once.

    Returns ``{"content":.., "kind":..}`` on an exact (session_id, digest) match,
    else ``None`` (absent, digest mismatch, wrong session, or already consumed).
    """
    return _store().consume(session_id, digest)


def latest_pending_digest(session_id: str) -> str | None:
    """Digest of the most recently frozen candidate for this session, if any.

    The confirmation-replay consumer needs it because the digest is never trusted
    from the model — it lives only in this ledger between the deny turn and the
    user's confirmation turn.
    """
    return _store().latest_digest(session_id)
