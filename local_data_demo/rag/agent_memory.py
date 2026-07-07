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

import chromadb

from core.llm_interface import call_ollama

# ---- tunables (Generative Agents) -------------------------------------------
RECENCY_DECAY = 0.995            # per-hour exponential decay of recency
RETRIEVE_CANDIDATES = 25         # vector top-K fetched before GA re-ranking
REFLECT_IMPORTANCE_THRESHOLD = 30  # accrued importance that triggers a reflection
                                   # (paper uses 150 over game-days; scaled for short chats)

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # local_data_demo/
    "chroma_db_agent_memory",
)
_DEFAULT_IMPORTANCE = {"semantic": 7, "reflection": 8, "episodic": 5}


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
    def add(self, text, mtype, session_id="default", user_id="default",
            role="", importance=None, idempotency_key=None) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        h = hashlib.md5(f"{mtype}|{user_id}|{text}".encode("utf-8")).hexdigest()
        with self._lock:
            if idempotency_key:
                try:
                    previous = self.col.get(where={"idempotency_key": idempotency_key})
                    if previous and previous.get("ids"):
                        return previous["ids"][0]
                except Exception:
                    pass
            try:
                existing = self.col.get(where={"hash": h})
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
                      user_id="default", tool_used=None, idempotency_key=None):
        """After-turn entry point (run this in the background — it makes LLM calls)."""
        try:
            if idempotency_key:
                existing = self.col.get(where={"idempotency_key": idempotency_key})
                if existing and existing.get("ids"):
                    return
            ep = f"User asked: {(user_msg or '').strip()[:300]}"
            if tool_used:
                ep += f"  [assistant used: {tool_used}]"
            self.add(
                ep, "episodic", session_id, user_id, role="user",
                idempotency_key=idempotency_key,
            )
            self._consolidate(self._extract_facts(user_msg, assistant_msg), session_id, user_id)
            self.maybe_reflect(session_id, user_id)
        except Exception as e:
            print(f"[memory] remember_turn error: {e}")

    def remember_turn_async(self, *args, **kwargs):
        threading.Thread(target=self.remember_turn, args=args, kwargs=kwargs, daemon=True).start()

    def maybe_reflect(self, session_id, user_id):
        key = (user_id, session_id)
        if self._accum.get(key, 0) < REFLECT_IMPORTANCE_THRESHOLD:
            return
        self._accum[key] = 0
        try:
            recent = self.col.get(where={"user_id": user_id})
            docs = recent.get("documents", []) or []
            if len(docs) < 4:
                return
            corpus = "\n".join(f"- {d}" for d in docs[-30:])
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
    def retrieve(self, query, session_id="default", user_id="default", n=6) -> list:
        """Generative-Agents scored retrieval: relevance + recency + importance."""
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
        _AGENT_MEMORY = AgentMemory()
    return _AGENT_MEMORY
