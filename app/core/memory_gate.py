"""Memory-write gate for taint policy A+ (design §2.8c).

The loop executor codes against this module. It provides:

  * ``user_authorizes_memory`` — does the CURRENT user message explicitly ask to
    remember something (zh+en)? An explicit request is authorization: the current
    user message is untainted by construction, so a model-initiated ``remember``
    may go through directly, no extra confirmation (A+ rule 2). Recall questions
    ("do you remember ...", "还记得吗") are NOT authorization.
  * ``content_is_user_stated`` — the refinement of rule 2 (H13). The direct-allow
    only holds when the content being saved IS what the user stated — the
    "current message is untainted" reasoning does NOT cover a scraped price the
    user never typed. This deterministic, conservative check returns True only when
    the candidate content is substantially derivable from the current user message.
  * ``write_authorization`` — the convenience the loop MUST use: authorization =
    ``user_authorizes_memory(msg) AND content_is_user_stated(content, msg)``. Tool-
    derived content under an authorization cue therefore fails authorization and
    still routes through rule 3 (ask_user shows the exact content) + rule 4 (frozen
    replay), instead of slipping through the direct-allow.
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


# -------------------------------------------------- content-is-user-stated (rule 2 refinement)

# Function words carry no distinguishing information; a user's phrasing and a saved
# fact both contain them, so their (mis)match tells us nothing about provenance.
# "user"/"users" are the framing tokens saved facts are conventionally written in
# ("user prefers ...") and never appear in the user's own message — treat as framing.
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "am",
    "to", "of", "in", "on", "at", "for", "and", "or", "but", "with", "as", "so",
    "my", "our", "i", "we", "you", "your", "me", "us", "it", "this", "that",
    "these", "those", "there", "here", "have", "has", "had", "do", "does", "did",
    "want", "wants", "need", "needs", "would", "will", "can", "could", "like",
    "please", "remember", "note", "save", "keep", "mind", "dont", "forget", "not",
    "user", "users", "prefer", "prefers", "preferred", "preference", "preferences",
    "likes", "liked", "about", "per",
})
# Domain-generic terms echoed by any budget/price fact. Excluding them from the
# "distinguishing" set is what lets a cross-language number-anchored write
# ("记住我预算1400" → "budget £1400/month") read as user-stated, while a specific
# tool-derived phrase ("cheapest flat on Camden Road") does not — the phrase's
# distinguishing nouns remain and are absent from the user's message.
_GENERIC_TERMS = frozenset({
    "budget", "budgets", "rent", "rental", "price", "priced", "cost", "costs",
    "month", "monthly", "months", "week", "weekly", "weeks", "year", "yearly",
    "pcm", "pm", "pw", "pounds", "pound", "gbp", "max", "maximum", "min",
    "minimum", "limit", "around", "approx", "approximately", "roughly",
})
_SHARE_THRESHOLD = 0.5  # a strict majority of distinguishing tokens must be present

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LATIN_RE = re.compile(r"[a-z][a-z']*")
_CJK_RE = re.compile(r"[㐀-䶿一-鿿]+")


def _numbers(text: str) -> set[str]:
    """Numbers in ``text``, currency-symbol/thousands-comma normalised (£1,400 → 1400)."""
    out: set[str] = set()
    for token in _NUM_RE.findall(text or ""):
        norm = token.replace(",", "")
        if norm:
            out.add(norm)
    return out


def _latin_tokens(text: str) -> set[str]:
    """Casefolded latin word tokens (apostrophes stripped) — for a presence check."""
    return {t.replace("'", "") for t in _LATIN_RE.findall((text or "").casefold())
            if t.replace("'", "")}


def _significant_latin(text: str) -> set[str]:
    """Latin tokens minus stopwords and domain-generic terms — the distinguishing ones."""
    return {t for t in _latin_tokens(text)
            if t not in _STOPWORDS and t not in _GENERIC_TERMS}


def _cjk_bigrams(text: str) -> set[str]:
    """CJK character bigrams (a lone CJK char contributes itself) — the CJK analogue of
    distinguishing tokens; bigrams are specific enough not to need a stopword list."""
    out: set[str] = set()
    for run in _CJK_RE.findall(text or ""):
        if len(run) == 1:
            out.add(run)
        else:
            for i in range(len(run) - 1):
                out.add(run[i:i + 2])
    return out


def content_is_user_stated(content: str, current_message: str) -> bool:
    """True when ``content`` is substantially derivable from ``current_message``.

    Deterministic and conservative — on doubt it returns False so the write is denied
    and routed through ask_user + frozen replay (which is safe), rather than allowed.

    Heuristic (both sides casefolded; numbers stripped of currency symbols/commas):

      1. Every NUMBER in the content must appear in the user message. A tool-derived
         figure the user never typed (H13: a scraped "cheapest" price) fails here.
      2. A strict-majority share (>= 0.5) of the content's DISTINGUISHING tokens must
         appear in the message — latin tokens minus stopwords/domain-generic terms,
         plus CJK bigrams. When the content has no distinguishing tokens (e.g.
         "budget £1400/month" — all generic), a matched number is the sole anchor and
         suffices; with neither a number nor a distinguishing token there is nothing
         to anchor on, so it fails.

    Empty/whitespace content → False.
    """
    c = (content or "").strip()
    if not c:
        return False
    msg = current_message or ""

    content_nums = _numbers(c)
    if content_nums and not content_nums <= _numbers(msg):
        return False

    c_latin = _significant_latin(c)
    c_bigrams = _cjk_bigrams(c)
    significant = c_latin | c_bigrams
    if not significant:
        # No distinguishing text; a matched number is the only anchor we have.
        return bool(content_nums)

    msg_latin = _latin_tokens(msg)
    msg_bigrams = _cjk_bigrams(msg)
    present = sum(1 for t in c_latin if t in msg_latin)
    present += sum(1 for b in c_bigrams if b in msg_bigrams)
    return (present / len(significant)) >= _SHARE_THRESHOLD


def write_authorization(current_message: str, content: str) -> bool:
    """A+ rule-2 authorization the loop MUST use for a model-initiated ``remember``.

    Authorization requires BOTH an explicit cue in the current user message AND that
    the candidate content is substantially the user's own statement. This closes H13:
    a 「记住」 cue alone no longer green-lights saving tool-derived content.
    """
    return user_authorizes_memory(current_message) and content_is_user_stated(
        content, current_message)


def memory_write_allowed(*, context_tainted: bool, user_authorized: bool) -> bool:
    """A+ truth table: only a tainted AND unauthorized write is denied.

    ``user_authorized`` is the CALLER's responsibility and MUST be computed via
    ``write_authorization`` (cue AND user-stated content), not ``user_authorizes_memory``
    alone — see the module docstring and ``content_is_user_stated``.
    """
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
