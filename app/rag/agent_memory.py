"""
AgentMemory — a layered, LLM-managed long-term memory for the rental assistant.

Synthesis of the 2024-2026 agent-memory SOTA:
  - Generative Agents (Park 2023): retrieval score = relevance + recency + importance,
    each min-max normalised to [0,1]; recency = 0.995^hours_since_last_access;
    importance = an LLM 1-10 poignancy rating cached at write time; relevance = embedding
    cosine. Plus periodic REFLECTION that distils higher-level insights from recent memories.
  - Mem0 (2025): write path = an LLM extracts atomic facts, then an LLM decides
    ADD / UPDATE / DELETE / NOOP for each against the semantically-nearest existing
    memories (dedup + conflict resolution), with an MD5 exact-dup guard.
  - Letta / LangMem: memory is exposed as TOOLS (recall / remember) so the agent and any
    sub-agent/tool can read & write it; namespaced by user_id/session_id so memory can be
    shared or isolated across agents.

Memory types (CoALA taxonomy):
  - episodic   : individual interaction/event records (what the user asked, tools used)
  - semantic   : distilled durable facts about the user (budget, destination, constraints)
  - reflection : higher-level insights synthesised from episodic memories (semantic subtype)
  (working/short-term memory stays the agent's context window — not stored here.)

Storage: ChromaDB (persistent, cosine) — one collection, mtype in metadata. The same
on-disk store is shared by the web process and the MCP tool subprocess, so memory written
in one is visible to the other. LLM calls go through core.llm_interface.call_ollama, which
(with LLM_PROVIDER=deepseek) uses the DeepSeek API.
"""
import os
import re
import json
import time
import hashlib
import datetime
import threading
import tempfile

import chromadb

from core.llm_interface import call_ollama

# ---- tunables (Generative Agents) -------------------------------------------
RECENCY_DECAY = 0.995            # per-hour exponential decay of recency
RETRIEVE_CANDIDATES = 25         # vector top-K fetched before GA re-ranking
REFLECT_IMPORTANCE_THRESHOLD = 30  # accrued importance that triggers a reflection
                                   # (paper uses 150 over game-days; scaled for short chats)
REFLECT_CORPUS_SIZE = 30         # newest-by-created_at records fed into a reflection

# Per-user cap on auto-logged episodic records. remember_turn appends one every
# turn forever, so without a cap the episodic layer grows unbounded. Enforced
# opportunistically on write: keep the newest N per user by created_at, delete the
# overflow. Semantic + reflection layers are deliberately uncapped (they are small,
# deduped/consolidated, and represent durable distilled knowledge).
EPISODIC_MAX_PER_USER = 500

# Static importance assigned to auto-episodic turn logs so remember_turn does NOT
# burn an LLM call rating a raw query log every turn. Explicit remember() writes
# (importance=None) still get an LLM poignancy rating.
_AUTO_EPISODIC_IMPORTANCE = 5

# Persist dir is configurable via RAG_DB_ROOT (a root directory). Default resolves
# to app/chroma_db_agent_memory so existing on-disk user data keeps working.
# AGENT_MEMORY_DB_PATH remains an absolute-path override (highest precedence) for
# back-compat with any caller that set it. run_benchmark rebinds am._DB_PATH
# directly, so this module-level name must persist.
_DEFAULT_DB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # app/
_DB_ROOT = os.getenv("RAG_DB_ROOT", _DEFAULT_DB_ROOT)
_DB_PATH = os.getenv("AGENT_MEMORY_DB_PATH", os.path.join(_DB_ROOT, "chroma_db_agent_memory"))
_DEFAULT_IMPORTANCE = {"semantic": 7, "reflection": 8, "episodic": 5}

# Bilingual triviality gate for the fact-extraction path. A very short message, a
# pure greeting/ack, or a bypass command carries nothing durable to remember, so we
# skip the (expensive) LLM extract+consolidate for it. Anything plausibly
# informational still goes through extraction.
_TRIVIAL_MIN_LEN = 15
_GREETING_PATTERNS = {
    "hi", "hii", "hey", "hello", "helo", "yo", "hiya", "sup",
    "thanks", "thank you", "thankyou", "thx", "ty", "cheers",
    "ok", "okay", "k", "kk", "okey", "cool", "nice", "great", "sure",
    "yes", "yeah", "yep", "no", "nope", "nah", "good", "fine",
    "bye", "goodbye", "gg", "np",
    "你好", "您好", "哈喽", "嗨", "谢谢", "多谢", "感谢", "好", "好的",
    "好吧", "行", "行吧", "嗯", "对", "是", "是的", "不", "不是", "没有",
    "可以", "收到", "了解", "明白", "再见", "拜拜", "棒", "赞",
}
_BYPASS_PATTERNS = {
    "search anyway", "just search", "search now", "go ahead", "proceed",
    "先不要搜索", "不要搜索", "先别搜索", "别搜索", "直接搜索", "现在搜索", "继续搜索",
}


def _normalize_trivial(text: str) -> str:
    """Lowercase, strip surrounding whitespace and trailing punctuation/emoji-ish
    filler so 'Hi!!!' / '好的。' collapse onto the pattern list."""
    t = (text or "").strip().lower()
    return t.strip(" .!?,~。！？，、…").strip()


def _is_trivial_for_extraction(user_msg: str) -> bool:
    """True when the user message is not worth running fact extraction on."""
    raw = (user_msg or "").strip()
    if not raw:
        return True
    norm = _normalize_trivial(raw)
    if not norm:
        return True
    if norm in _GREETING_PATTERNS or norm in _BYPASS_PATTERNS:
        return True
    # Bypass commands may carry trailing words ("search anyway please").
    for phrase in _BYPASS_PATTERNS:
        if norm.startswith(phrase) or norm == phrase:
            return True
    # Short messages with no CJK are almost always acks/greetings. Keep short CJK
    # messages (a few Chinese characters can be fully informational, e.g. a place
    # name or budget) unless they matched a pattern above.
    has_cjk = any("一" <= ch <= "鿿" for ch in norm)
    if not has_cjk and len(norm) < _TRIVIAL_MIN_LEN:
        return True
    return False


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _hours_since(iso_ts: str) -> float:
    try:
        then = datetime.datetime.fromisoformat(iso_ts)
        return max(0.0, (datetime.datetime.now() - then).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def _minmax(values):
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _valid_user_id(user_id) -> str | None:
    """Strict identity gate for every memory READ and WRITE path.

    Memory is namespaced by user_id. Returns the normalised id, or None when the
    caller failed to supply a real identity — in which case the operation must
    fail CLOSED (no write, empty retrieval), never fall back to a shared bucket.

    'default' is explicitly rejected: it was the implicit shared namespace that
    every call site missing a user_id silently read from / wrote to, so one
    user's preferences (budget, university, ...) leaked to brand-new users.
    (forget() deliberately does NOT use this gate, so the legacy 'default'
    bucket stays erasable via /api/forget_me.)
    """
    if not isinstance(user_id, str):
        return None
    uid = user_id.strip()
    if not uid or uid.lower() == "default":
        return None
    return uid


def _classify_pii(text: str) -> str:
    """Conservative metadata tag; content stays local and remains erasable by user_id."""
    value = (text or "").casefold()
    categories = []
    if re.search(r"\b£?\d{3,5}\s*(?:pcm|per month|budget)?\b", value):
        categories.append("financial_preference")
    if re.search(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", text or "", re.IGNORECASE):
        categories.append("location")
    if any(term in value for term in ("partner", "child", "family", "flatmate", "roommate")):
        categories.append("household")
    return ",".join(categories) or "none"


class AgentMemory:
    def __init__(self, db_path: str = _DB_PATH):
        self.client = chromadb.PersistentClient(path=db_path)
        self.col = self.client.get_or_create_collection(
            name="agent_memory", metadata={"hnsw:space": "cosine"},
        )
        self._lock = threading.Lock()
        self._accum = {}  # (user_id, session_id) -> accrued importance (reflection gate)

    # ------------------------------------------------------------------ writing
    def add(self, text, mtype, session_id="default", user_id=None,
            role="", importance=None, idempotency_key=None,
            extra_meta=None) -> str | None:
        user_id = _valid_user_id(user_id)
        if user_id is None:
            print("[memory] add rejected: missing/shared user_id (memory must be per-user)")
            return None
        text = (text or "").strip()
        if not text:
            return None
        h = hashlib.md5(f"{mtype}|{user_id}|{text}".encode("utf-8")).hexdigest()
        with self._lock:
            if idempotency_key:
                try:
                    # user-scoped: an idempotency-key collision must never surface
                    # (or suppress writes against) another user's records.
                    previous = self.col.get(where={"$and": [
                        {"idempotency_key": idempotency_key}, {"user_id": user_id}]})
                    if previous and previous.get("ids"):
                        return previous["ids"][0]
                except Exception:
                    pass
            try:
                existing = self.col.get(where={"$and": [{"hash": h}, {"user_id": user_id}]})
                if existing and existing.get("ids"):
                    return existing["ids"][0]
            except Exception:
                pass
            if importance is None:
                importance = (self._rate_importance(text) if mtype == "episodic"
                              else _DEFAULT_IMPORTANCE.get(mtype, 5))
            mem_id = f"{mtype}_{h[:10]}_{int(time.time()*1000)}"
            meta = {
                "mtype": mtype, "session_id": session_id, "user_id": user_id,
                "role": role, "importance": int(importance),
                "created_at": _now_iso(), "last_access": _now_iso(), "hash": h,
                "pii_categories": _classify_pii(text),
            }
            if idempotency_key:
                meta["idempotency_key"] = idempotency_key
            # Optional provenance (e.g. branch scoping). Chroma metadata values must
            # be str/int/float/bool — never None — so absent keys are simply omitted.
            if extra_meta:
                for k, v in extra_meta.items():
                    if v is None:
                        continue
                    meta[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
            try:
                self.col.add(documents=[text], metadatas=[meta], ids=[mem_id])
            except Exception as e:
                print(f"[memory] add error: {e}")
                return None
            key = (user_id, session_id)
            self._accum[key] = self._accum.get(key, 0) + int(importance)
            return mem_id

    def _rate_importance(self, text) -> int:
        prompt = (
            "On the scale of 1 to 10, where 1 is purely mundane small-talk and 10 is extremely "
            "significant for understanding this person's housing search (a hard budget, a fixed "
            "destination/university, a strict requirement, a major life constraint), rate the "
            "poignancy of the following memory. Respond with ONLY a single integer.\n\n"
            f"Memory: {text}\nRating: "
        )
        try:
            resp = call_ollama(prompt, timeout=30) or ""
            m = re.search(r"\b(10|[1-9])\b", resp)
            return int(m.group(1)) if m else 5
        except Exception:
            return 5

    def _extract_facts(self, user_msg, assistant_msg) -> list:
        prompt = (
            "You extract durable facts about a user from a rental-assistant conversation. "
            'Return STRICT JSON: {"facts": ["..."]}. Each fact is an atomic, self-contained '
            "statement about the user's preferences, constraints, or situation worth remembering "
            "for future turns (budget, destination/university, commute limit, must-have amenities, "
            "areas to avoid, lifestyle, who they live with, etc.). Do NOT include greetings, the "
            'assistant\'s suggestions, or ephemeral chit-chat. If nothing is worth remembering, return {"facts": []}.\n\n'
            f"User: {user_msg}\nAssistant: {assistant_msg}\n\nJSON: "
        )
        try:
            data = _extract_json(call_ollama(prompt, timeout=60) or "")
            facts = data.get("facts", []) if isinstance(data, dict) else []
            return [f.strip() for f in facts if isinstance(f, str) and f.strip()][:8]
        except Exception:
            return []

    def _consolidate(self, facts, session_id, user_id):
        if not facts:
            return
        existing, seen = [], set()
        try:
            res = self.col.query(
                query_texts=facts, n_results=4,
                where={"$and": [{"user_id": user_id}, {"mtype": "semantic"}]},
            )
            for docs, ids in zip(res.get("documents") or [], res.get("ids") or []):
                for d, i in zip(docs or [], ids or []):
                    if i not in seen:
                        seen.add(i)
                        existing.append({"id": i, "text": d})
        except Exception:
            pass
        prompt = (
            "You manage a user-profile memory. Given NEW candidate facts and the EXISTING memories, "
            'decide the operations. Return STRICT JSON: {"ops": [{"event": "ADD|UPDATE|DELETE|NOOP", '
            '"id": "<existing id or null>", "text": "<final memory text>"}]}. '
            "ADD a genuinely new fact; UPDATE (keep id) when a fact refines/replaces an existing one; "
            "DELETE (give id) when a new fact contradicts an existing one; NOOP if already known.\n\n"
            f"EXISTING:\n{json.dumps(existing, ensure_ascii=False)}\n\n"
            f"NEW FACTS:\n{json.dumps(facts, ensure_ascii=False)}\n\nJSON: "
        )
        try:
            data = _extract_json(call_ollama(prompt, timeout=60) or "") or {}
            for op in data.get("ops", []):
                ev = (op.get("event") or "").upper()
                text = (op.get("text") or "").strip()
                oid = op.get("id")
                if ev == "ADD" and text:
                    self.add(text, "semantic", session_id, user_id, importance=7)
                elif ev == "UPDATE" and oid and text:
                    with self._lock:
                        try:
                            self.col.update(ids=[oid], documents=[text])
                        except Exception:
                            self.add(text, "semantic", session_id, user_id, importance=7)
                elif ev == "DELETE" and oid:
                    with self._lock:
                        try:
                            self.col.delete(ids=[oid])
                        except Exception:
                            pass
        except Exception as e:
            print(f"[memory] consolidate error: {e}")

    def remember_turn(self, user_msg, assistant_msg, session_id="default",
                      user_id=None, tool_used=None, idempotency_key=None,
                      conversation_id=None, turn_id=None, turn_started_at=None):
        """After-turn entry point (run this in the background — it makes LLM calls).

        Branch scoping: conversation_id / turn_id / turn_started_at are recorded on
        the auto-EPISODIC turn log only, so retrieve() can restrict a forked
        conversation to the episodic memories it actually inherited. They are NOT
        attached to the distilled semantic/reflection records, which stay GLOBAL
        (per-user, all branches). Absent (None) values are omitted from metadata —
        chroma rejects None — so legacy call sites keep behaving exactly as before.
        """
        user_id = _valid_user_id(user_id)
        if user_id is None:
            print("[memory] remember_turn rejected: missing/shared user_id")
            return
        try:
            if idempotency_key:
                existing = self.col.get(where={"$and": [
                    {"idempotency_key": idempotency_key}, {"user_id": user_id}]})
                if existing and existing.get("ids"):
                    return
            ep = f"User asked: {(user_msg or '').strip()[:300]}"
            if tool_used:
                ep += f"  [assistant used: {tool_used}]"
            # Branch provenance for the episodic layer only (semantic/reflection global).
            episodic_meta = {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "turn_started_at": turn_started_at,
            }
            # Static importance for the auto-episodic turn log: never rate a raw
            # query log with an LLM call (see _AUTO_EPISODIC_IMPORTANCE).
            self.add(
                ep, "episodic", session_id, user_id, role="user",
                importance=_AUTO_EPISODIC_IMPORTANCE,
                idempotency_key=idempotency_key,
                extra_meta=episodic_meta,
            )
            # Opportunistically bound the per-user episodic layer.
            self._enforce_episodic_cap(user_id)
            # Triviality gate: skip the LLM extract+consolidate for greetings/acks/
            # bypass commands and very short messages — they carry nothing durable.
            if not _is_trivial_for_extraction(user_msg):
                self._consolidate(self._extract_facts(user_msg, assistant_msg), session_id, user_id)
            self.maybe_reflect(session_id, user_id)
        except Exception as e:
            print(f"[memory] remember_turn error: {e}")

    def _enforce_episodic_cap(self, user_id, cap: int = EPISODIC_MAX_PER_USER):
        """Keep only the newest ``cap`` episodic records for one user (by created_at);
        delete the overflow. No-op below the cap. Semantic/reflection are untouched."""
        try:
            with self._lock:
                existing = self.col.get(
                    where={"$and": [{"user_id": user_id}, {"mtype": "episodic"}]}
                )
                ids = list(existing.get("ids") or [])
                if len(ids) <= cap:
                    return
                metas = existing.get("metadatas") or []
                # newest first by created_at; blank created_at sorts oldest.
                order = sorted(
                    range(len(ids)),
                    key=lambda i: (metas[i] or {}).get("created_at", ""),
                    reverse=True,
                )
                overflow = [ids[i] for i in order[cap:]]
                if overflow:
                    self.col.delete(ids=overflow)
        except Exception as e:
            print(f"[memory] episodic cap error: {e}")

    def remember_turn_async(self, *args, **kwargs):
        threading.Thread(target=self.remember_turn, args=args, kwargs=kwargs, daemon=True).start()

    def maybe_reflect(self, session_id, user_id):
        user_id = _valid_user_id(user_id)
        if user_id is None:
            return
        key = (user_id, session_id)
        if self._accum.get(key, 0) < REFLECT_IMPORTANCE_THRESHOLD:
            return
        self._accum[key] = 0
        try:
            recent = self.col.get(where={"user_id": user_id})
            docs = recent.get("documents", []) or []
            metas = recent.get("metadatas", []) or []
            if len(docs) < 4:
                return
            # Chroma .get() has NO ordering guarantee, so "recent" must be derived
            # explicitly: sort by created_at and take the newest REFLECT_CORPUS_SIZE.
            order = sorted(
                range(len(docs)),
                key=lambda i: (metas[i] or {}).get("created_at", "") if i < len(metas) else "",
            )
            newest = order[-REFLECT_CORPUS_SIZE:]
            corpus = "\n".join(f"- {docs[i]}" for i in newest)
            prompt = (
                "Given the following memories about a user's housing search, synthesise 1-3 concise, "
                "higher-level insights about what this user really wants or needs (patterns, priorities, "
                'trade-offs). Return STRICT JSON: {"insights": ["..."]}.\n\n'
                f"{corpus}\n\nJSON: "
            )
            data = _extract_json(call_ollama(prompt, timeout=60) or "") or {}
            for ins in (data.get("insights") or [])[:3]:
                if isinstance(ins, str) and ins.strip():
                    self.add(ins.strip(), "reflection", session_id, user_id, importance=8)
        except Exception as e:
            print(f"[memory] reflect error: {e}")

    # ---------------------------------------------------------------- retrieval
    @staticmethod
    def _visible_in_lineage(meta, branch_lineage) -> bool:
        """Branch-scoped visibility for a single candidate memory.

        semantic/reflection (and any non-episodic) records are GLOBAL → always
        visible. Episodic records are branch-scoped:
          * WITH conversation_id metadata → visible iff some lineage entry matches
            that conversation_id AND the memory was written at/before that entry's
            cutoff (entry.before is None → no cutoff; a memory with no
            turn_started_at is treated as inherited);
          * WITHOUT conversation_id (legacy rows written before branch scoping) →
            visible (documented back-compat).
        """
        meta = meta or {}
        if meta.get("mtype") != "episodic":
            return True
        cid = meta.get("conversation_id")
        if not cid:
            return True
        started = meta.get("turn_started_at")
        for entry in branch_lineage or []:
            if not isinstance(entry, dict) or entry.get("conversation_id") != cid:
                continue
            before = entry.get("before")
            if before is None or not started or str(started) <= str(before):
                return True
        return False

    def retrieve(self, query, session_id="default", user_id=None, n=6,
                 branch_lineage=None) -> list:
        """Generative-Agents scored retrieval: relevance + recency + importance.

        STRICT per-user isolation: the where-filter is always applied with a real
        user_id. A missing/blank/'default' id fails CLOSED (returns []) — there is
        no global fallback, and records written without a user_id can never match.

        Branch scoping: when ``branch_lineage`` is provided (the store's
        get_branch_lineage output — a list of {"conversation_id", "before"}),
        candidates are post-filtered in Python BEFORE GA scoring so a forked
        conversation only sees the episodic memories it inherited (semantic /
        reflection stay global). ``branch_lineage=None`` → behaviour unchanged.
        """
        user_id = _valid_user_id(user_id)
        if user_id is None:
            return []
        query = (query or "").strip()
        if not query:
            return []
        try:
            res = self.col.query(query_texts=[query], n_results=RETRIEVE_CANDIDATES,
                                 where={"user_id": user_id})
        except Exception:
            return []
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        if not docs:
            return []
        # Branch-scoped post-filter (before scoring). None → global behaviour.
        if branch_lineage is not None:
            keep = [i for i in range(len(docs))
                    if self._visible_in_lineage(metas[i], branch_lineage)]
            docs = [docs[i] for i in keep]
            metas = [metas[i] for i in keep]
            ids = [ids[i] for i in keep]
            dists = [dists[i] for i in keep]
            if not docs:
                return []
        relevance = [1.0 - float(d) for d in dists]
        recency = [RECENCY_DECAY ** _hours_since(m.get("last_access") or m.get("created_at") or _now_iso())
                   for m in metas]
        importance = [int(m.get("importance", 5)) / 10.0 for m in metas]
        nr, nrec, nimp = _minmax(relevance), _minmax(recency), _minmax(importance)
        scored = [(nr[i] + nrec[i] + nimp[i], ids[i], docs[i], metas[i]) for i in range(len(docs))]
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:n]
        self._touch([t[1] for t in top])
        return [{"id": t[1], "text": t[2], "mtype": t[3].get("mtype"),
                 "importance": t[3].get("importance"), "score": round(t[0], 3)} for t in top]

    def _touch(self, ids):
        if not ids:
            return
        try:
            with self._lock:
                cur = self.col.get(ids=ids)
                new_metas = []
                for m in cur.get("metadatas", []) or []:
                    m = dict(m or {})
                    m["last_access"] = _now_iso()
                    new_metas.append(m)
                if new_metas:
                    self.col.update(ids=cur.get("ids", ids), metadatas=new_metas)
        except Exception:
            pass

    def format_for_prompt(self, mems) -> str:
        if not mems:
            return ""
        tag = {"semantic": "FACT", "reflection": "INSIGHT", "episodic": "PAST"}
        lines = [f"[{tag.get(m.get('mtype'), 'MEM')}] {m['text']}" for m in mems]
        return "What I remember about this user:\n" + "\n".join(lines)

    def stats(self) -> dict:
        try:
            return {"total": self.col.count()}
        except Exception:
            return {"total": 0}

    def forget(self, user_id: str) -> int:
        """Delete all memory for one user (UK GDPR erasure boundary)."""
        user_id = (user_id or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        with self._lock:
            existing = self.col.get(where={"user_id": user_id})
            ids = list(existing.get("ids") or [])
            if ids:
                self.col.delete(ids=ids)
            for key in [key for key in self._accum if key[0] == user_id]:
                self._accum.pop(key, None)
        return len(ids)


_AGENT_MEMORY = None


def get_agent_memory() -> AgentMemory:
    global _AGENT_MEMORY
    if _AGENT_MEMORY is None:
        try:
            _AGENT_MEMORY = AgentMemory()
        except Exception as exc:
            fallback_path = os.path.join(tempfile.gettempdir(), "uk-rent-agent", "agent_memory")
            print(
                f"[memory] store {_DB_PATH} is not writable ({exc}); "
                f"using {fallback_path}"
            )
            _AGENT_MEMORY = AgentMemory(fallback_path)
    return _AGENT_MEMORY
