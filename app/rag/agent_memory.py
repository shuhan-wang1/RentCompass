"""
AgentMemory — a layered, LLM-managed long-term memory for the rental assistant.

Synthesis of the 2024-2026 agent-memory SOTA:
  - Generative Agents (Park 2023): retrieval score = relevance + recency + importance,
    each min-max normalised to [0,1]; recency = 0.995^hours_since_last_access;
    importance = an LLM 1-10 poignancy rating cached at write time; relevance =
    deterministic multilingual lexical cosine. Plus periodic REFLECTION that distils
    higher-level insights from recent memories.
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

Storage: process-safe SQLite — one collection-compatible table, mtype in metadata. The
same on-disk store is shared by the web process and the MCP tool subprocess, so memory
written in one is visible to the other. A legacy Chroma database in the directory is
read and copied without importing Chroma; tombstones prevent deleted legacy records from
being re-imported during a rolling deployment. LLM calls go through
core.llm_interface.call_ollama, which (with LLM_PROVIDER=deepseek) uses the DeepSeek API.
"""
import os
import re
import json
import time
import hashlib
import datetime
import threading
import tempfile

from core.llm_interface import call_ollama
from rag.sqlite_memory_store import SQLiteMemoryCollection

# ---- tunables (Generative Agents) -------------------------------------------
RECENCY_DECAY = 0.995            # per-hour exponential decay of recency
RETRIEVE_CANDIDATES = 25         # lexical-cosine top-K fetched before GA re-ranking
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
    if not has_cjk and len(norm) < _TRIVIAL_MIN_LEN and not _FIRST_PERSON_STATEMENT.match(norm):
        return True
    return False


# A short first-person statement is durable, not an acknowledgement: "I have a dog" (12
# characters) and "I am vegan" fell under the length gate and were never extracted (R3-02).
_FIRST_PERSON_STATEMENT = re.compile(
    r"^(?:i\s+(?:have|am|own|work|study|live|need|use|can't|cannot|don't|do\s+not|prefer|hate|love|"
    r"smoke|keep)|i'm|i've|im\s|my\s|we\s+(?:have|are|own)|we're)\b", re.I)


# A message that asks not to be remembered (r07 optout1: "Please do not store or remember
# anything about me in this chat — no memory at all. …", "… Please do not store that."). The
# reply promised nothing would be kept, while the turn log kept the message and the extractor was
# free to store facts from it. Such a turn is logged without its content and nothing is extracted.
_STORE_VERB = (r"(?:store|save|remember|memori[sz]e|record|log|"
               r"(?:write|note|jot)\s+(?:(?:it|this|that)\s+)?down|"
               r"keep\s+(?:this|that|it|any|anything|everything|a\s+(?:record|note|log)|records?|"
               r"notes?|track|my\s+\w+|(?:any(?:thing)?\s+)?(?:about|of)\s+me))")
_DO_NOT_REMEMBER = re.compile(
    r"(?:^|[.,;:!?—–\n]\s*|\b(?:please|and|but|so|just|also|then)\s+)(?:please\s+)?"
    r"(?:do\s+not|don'?t|dont|never|no\s+need\s+to)\s+(?:(?:ever|even|actually)\s+)?"
    + _STORE_VERB + r"\b|"
    # "no memory please", "use/keep no memory of this"; never "I have no memory of that street".
    r"\b(?:keep|store|save|retain|use|want|leave|hold)\s+no\s+memory\b|"
    r"(?<!\bhave\s)(?<!\bhad\s)(?<!'ve\s)(?<!\bhas\s)(?<!\bgot\s)\bno\s+memory\b(?!\s+of\b)|"
    r"\boff\s+the\s+record\b|"
    r"\bnot\s+(?:to\s+)?(?:be\s+)?(?:stored|saved|remembered|recorded)\b(?!\s*\])|"
    r"(?:请不要|不要|别|不用|请勿|不准|不许)(?:帮我|给我)?(?:记住|记下|记录|保存|存储|存下|存档|记)", re.I)


def do_not_remember_requested(user_msg: str) -> bool:
    """True when the user's own message asks that it (or the rest of its conversation) not be
    remembered."""
    return memory_opt_out_scope(user_msg) is not None


_OPTED_OUT_LOG = "User asked: [message not stored at the user's request]"


# ── Conversation-wide opt-out (r6-M1) ─────────────────────────────────────────
# r03 priv1: "OK. Please don't save anything about me from now on. What will you keep?" was
# answered "I won't save anything about you to long-term memory from now on", while only that one
# message was withheld and every later turn of the conversation was logged and extracted again.
# An opt-out with forward or conversation scope now pauses automatic memory for the rest of the
# conversation; one about a single message ("don't remember this", "别记这条") stays per message.
MEMORY_SCOPE_CONVERSATION = "conversation"
MEMORY_SCOPE_MESSAGE = "message"
_NEG_STORE_EN = (r"(?:do\s+not|don'?t|dont|never|no\s+need\s+to)\s+(?:(?:ever|even|actually)\s+)?"
                 r"(?:store|save|remember|memori[sz]e|record|log|keep|retain|track)")
_STORE_ALT_EN = r"(?:store|save|remember|memori[sz]e|record|log|keep|retain|track)"
# "for the rest of this chat", "from now on", "in this conversation".
_FORWARD_SCOPE_EN = re.compile(
    r"\bfrom\s+(?:now|here|this\s+point|today)\s+on(?:wards?)?\b|\bgoing\s+forward\b|"
    r"\bhenceforth\b|\b(?:for|during|in|throughout)\s+(?:the\s+(?:rest|remainder)\s+of\s+)?"
    r"(?:this|our|the)\s+(?:whole\s+|entire\s+)?(?:chat|conversation|session|thread)\b", re.I)
_CONVERSATION_OPT_OUT_EN = re.compile(
    # "don't save anything about me", "do not store or remember anything", "don't keep my data";
    # never "any of that", "anything from this message" or "anything about my salary".
    r"\b" + _NEG_STORE_EN + r"(?:\s+(?:or|and|/)\s+" + _STORE_ALT_EN + r")?\s+"
    r"(?:(?:any|every)thing|a\s+thing|any\s+(?:info(?:rmation)?|data|details?|facts?|personal\s+\w+)"
    r"|(?:any\s+)?(?:stuff|things)\s+about\s+me|my\s+(?:personal\s+)?(?:data|info(?:rmation)?|details))\b"
    r"(?!\s+(?:from|in|of)\s+(?:this|that)\s+(?:message|one|bit|part|turn|sentence|line)\b)"
    r"(?!\s+(?:about|regarding|concerning|on)\s+(?:my|the|this|that|these|those|his|her|their)\b)|"
    # "stop remembering things", "stop saving my info"; never "stop saving listings".
    r"\b(?:stop|quit|cease|no\s+more)\s+(?:remembering|saving|storing|recording|logging|"
    r"memori[sz]ing|keeping\s+(?:track|records?|notes?|(?:any|every)thing))\b"
    r"(?!\s+(?:the\s+|this\s+|that\s+|these\s+|those\s+|my\s+)?(?:listings?|flats?|propert\w+|"
    r"searches|search|results?|homes?|favou?rites?|places?)\b)|"
    r"\b(?:turn|switch|shut)\s+(?:off\s+(?:the\s+|your\s+)?memory|(?:the\s+|your\s+)?memory\s+off)\b|"
    r"\b(?:disable|deactivate|pause)\s+(?:the\s+|your\s+)?memory\b|"
    r"(?<!\bhave\s)(?<!\bhad\s)(?<!'ve\s)(?<!\bhas\s)(?<!\bgot\s)\bno\s+memory\s+at\s+all\b(?!\s+of\b)",
    re.I)
_STOP_STORE_EN = re.compile(
    r"\b(?:stop|quit|cease)\s+(?:remembering|saving|storing|recording|logging|memori[sz]ing)\b", re.I)
_NEG_STORE_ZH = (r"(?:请不要|不要|别|不用|请勿|不准|不许|不必|无需|不需要)(?:再|都|也|帮我|给我|替我)*"
                 r"(?:记住|记下|记录|保存|存储|存下|存档|储存|记)")
_SCOPE_ZH = (r"(?:以后|今后|从现在(?:开始|起)|从今(?:天)?(?:开始|起)|接下来|往后|此后|"
             r"(?:这次|本次|这个|本|这段|整个)(?:对话|聊天|会话))")
_CONVERSATION_OPT_OUT_ZH = re.compile(
    # "以后都别记", "这次对话都不要记", "不要记录这次对话"
    _SCOPE_ZH + r"[^。！？!?；;\n]{0,8}?" + _NEG_STORE_ZH + r"|"
    + _NEG_STORE_ZH + r"[^。！？!?；;\n]{0,8}?" + _SCOPE_ZH + r"|"
    # "什么都别记", "不要记住关于我的任何信息", "别再记了", "关闭记忆"
    r"(?:什么|任何|一切|所有)(?:东西|信息|内容|事情?|资料|数据)?(?:都|也)" + _NEG_STORE_ZH + r"|"
    + _NEG_STORE_ZH + r"(?:关于)?(?:我的)?(?:任何|一切|所有)|"
    r"(?:别|不要)再(?:记住|记录|保存|存储|记)(?:了|啦|啊|吧)?\s*(?:[。！!，,]|$)|"
    r"(?:关闭|停止|暂停)(?:记忆|记录功能|记忆功能)|停止(?:记住|记录|保存|存储)")
# Re-consent: "you can remember things again", "ok start saving again", "可以重新记了".
_RESUME_OBJECT_EN = (r"(?:\s+(?:things|stuff|everything|anything|me|(?:things|stuff)\s+about\s+me|"
                     r"about\s+me|my\s+(?:info(?:rmation)?|details|data|preferences|stuff|messages)|"
                     r"(?:what|whatever)\s+i\s+(?:say|tell\s+you)))?"
                     r"(?:\s+(?:in|for|from)\s+(?:this|the)\s+(?:chat|conversation|session))?")
_RESUME_EN = re.compile(
    r"\b(?:you\s+(?:can|may|could)(?:\s+now)?|feel\s+free\s+to|it'?s\s+(?:ok(?:ay)?|fine|alright)\s+"
    r"(?:for\s+you\s+)?to|i'?m\s+(?:happy|fine|ok(?:ay)?)\s+(?:for\s+you\s+)?to|go\s+ahead\s+and|"
    r"i\s+(?:don'?t|do\s+not)\s+mind\s+(?:you\s+)?|ok(?:ay)?,?)\s+"
    r"(?:(?:start|begin|resume|continue|go\s+back\s+to)\s+(?:to\s+)?)?"
    r"(?:remember(?:ing)?|sav(?:e|ing)|stor(?:e|ing)|record(?:ing)?|keep(?:ing)?|log(?:ging)?|"
    r"memori[sz](?:e|ing))\b" + _RESUME_OBJECT_EN + r"\s+(?:again|now|back\s+on)\b|"
    r"\b(?:start|begin|resume|continue)\s+(?:to\s+)?(?:remember(?:ing)?|sav(?:e|ing)|stor(?:e|ing)|"
    r"record(?:ing)?|logging|memori[sz]ing)\b" + _RESUME_OBJECT_EN + r"\s+again\b|"
    r"\bresume\s+(?:saving|remembering|storing|recording|memory)\b|"
    r"\b(?:turn|switch|put)\s+(?:the\s+|your\s+)?memory\s+(?:back\s+)?on\b|"
    r"\b(?:turn|switch)\s+(?:back\s+)?on\s+(?:the\s+|your\s+)?memory\b|\bmemory\s+(?:is\s+)?back\s+on\b|"
    r"\b(?:re-?enable|reactivate|unpause)\s+(?:the\s+|your\s+)?memory\b", re.I)
_RESUME_ZH = re.compile(
    r"(?:重新|恢复)(?:开始)?(?:记住|记录|保存|存储|记忆|记)(?:吧|了|啦|好了)?\s*(?:[。！!，,；;]|$)|"
    r"(?:可以|能)(?:继续|重新)?(?:记住|记录|保存|存储|记)了(?!吗|么|嘛)|"
    r"(?:恢复|打开|开启|重新开启)(?:记忆|记录功能|记忆功能)")
_QUESTION_END = re.compile(r"[?？]\s*$|(?:吗|么|嘛)\s*[?？]?\s*$")


def _sentences(text: str) -> list:
    return [s for s in re.split(r"(?<=[.!?。！？；;\n])", str(text or "")) if s.strip()]


def memory_opt_out_scope(user_msg: str) -> str | None:
    """``"conversation"`` when the message asks not to be remembered from now on or for the rest
    of the conversation, ``"message"`` when it asks only about itself, else None (r6-M1)."""
    value = str(user_msg or "")[:_REDACT_MAX_CHARS]
    if not value.strip():
        return None
    for sentence in _sentences(value):
        if _CONVERSATION_OPT_OUT_EN.search(sentence) or _CONVERSATION_OPT_OUT_ZH.search(sentence):
            return MEMORY_SCOPE_CONVERSATION
        if _FORWARD_SCOPE_EN.search(sentence) and (
                _DO_NOT_REMEMBER.search(sentence) or _STOP_STORE_EN.search(sentence)):
            return MEMORY_SCOPE_CONVERSATION
    if _DO_NOT_REMEMBER.search(value):
        return MEMORY_SCOPE_MESSAGE
    return None


def memory_resume_requested(user_msg: str) -> bool:
    """True when the user explicitly lets automatic memory run again ("you can remember things
    again"). A question about it, or a message that also opts out, is not a re-consent."""
    value = str(user_msg or "")[:_REDACT_MAX_CHARS]
    if memory_opt_out_scope(value) == MEMORY_SCOPE_CONVERSATION:
        return False
    for sentence in _sentences(value):
        match = _RESUME_EN.search(sentence) or _RESUME_ZH.search(sentence)
        if match and not _QUESTION_END.search(sentence[match.start():]) and not re.match(
                r"\s*(?:can|could|will|would|may|do|did|should|shall)\s+you\b", sentence, re.I):
            return True
    return False


def conversation_memory_turn(previously_paused, user_msg: str) -> dict:
    """What automatic memory does with this turn, given the conversation's pause flag.

    ``{"withhold": keep nothing from this turn, "paused": the flag after it, "event":
    "paused" | "resumed" | None, "scope": memory_opt_out_scope(user_msg)}``. ``previously_paused``
    is None when the flag could not be read: the turn is then withheld and nothing changes."""
    scope = memory_opt_out_scope(user_msg)
    if scope == MEMORY_SCOPE_CONVERSATION:
        return {"withhold": True, "paused": True, "event": "paused", "scope": scope}
    if previously_paused is None:
        return {"withhold": True, "paused": None, "event": None, "scope": scope}
    if previously_paused and memory_resume_requested(user_msg):
        return {"withhold": scope is not None, "paused": False, "event": "resumed", "scope": scope}
    if previously_paused:
        return {"withhold": True, "paused": True, "event": None, "scope": scope}
    return {"withhold": scope is not None, "paused": False, "event": None, "scope": scope}


def _paused_before_fork(user_id, lineage) -> bool | None:
    """True when an ancestor of a branched conversation was paused before the branch point: the
    branch continues a stretch the user asked not to be remembered. None when unknown."""
    from core import memory_gate
    for entry in list(lineage or [])[1:]:
        if not isinstance(entry, dict) or not entry.get("conversation_id"):
            continue
        try:
            paused_at = memory_gate.conversation_memory_paused_at(user_id, entry["conversation_id"])
            if paused_at is None:
                continue
            before = entry.get("before")
            if before is None or paused_at <= datetime.datetime.fromisoformat(
                    str(before)).timestamp():
                return True
        except Exception:
            return None
    return False


def apply_conversation_memory_turn(user_id, conversation_id, user_msg: str,
                                   lineage=None) -> dict:
    """Read this conversation's durable pause flag, apply the user's message to it and store the
    result (memory_gate ledger, so it survives a restart and is seen by every pool). Called once
    per turn, when the turn starts; the background memory job is handed the turn's ``withhold``.

    ``lineage`` (``conversation_store.get_branch_lineage``): a branch made after its parent was
    paused starts paused too."""
    from core import memory_gate
    previously = memory_gate.conversation_memory_paused(user_id, conversation_id)
    if previously is False and lineage:
        inherited = _paused_before_fork(user_id, lineage)
        if inherited:
            memory_gate.set_conversation_memory_paused(user_id, conversation_id, True)
        previously = None if inherited is None else bool(inherited)
    turn = conversation_memory_turn(previously, user_msg)
    if turn["paused"] is not None and turn["paused"] != previously:
        memory_gate.set_conversation_memory_paused(user_id, conversation_id, turn["paused"])
    return dict(turn, previously_paused=previously)


# The criteria slots a turn can change, with the keys they live under in
# ``accumulated_search_criteria``. Deterministic: derived from the system's criteria state,
# never from assistant prose (R2-06 stands).
_CRITERIA_SLOTS = (
    ("area", ("areas", "area")),
    ("budget", ("max_budget",)),
    ("bedrooms", ("bedrooms",)),
    ("room_type", ("room_type",)),
    ("commute_destination", ("commute_destination", "destination")),
    ("max_commute_minutes", ("max_commute_time", "max_travel_time")),
)


def _slot_value(criteria: dict, keys: tuple):
    for key in keys:
        value = criteria.get(key)
        if key == "areas" and isinstance(value, (list, tuple)):
            value = [str(v).strip() for v in value if str(v or "").strip()]
            if value:
                return value
            continue
        if value not in (None, "", [], {}):
            if key == "area":
                return [str(value).strip()]
            return value
    return None


def criteria_changes(before, after) -> dict:
    """``{slot: {"from": old, "to": new}}`` for each search criterion this turn changed.

    "Make that Poplar, budget £1,500" after "a studio in Whitechapel under £1,700" gives
    ``{"area": {"from": ["Whitechapel"], "to": ["Poplar"]}, "budget": {"from": 1700,
    "to": 1500}}``. Only a slot that HAD a value and now holds a different one is a change;
    a newly set slot supersedes nothing."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    changes = {}
    for slot, keys in _CRITERIA_SLOTS:
        old, new = _slot_value(before, keys), _slot_value(after, keys)
        if old is None or new is None:
            continue
        same = ({str(v).lower() for v in old} == {str(v).lower() for v in new}
                if isinstance(old, list) and isinstance(new, list) else str(old) == str(new))
        if not same:
            changes[slot] = {"from": old, "to": new}
    return changes


def _sanitize_changes(changes) -> dict:
    """Keep only well-formed slot changes with short scalar/list values."""
    if not isinstance(changes, dict):
        return {}
    clean = {}
    known = {slot for slot, _ in _CRITERIA_SLOTS}
    for slot, change in changes.items():
        if slot not in known or not isinstance(change, dict):
            continue
        pair = {}
        for side in ("from", "to"):
            value = change.get(side)
            if isinstance(value, (list, tuple)):
                value = [str(v)[:60] for v in value if isinstance(v, (str, int, float))][:6]
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                value = value if not isinstance(value, str) else value[:60]
            else:
                value = None
            pair[side] = value
        if pair["from"] not in (None, []) and pair["to"] not in (None, []):
            clean[slot] = pair
    return clean


def _resolved_criteria_block(changes: dict) -> str:
    """The extractor's labelled context: what an elliptical user message refers to."""
    return ("RESOLVED CRITERIA CHANGES THIS TURN (computed by the system from the user's own "
            "messages, not assistant text; use them only to understand what the user's message "
            "refers to, e.g. 'make that Poplar' means the area is now Poplar, and state such a "
            "fact in full; never add a fact the user did not state):\n"
            + _render_changes(changes) + "\n\n")


def _render_changes(changes: dict) -> str:
    def show(value):
        return " / ".join(map(str, value)) if isinstance(value, list) else str(value)
    return "\n".join(f"- {slot}: {show(change['from'])} -> {show(change['to'])}"
                     for slot, change in changes.items())


# Provenance of a semantic row (R4-02). An explicit "please remember …" row is the user's own
# wording and the reply told them exactly that text was saved; the background job may ADD
# beside it but never UPDATE or DELETE it.
SOURCE_EXPLICIT = "explicit_remember"
SOURCE_AUTO = "auto_extracted"
# A fact about another person is never touched by a change of the user's search criteria:
# "my guarantor is in Manchester" became "…in Canary Wharf" when the area slot changed.
_THIRD_PARTY = re.compile(
    r"\b(?:guarantor|partner|wife|husband|spouse|son|daughter|child|children|kids?|landlord|"
    r"friend|mother|mum|mom|father|dad|parents?|brother|sister|colleague|flatmate|roommate|"
    r"housemate|boss|employer|girlfriend|boyfriend|fianc[ée]e?)\b|"
    r"担保人|伴侣|老公|老婆|丈夫|妻子|孩子|儿子|女儿|房东|朋友|父母|同事|室友", re.I)
# A row that states the user's own SEARCH criterion (area, budget, bedrooms, room type,
# commute requirement) — the only rows a criteria change may rewrite.
_CRITERIA_FACT = re.compile(
    r"\b(?:looking|search(?:es|ing)?|find|wants?|needs?|prefers?|budget|rent|max(?:imum)?|"
    r"under|up\s+to|studio|flat|apartment|bed(?:room)?s?|\d-bed|area|live|living|move|moving|"
    r"commute)\b|预算|租金|找|想住|区域|通勤|卧室|开间", re.I)


def _criteria_row(text: str) -> bool:
    value = str(text or "")
    return bool(_CRITERIA_FACT.search(value)) and not _THIRD_PARTY.search(value)


def _money_pattern(amount) -> re.Pattern | None:
    try:
        value = int(round(float(amount)))
    except (TypeError, ValueError):
        return None
    plain, grouped = str(value), f"{value:,}"
    alternatives = sorted({re.escape(grouped), re.escape(plain)}, key=len, reverse=True)
    return re.compile(r"(£\s?)?(?<![\d,.])(?:" + "|".join(alternatives) + r")(?![\d,])")


def _superseded_text(text: str, changes: dict) -> str | None:
    """``text`` with each changed slot's OLD value replaced by the new one, or None.

    Only the two slots whose values have one spelling are rewritten deterministically:
    a single area replaced by a single area, and a budget amount. The rest is left to the
    consolidator, which sees the same changes as labelled context."""
    out = str(text or "")
    area = changes.get("area")
    if area and isinstance(area.get("from"), list) and isinstance(area.get("to"), list):
        removed = [a for a in area["from"] if a.lower() not in {b.lower() for b in area["to"]}]
        added = [b for b in area["to"] if b.lower() not in {a.lower() for a in area["from"]}]
        if len(removed) == 1 and len(added) == 1:
            pattern = re.compile(r"(?<![A-Za-z])" + re.escape(removed[0]) + r"(?![A-Za-z])", re.I)
            if pattern.search(out) and not re.search(
                    r"(?<![A-Za-z])" + re.escape(added[0]) + r"(?![A-Za-z])", out, re.I):
                out = pattern.sub(added[0], out)
    budget = changes.get("budget")
    if budget:
        pattern = _money_pattern(budget.get("from"))
        try:
            new_value = int(round(float(budget.get("to"))))
        except (TypeError, ValueError):
            pattern = None
        if pattern is not None and pattern.search(out):
            out = pattern.sub(lambda m: (m.group(1) or "") + f"{new_value:,}", out)
    return out if out != text else None


# ── Bounds the user stated (stress A-08) ─────────────────────────────────────
# "my budget is at most £1,900" was consolidated into "Budget: up to £2,100 per month rent for
# the right place" after the user said "my budget could stretch to £2,100 for the right place":
# a conditional stretch replaced the ceiling, and the next search used 2,100. A stored ceiling
# (rent or commute minutes) is only loosened, or dropped, when the user's message states the new
# ceiling without a condition.
# Bounded number shapes ("1,900", "1900", "2.1k"): an open [\d,]+ backtracked quadratically on
# a long "1,1,1,…" run.
_BOUND_MONEY = re.compile(
    r"£\s{0,3}((?:\d{1,3}(?:,\d{3}){1,3}|\d{1,7})(?:\.\d{1,2})?)(?![\d,])\s{0,3}(k\b)?|"
    r"(?<![\d,.])((?:\d{1,3}(?:,\d{3}){1,3}|\d{3,7})(?:\.\d{1,2})?)(?![\d,])\s{0,3}(?:pcm|p/m|per\s+month|"
    r"a\s+month|/\s*month|pw|p/w|per\s+week|a\s+week|/\s*week|英镑|镑)", re.I)
_BOUND_MINUTES = re.compile(r"\b(\d{1,3})\s*-?\s*(?:min(?:ute)?s?\b|分钟)", re.I)
_COMMUTE_TERM = re.compile(r"\b(?:commut\w*|travel\w*|journey|door[\s-]to[\s-]door)\b|通勤|路程", re.I)
# A conditional or tentative bound: "could stretch to", "for the right place", "if needed".
_SOFT_BOUND = re.compile(
    r"\b(?:stretch\w*|flex\w*|negotiable|could|might|maybe|possibly|perhaps|if|"
    r"at\s+a\s+push|for\s+the\s+right|willing\s+to|would\s+consider|wiggle\s+room|leeway)\b|"
    r"如果|可以商量|可商量|灵活|弹性|放宽|上浮|浮动|勉强|实在不行|必要时|合适的话|稍微", re.I)
_BOUND_REMOVED = re.compile(
    r"\b(?:no|cancel|remove|drop|forget|without|ignore|scrap)\s+(?:the\s+|my\s+|any\s+|a\s+)?"
    r"(?:budget|price|rent|commute)(?:\s+(?:limit|cap|ceiling|max(?:imum)?))?\b|"
    r"取消预算|不限预算|没有预算|不限通勤", re.I)
_BUDGET_WORD = re.compile(r"\b(?:budget|rent|price|spend|afford|pcm)\b|预算|租金|价格|房租", re.I)
_BOUND_CLAUSE = re.compile(r"[,;!?，；。！？\n]|\.(?!\d)|\b(?:but|though|although)\b", re.I)


def _money_amounts(text: str) -> list:
    values = []
    for match in _BOUND_MONEY.finditer(str(text or "")):
        raw = match.group(1) or match.group(3)
        try:
            value = float(raw.replace(",", ""))
        except (AttributeError, ValueError):
            continue
        values.append(value * 1000 if match.group(2) else value)
    return values


def _row_bounds(text: str) -> dict:
    """``{"money": highest rent figure, "minutes": highest commute minutes}`` a row states."""
    value = str(text or "")[:_PREFERENCE_MAX_CHARS * 4]
    bounds = {}
    money = _money_amounts(value)
    if money:
        bounds["money"] = max(money)
    if _COMMUTE_TERM.search(value):
        minutes = [int(m.group(1)) for m in _BOUND_MINUTES.finditer(value)]
        if minutes:
            bounds["minutes"] = max(minutes)
    return bounds


def _clauses(text: str) -> list:
    return [part for part in _BOUND_CLAUSE.split(str(text or "")[:_REDACT_MAX_CHARS]) if part.strip()]


def _loosened_bound(old: str, new: str) -> str | None:
    """The bound kind ("money"/"minutes") that ``new`` loosens relative to ``old``, or None.

    Looser means a higher ceiling, the ceiling gone from the row, or a new condition on it
    ("could stretch", "flexible") the old row did not carry."""
    before, after = _row_bounds(old), _row_bounds(new)
    for kind, old_value in before.items():
        new_value = after.get(kind)
        if new_value is None or new_value > old_value:
            return kind
        if _SOFT_BOUND.search(str(new or "")) and not _SOFT_BOUND.search(str(old or "")):
            return kind
    return None


def _firm_bound_stated(user_msg: str, kind: str, value=None) -> bool:
    """True when the user's message states a ``kind`` ceiling (``value`` when given) with no
    condition attached, or removes that ceiling outright."""
    msg = str(user_msg or "")
    if value is None and _BOUND_REMOVED.search(msg) and not _SOFT_BOUND.search(msg):
        return True
    for clause in _clauses(msg):
        if kind == "money":
            amounts = _money_amounts(clause)
            if not amounts and _BUDGET_WORD.search(clause):
                # "budget 2100": a bare figure counts only beside a budget word (never a year).
                amounts = [float(n.replace(",", "")) for n in
                           re.findall(r"(?<![\d,.])(\d{1,3}(?:,\d{3}){1,3}|\d{3,7})(?![\d,])", clause)]
        else:
            amounts = [int(m.group(1)) for m in _BOUND_MINUTES.finditer(clause)]
        if not amounts or _SOFT_BOUND.search(clause):
            continue
        if value is None or any(abs(float(a) - float(value)) < 1 for a in amounts):
            return True
    return False


def _incidental_place(user_msg: str, place: str) -> bool:
    """True when every mention of ``place`` in the message is about someone else ("my friend
    lives in Peckham"), so it is not the user's own search area."""
    name = str(place or "").split(",")[0].strip()
    if not name:
        return False
    pattern = re.compile(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", re.I)
    seen = False
    for clause in _clauses(user_msg):
        for match in pattern.finditer(clause):
            seen = True
            before = clause[:match.start()]
            other = None
            for other in _THIRD_PARTY.finditer(before):
                pass
            if other is None or re.search(r"\b(?:i|i'm|i've|me|we|us|our)\b|我", before[other.end():], re.I):
                return False
    return seen


def user_stated_changes(changes: dict, user_msg: str) -> dict:
    """The criteria changes a memory row may be superseded by: the ones the user's message
    states as their own (stress A-08). "my friend lives in Peckham" moved the area slot to
    Peckham and "my budget could stretch to £2,100 for the right place" moved the budget to
    2,100; neither is a new criterion of the user's, so neither rewrites what memory holds."""
    out = dict(changes or {})
    area = out.get("area")
    if area and isinstance(area.get("to"), list):
        before = {str(a).lower() for a in area.get("from") or []}
        added = [b for b in area["to"] if str(b).lower() not in before]
        if added and all(_incidental_place(user_msg, b) for b in added):
            out.pop("area")
    budget = out.get("budget")
    if budget:
        try:
            raised = float(budget.get("to")) > float(budget.get("from"))
        except (TypeError, ValueError):
            raised = False
        if raised and not _firm_bound_stated(user_msg, "money", budget.get("to")) and any(
                _SOFT_BOUND.search(clause) for clause in _clauses(user_msg)
                if _money_amounts(clause) or _BUDGET_WORD.search(clause)):
            out.pop("budget")
    return out


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


# ---------------------------------------------------------------- targeted erasure
# Defect G9: an explicit "forget my budget" deleted the SEMANTIC fact and left the
# EPISODIC records — including the raw "Please remember my budget is £1400 a month."
# — so retrieve()/recall_memory happily handed the deleted figure straight back and the
# answer quoted it. A user who is told "done, deleted" and then quoted the value has
# been lied to, so the erasure has to be CROSS-LAYER (every store retrieve() can read),
# not semantic-only. See AgentMemory.forget_fact.
#
# Fields whose stated VALUE is worth erasing. Only money-bearing fields are listed: the
# scrub predicate requires a value, so a field with no numeric value has nothing to
# scrub and listing it would only widen the destructive trigger for no benefit.
_FIELD_TERMS = {
    "budget": ("budget", "budgets", "rent", "rents", "rental", "price", "prices",
               "spend", "afford", "pcm", "预算", "租金", "价格", "房租"),
}

# Delete IMPERATIVES. Each requires a field term within a short, punctuation-free window
# so a delete verb in one clause cannot reach a field in the next.
_ERASE_EN_TEMPLATES = (
    r"\b(?:forget|delete|remove|erase|discard|clear|wipe|scrub|drop)\b"
    r"[^.!?;\n]{{0,24}}?\b(?:{f})\b",
    r"\b(?:don'?t|do\s+not|stop|no\s+longer)\s+"
    r"(?:keep|keeping|save|saving|store|storing|remember|remembering|hold|holding)\b"
    r"[^.!?;\n]{{0,24}}?\b(?:{f})\b",
)
_ERASE_ZH = ("忘掉", "忘记", "删除", "删掉", "清除", "清掉", "不要记", "别记", "不用记",
             "不要保存", "别保存", "取消记录")

# The erasure must be about THIS user's own stored value. Without a first-person
# reference ("forget my budget", "don't keep any budget for me") a delete verb next to a
# money term is talking about something else ("forget about the price of that flat") and
# must NOT trigger a destructive scrub.
#
# "user" is deliberately NOT a first-person marker: remember_turn prefixes EVERY episodic
# turn log with "User asked: ", so including it would make the check vacuous for the whole
# episodic layer and let a market price the user merely enquired about ("User asked: what
# do Camden rents average? £1800") be scrubbed as if it were their own budget. The
# distilled layers do not need it — _states_field_value exempts semantic/reflection by
# mtype, which is what covers a pronoun-free "budget £1400/month".
_FIRST_PERSON = re.compile(r"\b(?:my|mine|me|i|i'?m|we|our|us)\b", re.IGNORECASE)
_FIRST_PERSON_ZH = ("我", "咱")

# A record "states a value" for a field when it names the field AND carries a money-sized
# number. Three+ digits (or a £ prefix) keeps "2 bedrooms" out of a budget scrub.
_MONEY_RE = re.compile(r"£\s*\d[\d,]*(?:\.\d+)?|\b\d[\d,]{2,}(?:\.\d+)?\b")

# field -> compiled EN delete-imperative patterns (field alternation substituted in).
_ERASE_PATTERNS = {
    field: tuple(
        re.compile(tpl.format(f="|".join(re.escape(t) for t in terms)), re.IGNORECASE)
        for tpl in _ERASE_EN_TEMPLATES
    )
    for field, terms in _FIELD_TERMS.items()
}


def _mentions_field(text: str, field: str) -> bool:
    value = (text or "").casefold()
    return any(term in value for term in _FIELD_TERMS.get(field, ()))


def _states_field_value(text: str, field: str, mtype: str = "") -> bool:
    """True when ``text`` is a statement of THIS user's value for ``field``.

    Requires (a) a field term, (b) a money-sized number, and (c) first-person framing
    OR a distilled layer (semantic/reflection records are about the user by
    construction — "budget £1400/month" carries no pronoun). (c) is what keeps a market
    fact the user merely asked about ("Camden rents average £1800") out of the scrub.
    """
    if not _mentions_field(text, field):
        return False
    if not _MONEY_RE.search(text or ""):
        return False
    if mtype in ("semantic", "reflection"):
        return True
    return bool(_FIRST_PERSON.search(text or "")
                or any(ch in (text or "") for ch in _FIRST_PERSON_ZH))


def erasure_request_fields(text: str) -> tuple:
    """Fields the user is explicitly asking us to DELETE, from their own message.

    Deterministic (no LLM). Returns () for anything that is not an unambiguous,
    first-person delete imperative — the conservative direction, because the caller
    acts on this destructively.
    """
    raw = (text or "").strip()
    if not raw:
        return ()
    if not (_FIRST_PERSON.search(raw) or any(ch in raw for ch in _FIRST_PERSON_ZH)):
        return ()
    hits = []
    for field, terms in _FIELD_TERMS.items():
        matched = any(pat.search(raw) for pat in _ERASE_PATTERNS[field])
        if not matched:
            # zh has no word boundaries: a delete cue plus a field term anywhere in the
            # (short) message is the signal.
            matched = (any(cue in raw for cue in _ERASE_ZH)
                       and any(t in raw for t in terms))
        if matched:
            hits.append(field)
    return tuple(hits)


# ── Sensitive personal data in AUTO-memory (2026-10-19 A-09) ─────────────────
# Told "I earn £62,000, I'm on a Skilled Worker visa, I have asthma, my phone is 07700
# 900123", the assistant said it would not store health, immigration, income or contact
# details — while remember_turn wrote all of them verbatim into the episodic layer
# ("User asked: <first 300 chars>"), retrievable for 500 turns. Storage now matches the
# promise: the AUTOMATIC layers (the episodic turn log, the facts extracted from a turn,
# the reflections over them) never keep these categories. A user's explicit "remember
# X" goes through the remember tool and its own authorisation, not through here.
#
# A housing REQUIREMENT (step-free access, a lift, wheelchair-accessible) is not in these
# lists: the search reads it from the turn's own criteria. A statement ABOUT the person —
# using a wheelchair, a disability, being on Universal Credit, a child's name or school —
# is (R4-08): r07 stored 'Uses a wheelchair', 'User is on Universal Credit' and 'Has a son,
# Tom, who attends Oakfield Primary' with pii_categories none.
_SENSITIVE_CLAUSE_PATTERNS = (
    # Income words only: "a year" alone also describes a tenancy ("rent for a year in
    # Hackney") and must not cost the user their area.
    ("income", re.compile(
        r"\b(?:earn(?:s|ing)?|salary|salaries|income|wage|wages|take[- ]home|per annum)\b|"
        r"年薪|月薪|收入|工资", re.I)),
    ("immigration", re.compile(
        r"\b(?:visa|immigration|asylum|refugee|brp|biometric residence|indefinite leave|"
        r"settled status|pre-settled|right to remain|leave to remain|work permit)\b|"
        r"签证|移民|难民|居留", re.I)),
    ("health", re.compile(
        r"\b(?:asthma|diabet\w*|epilep\w*|cancer|hiv|pregnan\w*|depress\w*|anxiety|"
        r"adhd|autis\w*|bipolar|schizo\w*|mental health|medical condition|health condition|"
        r"chronic (?:illness|pain|condition)|diagnos\w*|medication|therapy|chemo\w*|"
        r"illness|allerg\w*)\b|哮喘|糖尿病|癫痫|癌|怀孕|抑郁|焦虑|病情|疾病|过敏", re.I)),
    ("disability", re.compile(
        r"\b(?:disab(?:led|ility|ilities)|wheelchair[- ]user|(?:use[sd]?|using|in)\s+a\s+wheelchair|"
        r"mobility\s+(?:impair\w*|issues?|problems?|aid|scooter)|blind|deaf|visually\s+impaired|"
        r"hearing\s+impaired|paralys\w*|amputee|medical\s+conditions?)\b|"
        r"残疾|坐轮椅|用轮椅|视障|听障|失明|耳聋", re.I)),
    ("benefits", re.compile(
        r"\b(?:universal\s+credit|housing\s+benefit|personal\s+independence\s+payment|"
        r"local\s+housing\s+allowance|jobseeker'?s?\s+allowance|"
        r"employment\s+and\s+support\s+allowance|pension\s+credit|on\s+benefits|"
        r"council\s+tax\s+(?:support|reduction))\b|\b(?-i:PIP|LHA|JSA|ESA|DSS)\b|"
        r"统一福利|救济金|福利金|住房补贴|低保", re.I)),
    # A child's SCHOOL by name or attendance. "near a good primary school" is a housing
    # preference and stays.
    ("children", re.compile(
        r"\b(?:attends?|attending|goes\s+to|is\s+at|enrolled\s+at)\s+(?-i:[A-Z][\w'’-]+)|"
        r"(?-i:[A-Z][a-z]+)\s+(?-i:Primary|Secondary|Academy|School|Nursery|Infants|Juniors)\b|"
        r"(?:儿子|女儿|孩子)(?:叫|名叫|名字)|(?:小学|中学|幼儿园)(?:上学|读书|念书)|就读于", re.I)),
    # A self-identifying name (R1-08): "Hi. My name is Priya Raman, ..." stayed in the
    # episodic layer while the assistant told the user "I don't have your name".
    ("identity", re.compile(
        r"\b(?:my|user'?s?|the user'?s|his|her|their)\s+(?:full\s+|first\s+|last\s+|sur|given\s+|family\s+)?"
        r"name\b|\b(?:i\s+am|i'm|user\s+is|they\s+are|he\s+is|she\s+is)\s+(?:called|named)\b|"
        r"\bcall\s+me\s+(?-i:[A-Z])|我叫|我的名字|名字(?:是|叫)|我姓|用户(?:名叫|叫|的名字)", re.I)),
)
# Anchored and bounded: unanchored, a long word or digit run was re-scanned from every
# position looking for an "@" (O(n²) in one C call, holding the GIL — 10 s for 60k chars).
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
_PHONE_RE = re.compile(
    r"(?<![\d£$])(?:\+44\s?\(?0?\)?\s?\d|\(0\d|\b0\d)(?:[\s)-]{0,2}\d){8,9}(?!\d)")
# Clause boundaries: sentence/clause punctuation (a comma or full stop between digits is a
# thousands separator or a decimal point, never a boundary), or "and/but" introducing a new
# first-person clause ("... a year and I'm on a visa").
_CLAUSE_BOUNDARY = re.compile(
    r"[;:!?，；：。！？]\s*|[,.](?!\d)\s*|(?<!\s)\s+(?:and|but)\s+(?=(?:i|i'm|i am|my)\b)", re.I)
# Auto-memory stores 300 characters of the user message and extracts facts from a turn;
# nothing past this bound is needed, and redaction must never be the expensive step.
_REDACT_MAX_CHARS = 8_000
_SENSITIVE_MARKER = "[personal detail not stored]"
_CHILD_NAME_RE = re.compile(
    r"\b((?i:son|daughter|child|kid|stepson|stepdaughter))\b,?\s+(?:(?i:called|named)\s+)?"
    r"[A-Z][a-z]{1,20}\b,?")


def redact_sensitive(text: str) -> tuple[str, list[str]]:
    """``text`` with income, immigration, health and contact details removed.

    Clause-level: a clause that states one of these categories is replaced by a neutral
    marker, so the housing parts of the same message ("1-bed in Hackney under £1,900")
    survive byte-for-byte. Emails and phone numbers are removed wherever they appear.
    Returns the redacted text and the categories found (for the ``pii_categories``
    metadata). Never raises."""
    try:
        value = str(text or "")[:_REDACT_MAX_CHARS]
        found: list[str] = []
        if _EMAIL_RE.search(value) or _PHONE_RE.search(value):
            found.append("contact")
            value = _PHONE_RE.sub("[contact detail not stored]",
                                  _EMAIL_RE.sub("[contact detail not stored]", value))
        if _CHILD_NAME_RE.search(value):
            # "Has a son, Tom, who …": the name sits in its own comma clause (R4-08).
            found.append("children")
            value = _CHILD_NAME_RE.sub(lambda m: m.group(1) + " " + _SENSITIVE_MARKER, value)
        out: list[str] = []
        start = 0
        bounds = [(m.start(), m.end()) for m in _CLAUSE_BOUNDARY.finditer(value)]
        bounds.append((len(value), len(value)))
        for left, right in bounds:
            clause, separator = value[start:left], value[left:right]
            start = right
            hit = next((name for name, pattern in _SENSITIVE_CLAUSE_PATTERNS
                        if pattern.search(clause)), None)
            if hit:
                if hit not in found:
                    found.append(hit)
                if out and out[-1].startswith(_SENSITIVE_MARKER):
                    out[-1] = _SENSITIVE_MARKER + separator
                    continue
                clause = _SENSITIVE_MARKER
            out.append(clause + separator)
        return "".join(out).strip(), found
    except Exception:
        return "", ["unparsed"]


# A stored search preference keeps the HOUSING requirement and drops the health reason given
# for it: "mould-free due to asthma" sat in the conversation's criteria (and view_state) while
# the memory layer redacted the same clause and the assistant said asthma was "only reflected
# indirectly" (R2-04). The reason is cut at its connector, or as a parenthetical aside; what is
# left that still names a health condition goes through the same clause redaction as memory.
_HEALTH_TERMS = dict(_SENSITIVE_CLAUSE_PATTERNS)["health"]
# Anchored at the first separator of a run, so a long blank run is scanned once (B-01 class).
_HEALTH_REASON_START = re.compile(
    r"(?<![\s,;:–—-])[\s,;:–—-]*\b(?:due\s+to|because(?:\s+of)?|owing\s+to|on\s+account\s+of|"
    r"as|since|for|given)\b|(?<![\s，,])[\s，,]*(?:因为|由于)", re.I)
_HEALTH_REASON_END = re.compile(r"(?<!\s)\s+(?:and|but|plus)\s+|[,;.，；。]")
_PARENTHETICAL_ASIDE = re.compile(r"(?<!\s)\s*[(（\[][^()（）\[\]]{0,120}[)）\]]")
_PREFERENCE_MAX_CHARS = 500


def strip_health_reason(text: str) -> str | None:
    """``text`` without a stated health reason; None when nothing but health is left.

    "mould-free due to asthma" -> "mould-free"; "quiet flat (I have anxiety)" -> "quiet
    flat"; "I have asthma" -> None. Text naming no health condition is returned unchanged.
    Never raises."""
    try:
        value = str(text or "").strip()[:_PREFERENCE_MAX_CHARS]
        if not _HEALTH_TERMS.search(value):
            return value or None
        value = _PARENTHETICAL_ASIDE.sub(
            lambda m: "" if _HEALTH_TERMS.search(m.group()) else m.group(), value)
        position = 0
        for _guard in range(20):
            match = _HEALTH_REASON_START.search(value, position)
            if not match:
                break
            end = _HEALTH_REASON_END.search(value, match.end())
            stop = end.start() if end else len(value)
            if _HEALTH_TERMS.search(value[match.start():stop]):
                # "mould-free due to asthma and close to a GP" -> "mould-free and close to a GP"
                value = value[:match.start()] + value[stop:]
                position = match.start()
            else:
                position = match.end()
        value = value.strip(" \t,;:–—-，；：。")
        if _HEALTH_TERMS.search(value):
            value, _found = redact_sensitive(value)
            value = value.replace(_SENSITIVE_MARKER, " ")
            value = re.sub(r"\s+([,;.])", r"\1", " ".join(value.split())).strip(" \t,;:.–—-，；：。")
        return value if re.search(r"[0-9A-Za-z\u4e00-\u9fff]", value) else None
    except Exception:
        return None


def sanitize_soft_preferences(values) -> list:
    """Each stored soft preference with any health reason removed (order kept, empties gone)."""
    if isinstance(values, str):
        values = [values]
    out: list = []
    for item in values or []:
        if not isinstance(item, str):
            out.append(item)
            continue
        cleaned = strip_health_reason(item)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


# A reflection is an INFERENCE about the person, not something they said. "Needs a lift ->
# probably has a mobility impairment" turned a stated housing requirement into a stored
# health inference (2026-10-19 A-10). Reflections never keep inferences of this kind; the
# stated requirement itself stays in the semantic layer as a property fact.
_SENSITIVE_INFERENCE_RE = re.compile(
    # "Accessibility is likely a non-negotiable requirement…" (R4-08): an inference about
    # the person; the stated requirement stays in the turn's criteria.
    r"\b(?:accessib\w*|wheelchair|benefits?|universal credit|"
    r"mobility|disab\w*|impair\w*|medical|illness|elderly|frail|"
    r"health (?:condition|issue|problem|need)s?|(?:medical|health|physical) condition)\b|"
    r"行动不便|残疾|身体状况", re.I)


# ── Reflection wording (R4-12) ────────────────────────────────────────────────
# r06: "pet-friendliness (cat) and area safety are deal-breakers, not nice-to-haves" for a user who
# said "I have a cat" and once asked whether the area was safe; the next search carried both as
# preferences. r08: "User has a firm, non-negotiable set of core criteria …". A reflection is the
# assistant's guess: it may use a word that turns a preference into a requirement (or a stated
# limit into a flexible one) only when the user's own messages used that word, and it never
# says the user stated something.
_ESCALATION_TERMS = tuple((key, re.compile(pattern, re.I)) for key, pattern in (
    ("non-negotiable", r"\bnon[\s-]?negotiables?\b|\bnot\s+negotiable\b"),
    ("deal-breaker", r"\bdeal[\s-]?breakers?\b"),
    ("must", r"\bmusts?\b"),
    ("firm", r"\bfirm(?:ly)?\b"),
    ("hard", r"\bhard\s+(?:\w+\s+)?(?:requirements?|constraints?|limits?|lines?|rules?|ceilings?|"
             r"caps?|cut[\s-]?offs?|stops?|filters?|criteria|criterion|anchors?|conditions?|musts?)\b"),
    ("strict", r"\bstrict(?:ly)?\b"),
    ("absolute", r"\babsolute(?:ly)?\b"),
    ("essential", r"\bessentials?\b"),
    ("mandatory", r"\bmandatory\b|\bobligatory\b"),
    ("required", r"\brequired\b"),
    ("critical", r"\bcritical\b|\bcrucial\b|\bvital\b|\bindispensable\b|\bimperative\b"),
    ("insist", r"\binsist\w*"),
    ("compromise", r"\b(?:no|not|never|won'?t|will\s+not|cannot|can'?t|without|rather\s+than)\s+"
                   r"(?:\w+\s+)?compromis\w*|\buncompromising\b"),
    ("non-starter", r"\bnon[\s-]?starters?\b|\bno[\s-]go\b|\bred\s+lines?\b"),
    ("priority", r"\b(?:top|highest|first|number[\s-]one|overriding|main|primary|biggest)\s+priorit\w*|"
                 r"\btakes?\s+priority\b|\bcomes?\s+first\b"),
    ("core", r"\b(?:key|core|fixed|stable|fundamental|main|primary|dominant|decisive|central)\s+(?:\w+\s+)?"
             r"(?:requirements?|constraints?|criteria|criterion|anchors?|conditions?|filters?|factors?|"
             r"drivers?)\b"),
    # Explicit hard-constraint escalations missed in the pre-R4-12 rows (r6-M2 follow-up): "a real
    # constraint, not a nice-to-have", "a must-have", "won't consider". A bare "prioritises" stays
    # acceptable: it describes a trade-off, not a requirement.
    ("real-constraint", r"\b(?:real|genuine|actual|true)\s+(?:hard\s+)?(?:constraints?|requirements?|"
                        r"limits?|deal[\s-]?breakers?|must[\s-]?haves?)\b"),
    ("nice-to-have", r"\bnot\s+(?:just\s+|merely\s+|simply\s+|only\s+)?(?:an?\s+)?nice[\s-]to[\s-]haves?\b|"
                     r"\b(?:more|rather)\s+than\s+(?:just\s+)?an?\s+nice[\s-]to[\s-]haves?\b"),
    ("must-have", r"\bmust[\s-]haves?\b"),
    ("wont-consider", r"\b(?:won'?t|will\s+not|wouldn'?t|would\s+not|refuses?\s+to|never)\s+(?:even\s+)?"
                      r"consider\w*\b"),
    # Softening a stated limit is the same inference in the other direction.
    ("negotiable", r"(?<!non-)(?<!non )(?<!not )\bnegotiable\b"),
    ("flexible", r"\bflex\w*"),
    ("stretch", r"\bstretch\w*"),
    ("relax", r"\brelax(?:e|es|ed|ing)?\s+(?:\w+\s+){0,2}(?:budget|price|rent|constraints?|limits?|"
              r"ceilings?|caps?|requirements?|criteria)\b"),
    ("willing", r"\bwilling\s+to\s+(?:go|pay|spend|stretch|flex|exceed|compromise)\b"),
    ("soft", r"\bsoft\s+(?:limits?|ceilings?|caps?|budgets?|targets?|constraints?)\b"),
    ("leeway", r"\bwiggle\s+room\b|\bleeway\b"),
    ("zh-must", r"必须|务必|一定要|非得|非.{1,6}不可"),
    ("zh-hard", r"硬性|硬指标|硬条件|硬约束|刚需|刚性"),
    ("zh-bottom", r"底线|红线|一票否决"),
    ("zh-nonneg", r"不可妥协|不能妥协|绝不妥协|没得商量|不容商量|不可协商|不能商量|不可商量|没有商量"),
    ("zh-critical", r"不可或缺|必不可少|至关重要|决定性|首要|最重要|最核心|关键要求|核心要求"),
    ("zh-absolute", r"绝对"),
    ("zh-flex", r"可以商量|可商量|灵活|弹性|放宽|上浮|浮动"),
))
# An inference worded as the user's own statement: "the user stated", "they insisted on", "the
# user's own request said".
_ATTRIBUTION_RE = re.compile(
    r"\b(?:the\s+)?(?:user|they|he|she)(?:'s)?\s+(?:has\s+|have\s+|had\s+)?(?:\w+ly\s+)?"
    r"(?:said|says|stated|states|told|tells|insisted|insists|specified|specifies|"
    r"made\s+(?:it\s+)?clear|confirmed|confirms|emphasi[sz]ed|emphasi[sz]es|stressed|stresses|"
    r"declared|declares|asked\s+for|asks\s+for|requested|requests|demanded|demands|"
    r"mentioned|mentions)\b|"
    r"\bexplicitly\b|\b(?:as|per)\s+(?:stated|requested|specified|instructed)\b|"
    r"\baccording\s+to\s+the\s+user\b|"
    r"\b(?:user|their|his|her)(?:'s)?\s+own\s+(?:words|request|statement|message)s?\b|"
    r"用户(?:已经|已|曾经|曾|明确|亲口|反复|一再)?(?:说过|说|表示|表明|要求|强调|提到|声明|指出|坚持)",
    re.I)
# Shown with every reflection the model reads (format_for_prompt, recall_memory).
INFERENCE_NOTE = ("tentative inference by the assistant (a guess that may be wrong); "
                  "not said or saved by the user")


def reflection_rejection(text: str, user_words: str = "") -> str | None:
    """Why a reflection may not be stored or shown, or None.

    ``user_words`` is what the user actually wrote; an escalation word it also contains is the
    user's own and may stay ("pets must be allowed" -> "a cat-friendly landlord seems to be a
    must")."""
    value = str(text or "")
    said = str(user_words or "")
    for key, pattern in _ESCALATION_TERMS:
        if pattern.search(value) and not pattern.search(said):
            return f"escalation:{key}"
    if _ATTRIBUTION_RE.search(value):
        return "attribution"
    return None


def presentable_memories(mems, history=None) -> list:
    """The memory records a prompt may show.

    Rows written before R4-12 were never checked, so a reflection is shown only when its
    wording passes :func:`reflection_rejection` against what the user said. ``history`` (a
    string, or a callable returning one: :meth:`AgentMemory.user_stated_history`) is the user's
    FULL own-message history; it is read only when a reflection fails against the memories
    beside it, so a word the user said in a row that was not retrieved no longer hides a valid
    reflection, and this read-side verdict is the one the purge tool applies (r6-M2)."""
    items = [m for m in (mems or []) if isinstance(m, dict)]
    stated = "\n".join(str(m.get("text") or "") for m in items if m.get("mtype") != "reflection")
    full = None
    out = []
    for m in items:
        if m.get("mtype") != "reflection" or not reflection_rejection(m.get("text"), stated):
            out.append(m)
            continue
        if history is None:
            continue
        if full is None:
            try:
                full = str((history() if callable(history) else history) or "")
            except Exception:
                full = ""
        if not reflection_rejection(m.get("text"), stated + "\n" + full):
            out.append(m)
    return out


def _stated_rows_text(rows) -> str:
    """The user's own messages and saved facts among ``rows`` ((document, metadata) pairs):
    every episodic and semantic row, never a reflection (r6-M2)."""
    return "\n".join(str(document or "") for document, meta in rows
                     if ((meta or {}).get("mtype") in ("episodic", "semantic")))


def rejected_reflections(records) -> list:
    """The reflection rows the R4-12 wording guard rejects, each judged against the FULL
    own-message history of ITS user (every episodic and semantic row of that user in
    ``records``), the same verdict :func:`presentable_memories` gives on read.

    ``records`` is an iterable of ``(id, document, metadata)``. Returns ``[{"id", "user_id",
    "text", "metadata", "reason"}]`` in input order (r6-M2)."""
    rows = [(rid, doc, meta if isinstance(meta, dict) else {}) for rid, doc, meta in records]
    by_user: dict = {}
    for _rid, doc, meta in rows:
        by_user.setdefault(meta.get("user_id"), []).append((doc, meta))
    history = {user: _stated_rows_text(items) for user, items in by_user.items()}
    out = []
    for rid, doc, meta in rows:
        if meta.get("mtype") != "reflection":
            continue
        reason = reflection_rejection(doc, history.get(meta.get("user_id"), ""))
        if reason:
            out.append({"id": rid, "user_id": meta.get("user_id"), "text": doc,
                        "metadata": dict(meta), "reason": reason})
    return out


_RATIONALE_PAREN = re.compile(r"\s*\(([^()]{1,160})\)")
_RATIONALE_TAIL = re.compile(
    r"\s*[,;:–—-]?\s*\b(?:because|since|so\s+that|in\s+order\s+to|as\s+(?:they|she|he|the\s+user)|"
    r"which\s+(?:is|makes)|to\s+(?:live|be|stay|avoid|keep|save|reduce|minimi[sz]e))\b[^.;]{0,200}$",
    re.I)
_RATIONALE_WORD = re.compile(r"[a-z\u4e00-\u9fff]{4,}", re.I)


def strip_unstated_rationale(fact: str, user_msg: str) -> str:
    """``fact`` without a reason/aside the user did not state.

    "Wants no commute (to live very close to their destination)" was stored for a user who
    said only "no commute", and repeated to them as their preference (R2-20). An aside or a
    because/so-that tail is kept only when its words come from the user's message."""
    text = str(fact or "").strip()[:400]
    said = str(user_msg or "").lower()

    def stated(part: str) -> bool:
        words = _RATIONALE_WORD.findall(part.lower())
        return bool(words) and all(word in said or word.rstrip("s") in said for word in words)

    text = _RATIONALE_PAREN.sub(lambda m: m.group(0) if stated(m.group(1)) else "", text)
    tail = _RATIONALE_TAIL.search(text)
    if tail and not stated(tail.group(0)):
        text = text[:tail.start()]
    return text.strip(" \t,;:–—-")


def contains_sensitive(text: str) -> bool:
    """True when an extracted fact or insight states a category auto-memory never keeps."""
    return bool(redact_sensitive(text)[1])


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
    categories.extend(c for c in redact_sensitive(text)[1] if c not in categories)
    return ",".join(categories) or "none"


class AgentMemory:
    # The turn a background write belongs to, for UPDATE provenance (per thread).
    _turn_local = threading.local()

    def __init__(self, db_path: str = _DB_PATH):
        self.col = SQLiteMemoryCollection(db_path)
        self._lock = threading.Lock()
        self._accum = {}  # (user_id, session_id) -> accrued importance (reflection gate)
        # Report of the most recent targeted erasure — a value that is COMPUTED and
        # ASSERTED ON (tests pin it), not merely logged.
        self.last_erasure_report = None

    # ------------------------------------------------------------------ writing
    def add(self, text, mtype, session_id="default", user_id=None,
            role="", importance=None, idempotency_key=None,
            extra_meta=None) -> str | None:
        """Store one memory record; honour a user's DELETE instruction cross-layer.

        The erasure hook fires ONLY for the user's own message as recorded by the
        auto-episodic turn log (``mtype == "episodic" and role == "user"``). That is
        deliberate and is the narrowest reachable surface:

          * ``remember_turn`` writes that record from the CURRENT USER MESSAGE, which is
            untainted by construction (the same premise taint policy A+ rule 2 rests on),
            and the benchmark replays prior user turns the same way;
          * the model-initiated ``remember`` tool and ``_consolidate`` both write
            ``semantic``, so neither can reach the destructive path — a scraped listing or
            an LLM-extracted fact can never delete a user's stored data.

        Nothing in the taint/authorization model is relaxed: this adds a gate, it removes
        none. ``memory_write_allowed`` still governs every write exactly as before.
        """
        mem_id = self._add_record(text, mtype, session_id=session_id, user_id=user_id,
                                  role=role, importance=importance,
                                  idempotency_key=idempotency_key, extra_meta=extra_meta)
        uid = _valid_user_id(user_id)
        if uid is None or mtype != "episodic" or role != "user":
            return mem_id
        fields = erasure_request_fields(text)
        if not fields:
            return mem_id
        report = self.forget_fact(uid, fields)
        # Truthful return: if the instruction itself carried the value ("forget my £1400
        # budget") the scrub erased this very record, so claiming it was stored would be
        # the same lie G9 punished.
        if mem_id and mem_id in report.get("deleted_ids", ()):
            return None
        return mem_id

    def forget_fact(self, user_id: str, fields, session_id: str = "default") -> dict:
        """Erase one or more FIELDS' stated values for one user across EVERY layer.

        This is a FULL SCRUB, not a truthful-partial: ``retrieve()`` reads one collection
        with a single ``user_id`` filter and no mtype restriction, so any record left
        behind is a record ``recall_memory`` can hand back. Retaining the episodic text
        "for audit" while promising the user it was deleted is precisely the G9 bug, and
        the only enforcement that survives contact with the MCP subprocess, the WHAT I
        REMEMBER block and the recall tool alike is deleting the row.

        Value-FREE mentions are retained on purpose — the user's own deletion request
        ("forget my budget entirely") names the field but leaks nothing, and keeping it
        is what lets the next turn answer "there is no budget on file" instead of
        guessing. They are counted in ``retained_mentions`` so the retention is stated,
        never implied.

        Returns a report: ``{"user_id", "fields", "deleted", "deleted_ids", "by_layer",
        "retained_mentions", "residual_ids", "complete"}``. ``complete`` is the result of
        RE-READING the store after the delete and confirming no value-bearing record for
        those fields survives — a source guard, not a promise. A False ``complete`` is the
        honest partial-deletion signal for the caller to surface.
        """
        empty = {"user_id": None, "fields": (), "deleted": 0, "deleted_ids": (),
                 "by_layer": {}, "retained_mentions": 0, "residual_ids": (),
                 "legacy_residual_ids": (), "complete": True}
        uid = _valid_user_id(user_id)
        if uid is None:
            # Same fail-closed identity gate as every other read/write path.
            out = dict(empty, complete=False)
            self.last_erasure_report = out
            return out
        wanted = tuple(f for f in (fields or ()) if f in _FIELD_TERMS)
        if not wanted:
            out = dict(empty, user_id=uid)
            self.last_erasure_report = out
            return out

        def _scan():
            got = self.col.get(where={"user_id": uid})
            ids = list(got.get("ids") or [])
            docs = list(got.get("documents") or [])
            metas = list(got.get("metadatas") or [])
            doomed, by_layer, retained = [], {}, 0
            for i, rid in enumerate(ids):
                doc = docs[i] if i < len(docs) else ""
                mtype = ((metas[i] if i < len(metas) else None) or {}).get("mtype", "")
                if any(_states_field_value(doc, f, mtype) for f in wanted):
                    doomed.append(rid)
                    by_layer[mtype or "unknown"] = by_layer.get(mtype or "unknown", 0) + 1
                elif any(_mentions_field(doc, f) for f in wanted):
                    retained += 1
            return doomed, by_layer, retained

        deleted_ids, by_layer, retained, residual = [], {}, 0, []
        legacy_residual = []
        with self._lock:
            try:
                deleted_ids, by_layer, retained = _scan()
                if deleted_ids:
                    self.col.delete(ids=deleted_ids)
                # Verification pass: assert the scrub actually happened.
                residual, _, retained = _scan()
                duplicate = self.col.legacy_residual_records(
                    where={"user_id": uid}
                )
                legacy_residual = [
                    record_id
                    for record_id, document, meta in zip(
                        duplicate.get("ids") or [],
                        duplicate.get("documents") or [],
                        duplicate.get("metadatas") or [],
                    )
                    if any(
                        _states_field_value(document, field, (meta or {}).get("mtype", ""))
                        for field in wanted
                    )
                ]
            except Exception as e:
                print(f"[memory] forget_fact error_type={type(e).__name__}")
                residual = residual or ["<unverified>"]
        report = {
            "user_id": uid, "fields": wanted, "deleted": len(deleted_ids),
            "deleted_ids": tuple(deleted_ids), "by_layer": by_layer,
            "retained_mentions": retained,
            "residual_ids": tuple(residual),
            "legacy_residual_ids": tuple(legacy_residual),
            "complete": not residual and not legacy_residual,
        }
        self.last_erasure_report = report
        if residual or legacy_residual:
            print(f"[memory] forget_fact INCOMPLETE user_id_chars={len(uid)} "
                  f"field_count={len(wanted)} residual_count={len(residual)} "
                  f"legacy_residual_count={len(legacy_residual)}")
        else:
            print(f"[memory] forget_fact user_id_chars={len(uid)} field_count={len(wanted)} "
                  f"deleted={len(deleted_ids)} layer_count={len(by_layer)} "
                  f"retained_value_free_mentions={retained}")
        return report

    def _add_record(self, text, mtype, session_id="default", user_id=None,
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
            # Stable identifiers make the read-then-write idempotency check safe
            # across the legacy/fc processes. If both pools race, SQLite accepts one
            # INSERT and the loser reads back the same record below.
            if idempotency_key:
                identity = hashlib.sha256(
                    f"{user_id}|{idempotency_key}".encode("utf-8")
                ).hexdigest()
                mem_id = f"idem_{identity}"
            else:
                mem_id = f"{mtype}_{h}"
            meta = {
                "mtype": mtype, "session_id": session_id, "user_id": user_id,
                "role": role, "importance": int(importance),
                "created_at": _now_iso(), "last_access": _now_iso(), "hash": h,
                "pii_categories": _classify_pii(text),
            }
            if idempotency_key:
                meta["idempotency_key"] = idempotency_key
            # Optional provenance (e.g. branch scoping). Memory metadata values must
            # be str/int/float/bool — never None — so absent keys are simply omitted.
            if extra_meta:
                for k, v in extra_meta.items():
                    if v is None:
                        continue
                    meta[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
            try:
                self.col.add(documents=[text], metadatas=[meta], ids=[mem_id])
            except Exception as e:
                try:
                    predicate = (
                        {"$and": [
                            {"idempotency_key": idempotency_key},
                            {"user_id": user_id},
                        ]}
                        if idempotency_key
                        else {"$and": [{"hash": h}, {"user_id": user_id}]}
                    )
                    winner = self.col.get(where=predicate)
                    if winner.get("ids"):
                        return winner["ids"][0]
                except Exception:
                    pass
                print(f"[memory] add error_type={type(e).__name__}")
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

    def _extract_facts(self, user_msg, assistant_msg="", changes=None) -> list:
        """Durable facts the USER stated, from the user's own message only.

        ``assistant_msg`` is accepted for call compatibility and never read: an answer to
        "What do you remember about me?" restates memory, and extracting from it wrote the
        recall (and the reflections it quoted) back as new semantic "facts" — "Pet-
        friendliness is a non-negotiable dealbreaker for the user" — which the next search
        then applied (R2-06)."""
        prompt = (
            "You extract durable facts that the USER STATED about themselves in this message to "
            'a rental assistant. Return STRICT JSON: {"facts": ["..."]}. Each fact is an atomic, '
            "self-contained statement of the user's own preference, constraint, or situation "
            "(budget, destination/university, commute limit, must-have amenities, areas to avoid, "
            "who they live with, etc.), worded as close to the user's words as possible. Never add "
            "a reason, priority or rationale the user did not say (no 'to live close to work', "
            "no 'dealbreaker', no 'key requirement'). Do NOT include greetings, questions, or "
            'chit-chat. If the user stated nothing durable, return {"facts": []}.\n\n'
            + (_resolved_criteria_block(changes) if changes else "")
            + f"User: {user_msg}\n\nJSON: "
        )
        try:
            data = _extract_json(call_ollama(prompt, timeout=60) or "")
            facts = data.get("facts", []) if isinstance(data, dict) else []
            # A fact the extractor inferred despite the redaction ("user has asthma",
            # "on a work visa") is dropped, never stored (A-09).
            out = []
            for fact in facts:
                if not isinstance(fact, str) or not fact.strip() or contains_sensitive(fact):
                    continue
                cleaned = strip_unstated_rationale(fact, user_msg)
                if cleaned:
                    out.append(cleaned)
            return out[:8]
        except Exception:
            return []

    def _consolidate(self, facts, session_id, user_id, changes=None, user_msg=""):
        if not facts:
            return
        existing, seen, protected = [], set(), set()
        texts = {}
        try:
            res = self.col.query(
                query_texts=facts, n_results=4,
                where={"$and": [{"user_id": user_id}, {"mtype": "semantic"}]},
            )
            metas_by_query = res.get("metadatas") or [[] for _ in (res.get("ids") or [])]
            for docs, ids, metas in zip(res.get("documents") or [], res.get("ids") or [],
                                        metas_by_query):
                for position, (d, i) in enumerate(zip(docs or [], ids or [])):
                    meta = ((metas or [])[position] if position < len(metas or []) else None) or {}
                    if i not in seen:
                        seen.add(i)
                        texts[i] = d
                        explicit = meta.get("source") == SOURCE_EXPLICIT
                        existing.append({"id": i, "text": d,
                                         "kind": "saved" if explicit else "auto"})
                        # Never UPDATEd or DELETEd by this job: an explicitly saved row, and
                        # under a criteria change any row that is not the user's own
                        # auto-extracted search criterion (R4-02).
                        if explicit or (changes and (meta.get("source") != SOURCE_AUTO
                                                     or not _criteria_row(d))):
                            protected.add(i)
        except Exception:
            pass
        prompt = (
            "You manage a user-profile memory. Given NEW candidate facts and the EXISTING memories, "
            'decide the operations. Return STRICT JSON: {"ops": [{"event": "ADD|UPDATE|DELETE|NOOP", '
            '"id": "<existing id or null>", "text": "<final memory text>"}]}. '
            "ADD a genuinely new fact; UPDATE (keep id) when a fact refines/replaces an existing one; "
            "DELETE (give id) when a new fact contradicts an existing one; NOOP if already known.\n\n"
            + (("SEARCH CRITERIA CHANGED THIS TURN (computed by the system from the user's "
                "messages; not a fact by itself):\n" + _render_changes(changes) + "\n"
                "An existing memory that states the OLD value of a changed criterion is "
                "superseded: UPDATE it to the new value or DELETE it. Never keep both values. "
                "This applies only to memories of kind \"auto\" about the user's own search; "
                "never change a memory of kind \"saved\" or a fact about another person "
                "(guarantor, partner, child, landlord).\n\n")
               if changes else "")
            + f"EXISTING:\n{json.dumps(existing, ensure_ascii=False)}\n\n"
            f"NEW FACTS:\n{json.dumps(facts, ensure_ascii=False)}\n\nJSON: "
        )
        turn_ref = getattr(self._turn_local, "turn_id", None)
        try:
            data = _extract_json(call_ollama(prompt, timeout=60) or "") or {}
            for op in data.get("ops", []):
                ev = (op.get("event") or "").upper()
                text = (op.get("text") or "").strip()
                oid = op.get("id")
                if ev in ("UPDATE", "DELETE") and oid in protected:
                    # A protected row stays as it is. A genuinely NEW fact the model folded
                    # into an UPDATE of it is still added beside it, verbatim (R4-02).
                    if ev == "UPDATE" and text in facts:
                        self.add(text, "semantic", session_id, user_id, importance=7,
                                 extra_meta={"source": SOURCE_AUTO})
                    continue
                loosened = None
                if ev in ("UPDATE", "DELETE") and oid in texts:
                    loosened = _loosened_bound(texts[oid], text if ev == "UPDATE" else "")
                if loosened and not _firm_bound_stated(
                        user_msg, loosened,
                        _row_bounds(text).get(loosened) if ev == "UPDATE" else None):
                    # A stored ceiling is never loosened by a conditional statement ("could
                    # stretch to £2,100 for the right place"): the row stays, and the user's own
                    # statement of the stretch is added beside it (stress A-08).
                    print(f"[memory] consolidate kept_bound kind={loosened} event={ev}")
                    for fact in facts:
                        if loosened in _row_bounds(fact):
                            self.add(fact, "semantic", session_id, user_id, importance=7,
                                     extra_meta={"source": SOURCE_AUTO})
                    continue
                if ev == "ADD" and text:
                    self.add(text, "semantic", session_id, user_id, importance=7,
                             extra_meta={"source": SOURCE_AUTO})
                elif ev == "UPDATE" and oid and text:
                    update_failed = not self._update_semantic(oid, text, turn_ref)
                    if update_failed:
                        # add() acquires self._lock itself. Run the fallback only after
                        # releasing the non-reentrant lock above, otherwise an UPDATE
                        # failure deadlocks the durable outbox worker forever.
                        self.add(text, "semantic", session_id, user_id, importance=7,
                                 extra_meta={"source": SOURCE_AUTO})
                elif ev == "DELETE" and oid:
                    with self._lock:
                        try:
                            self.col.delete(ids=[oid])
                        except Exception:
                            pass
        except Exception as e:
            print(f"[memory] consolidate error_type={type(e).__name__}")

    def _update_semantic(self, oid, text, turn_ref=None) -> bool:
        """Rewrite one semantic row in place, recording WHEN and by which turn.

        An UPDATE used to keep only the original ``created_at``, so a turn-2 correction
        read as turn 1 holding turn 2's values (R3-02)."""
        with self._lock:
            try:
                meta = {}
                try:
                    current = self.col.get(ids=[oid])
                    meta = dict(((current.get("metadatas") or [None])[0]) or {})
                except Exception:
                    meta = {}
                meta["updated_at"] = _now_iso()
                if turn_ref:
                    meta["updated_turn_id"] = str(turn_ref)[:80]
                if meta.get("hash") is not None and meta.get("user_id"):
                    # The content hash follows the text, as ``add`` computes it, so the
                    # superseded wording can be added again later if the user says it.
                    meta["hash"] = hashlib.md5(
                        f"{meta.get('mtype') or 'semantic'}|{meta['user_id']}|{text}"
                        .encode("utf-8")).hexdigest()
                if meta.get("user_id"):
                    self.col.update(ids=[oid], documents=[text], metadatas=[meta])
                else:
                    # The current metadata could not be read: never replace it with a
                    # partial dict (that would drop user_id and orphan the row).
                    self.col.update(ids=[oid], documents=[text])
                return True
            except Exception:
                return False

    def _supersede_slot_facts(self, changes, user_id, turn_ref=None) -> int:
        """Rewrite semantic rows that state a criterion this turn replaced (R3-02).

        "Make that Poplar, budget £1,500" after "a studio in Whitechapel under £1,700" left
        'Find a studio in Whitechapel' standing beside a new 'Poplar', and the next
        conversation recalled a studio in Whitechapel at £1,500 — a combination the user
        never asked for. The change itself is deterministic (the criteria state), so the
        old value is replaced here, before the consolidator runs."""
        if not changes:
            return 0
        try:
            rows = self.col.get(where={"$and": [{"user_id": user_id}, {"mtype": "semantic"}]})
        except Exception:
            return 0
        rewritten = 0
        metas = rows.get("metadatas") or []
        for index, (oid, doc) in enumerate(zip(rows.get("ids") or [], rows.get("documents") or [])):
            meta = (metas[index] if index < len(metas) else None) or {}
            # Only an auto-extracted row about the user's own search (R4-02): never an
            # explicitly saved fact, a legacy row of unknown origin, or a third party's fact.
            if meta.get("source") != SOURCE_AUTO or not _criteria_row(doc):
                continue
            new_text = _superseded_text(doc, changes)
            if new_text and self._update_semantic(oid, new_text, turn_ref):
                rewritten += 1
        return rewritten

    def remember_turn(self, user_msg, assistant_msg, session_id="default",
                      user_id=None, tool_used=None, idempotency_key=None,
                      conversation_id=None, turn_id=None, turn_started_at=None,
                      context_tainted=False, criteria_changes=None, memory_withheld=None):
        """After-turn entry point (run this in the background — it makes LLM calls).

        ``memory_withheld`` is the turn's conversation-memory decision, taken when the turn
        started (:func:`apply_conversation_memory_turn`): True keeps nothing from it. The
        caller that took it also stored the conversation's pause flag; a background job may
        run after a LATER turn lifted the pause, so this job never re-reads or changes the flag.
        ``None`` (a direct caller) applies and stores the flag here (r6-M1).

        Branch scoping: conversation_id / turn_id / turn_started_at are recorded on
        the auto-EPISODIC turn log only, so retrieve() can restrict a forked
        conversation to the episodic memories it actually inherited. They are NOT
        attached to the distilled semantic/reflection records, which stay GLOBAL
        (per-user, all branches). Absent (None) values are omitted from metadata so
        legacy call sites keep behaving exactly as before.

        Taint policy A+ (design §2.8c): when ``context_tainted`` is True this turn
        carried untrusted (e.g. scraped listing) content, so the auto-memory bypass
        is hardened — semantic facts are extracted from the USER message ONLY;
        assistant/tool output never reaches the extractor, and therefore never
        enters the semantic or (downstream) reflection layers. The episodic layer
        still stores the raw user message (untainted by construction). clean
        sessions (``context_tainted=False``) are unchanged: user + assistant are
        both fed to extraction.
        """
        user_id = _valid_user_id(user_id)
        if user_id is None:
            print("[memory] remember_turn rejected: missing/shared user_id")
            return False
        try:
            if idempotency_key:
                existing = self.col.get(where={"$and": [
                    {"idempotency_key": idempotency_key}, {"user_id": user_id}]})
                if existing and existing.get("ids"):
                    return True
            if memory_withheld is None:
                memory_withheld = apply_conversation_memory_turn(
                    user_id, conversation_id, user_msg)["withhold"]
            # A turn in a conversation the user paused memory for is kept like a turn that asked
            # not to be kept: no content, no extraction, no consolidation, no reflection (r6-M1).
            opted_out = bool(memory_withheld) or do_not_remember_requested(user_msg)
            # Sensitive categories are redacted BEFORE anything is written or extracted
            # (A-09): the episodic log, the fact extractor and the reflections built on
            # them all see the redacted text only.
            user_msg, _sensitive = redact_sensitive(user_msg)
            assistant_msg, _ = redact_sensitive(assistant_msg)
            ep = f"User asked: {(user_msg or '').strip()[:300]}"
            if opted_out:
                # The user asked for this message not to be kept: the log records that a turn
                # happened, not what it said, and nothing is extracted from it. A field it names
                # ("don't remember my budget") is still erased across layers, as add() would.
                fields = erasure_request_fields(user_msg)
                if fields:
                    self.forget_fact(user_id, fields)
                ep = _OPTED_OUT_LOG
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
            # The turn's criteria CHANGES (system state, labelled, never assistant prose)
            # let an elliptical correction ("Make that Poplar") supersede the old value
            # instead of being stored beside it (R3-02).
            # Only a change the user's message states as their own criterion supersedes what
            # memory holds (stress A-08: "my friend lives in Peckham" is not a new area).
            changes = user_stated_changes(_sanitize_changes(criteria_changes), user_msg)
            self._turn_local.turn_id = turn_id
            try:
                if changes and not opted_out:
                    self._supersede_slot_facts(changes, user_id, turn_id)
                if not opted_out and not _is_trivial_for_extraction(user_msg):
                    # Semantic facts come from the USER message only, on every turn (R2-06):
                    # the assistant's text — a recall answer, a reflection it quoted, a
                    # suggestion — is never a statement by the user. (Taint A+ asked for this
                    # on tainted turns only; it now holds everywhere.)
                    if changes:
                        self._consolidate(self._extract_facts(user_msg, "", changes),
                                          session_id, user_id, changes, user_msg=user_msg)
                    else:
                        self._consolidate(self._extract_facts(user_msg, ""), session_id, user_id,
                                          user_msg=user_msg)
            finally:
                self._turn_local.turn_id = None
            if not opted_out:
                # A withheld turn triggers no reflection: nothing new is inferred about a user
                # while they asked not to be remembered (r6-M1).
                self.maybe_reflect(session_id, user_id)
            return True
        except Exception as e:
            print(f"[memory] remember_turn error_type={type(e).__name__}")
            return False

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
            print(f"[memory] episodic cap error_type={type(e).__name__}")

    def remember_turn_async(self, *args, **kwargs):
        # A bare `threading.Thread(target=...)` starts in a FRESH, EMPTY context, so
        # every provider call `remember_turn` makes (fact extraction, consolidation)
        # logged `request_id: "-"` and belonged to no turn. asyncio never had this
        # problem — `create_task`/`run_coroutine_threadsafe` capture `copy_context()`
        # — which is why only the thread/executor spawn sites were unattributable.
        # `spawn_observed_thread` makes the thread boundary behave like an await
        # boundary; it starts the thread, so this stays one call.
        from uk_rent_agent.observability import spawn_observed_thread

        spawn_observed_thread(self.remember_turn, args=args, kwargs=kwargs)

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
            # What the user actually wrote: the turn log of their own messages (R4-12).
            user_words = "\n".join(
                str(docs[i]) for i in range(len(docs))
                if ((metas[i] if i < len(metas) else None) or {}).get("mtype") == "episodic"
                and ((metas[i] if i < len(metas) else None) or {}).get("role") == "user")
            # Reflections are built from what the user said and asked, never from earlier
            # reflections: feeding them back escalated "pet-friendliness is a key
            # requirement" to "a non-negotiable dealbreaker" in two minutes (R2-06). A turn the
            # user asked not to be kept is a placeholder with nothing to reflect on (r6-M1).
            own = [i for i in range(len(docs))
                   if ((metas[i] if i < len(metas) else None) or {}).get("mtype") != "reflection"
                   and not str(docs[i] or "").startswith(_OPTED_OUT_LOG)]
            docs = [docs[i] for i in own]
            metas = [(metas[i] if i < len(metas) else None) or {} for i in own]
            if len(docs) < 4:
                return
            # Collection .get() has NO ordering guarantee, so "recent" must be derived
            # explicitly: sort by created_at and take the newest REFLECT_CORPUS_SIZE.
            order = sorted(
                range(len(docs)),
                key=lambda i: (metas[i] or {}).get("created_at", "") if i < len(metas) else "",
            )
            newest = order[-REFLECT_CORPUS_SIZE:]
            corpus = "\n".join(f"- {docs[i]}" for i in newest)
            # R4-12: an insight is the assistant's tentative guess. The prompt asks for that
            # wording; reflection_rejection enforces it, because the prompt alone did not.
            prompt = (
                "Given the following memories about a user's housing search, write 1-3 short, "
                "TENTATIVE observations about what this user may want (patterns, trade-offs). They "
                "are your own guesses, not things the user said, so word each one as a guess "
                "(\"seems to\", \"may\", \"probably\"). Never call anything a requirement, a "
                "priority or a limit the user set; never use the words non-negotiable, deal-breaker, "
                "must, firm, strict, essential, hard constraint or top priority, and never call a "
                "stated limit flexible, negotiable or stretchable, unless the user used that word "
                "themselves. Never write that the user said, stated, insisted on or asked for "
                "something. Keep every number exactly as the user gave it, and never combine an "
                'older value with a newer one. Return STRICT JSON: {"insights": ["..."]}.\n\n'
                f"{corpus}\n\nJSON: "
            )
            data = _extract_json(call_ollama(prompt, timeout=60) or "") or {}
            rejected = 0
            for ins in (data.get("insights") or [])[:3]:
                if (isinstance(ins, str) and ins.strip() and not contains_sensitive(ins)
                        and not _SENSITIVE_INFERENCE_RE.search(ins)):
                    if reflection_rejection(ins, user_words):
                        rejected += 1
                        continue
                    self.add(ins.strip(), "reflection", session_id, user_id, importance=8)
            if rejected:
                print(f"[memory] reflect rejected_wording={rejected}")
        except Exception as e:
            print(f"[memory] reflect error_type={type(e).__name__}")

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

    def semantic_records(self, *, user_id=None) -> list:
        """Read this user's durable facts without episodic/relevance crowding.

        Explicit restoration needs the saved preference itself. A top-six mixed
        relevance result can consist entirely of reflections and omit that record.
        Semantic facts are user-global; episodic branch records never enter here.
        """
        uid = _valid_user_id(user_id)
        if uid is None:
            return []
        try:
            result = self.col.get(where={'$and': [{'user_id': uid}, {'mtype': 'semantic'}]})
        except Exception:
            return []
        records = []
        for record_id, document, metadata in zip(result.get('ids') or [],
                result.get('documents') or [], result.get('metadatas') or []):
            if not isinstance(metadata, dict) or metadata.get('user_id') != uid or metadata.get('mtype') != 'semantic':
                continue
            records.append({'id': record_id, 'text': document, 'mtype': 'semantic',
                            'created_at': metadata.get('created_at')})
        return sorted(records, key=lambda m: str(m.get('created_at') or ''), reverse=True)

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
                 "importance": t[3].get("importance"), "score": round(t[0], 3),
                 "created_at": t[3].get('created_at')} for t in top]

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

    def user_stated_history(self, user_id) -> str:
        """Everything one user said or saved here: the text of every episodic and semantic row
        of that user (never a reflection). The history a reflection's wording is judged by."""
        uid = _valid_user_id(user_id)
        if uid is None:
            return ""
        try:
            got = self.col.get(where={"user_id": uid})
        except Exception:
            return ""
        return _stated_rows_text(zip(got.get("documents") or [], got.get("metadatas") or []))

    def format_for_prompt(self, mems, saved_total=None, user_id=None) -> str:
        """The memory block. Reflections are the assistant's own INFERENCES and are labelled
        and listed apart: they were recalled as "everything I have saved" and applied as
        search preferences (R2-06/R2-20). They are labelled TENTATIVE, and one whose wording
        escalates or attributes is not shown at all (R4-12); with ``user_id`` it is judged
        against that user's full history, as the purge tool judges it (r6-M2).

        ``saved_total`` (a recall question): how many saved facts the user has, so the block
        says whether it lists all of them. "Here is everything I have stored" listed 19 of 25
        (R4-13)."""
        mems = presentable_memories(
            mems, history=(lambda: self.user_stated_history(user_id)) if user_id else None)
        if not mems:
            return ""
        tag = {"semantic": "FACT", "episodic": "PAST"}
        stated = [f"[{tag.get(m.get('mtype'), 'MEM')}] {m['text']}" for m in mems
                  if m.get("mtype") != "reflection"]
        inferred = [f"[INFERENCE] {m['text']}" for m in mems if m.get("mtype") == "reflection"]
        header = "What I remember about this user:"
        if saved_total is not None:
            shown = sum(1 for m in mems if m.get("mtype") == "semantic")
            header = (f"What I remember about this user (all {saved_total} saved facts are "
                      "listed):" if shown >= saved_total else
                      f"What I remember about this user (showing {shown} of {saved_total} saved "
                      "facts; this list is NOT complete, so never say it is everything you have "
                      "stored):")
        block = header + "\n" + "\n".join(stated) if stated else ""
        if inferred:
            block = (block + "\n" if block else "") + (
                "The assistant's own inferences: TENTATIVE guesses that may be wrong, NOT said or "
                "saved by the user (never apply them as search filters or preferences, never "
                "present them as things the user told you or asked to save, and never describe "
                "them as the user's requirements, limits or priorities):\n" + "\n".join(inferred))
        return block

    def stats(self) -> dict:
        try:
            return {"total": self.col.count()}
        except Exception:
            return {"total": 0}

    def health(self) -> dict:
        """Run the required durable-store and legacy-migration readiness checks."""
        return self.col.health()

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
        # The user's per-conversation memory pauses go with the rest of their memory (r6-M1).
        from core import memory_gate
        memory_gate.forget_conversation_memory_pauses(user_id)
        return len(ids)

    def privacy_inventory(self, user_id: str) -> dict[str, int]:
        """Return non-content residual counts for deletion read-back verification."""
        user_id = (user_id or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        with self._lock:
            existing = self.col.get(where={"user_id": user_id})
            records = len(list(existing.get("ids") or []))
            pending_buffers = sum(1 for key in self._accum if key[0] == user_id)
            # During a rolling migration the old Chroma database is a second physical
            # copy. Tombstoning a row in the new store is not a complete GDPR erasure
            # until that duplicate source has been explicitly retired.
            legacy_records = self.col.legacy_residual_count(where={"user_id": user_id})
        from core import memory_gate
        pause_flags = memory_gate.conversation_memory_pause_count(user_id)
        return {
            "records": records,
            "pending_buffers": pending_buffers,
            "legacy_records": legacy_records,
            "pause_flags": pause_flags,
            "total": records + pending_buffers + legacy_records + pause_flags,
        }


_AGENT_MEMORY = None


def get_agent_memory() -> AgentMemory:
    global _AGENT_MEMORY
    if _AGENT_MEMORY is None:
        try:
            _AGENT_MEMORY = AgentMemory()
        except Exception as exc:
            allow_ephemeral = os.getenv(
                "AGENT_MEMORY_ALLOW_EPHEMERAL_FALLBACK", ""
            ).strip().lower() in {"1", "true", "yes", "on"}
            if not allow_ephemeral:
                raise RuntimeError(
                    "durable agent-memory store is unavailable"
                ) from exc
            fallback_path = os.path.join(
                tempfile.gettempdir(), "uk-rent-agent", "agent_memory"
            )
            print(
                f"[memory] durable store unavailable; error_type={type(exc).__name__}; "
                "explicit_ephemeral_fallback=True"
            )
            _AGENT_MEMORY = AgentMemory(fallback_path)
    return _AGENT_MEMORY
