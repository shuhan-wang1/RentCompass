"""Context assembly + turn-snapshot semantics for the session-fork feature.

This module centralizes the prompt-context construction that ``app/app.py`` did by
hand (see the historical block around lines ~1059-1114) and adds three things on
top of byte-for-byte behavior parity:

  * a token budget with a deterministic trim order,
  * a rolling conversation summary (dependency-injected LLM only),
  * the durable-vs-transient turn-snapshot whitelist used when forking sessions.

Design constraints (enforced):
  * NO network calls and NO import of any LLM / provider module at import time.
    The rolling summary receives its completion function by dependency injection.
  * Pure standard library + typing.

Public API
----------
    CONTEXT_SCHEMA_VERSION
    SnapshotSchemaError
    build_turn_snapshot(*, turn_id, persistent_state, context_revision=0) -> dict
    snapshot_to_session_patch(snapshot) -> dict
    render_recommended_index(registry, max_items=200) -> str
    assemble(*, user_message, history, memory_block="", has_property_context=False,
             rolling_summary=None, token_budget=6000) -> str
    assemble_messages(*, user_message, history, memory_block="", context_block=None,
                      reply_language="en", token_budget=6000) -> list  # BaseMessage list
    estimate_tokens(text) -> int
    update_rolling_summary(llm_complete, prior_summary, folded_turns,
                           reply_language="en") -> str
    should_update_summary(history_len, max_history) -> bool
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional

CONTEXT_SCHEMA_VERSION = 1

# Maximum length (characters) of a rolling summary produced by update_rolling_summary.
_SUMMARY_MAX_CHARS = 1600

# Clarification-answer detection markers — copied verbatim from app/app.py so the
# clarification vs. plain-history branch selection stays identical.
_CLARIFICATION_MARKERS = (
    "what is your",
    "could you tell me",
    "what's the maximum",
    "please provide",
    "how many",
    "which area",
    "?",
)

# Number of history turns each branch pulls today (before any budget trimming).
_CLARIFICATION_TURNS = 5
_HISTORY_TURNS = 3
_MIN_HISTORY_TURNS = 2


class SnapshotSchemaError(Exception):
    """Raised when a turn snapshot carries an unrecognized schema_version.

    The integrator catches this and falls back to the legacy rehydrate path.
    """


# ---------------------------------------------------------------------------
# Snapshot build / apply
# ---------------------------------------------------------------------------

def build_turn_snapshot(*, turn_id: Any, persistent_state: Dict[str, Any],
                        context_revision: int = 0) -> Dict[str, Any]:
    """Build a v1 turn snapshot from ``persistent_state`` using a STRICT WHITELIST.

    Only the durable keys below are ever copied. Transient runtime keys that may
    live in ``extracted_context`` (run_id, request_id, tool_decision,
    tool_observation, loop_turn, observations, task_plan, task_results,
    critic_attempts, verdict, current_message, memory_context, reply_language,
    previous_search_results, comparison_properties, property_* focus keys,
    viewed_properties, ...) are never included — they are rebuilt per request.
    """
    ec = persistent_state.get("extracted_context") or {}
    if not isinstance(ec, dict):
        ec = {}

    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "turn_id": turn_id,
        "user_preferences": deepcopy(persistent_state.get("user_preferences", {})),
        "accumulated_search_criteria": deepcopy(
            persistent_state.get("accumulated_search_criteria", {})),
        "last_results": deepcopy(ec.get("last_results")) or [],
        # 累计推荐注册表：轻量条目（historical recommendations index），随快照存活重启/fork。
        "recommended_registry": deepcopy(ec.get("recommended_registry")) or [],
        "summary": ec.get("rolling_summary") or None,
        "summary_through_turn_id": ec.get("rolling_summary_through_turn_id") or None,
        "open_questions": [],   # reserved for v2
        "active_property": None,  # reserved for v2
        "context_revision": context_revision,
    }


def snapshot_to_session_patch(snapshot: Any) -> Dict[str, Any]:
    """Translate a stored snapshot into a session-state patch.

    Raises :class:`SnapshotSchemaError` on an unknown schema_version (the caller
    then falls back to the legacy rehydrate path). Malformed *inner* content is
    never fatal: each field is sanitized to a safe default because production
    snapshots may predate the current shape.
    """
    if not isinstance(snapshot, dict):
        raise SnapshotSchemaError(
            f"snapshot must be a dict, got {type(snapshot).__name__}")

    version = snapshot.get("schema_version")
    if version != CONTEXT_SCHEMA_VERSION:
        raise SnapshotSchemaError(f"unknown snapshot schema_version: {version!r}")

    user_preferences = snapshot.get("user_preferences")
    if not isinstance(user_preferences, dict):
        user_preferences = {}

    accumulated = snapshot.get("accumulated_search_criteria")
    if not isinstance(accumulated, dict):
        accumulated = {}

    last_results = snapshot.get("last_results")
    if not isinstance(last_results, list):
        last_results = []

    recommended_registry = snapshot.get("recommended_registry")
    if not isinstance(recommended_registry, list):
        recommended_registry = []

    summary = snapshot.get("summary")
    if not (isinstance(summary, str) and summary.strip()):
        summary = None

    summary_through = snapshot.get("summary_through_turn_id")
    if not (isinstance(summary_through, str) and summary_through.strip()):
        summary_through = None

    return {
        "user_preferences": deepcopy(user_preferences),
        "accumulated_search_criteria": deepcopy(accumulated),
        "last_results": deepcopy(last_results),
        "recommended_registry": deepcopy(recommended_registry),
        "rolling_summary": summary,
        "rolling_summary_through_turn_id": summary_through,
    }


# ---------------------------------------------------------------------------
# Recommended-listings index (accumulated registry -> compact prompt block)
# ---------------------------------------------------------------------------

def render_recommended_index(registry: Optional[List[Dict[str, Any]]],
                             max_items: int = 200) -> str:
    """Render the accumulated recommended-listings registry as a COMPACT numbered index
    for the agent prompt — ONE line per listing, SUMMARIES ONLY (address / price / area /
    commute / available-from / URL).

    The block carries an explicit instruction: full details (description, amenities, bills,
    policies) of any listing are NOT inline and MUST be fetched with ``get_property_details``
    using that listing's exact URL. This keeps the whole search history addressable in
    context without ever inlining large per-listing description text. Returns ``''`` for an
    empty/missing registry."""
    if not registry:
        return ""
    lines = [
        "=== RECOMMENDED LISTINGS INDEX (every listing shown so far; summaries only) ===",
        "Each line is a SUMMARY. For a listing's full details (description, amenities, "
        "bills, guest/payment policy) call the get_property_details tool with that "
        "listing's exact URL below. Never invent details that are not shown here.",
    ]
    for e in registry[:max_items]:
        if not isinstance(e, dict):
            continue
        idx = e.get("index", "?")
        addr = str(e.get("address") or "Unknown").strip()
        seg = [f"[{idx}] {addr}"]
        if e.get("price") not in (None, "", "N/A"):
            seg.append(f"price {e['price']}")
        if e.get("area"):
            seg.append(f"area {e['area']}")
        if e.get("travel_time") not in (None, "", "N/A"):
            seg.append(f"commute {e['travel_time']}")
        if e.get("available_from"):
            seg.append(f"available {e['available_from']}")
        line = " | ".join(seg)
        url = str(e.get("url") or "").strip()
        if url:
            line += f" | {url}"
        lines.append(line)
    lines.append("=== END RECOMMENDED LISTINGS INDEX ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _is_cjk(ch: str) -> bool:
    """True for CJK ideographs, kana, Hangul, and CJK/fullwidth punctuation.

    CJK glyphs carry roughly one token each, unlike Latin text (~4 chars/token),
    so they are counted individually by :func:`estimate_tokens`.
    """
    o = ord(ch)
    return (
        0x3000 <= o <= 0x303F      # CJK symbols & punctuation
        or 0x3040 <= o <= 0x30FF   # Hiragana + Katakana
        or 0x3400 <= o <= 0x4DBF   # CJK Ext A
        or 0x4E00 <= o <= 0x9FFF   # CJK Unified Ideographs
        or 0xAC00 <= o <= 0xD7AF   # Hangul syllables
        or 0xF900 <= o <= 0xFAFF   # CJK compatibility ideographs
        or 0xFF00 <= o <= 0xFFEF   # Fullwidth / halfwidth forms
        or 0x20000 <= o <= 0x2A6DF  # CJK Ext B
    )


def estimate_tokens(text: str) -> int:
    """Rough token count: 1 per CJK char + ceil(other chars / 4)."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


def _truncate_lines_to_cap(text: str, token_cap: float) -> str:
    """Keep whole lines from the START, cutting whole lines off the END, until
    the retained block fits under ``token_cap`` tokens."""
    if estimate_tokens(text) <= token_cap:
        return text
    lines = text.split("\n")
    kept: List[str] = []
    for line in lines:
        candidate = "\n".join(kept + [line])
        if estimate_tokens(candidate) <= token_cap:
            kept.append(line)
        else:
            break
    return "\n".join(kept)


def _truncate_chars_to_cap(text: str, token_cap: float) -> str:
    """Keep the longest character prefix of ``text`` under ``token_cap`` tokens."""
    if estimate_tokens(text) <= token_cap:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= token_cap:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def assemble(*, user_message: str, history: Optional[List[Dict[str, str]]],
             memory_block: str = "", has_property_context: bool = False,
             rolling_summary: Optional[str] = None,
             token_budget: int = 6000) -> str:
    """Build the query string handed to the agent graph.

    ``history`` is the SessionStore shape: ``[{"user": str, "assistant": str}, ...]``.

    Behavior parity with today's app.py (when nothing trims):
      * ``has_property_context`` → base query is just ``user_message``.
      * else if ``history`` and the last assistant reply looks like a clarification
        question AND the user's reply is <= 5 words → 5-turn "clarification" wrapper.
      * else if ``history`` → 3-turn "Previous conversation" wrapper.
      * ``memory_block`` (when non-empty) is prefixed as ``f"{memory_block}\\n\\n{query}"``.
      * ``rolling_summary`` (when set AND history context is included) is inserted as
        ``"Earlier conversation summary:\\n{summary}"`` between memory and history.

    Token budget: if the assembled string exceeds ``token_budget`` tokens, trim in
    order — (1) reduce history turns down to a floor of 2; (2) cap memory_block at
    25% of budget (whole lines from the end); (3) cap rolling_summary at 20%;
    (4) drop memory_block, then summary, entirely. ``user_message`` is never trimmed.
    """
    memory_block = memory_block or ""
    history = history or []
    summary = rolling_summary if (rolling_summary and str(rolling_summary).strip()) else None

    # ── Branch selection (mirrors app.py) ──────────────────────────────────
    if has_property_context:
        include_history = False
        mode = "property"
        initial_turns = 0
    elif history:
        last = history[-1] if isinstance(history[-1], dict) else {}
        last_response = last.get("assistant", "") or ""
        is_clarification = (
            any(q in last_response.lower() for q in _CLARIFICATION_MARKERS)
            and len(user_message.split()) <= 5
        )
        include_history = True
        if is_clarification:
            mode = "clarification"
            initial_turns = _CLARIFICATION_TURNS
        else:
            mode = "history"
            initial_turns = _HISTORY_TURNS
    else:
        include_history = False
        mode = "plain"
        initial_turns = 0

    def build_history_query(n_turns: int) -> str:
        if not include_history:
            return user_message
        turns = history[-n_turns:] if n_turns > 0 else []
        history_text = "\n".join(
            f"User: {h.get('user', '')}\nAlex: {h.get('assistant', '')}"
            for h in turns
        )
        if mode == "clarification":
            return (
                "Previous conversation (IMPORTANT - user is answering a "
                "clarification question):\n"
                f"{history_text}\n\n"
                f"User's answer to the clarification question: {user_message}\n\n"
                "INSTRUCTIONS: The user just answered your clarification question. "
                "Use their answer to complete the ORIGINAL request. Do NOT ask more "
                "questions about the same thing. Do NOT treat their answer as a "
                "confusing new command."
            )
        return (
            "Previous conversation:\n"
            f"{history_text}\n\n"
            f"Current user message: {user_message}"
        )

    def compose(n_turns: int, mem: str, summ: Optional[str]) -> str:
        out = build_history_query(n_turns)
        if summ and include_history:
            out = f"Earlier conversation summary:\n{summ}\n\n{out}"
        if mem:
            out = f"{mem}\n\n{out}"
        return out

    n_turns = initial_turns
    mem = memory_block
    summ = summary

    result = compose(n_turns, mem, summ)
    if estimate_tokens(result) <= token_budget:
        return result

    # (1) reduce history turns one at a time down to a floor of 2.
    while include_history and n_turns > _MIN_HISTORY_TURNS:
        n_turns -= 1
        result = compose(n_turns, mem, summ)
        if estimate_tokens(result) <= token_budget:
            return result

    # (2) cap memory_block at 25% of budget, cutting whole lines from the END.
    if mem:
        mem = _truncate_lines_to_cap(mem, token_budget * 0.25)
        result = compose(n_turns, mem, summ)
        if estimate_tokens(result) <= token_budget:
            return result

    # (3) cap rolling_summary at 20% of budget.
    if summ and include_history:
        summ = _truncate_chars_to_cap(summ, token_budget * 0.20)
        result = compose(n_turns, mem, summ)
        if estimate_tokens(result) <= token_budget:
            return result

    # (4) drop memory_block, then summary, entirely. Never trim user_message.
    if mem:
        mem = ""
        result = compose(n_turns, mem, summ)
        if estimate_tokens(result) <= token_budget:
            return result
    if summ:
        summ = None
        result = compose(n_turns, mem, summ)

    return result


# ---------------------------------------------------------------------------
# Message-array assembly (native function-calling loop) — §2.7
# ---------------------------------------------------------------------------

def assemble_messages(*, user_message: str,
                      history: Optional[List[Dict[str, str]]],
                      memory_block: str = "",
                      context_block: Optional[Dict[str, Any]] = None,
                      reply_language: str = "en",
                      token_budget: int = 6000) -> list:
    """Build the message array handed to the native function-calling agent loop.

    This is the message-granularity sibling of :func:`assemble` (which returns a single
    concatenated string for the legacy path). It returns a list of ``langchain_core``
    BaseMessage objects in this fixed order (design §2.7):

      1. SystemMessage — identity/capability boundary + SECURITY_DIRECTIVE +
         reply-language directive + behaviour rules. NEVER trimmed.
      2. SystemMessage — the context block (accumulated criteria | focused property |
         last-results digest | recommendations index | memory). OMITTED when empty.
      3. History turns as alternating HumanMessage / AIMessage from the SessionStore
         shape ``[{"user": str, "assistant": str}, ...]``.
      4. HumanMessage — the current ``user_message`` VERBATIM (no prefix concatenation;
         killing the legacy string-wrapper pattern is the point of this rewrite).

    ``context_block`` keys (all optional): ``accumulated_criteria`` (dict),
    ``focused_property`` (dict — focus-stack top record), ``last_results`` (list of
    listing dicts), ``recommendations_index`` (list — cumulative registry entries).

    Token budget: the :func:`assemble` trimming ladder ported to message granularity —
    (1) drop oldest history turns down to a floor of 2; (2) cap ``memory_block`` at 25%
    of budget (whole lines from the end); (3) cap the context sections to the remaining
    budget. The system directives (message #1) and the current ``user_message`` are
    never trimmed.
    """
    # Lazy imports: keeps context_assembler import-time free of LLM/provider modules
    # (langchain_core.messages is a light message-class module; loop_prompts pulls the
    # security/language directives from langgraph_agent only when called).
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from core import loop_prompts

    history = history or []
    memory_block = memory_block or ""
    ctx = context_block or {}

    system_directive = loop_prompts.build_system_directive(reply_language)
    context_sections = loop_prompts.build_context_sections(
        accumulated_criteria=ctx.get("accumulated_criteria"),
        focused_property=ctx.get("focused_property"),
        last_results=ctx.get("last_results"),
        recommendations_index=ctx.get("recommendations_index"),
    )

    def build(n_turns: int, mem: str, sections: str) -> list:
        msgs: list = [SystemMessage(content=system_directive)]
        context_msg = loop_prompts.compose_context_message(sections, mem)
        if context_msg:
            msgs.append(SystemMessage(content=context_msg))
        turns = history[-n_turns:] if n_turns > 0 else []
        for h in turns:
            if not isinstance(h, dict):
                continue
            user_text = (h.get("user") or "").strip()
            assistant_text = (h.get("assistant") or "").strip()
            if user_text:
                msgs.append(HumanMessage(content=user_text))
            if assistant_text:
                msgs.append(AIMessage(content=assistant_text))
        # Current message VERBATIM — never a wrapper, never trimmed.
        msgs.append(HumanMessage(content=user_message))
        return msgs

    def total_tokens(msgs: list) -> int:
        return sum(estimate_tokens(m.content or "") for m in msgs)

    n_turns = len(history)
    mem = memory_block
    sections = context_sections

    msgs = build(n_turns, mem, sections)
    if total_tokens(msgs) <= token_budget:
        return msgs

    # (1) drop oldest history turns down to a floor of 2.
    while n_turns > _MIN_HISTORY_TURNS:
        n_turns -= 1
        msgs = build(n_turns, mem, sections)
        if total_tokens(msgs) <= token_budget:
            return msgs

    # (2) cap memory_block at 25% of budget (whole lines from the END).
    if mem:
        mem = _truncate_lines_to_cap(mem, token_budget * 0.25)
        msgs = build(n_turns, mem, sections)
        if total_tokens(msgs) <= token_budget:
            return msgs

    # (3) cap the context sections to whatever budget the never-trimmed parts leave.
    if sections:
        without_sections = total_tokens(build(n_turns, mem, ""))
        remaining = max(int(token_budget - without_sections), 0)
        sections = _truncate_lines_to_cap(sections, remaining)
        msgs = build(n_turns, mem, sections)

    # Best effort: message #1 and the current user_message are never trimmed, so the
    # result may still exceed a pathologically small budget — that is by contract.
    return msgs


# ---------------------------------------------------------------------------
# Rolling summary
# ---------------------------------------------------------------------------

def should_update_summary(history_len: int, max_history: int) -> bool:
    """True when the hot history is at/over ``max_history`` (turns about to trim)."""
    return history_len >= max_history


def _build_summary_prompt(prior_summary: Optional[str],
                          folded_turns: List[Dict[str, str]],
                          reply_language: str) -> str:
    lang = "Chinese" if str(reply_language).lower().startswith("zh") else "English"
    prior = (prior_summary or "").strip() or "(none)"

    turns_text_parts = []
    for h in (folded_turns or []):
        if not isinstance(h, dict):
            continue
        turns_text_parts.append(
            f"User: {h.get('user', '')}\nAlex: {h.get('assistant', '')}")
    turns_text = "\n\n".join(turns_text_parts) or "(none)"

    return (
        "You maintain a rolling memory of a UK rental search conversation. "
        f"Write the updated summary in {lang}.\n\n"
        "Merge the PRIOR SUMMARY with the OLDER TURNS being trimmed out of the "
        "live history. Produce a compact, structured plain-text summary of AT MOST "
        f"{_SUMMARY_MAX_CHARS} characters using exactly these labeled lines "
        "(omit a line only if it has no content):\n"
        "Goals: <what the user is ultimately trying to do>\n"
        "Hard criteria: <budget / area / room type / commute / dates — keep each "
        "with the turn it was stated in if the origin is clear>\n"
        "Soft preferences: <nice-to-haves, vibe, amenities>\n"
        "Rejected: <areas or listings the user ruled out>\n"
        "Unresolved: <open questions or pending decisions>\n\n"
        "Rules: keep hard criteria verbatim and attributed; drop greetings and "
        "chit-chat; never invent facts the user did not state; prefer the most "
        "recent value when a criterion changed.\n\n"
        f"PRIOR SUMMARY:\n{prior}\n\n"
        f"OLDER TURNS:\n{turns_text}\n\n"
        "Return ONLY the summary text, no preamble."
    )


def update_rolling_summary(llm_complete: Callable[[str], str],
                           prior_summary: Optional[str],
                           folded_turns: List[Dict[str, str]],
                           reply_language: str = "en") -> Optional[str]:
    """Fold ``folded_turns`` into ``prior_summary`` via an injected completion fn.

    ``llm_complete`` is a synchronous ``callable(prompt: str) -> str``. This module
    performs no network I/O itself. On ANY exception or empty/blank LLM output,
    ``prior_summary`` is returned unchanged (this function never raises).
    """
    try:
        prompt = _build_summary_prompt(prior_summary, folded_turns, reply_language)
        out = llm_complete(prompt)
        if out is None:
            return prior_summary
        out = str(out).strip()
        if not out:
            return prior_summary
        if len(out) > _SUMMARY_MAX_CHARS:
            out = out[:_SUMMARY_MAX_CHARS]
        return out
    except Exception:
        return prior_summary
