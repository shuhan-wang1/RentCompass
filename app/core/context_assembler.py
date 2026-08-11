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
    detect_history_conflicts(history, current_message="") -> list[dict]
    render_history_conflicts(conflicts) -> str
    conflict_question(conflicts, reply_language="en") -> str
    history_conflict_decision(history, current_message="",
                              reply_language="en") -> dict | None
    assemble(*, user_message, history, memory_block="", has_property_context=False,
             rolling_summary=None, token_budget=6000) -> str
    assemble_messages(*, user_message, history, memory_block="", rolling_summary="",
                      context_block=None, reply_language="en",
                      token_budget=6000) -> list  # BaseMessage list
    estimate_tokens(text) -> int
    update_rolling_summary(llm_complete, prior_summary, folded_turns,
                           reply_language="en") -> str
    should_update_summary(history_len, max_history) -> bool
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional

from core.tenancy_reference import monthly_from_weekly

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
        # A listing's NAME is not its identity: the source site routinely publishes two
        # different flats under one street name (observed: two distinct "Marriott Road,
        # London" listings at £800 and £850). Identify by [N] / URL, and when a user's
        # reference fits more than one line, ask which — never answer about whichever
        # came first.
        "A listing is identified by its [N] and its URL, NOT by its name: two lines may "
        "share an address and still be different properties. If the user's reference "
        "fits more than one line, ask which [N] they mean (quote the distinguishing "
        "price/commute) instead of picking one.",
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
# Cross-history contradiction detection (benchmark G11)
# ---------------------------------------------------------------------------
#
# Measured on the 8793c0b internal round: history held "My absolute max is £1200 per
# month." and, two turns later, "My budget is £1200 per week, by the way." The answer
# silently adopted the later one — "within your £1,200/week budget" — with no detection,
# no flag and no question. £1200/week is ~£5,200/month: a 4.3x difference, so the answer
# was searching against a figure the user may never have meant.
#
# THE UPDATE-vs-CONTRADICTION RULE. A user is allowed to change their mind, and treating
# every revision as a conflict would be worse than the bug, so a later statement is read
# as a legitimate UPDATE (no flag) when EITHER:
#
#   (1) it carries an explicit revision marker ("actually", "instead", "make it",
#       "I meant", 「改成」…) — the user announced the change, so the later value wins; or
#   (2) it restates the field in the SAME unit with a different value — re-stating a
#       quantity in its own unit is the normal way to move a number, so the newer value
#       simply wins ("£1200/month" then "£1500/month" is a revision, not a conflict).
#
# Only when neither holds do we look for a MUTUAL INCONSISTENCY — a pair that cannot
# both be true and that a revision does not explain:
#
#   (U) unit_ambiguity      — same field, IDENTICAL value, different period unit. The
#                             number was not revised at all, only the unit changed, so
#                             which was meant is genuinely unknown. This is G11.
#   (M) incompatible_magnitude — same field, different period units whose monthly-
#                             normalised amounts differ by >= 1.5x. A cross-unit restatement
#                             that lands near the original ("£1200/month" then "£280/week")
#                             is a consistent refinement and is NOT flagged.
#   (A) absolute_violated   — the earlier statement was framed as an ABSOLUTE ceiling
#                             ("absolute max", "no more than", 「最多」) and the later
#                             amount exceeds it. An absolute is a claim about all future
#                             values, so a later value breaking it is inconsistent with
#                             it rather than a refinement of it — unless the user said
#                             they were changing it, which is case (1).
#
# A field stated once never conflicts; an identical restatement never conflicts.

# Period units and the factor that normalises an amount to "per month" for comparison.
_PERIOD_CUES = (
    ("week", ("per week", "a week", "/week", "/wk", "per wk", "pw", "p/w", "weekly")),
    ("month", ("per month", "a month", "/month", "/mo", "per mo", "pcm", "p/m", "pm",
               "monthly")),
    ("year", ("per year", "a year", "/year", "/yr", "per yr", "per annum", "pa",
              "annually", "yearly")),
)
# Chinese puts the period BEFORE the amount (「每月1200镑」), so these are matched by a
# backward scan. Kept separate from the English cues on purpose: a backward scan with
# English cues would let "£1200 per month and £800" read "per month" onto the 800.
_PERIOD_CUES_ZH = (
    ("week", ("每周", "一周", "每星期", "每個星期")),
    ("month", ("每月", "一个月", "每個月", "每个月", "月租")),
    ("year", ("每年", "一年")),
)
_MONTHLY_FACTOR = {"week": monthly_from_weekly(1.0), "month": 1.0, "year": 1.0 / 12.0}

# How far a cross-unit restatement may drift from the original before it stops being a
# plausible refinement. £1200/month vs £280/week is 1.01x (fine); vs £1200/week is about 52/12x.
_MAGNITUDE_RATIO = 1.5

# Fields we can compare. Only quantity fields are listed: the detector needs a value and
# (for the unit rules) a unit, and a field without them has nothing comparable.
_QUANTITY_FIELDS = {
    "budget": ("budget", "budgets", "rent", "spend", "spending", "afford", "max",
               "maximum", "ceiling", "limit", "price", "pcm",
               "预算", "租金", "房租", "上限", "最多"),
}

# The later statement announces its own change → an UPDATE, never a contradiction.
_REVISION_MARKERS = (
    "actually", "instead", "make it", "make that", "change it", "change that",
    "changed my mind", "i meant", "i mean", "correction", "scratch that",
    "let's say", "lets say", "update that", "revise", "no wait", "rather",
    "on second thought", "sorry, ", "not ", "now it's", "now its",
    "其实", "改成", "改为", "更新", "不对", "算了", "重新", "我是说", "应该是",
)

# The earlier statement frames itself as a hard ceiling.
_ABSOLUTE_MARKERS = (
    "absolute max", "absolute maximum", "absolute limit", "hard limit", "hard max",
    "hard ceiling", "no more than", "not more than", "at most", "cannot exceed",
    "can't exceed", "cannot go above", "can't go above", "under no circumstances",
    "strict max", "strict limit", "absolutely no more",
    "绝对上限", "最高", "最多", "不能超过", "不超过", "上限",
)

# A £-prefixed amount, or a bare 3+-digit number. The bare alternative uses an explicit
# lookbehind rather than \b: in 「每月1200镑」 the CJK character before the digit is a word
# character, so \b never matches there and every Chinese amount was invisible.
_AMOUNT_RE = re.compile(
    r"£\s*(\d[\d,]*(?:\.\d+)?)|(?<![\d.,])(\d[\d,]{2,}(?:\.\d+)?)(?!\d)")

# Characters after an amount within which a period cue still belongs to that amount.
_UNIT_WINDOW = 28


def _amount_value(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", ""))
    except (TypeError, ValueError):
        return None


_STOPS = (".", "!", "?", ";", "。", "！", "？", "；", ",", "，")


def _unit_for_amount(text: str, start: int, end: int) -> Optional[str]:
    """The period unit qualifying the amount spanning ``[start, end)``, if any.

    English cues are read FORWARD ("£1200 per month"); Chinese cues BACKWARD
    (「每月1200镑」). Both windows are short and stop at clause punctuation, and the
    backward window additionally stops at the previous digit, so a unit belonging to a
    different amount can never be borrowed.
    """
    ahead = text[end:end + _UNIT_WINDOW]
    for stop in _STOPS:
        cut = ahead.find(stop)
        if cut != -1:
            ahead = ahead[:cut]
    low = ahead.casefold()
    best: Optional[tuple] = None
    for unit, cues in _PERIOD_CUES:
        for cue in cues:
            at = low.find(cue)
            if at != -1 and (best is None or at < best[0]):
                best = (at, unit)
    if best:
        return best[1]

    behind = text[max(0, start - _UNIT_WINDOW):start]
    for stop in _STOPS:
        cut = behind.rfind(stop)
        if cut != -1:
            behind = behind[cut + 1:]
    for i in range(len(behind) - 1, -1, -1):
        if behind[i].isdigit():
            behind = behind[i + 1:]
            break
    nearest: Optional[tuple] = None
    for unit, cues in _PERIOD_CUES_ZH:
        for cue in cues:
            at = behind.rfind(cue)
            if at != -1 and (nearest is None or at > nearest[0]):
                nearest = (at, unit)
    return nearest[1] if nearest else None


def _field_of(text: str) -> Optional[str]:
    low = (text or "").casefold()
    for field, terms in _QUANTITY_FIELDS.items():
        if any(term in low for term in terms):
            return field
    return None


def _statements(text: str, turn: int) -> List[Dict[str, Any]]:
    """Every quantity statement in one message: ``{field, value, unit, monthly,
    absolute, revision, turn, text}``. Unit ``None`` means the user did not qualify it."""
    raw = (text or "").strip()
    if not raw:
        return []
    field = _field_of(raw)
    if not field:
        return []
    low = raw.casefold()
    absolute = any(mk in low for mk in _ABSOLUTE_MARKERS)
    revision = any(mk in low for mk in _REVISION_MARKERS)
    out: List[Dict[str, Any]] = []
    for m in _AMOUNT_RE.finditer(raw):
        value = _amount_value(m.group(1) or m.group(2))
        if value is None or value < 100:
            # Below £100 a bare number is a bedroom count / minutes / a postcode digit,
            # not a rent. A £-prefixed small amount is still skipped: it cannot be a
            # monthly budget and comparing it would manufacture conflicts.
            continue
        unit = _unit_for_amount(raw, m.start(), m.end())
        out.append({
            "field": field, "value": value, "unit": unit,
            "monthly": value * _MONTHLY_FACTOR[unit] if unit else value,
            "absolute": absolute, "revision": revision,
            "turn": turn, "text": raw,
        })
    return out


def _classify_pair(earlier: Dict[str, Any], later: Dict[str, Any]) -> Optional[str]:
    """The rule, in one place. Returns a conflict kind, or None when the later statement
    is a legitimate update (or the two are simply consistent)."""
    if later["revision"]:
        return None                                  # (1) the user announced the change
    if not (earlier["unit"] and later["unit"]):
        # An UNQUALIFIED amount is not a comparable quantity — we do not know what period
        # it is per, or even that it is a rent. Comparing it manufactures conflicts out of
        # deposits and fees ("absolute max £1200 pcm ... the deposit is £1800"), so an
        # amount the user did not qualify never raises a flag. Conservative on purpose:
        # over-flagging an update would be worse than the bug.
        return None
    if earlier["unit"] != later["unit"]:
        if earlier["value"] == later["value"]:
            return "unit_ambiguity"                  # (U) — G11
        lo, hi = sorted((earlier["monthly"], later["monthly"]))
        if lo > 0 and hi / lo >= _MAGNITUDE_RATIO:
            return "incompatible_magnitude"          # (M)
        return None                                  # consistent cross-unit restatement
    if earlier["absolute"] and later["monthly"] > earlier["monthly"] * 1.01:
        return "absolute_violated"                   # (A)
    # (2) same unit, different value, no absolute framing → a plain revision, not a
    # conflict. The user is allowed to change their mind; the newer value wins.
    return None


def detect_history_conflicts(history: Optional[List[Dict[str, str]]],
                             current_message: str = "") -> List[Dict[str, Any]]:
    """Mutually inconsistent stated facts across the conversation.

    ``history`` is the SessionStore shape ``[{"user": str, "assistant": str}, ...]``;
    ``current_message`` is appended as the newest user turn. Only USER turns are read —
    the assistant's own echo of a figure is not the user stating it.

    Deterministic, pure-stdlib, no LLM. Returns at most one conflict per field (the
    earliest unresolved pair), each ``{field, kind, earlier, later, ratio}``. Empty list
    when nothing conflicts, which is the overwhelmingly common case.

    See the module section above for the update-vs-contradiction rule.
    """
    turns = list(history or [])
    stmts: List[Dict[str, Any]] = []
    for i, h in enumerate(turns):
        if isinstance(h, dict):
            stmts.extend(_statements(h.get("user") or "", i))
    if current_message:
        stmts.extend(_statements(current_message, len(turns)))

    conflicts: List[Dict[str, Any]] = []
    seen_fields = set()
    for j, later in enumerate(stmts):
        for earlier in stmts[:j]:
            if earlier["field"] != later["field"] or later["field"] in seen_fields:
                continue
            if earlier["value"] == later["value"] and earlier["unit"] == later["unit"]:
                continue                             # identical restatement
            kind = _classify_pair(earlier, later)
            if not kind:
                continue
            lo, hi = sorted((earlier["monthly"], later["monthly"]))
            conflicts.append({
                "field": later["field"], "kind": kind,
                "earlier": earlier, "later": later,
                "ratio": round(hi / lo, 2) if lo > 0 else None,
            })
            seen_fields.add(later["field"])
            break
    return conflicts


def _describe(stmt: Dict[str, Any]) -> str:
    unit = f" per {stmt['unit']}" if stmt["unit"] else " (no period given)"
    amount = int(stmt["value"]) if float(stmt["value"]).is_integer() else stmt["value"]
    return f"£{amount}{unit}"


def render_history_conflicts(conflicts: Optional[List[Dict[str, Any]]]) -> str:
    """The context section that makes the agent ASK instead of silently picking one.

    Renders '' when there is no conflict, so the section is absent in the normal case.
    """
    if not conflicts:
        return ""
    lines = ["=== UNRESOLVED CONTRADICTION IN THIS CONVERSATION "
             "(you MUST ask, you may NOT choose) ==="]
    for c in conflicts:
        e, l = c["earlier"], c["later"]
        ratio = f" — they differ by ~{c['ratio']}x" if c.get("ratio") else ""
        lines.append(
            f"- The user has stated their {c['field']} two ways that cannot both be "
            f"true, and has NOT said which one replaces the other{ratio}:"
        )
        lines.append(f"    1. {_describe(e)}  (they said: \"{e['text']}\")")
        lines.append(f"    2. {_describe(l)}  (they said: \"{l['text']}\")")
    lines.append(
        "ASK the user which one applies before searching, quoting a figure, or filtering "
        "on it. Do NOT assume the later statement supersedes the earlier one — they did "
        "not say so. Do NOT average them, and do NOT invent a third figure."
    )
    lines.append("=== END UNRESOLVED CONTRADICTION ===")
    return "\n".join(lines)


def conflict_question(conflicts: Optional[List[Dict[str, Any]]],
                      reply_language: str = "en") -> str:
    """The bilingual user-facing question that asks WHICH figure applies.

    Separate from :func:`render_history_conflicts` (which addresses the model) because
    this text is shown to the user verbatim.
    """
    if not conflicts:
        return ""
    c = conflicts[0]
    one, two = _describe(c["earlier"]), _describe(c["later"])
    if str(reply_language).lower().startswith("zh"):
        return (f"你先前提到的{'预算' if c['field'] == 'budget' else c['field']}有两种说法："
                f"{one} 和 {two}，两者相差约 {c.get('ratio')} 倍，我不确定该用哪一个。"
                f"请告诉我哪个才是你的实际预算，我再帮你找房。")
    return (f"Before I search — I have two different figures for your {c['field']}: "
            f"{one} and {two}. They differ by about {c.get('ratio')}x, so I don't want to "
            f"guess which one you meant. Which should I use?")


def history_conflict_decision(history: Optional[List[Dict[str, str]]],
                              current_message: str = "",
                              reply_language: str = "en") -> Optional[Dict[str, Any]]:
    """A ready-made routing decision for an unresolved cross-history contradiction.

    Returns the graph's ``clarification`` decision shape, or ``None`` when nothing
    conflicts. This exists so the router can turn a detected contradiction into a
    DETERMINISTIC question with a single call — a source guard on the route rather than a
    prompt instruction the model may ignore. The context section rendered by
    :func:`render_history_conflicts` remains the belt to this braces.
    """
    conflicts = detect_history_conflicts(history, current_message)
    if not conflicts:
        return None
    return {
        "tool": "clarification",
        "params": {},
        "clarification_message": conflict_question(conflicts, reply_language),
        "reason": (f"Unresolved contradiction in conversation history "
                   f"({conflicts[0]['field']}: {conflicts[0]['kind']}) — ask, never pick"),
        "history_conflicts": conflicts,
    }


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

    # G11: an unresolved contradiction is detected over the FULL history (not just the
    # turns that survive trimming) and is NEVER trimmed — dropping it is exactly how the
    # answer ends up silently picking one of the two figures.
    conflict_block = render_history_conflicts(
        detect_history_conflicts(history, user_message))

    def compose(n_turns: int, mem: str, summ: Optional[str]) -> str:
        out = build_history_query(n_turns)
        if summ and include_history:
            out = f"Earlier conversation summary:\n{summ}\n\n{out}"
        if mem:
            out = f"{mem}\n\n{out}"
        if conflict_block:
            out = f"{conflict_block}\n\n{out}"
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
                      rolling_summary: str = "",
                      context_block: Optional[Dict[str, Any]] = None,
                      reply_language: str = "en",
                      token_budget: int = 6000) -> list:
    """Build the message array handed to the native function-calling agent loop.

    This is the message-granularity sibling of :func:`assemble` (which returns a single
    concatenated string for the legacy path). It returns a list of ``langchain_core``
    BaseMessage objects in this fixed order (design §2.7):

      1. SystemMessage — one immutable, versioned PromptSpec containing the
         identity/capability/security/behaviour contract. NEVER trimmed.
      2. HumanMessage — an explicitly labelled LOW-PRIVILEGE UNTRUSTED DATA packet
         containing runtime context | rolling summary | memory. OMITTED when empty.
      3. History turns as alternating HumanMessage / AIMessage from the SessionStore
         shape ``[{"user": str, "assistant": str}, ...]``.
      4. HumanMessage — the current ``user_message`` VERBATIM (no prefix concatenation;
         killing the legacy string-wrapper pattern is the point of this rewrite).

    ``context_block`` keys (all optional): ``accumulated_criteria`` (dict),
    ``focused_property`` (dict — focus-stack top record), ``focus_stack`` (list of those
    records, oldest -> newest; its top supplies ``focused_property`` when that key is
    absent), ``last_results`` (list of listing dicts), ``recommendations_index`` (list —
    cumulative registry entries), ``discussed_areas`` (list[str] — curated area names for
    zh-deictic anchoring, H6).

    Every listing record uses the LISTING key names (``address`` / ``price`` /
    ``travel_time`` / ``url`` / ``description`` …) — the shape ``_format_single_result``
    reads. A record keyed ``property_address`` renders as "(no details captured)".

    Token budget: the :func:`assemble` trimming ladder ported to message granularity —
    (1) drop oldest history turns down to a floor of 2; (2) cap memory at 25% of
    budget (whole lines from the end); (3) cap the rolling summary at 20%; (4) cap
    context sections to the remaining budget. The PromptSpec (message #1) and current
    user message are never trimmed.
    """
    # Lazy imports: keeps context_assembler import-time free of LLM/provider modules
    # (langchain_core.messages is a light message-class module; loop_prompts pulls the
    # security/language directives from langgraph_agent only when called).
    from langchain_core.messages import AIMessage, HumanMessage
    from core import loop_prompts
    from core.prompt_spec import assert_registered_system_messages, system_message

    history = history or []
    memory_block = memory_block or ""
    rolling_summary = rolling_summary or ""
    ctx = context_block or {}

    system_spec = loop_prompts.get_system_prompt_spec(reply_language)
    context_sections = loop_prompts.build_context_sections(
        accumulated_criteria=ctx.get("accumulated_criteria"),
        focused_property=ctx.get("focused_property"),
        focus_stack=ctx.get("focus_stack"),
        last_results=ctx.get("last_results"),
        recommendations_index=ctx.get("recommendations_index"),
        discussed_areas=ctx.get("discussed_areas"),
    )
    # G11: detected over the FULL history, before any trimming, and pinned to the FRONT
    # of the context sections so the trim ladder (which cuts whole lines from the end)
    # cannot silently drop the one section whose absence caused the defect. Empty string
    # in the normal no-conflict case, so nothing changes for any other turn.
    conflict_section = render_history_conflicts(
        detect_history_conflicts(history, user_message))
    if conflict_section:
        context_sections = (f"{conflict_section}\n\n{context_sections}"
                            if context_sections else conflict_section)

    def build(n_turns: int, mem: str, summary: str, sections: str) -> list:
        msgs: list = [system_message(system_spec)]
        context_msg = loop_prompts.compose_context_message(sections, mem, summary)
        if context_msg:
            # Runtime context can contain listing descriptions, remembered user text,
            # and an LLM-derived rolling summary. It must never inherit system priority.
            msgs.append(HumanMessage(content=context_msg))
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
        assert_registered_system_messages(msgs)
        return msgs

    def total_tokens(msgs: list) -> int:
        return sum(estimate_tokens(m.content or "") for m in msgs)

    n_turns = len(history)
    mem = memory_block
    summary = rolling_summary
    sections = context_sections

    msgs = build(n_turns, mem, summary, sections)
    if total_tokens(msgs) <= token_budget:
        return msgs

    # (1) drop oldest history turns down to a floor of 2.
    while n_turns > _MIN_HISTORY_TURNS:
        n_turns -= 1
        msgs = build(n_turns, mem, summary, sections)
        if total_tokens(msgs) <= token_budget:
            return msgs

    # (2) cap memory_block at 25% of budget (whole lines from the END).
    if mem:
        mem = _truncate_lines_to_cap(mem, token_budget * 0.25)
        msgs = build(n_turns, mem, summary, sections)
        if total_tokens(msgs) <= token_budget:
            return msgs

    # (3) cap the rolling summary at 20% of budget. It remains available after hot
    # history turns are trimmed, but can never crowd out the current request.
    if summary:
        summary = _truncate_lines_to_cap(summary, token_budget * 0.20)
        msgs = build(n_turns, mem, summary, sections)
        if total_tokens(msgs) <= token_budget:
            return msgs

    # (4) cap the context sections to whatever budget the never-trimmed parts leave.
    if sections:
        without_sections = total_tokens(build(n_turns, mem, summary, ""))
        remaining = max(int(token_budget - without_sections), 0)
        sections = _truncate_lines_to_cap(sections, remaining)
        msgs = build(n_turns, mem, summary, sections)

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
