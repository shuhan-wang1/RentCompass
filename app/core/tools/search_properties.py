"""
Tool 1: Search Properties Tool
搜索符合条件的房源 - 完整的 ReAct Agent 工具

这个工具整合了：
1. Fine-tuned Model：从用户自然语言中提取搜索条件
2. RAG 检索：向量搜索找到相关房源  
3. 智能过滤：硬过滤 + 软过滤
4. 通勤时间计算：真实 API 计算

核心原则：
- 作为 ReAct Agent 的一个工具被调用
- LLM 决定何时调用此工具
- 工具内部处理所有房源搜索逻辑
"""

from core.tool_system import Tool
from core.tools._argtext import clean_arg_text
from typing import Optional, List, Dict
import pandas as pd
import asyncio
import logging
import math
import os
import sys
import threading
import time
from functools import partial
from pathlib import Path
from datetime import date
import json
import re
from uk_rent_agent.domain import constants as C
from core.tenancy_reference import monthly_from_weekly
from core.source_status import source_failure_summary, FAILED_SOURCE_STATES
from core.constraint_updates import (
    scoped_update_text, mention_is_requirement, is_hypothetical, clause_at, date_is_cleared,
    is_criteria_consultation,
)
from core.search_readiness import (
    SEARCH_TOOL_DESCRIPTION,
    assess_search_readiness,
    is_proceed_intent as _is_proceed_intent,
)


logger = logging.getLogger("app.search")

# Keep optional area-recommendation tasks strongly referenced after their inline wait
# expires.  They populate a durable cache for the next request, so timing out the foreground
# await must not garbage-collect/cancel the useful work.  The callback both releases that
# reference and consumes any exception (avoids "Task exception was never retrieved").
_BACKGROUND_AREA_RECO_TASKS: set[asyncio.Task] = set()


def _consume_task_exception(task: asyncio.Task) -> None:
    """Retrieve a detached task's exception without changing cancellation semantics."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def _finish_background_area_recommendation(task: asyncio.Task) -> None:
    _BACKGROUND_AREA_RECO_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.warning(
            "property_search.background_area_recommendation_failed",
            extra={"error_type": type(exc).__name__},
        )


def _detach_background_area_recommendation(task: asyncio.Task) -> None:
    """Retain optional cache-filling work and consume its eventual outcome exactly once."""
    if task in _BACKGROUND_AREA_RECO_TASKS:
        return
    _BACKGROUND_AREA_RECO_TASKS.add(task)
    task.add_done_callback(_finish_background_area_recommendation)

# Sentinel meaning "user set no commute limit" — kept internally so the search can
# proceed without asking, but must never be shown to the user (see _commute_phrase).
NO_COMMUTE_LIMIT = C.NO_COMMUTE_LIMIT


def _has_cjk(text: str) -> bool:
    """True if the text contains Chinese/Japanese/Korean characters. Used to answer
    a Chinese user in Chinese without a full language-detection dependency."""
    return bool(re.search(r"[㐀-鿿豈-﫿]", text or ""))


# Phrases that explicitly mean "I do not commute" — English + Chinese. Substring
# match is intentional (glued/inflected forms should still trip it); the design
# treats "no commute limit" style phrasing as no-commute-filter too, which is fine.
_NO_COMMUTE_PHRASES = (
    "取消通勤", "取消通勤时间", "移除通勤", "去掉通勤", "不限制通勤", "remove the commute", "drop the commute",
    "no commute", "not commuting", "don't commute", "dont commute",
    "i don't need to commute", "just living", "just live",
    "work from home", "working from home", "wfh", "remote work", "fully remote",
    "不通勤", "不用通勤", "不需要通勤", "无需通勤",
    "单纯住", "就是住", "只是住", "不上班", "在家办公", "居家办公", "远程工作",
)


def _extract_no_commute(text: str) -> bool:
    """Read a whole-work-pattern opt-out, keeping hybrid attendance separate.

    WFH alone is an opt-out; WFH on the other days does not cancel office travel.
    A journey request or a transit time alone is not evidence of office attendance.
    """
    if not text:
        return False
    t = str(text).lower()
    t = re.sub(r"\b(?:not|isn.t|am not) (?:fully remote|working from home|work from home)\b", '', t)
    from core.constraint_updates import extract_office_days
    attendance = extract_office_days(t)
    positive_t = re.sub(r'不(?:用|需要)|无需', 'NEG', t)
    explicit_travel = re.search(r"\bi (?:do |still |need to |have to )?commute\b|"
                                r'\b(?:go|going) (?:in)?to (?:the )?office\b|'
                                r'每周[^。;；]{0,15}(?:到公司|到岗)|(?:需要|要|仍然)通勤', positive_t, re.I)
    if attendance or explicit_travel:
        return False
    return any(p in t for p in _NO_COMMUTE_PHRASES)


# Room-type synonyms -> canonical value ('studio' | 'ensuite' | 'shared'). Substring
# match (CJK + EN). 'studio' is a distinct property form (implies 0 bedrooms), so it is
# checked first and never shadowed by the room-in-a-share types.
_ROOM_TYPE_SYNONYMS = (
    ("whole", ("whole", "self-contained", "self contained", "entire flat", "entire house", "整租", "独立整套", "整套")),
    ("studio", ("studio", "单间公寓", "一室户", "开间")),
    ("ensuite", ("ensuite", "en-suite", "en suite", "独立卫浴", "独卫", "套间", "带独卫")),
    ("shared", ("shared room", "flatshare", "flat share", "house share", "houseshare",
                "shared", "合租房", "合租", "共享房间", "单人间", "单间")),
)


def _extract_room_type(text: str):
    """Canonical room type from a message: 'studio' | 'ensuite' | 'shared' | None.
    Deterministic so a chat answer after the soft gate ("我要ensuite") updates the
    accumulated room_type even without any 'find'/'search' verb."""
    if not text or is_criteria_consultation(text):
        return None
    t = scoped_update_text(text).lower()
    if re.search(r'先[^。.!?]{0,12}(?:确认|问)|还没确认|尚未确认|\b(?:first ask|first confirm|not yet decided)\b', t, re.I) and re.search(
            r'整套|整租|合租|房型|whole|shared|room type', t, re.I):
        return None
    for canonical, needles in _ROOM_TYPE_SYNONYMS:
        for needle in needles:
            for match in re.finditer(re.escape(needle), t):
                if mention_is_requirement(t, match.start(), match.end()):
                    return canonical
    # "a 1-bed flat" / "two bedroom apartment" asks for a WHOLE home. With no room type a
    # "1 bed Flat share" room satisfied bedrooms=1 and was presented under "Meets all
    # declared conditions (verified)" for a 1-bed-flat request (2026-10-19 C-11, 5/5).
    # A share/room phrasing never reaches here: the "shared" needles above win first.
    if re.search(r"\b(?:a|one|single|double|twin)\s+room\b|\broom\s+in\s+(?:a|an|the)\b", t):
        return None          # "a room in a 3-bed flat" is a room, not a whole flat
    for match in _WHOLE_HOME_REQUEST.finditer(t):
        if mention_is_requirement(t, match.start(), match.end()):
            return "whole"
    return None


_WHOLE_HOME_REQUEST = re.compile(
    r"\b(?:\d+|one|two|three|four|five)[\s-]*(?:bed(?:room)?s?|br)[\s-]+"
    r"(?:flat|apartment|house|maisonette|bungalow|home)\b(?![\s-]*(?:share|shares|mate))"
    r"|[一二三四两1-5][室居](?:室)?(?:公寓|整套|住宅)", re.I)


def _normalize_room_type(value):
    """Coerce an incoming room_type (from the form, accumulated state, or free text)
    to a canonical value or None. Unknown/free-text values are run through the
    synonym extractor so 'en-suite' or 'a studio please' still map correctly."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower()
    if v in ("studio", "ensuite", "shared", "whole"):
        return v
    return _extract_room_type(v)


# Display labels for room type, localized (en, zh).
_ROOM_TYPE_LABELS = {
    "whole": ("Whole property", "整套住宅"),
    "studio": ("Studio", "Studio 单间公寓"),
    "ensuite": ("en-suite room", "独立卫浴房间"),
    "shared": ("shared room", "合租房间"),
}


def _room_type_label(room_type, is_cjk: bool) -> str:
    en, zh = _ROOM_TYPE_LABELS.get(room_type, (room_type, room_type))
    return zh if is_cjk else en


def _matches_room_type(prop: dict, room_type: str) -> bool:
    """True when a listing satisfies the requested room type, inspecting the scraped
    Room_Type_Category / Description / Detailed_Amenities. Mirrors the data's own
    vocabulary (e.g. 'Studio', 'En-suite Room', 'Room (Shared)', '1 bed Flat share')."""
    if not room_type:
        return True
    rt = str(prop.get('Room_Type_Category', '')).lower()
    desc = str(prop.get('Description', '')).lower()
    amen = str(prop.get('Detailed_Amenities', '')).lower()
    blob = f"{rt} {desc} {amen}"
    if room_type == 'whole':
        rt = f"{rt} {prop.get('Type', prop.get('property_type', ''))}".lower()
        if re.search(r'\b(?:rooms?|flat\s*share|house\s*share|shared|hmo)\b', rt):
            return False
        return bool(re.search(r'\b(?:flat|apartment|house|bungalow|maisonette|studio|whole|entire)\b', rt))
    if room_type == 'studio':
        return 'studio' in rt or 'studio' in desc
    if room_type == 'ensuite':
        return 'en-suite' in blob or 'ensuite' in blob or 'en suite' in blob
    if room_type == 'shared':
        return 'shar' in rt or 'shar' in desc or 'flatshare' in blob or 'flat share' in blob
    return True


# Canonical accessibility features shared with the deterministic conversation
# extractor in langgraph_agent. Keep these as property facts, not demographic
# labels: asking for an accessible home is a lawful housing requirement.
_ACCESSIBILITY_FEATURE_PATTERNS = (
    (
        "accessible bathroom",
        re.compile(
            r"\b(?:(?:wheelchair[- ]accessible|accessible|adapted|disabled)[- ]+"
            r"(?:bathroom|shower|toilet|wc)|roll[- ]in shower)\b"
            r"|(?:无障碍|适老|残障)(?:卫生间|洗手间|浴室|淋浴)|轮椅可用(?:卫生间|浴室)",
            re.IGNORECASE,
        ),
    ),
    (
        "wheelchair-accessible",
        re.compile(
            r"\b(?:wheelchair[- ]accessible(?![- ](?:bathroom|shower|toilet|wc))|"
            r"wheelchair[- ]friendly|wheelchair access(?:ible)?|accessible (?:flat|apartment|"
            r"property|home|accommodation|entrance|building))\b"
            r"|轮椅(?:可进入|可通行|通道|友好|无障碍)(?!(?:卫生间|洗手间|浴室|淋浴))"
            r"|适合轮椅(?:使用者)?|无障碍(?:房|住宅|公寓|出入|入口|通道)"
            r"|\b(?:enough|sufficient|adequate)\b[^.;。；]{0,85}\b(?:doorway|corridor|turning|turn)\b[^.;。；]{0,65}\bwheelchair\b"
            r"|\b(?:doors?|doorways?|corridors?)\b[^.;。；]{0,35}\bwide enough\b[^.;。；]{0,35}\bwheelchair\b"
            r"|(?:室内|内部)[^。；;]{0,25}轮椅[^。；;]{0,20}(?:通行|转身|转弯)[^。；;]{0,15}(?:空间|宽度)" ,
            re.IGNORECASE,
        ),
    ),
    (
        "step-free",
        re.compile(
            r"\b(?:step[- ]free|no[- ]step|level access|without steps?|zero[- ]step)\b"
            r"|(?:无|没有)(?:台阶|阶梯)|零台阶|平层(?:入口|通道|出入)",
            re.IGNORECASE,
        ),
    ),
    (
        "lift",
        re.compile(r"\b(?:passenger[- ]+)?(?:lifts?|elevators?)\b|电梯|升降机", re.IGNORECASE),
    ),
)

_ACCESSIBILITY_FEATURES = frozenset(name for name, _ in _ACCESSIBILITY_FEATURE_PATTERNS)
_EVIDENCE_ONLY_FEATURES = _ACCESSIBILITY_FEATURES | {'cats allowed', 'pets allowed', 'cat_written_consent'}


def _requires_explicit_feature_evidence(feature: str) -> bool:
    return feature in _EVIDENCE_ONLY_FEATURES or bool(re.fullmatch(r'wheelchair_space_\d+(?:\.\d+)?cm', feature))


def _accessibility_feature_mentions(text: str) -> list[tuple[str, int, int]]:
    """Return canonical accessibility mentions and their spans, in source order."""
    source = str(text or "")
    found: list[tuple[int, int, str]] = []
    for canonical, pattern in _ACCESSIBILITY_FEATURE_PATTERNS:
        for match in pattern.finditer(source):
            found.append((match.start(), match.end(), canonical))
    # Functional space wording can refer back to an explicitly mentioned
    # wheelchair (including its stated width), without saying "accessible".
    functional_space = re.compile(
        r"\b(?:usable|enough|sufficient|adequate)\s+(?:internal\s+)?(?:space|room|width|clearance)\b"
        r"[^.!?;。；]{0,110}\b(?:doors?|doorways?|corridors?|bathroom|turning|turn)\b", re.I)
    from core.constraint_updates import mention_is_requirement
    for match in functional_space.finditer(source):
        context_start = max(0, match.start() - 220)
        context = source[context_start:match.end()]
        anchors = list(re.finditer(r'\bwheelchair\b|轮椅', context, re.I))
        if any(mention_is_requirement(source, context_start + anchor.start(), context_start + anchor.end())
               and not re.search(r"\b(?:don.t|do not|not|no|without)\b[^.!?;:]{0,25}$",
                                 context[:anchor.start()], re.I) for anchor in anchors):
            found.append((match.start(), match.end(), 'wheelchair-accessible'))
    found.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    result: list[tuple[str, int, int]] = []
    seen = set()
    for start, end, canonical in found:
        key = (canonical, start, end)
        if key not in seen:
            seen.add(key)
            result.append((canonical, start, end))
    return result


def _canonical_property_feature(value) -> str:
    """Canonicalise known feature aliases while preserving other existing tags."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    mentions = _accessibility_feature_mentions(raw)
    if mentions:
        for canonical, start, end in mentions:
            if not raw[:start].strip(" -_") and not raw[end:].strip(" -_"):
                return canonical
        return mentions[0][0]
    normal = raw.lower()
    return "en-suite" if normal == "ensuite" else normal


def _flatten_structured_amenities(value) -> str:
    """Render only structured amenity values into searchable text."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " | ".join(part for part in (_flatten_structured_amenities(v) for v in value)
                          if part)
    if isinstance(value, dict):
        parts = []
        for key, nested in value.items():
            display_key = str(key).replace("_", " ").replace("-", " ")
            if nested is True:
                parts.append(display_key)
            elif nested is False:
                # Boolean maps are complete, structured assertions: false is
                # explicit negative evidence, not a missing disclosure.
                parts.append(f"no {display_key}")
            elif nested not in (None, 0, ""):
                rendered = _flatten_structured_amenities(nested)
                if rendered:
                    parts.append(f"{display_key}: {rendered}")
        return " | ".join(parts)
    return str(value) if value not in (None, False) else ""


_FEATURE_NEGATED_BEFORE_RE = re.compile(
    r"(?:\b(?:no|without|not|lacks?|missing|unavailable|does(?:n't| not) have)\s+(?:an?\s+)?|"
    r"(?:没有|不含|不提供|缺少|无)\s*)$",
    re.IGNORECASE,
)
_FEATURE_NEGATED_AFTER_RE = re.compile(
    r"^\s*(?:is\s+)?(?:not available|unavailable|absent|未提供|不可用|没有)", re.IGNORECASE)


def _feature_text_evidence(text: str, canonical: str) -> tuple[bool, bool]:
    """Return (positive, negative) mentions for one canonical feature."""
    pattern = dict(_ACCESSIBILITY_FEATURE_PATTERNS).get(canonical)
    if pattern is None:
        return False, False
    source = str(text or "")
    positive = negative = False
    for match in pattern.finditer(source):
        before = source[max(0, match.start() - 36):match.start()]
        after = source[match.end():match.end() + 28]
        if _FEATURE_NEGATED_BEFORE_RE.search(before) or _FEATURE_NEGATED_AFTER_RE.search(after):
            negative = True
        else:
            positive = True
    return positive, negative


def _property_feature_evidence(prop: dict, feature: str) -> str:
    """Tri-state structured evidence: verified / absent / unverified.

    Accessibility is proven only by Detailed_Amenities (or explicitly denied by
    that field / Excluded_Features). Free-form descriptions are not an
    authoritative accessibility audit. Missing mention is unknown, not absence.
    """
    canonical = _canonical_property_feature(feature)
    room_type = str(prop.get("Room_Type_Category", "") or "").lower()
    amenities = _flatten_structured_amenities(prop.get("Detailed_Amenities"))

    if canonical == "studio":
        return "verified" if "studio" in room_type else ("absent" if room_type else "unverified")
    if canonical == "private":
        description = str(prop.get("Description", "") or "").lower()
        return "verified" if "private" in room_type or "private" in description else "unverified"
    if canonical == "en-suite":
        blob = f"{room_type} {amenities}".lower()
        return ("verified" if any(token in blob for token in ("en-suite", "ensuite", "en suite"))
                else "unverified")
    if canonical in {'cats allowed', 'pets allowed'}:
        keys = ('Cats_Allowed', 'Pets_Allowed') if canonical == 'cats allowed' else ('Pets_Allowed',)
        for key in keys:
            if prop.get(key) is True:
                return 'verified'
            if prop.get(key) is False:
                return 'absent'
        return 'unverified'
    if canonical not in _ACCESSIBILITY_FEATURES:
        return "unverified"

    positive, negative = _feature_text_evidence(amenities, canonical)
    excluded = _flatten_structured_amenities(prop.get("Excluded_Features"))
    excluded_positive, excluded_negative = _feature_text_evidence(excluded, canonical)
    # Excluded_Features is itself a structured negative field, so either
    # "Lift" or "No lift" in it denies the feature.
    negative = negative or excluded_positive or excluded_negative
    if positive and not negative:
        return "verified"
    if negative and not positive:
        return "absent"
    return "unverified"


def _money_value(token: str):
    """Parse pounds through Decimal; JSON-facing amounts retain exact pennies."""
    from decimal import Decimal, InvalidOperation
    try:
        value = Decimal(str(token).replace(',', '').replace('£', '').strip())
    except InvalidOperation:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _amount_role(text: str, lo: int, hi: int) -> str:
    """Bind each amount to its own noun phrase on either side of the number.

    A leading rent ceiling owns its amount even when the next conjoined quantity
    is a combined cap. Within a side, the latest complete role phrase wins, so
    "total cap ... and rent ceiling" does not inherit the earlier total role.
    """
    punctuation = r'[。!?;；\n，,]|(?<!\d)\.(?!\d)'
    before = re.split(punctuation, text[:lo])[-1][-110:]
    before = re.split(r'(?:£\s*)?(?<![\d.])\d[\d,]*(?:\.\d+)?', before)[-1]
    before = re.split(r'\b(?:separate from|apart from|as distinct from|versus|rather than)\b', before, flags=re.I)[-1]
    if re.search(r'(?:\b(?:and|but|while|whereas)\b|和|以及|而)\s*£?\s*$', before, re.I):
        before = ''  # Amount-first phrase after an explicit quantity coordinator.
    tail = re.split(punctuation, text[hi:])[0]
    after = re.split(r'£|\b(?:while|whereas)\b', tail, flags=re.I)[0][:110]
    if '£' in tail:
        # The last coordinator before the next amount starts that quantity's
        # phrase. Earlier "rent and transport" can still describe this amount.
        joins = list(re.finditer(r'\b(?:and|but|while|whereas)\b|以及|而|另(?:外)?|和', after, re.I))
        if joins:
            after = after[:joins[-1].start()]
    if re.search(r'每人|人均|\b(?:each|per person|per head)\b', before, re.I):
        return 'per_person'
    total = (r'\brent[\s–—-]*(?:plus|and|&)[\s–—-]*(?:commut(?:e|ing)|travel|transport)\b'
             r'|(?:租金|房租|rent)[^£\d。;；]{0,35}(?:交通|通勤|travel|transport|commut)'
             r'|(?:交通|通勤|travel|transport|commut)[^£\d。;；]{0,25}(?:租金|房租|rent)'
             r'|all.in|combined|overall|总开销|总支出|总花费|总费用|总预算'
             r'|\btotal\s+(?:(?:monthly|rent.plus.commuting|rent.plus.transport|affordability)\s+)*(?:budget|cap)\b')
    rent = r'租金|房租|月租|整套(?:合计|总计|总价|共|预算|每月|每周|最多|最高|月租|周租)|\brent\b|rental'

    def role(fragment, *, trailing=False):
        totals = list(re.finditer(total, fragment, re.I))
        rent_pattern = r'\brent\b|rental|租金|房租|月租' if trailing else rent
        rents = list(re.finditer(rent_pattern, fragment, re.I))
        latest_total = max(totals, key=lambda match: match.end()) if totals else None
        latest_rent = max(rents, key=lambda match: match.end()) if rents else None
        if latest_total and (not latest_rent or latest_total.end() >= latest_rent.start()):
            return 'total'
        if latest_rent:
            return 'rent'
        return None

    return role(before) or role(after, trailing=True) or 'budget'


def _extract_total_budget(text: str):
    if not text or is_criteria_consultation(text):
        return None
    t = re.sub(r'(?<=\d),(?=\d{3}\b)', '', scoped_update_text(text).lower())
    matches = [m for m in re.finditer(r'£\s*(\d{3,5}(?:\.\d{1,2})?)(?![\d.])', t)
               if _amount_role(t, m.start(), m.end()) == 'total'
               and not is_hypothetical(clause_at(t, m.start(), m.end()))]
    if not matches:
        return None
    m = matches[-1]
    value = _money_value(m.group(1))
    return monthly_from_weekly(value) if _budget_period_near(t, m.start(), m.end()) == 'week' else value


# "bills" alone is not here: "budget £1500 including bills" describes the rent.
_NOT_RENT_MONEY = re.compile(
    r'\b(?:deposit|salary|earn\w*|income|wages?|after tax|fees?|council tax|'
    r'holding|admin)\b|押金|工资|收入|月薪|年薪', re.I)
_RENT_CONTEXT = re.compile(
    r'\b(?:budget|rent|max(?:imum)?|under|up to|below|pcm|per month|a month|ceiling)\b|'
    r'预算|租金|月租|上限|以内', re.I)


def _corrects_a_budget(t: str, correction) -> bool:
    """True when ``correction`` (a "no wait / actually / 不对 <amount>" match) retracts an
    earlier RENT figure in the same clause run, and is itself a rent figure.

    Without this a correction of any number won: "The deposit is £2000, sorry, £2500",
    "I earn 4000, sorry 4500 after tax" and "1500, actually 1200 sq ft" all replaced
    the budget (review of the A-05 fix)."""
    start, end = correction.start(1), correction.end(1)
    earlier = list(re.finditer(r'(?<![\d.])\d{3,5}(?!\d|\.\d)', t[:correction.start()]))
    if not earlier:
        return False
    prior = earlier[-1]
    bounds = list(re.finditer(r'[.!?。！？;；\n](?!\d)', t[:prior.start()]))
    clause = t[(bounds[-1].end() if bounds else 0):min(len(t), end + 16)]
    if not _RENT_CONTEXT.search(clause[:prior.end() - (bounds[-1].end() if bounds else 0) + 20]):
        return False
    if _NOT_RENT_MONEY.search(clause):
        return False
    if re.match(r'\s*(?:sq|square|ft|m2|minutes?|mins?|beds?|bedrooms?|people|persons?|%|分钟|平)',
                t[end:], re.I):
        return False
    return not is_hypothetical(clause_at(t, start, end))


def _extract_budget(text: str):
    """Pull an explicit monthly/weekly budget out of the *current* user message.
    Returns (amount:number, period:'week'|'month') or (None, None). Deterministic regex
    so a conversational update ("my budget is now 1800") overrides accumulated state
    without an extra LLM round-trip."""
    if not text or is_criteria_consultation(text):
        return None, None
    t = scoped_update_text(text).lower()
    t = re.sub(r'(?<=\d),(?=\d{3}\b)', '', t)
    if (re.search(r'not\s+(?:yet\s+)?(?:chosen|decided)|haven.t (?:yet )?chosen|尚未(?:选择|决定)|还没选|未确定', t, re.I)
            and re.search(r'\bor\b|还是|或者', t, re.I)
            and len(re.findall(r'£\s*\d{3,5}', t)) >= 2):
        return None, None
    if re.search(r'确认|澄清|口径选项|让我选|不清楚|\b(?:whether|clarify|options)\b|[?？]', t) and re.search(r'每人[^。.!?]{0,30}还是[^。.!?]{0,15}整套|per.person.{0,30}or.{0,15}(?:total|whole)', t, re.I):
        return None, None
    def valid_amount(value, period):
        monthly = monthly_from_weekly(value) if period == 'week' else value
        return (value, period) if 200 <= monthly <= 20000 else (None, None)
    if re.search(r'^(?:is (?:it|this|that)|does |how much|what is)|计算|相差|贵多少', t) and not re.search(
            r'keep|set|change|raise|lower|改为|调整|预算(?:是|为|改)', t):
        return None, None
    if re.search(r'房源|\blisting\b|\bproperty\b', t) and re.search(r'[?？]|多少', t) and not re.search(
            r'\b(?:set|keep|change|ceiling|budget)\b|预算|上限', t):
        return None, None
    # A self-correction names the budget twice and retracts the first: "Budget £1,500, no
    # wait, £1,700" searched at £1,500 (A-05, 2/2). The figure after the LAST correction
    # marker wins, provided an earlier figure in the same message is what it corrects.
    correction = None
    for candidate in re.finditer(
            r'(?:\bno\s*,?\s*wait\b|\bsorry\b|\bi\s+mean\b|\bmake\s+(?:that|it)\b|\bactually\b|'
            r'\bcorrection\b|\bor\s+rather\b|\bscratch\s+that\b|不对|说错了|改成|应该是)'
            r'[\s,，:：]*(?:£\s*)?(?<![\d.])(\d{3,5}(?:\.\d{1,2})?)(?!\d|\.\d)', t, re.I):
        correction = candidate
    if correction is not None and _corrects_a_budget(t, correction):
        return valid_amount(_money_value(correction.group(1)),
                            _budget_period_near(t, correction.start(1), correction.end(1)))
    # (?!\d|\.\d): a full stop after the figure ends the sentence, it is not a decimal
    # point — "…, £1,700." used to be invisible to every amount pattern here.
    rent_amounts = [m for m in re.finditer(r'(?:£\s*)?(?<![\d.])(\d{3,5}(?:\.\d{1,2})?)(?!\d|\.\d)', t)
                    if _amount_role(t, m.start(), m.end()) == 'rent'
                    and not is_hypothetical(clause_at(t, m.start(), m.end()))]
    if rent_amounts:
        m = rent_amounts[-1]
        return valid_amount(_money_value(m.group(1)), _budget_period_near(t, m.start(), m.end()))
    # A clearly stated household of N with a per-person ceiling can express a
    # whole-property ceiling. Bedrooms alone never establish occupant count.
    persons = re.search(r'(?:我们|共|一共)?\s*([2-6二两三四五六])\s*(?:个人|人)|\b([2-6]|two|three|four|five|six)\s+(?:people|persons|tenants|friends)\b', t)
    per_person = [m for m in re.finditer(r'(?:£\s*)?(?<![\d.])(\d{3,5}(?:\.\d{1,2})?)(?!\d|\.\d)', t)
                  if _amount_role(t, m.start(), m.end()) == 'per_person']
    if persons and len(per_person) == 1 and _extract_room_type(t) == 'whole' and not is_hypothetical(t):
        count_text = persons.group(1) or persons.group(2)
        counts = {'二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6,
                  'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}
        count = int(count_text) if count_text.isdigit() else counts[count_text]
        m = per_person[0]
        return valid_amount(_money_value(m.group(1)) * count, _budget_period_near(t, m.start(), m.end()))
    # Choose the declared ceiling before any quoted listing prices. Questions,
    # examples and property details are not changes to the household's budget.
    explicit = []
    for pattern in (
        r'(?:budget\b|ceiling\b|最多|预算|上限)[^£\d。;；]{0,30}£?\s*(\d{3,5}(?:\.\d{1,2})?)(?!\d)',
        r'(?:under|below|up\s+to|no\s+more\s+than|max(?:imum)?)\s*£?\s*(\d{3,5}(?:\.\d{1,2})?)(?!\d)',
        r'£?\s*(\d{3,5}(?:\.\d{1,2})?)(?!\d)[^。;；]{0,12}(?:ceiling|budget|以内|以下)',
    ):
        for candidate in re.finditer(pattern, t):
            clause = clause_at(t, candidate.start(), candidate.end())
            if (_amount_role(t, candidate.start(1), candidate.end(1)) not in ('per_person', 'total')
                    and not is_hypothetical(clause) and not re.search(r'是否|会怎样|what if|would .*budget', clause)):
                explicit.append(candidate)
    if explicit:
        m = max(explicit, key=lambda item: item.start())
        val = _money_value(m.group(1))
        return valid_amount(val, _budget_period_near(t, m.start(1), m.end(1)))
    if is_hypothetical(t) or re.search(r'比较|房源|\bcompare\b|budget unchanged|listing|property\s*(?:id|#)|\bat £|\bpriced?\b|[?？]', t):
        return None, None
    amount = None
    # 1) currency-tagged amount: "£1800", "£ 1800"
    m = re.search(r'£\s?(\d{3,5}(?:\.\d{1,2})?)\b', t)
    # 2) amount followed by a budget unit: "1800 pcm", "1800 per month", "1800 a week"
    if not m:
        m = re.search(r'\b(\d{3,5}(?:\.\d{1,2})?)\s*(?:pcm|pm|pw|/\s*(?:month|week|wk)|per\s+(?:month|week)|a\s+(?:month|week)|pounds?)\b', t)
    # 3) budget keyword + amount: "budget is now 1800", "budget of 1800", "max budget 1800"
    if not m:
        m = re.search(r'budget\b[^£\d]{0,20}£?\s?(\d{3,5}(?:\.\d{1,2})?)\b', t)
    # 4) budget-INTENT phrasing (esp. clarification answers): "under 1000", "up to 1200",
    #    "max 900", "around 1500", and Chinese "1000以内/以下/左右", "预算1000", "月租1000".
    if not m:
        m = re.search(r'\b(?:under|below|max(?:imum)?|up\s+to|around|about|no\s+more\s+than|less\s+than)\s*£?\s?(\d{3,5}(?:\.\d{1,2})?)\b', t)
    if not m:
        m = re.search(r'(\d{3,5}(?:\.\d{1,2})?)\s*(?:以内|以下|左右|块|镑|元|英镑)', t)
    if not m:
        m = re.search(r'(?:预算|月租|租金|房租)\s*[^\d]{0,6}(\d{3,5}(?:\.\d{1,2})?)', t)
    if m:
        if _amount_role(t, m.start(1), m.end(1)) in ('per_person', 'total'):
            return None, None
        val = _money_value(m.group(1))
        return valid_amount(val, _budget_period_near(t, m.start(1), m.end(1)))
    if amount is None:
        return None, None
    return amount, _budget_period_near(t, m.start(1), m.end(1))


# A rent PERIOD marker, in either language this product is used in. The amount patterns
# above have been bilingual for a long time (以内/以下/左右/块/镑/元/英镑, 预算/月租/租金/
# 房租); this test was English-only, and a missing marker defaults to monthly — so
# "预算每周£350" was read as £350/MONTH, the weekly->monthly conversion downstream never
# fired, and a real user's London search ran at a £100-402/month band and returned nothing.
#
# The `/wk` branch deliberately has no leading \b: there is no word boundary between a
# space and a slash, so the old `\b(?:...|/\s*w(?:k|eek)?|...)` could never match "350 /wk"
# either. Same line, same defect, both languages.
_WEEKLY_MARKER = re.compile(
    r'\bpw\b|\bper\s+week\b|\ba\s+week\b|/\s*w(?:k|eek)?\b'
    r'|每\s*[个個]?\s*(?:周|週|星期)|[周週]\s*租|按\s*[周週]|/\s*[周週]'
)
_MONTHLY_MARKER = re.compile(
    r'\bpcm\b|\bpm\b|\bper\s+month\b|\ba\s+month\b|/\s*month\b'
    r'|每\s*[个個]?\s*月|月\s*租|按\s*月|/\s*月'
)


def _budget_period_near(text: str, lo: int, hi: int) -> str:
    """'week' or 'month' for the amount at ``text[lo:hi]`` — whichever marker is NEARER.

    Nearest-wins rather than the old "a weekly marker anywhere makes it weekly", because
    both markers legitimately co-occur: "预算每月1500，我每周去两次健身房" states a MONTHLY
    rent and a weekly gym habit, and reading it as weekly would inflate £1500 to £6495.
    Distance is measured to the nearest edge of the amount, so a trailing unit ("350镑/周")
    and a leading one ("每周350镑") are treated alike. No marker at all still means monthly.
    """
    left = max([m.end() for m in re.finditer(r'[。.!?;；\n]', text[:lo])] or [0])
    stop = re.search(r'[。.!?;；\n]', text[hi:])
    right = hi + stop.start() if stop else len(text)
    def _nearest(pattern) -> float:
        best = float('inf')
        for mk in pattern.finditer(text):
            if mk.start() < left or mk.end() > right:
                continue
            if pattern is _WEEKLY_MARKER and re.search(r'\b(?:days?|times?|trips?)\s*$|[天次]\s*$', text[max(left, mk.start()-12):mk.start()]):
                continue
            if mk.end() <= lo:
                best = min(best, lo - mk.end())      # marker before the amount
            elif mk.start() >= hi:
                best = min(best, mk.start() - hi)    # marker after the amount
            else:
                return 0.0                           # overlapping the amount itself
        return best

    return 'week' if _nearest(_WEEKLY_MARKER) < _nearest(_MONTHLY_MARKER) else 'month'


def _extract_commute_minutes(text: str):
    """Pull an explicit commute-time limit (in minutes) out of the current message.
    Returns int minutes or None. Handles '40 min', 'within 30 minutes', '35分钟',
    'half an hour', and 'an hour'. The ASCII-left-boundary avoids matching an
    embedded identifier while still allowing CJK text immediately before a number
    (``通勤35分钟`` has no Unicode ``\\b`` boundary before ``35``)."""
    if not text:
        return None
    t = text.lower()
    if re.search(r'\bhalf\s+an?\s+hour\b', t):
        return 30
    if re.search(r'\b(?:an?|1)\s+hour\b', t):
        return 60
    m = re.search(
        r'(?<![a-z0-9])(\d{1,3})\s*(?:-)?\s*'
        r'(?:(?:min|mins|minute|minutes)(?![a-z])|(?:分钟|分鐘))',
        t,
    )
    if not m:
        m = re.search(
            r'(?<![a-z0-9])(\d{1,3})\s*(?:hours?(?![a-z])|小时|小時)',
            t,
        )
        if m:
            v = int(m.group(1)) * 60
            return v if 1 <= v <= 180 else None
    if m:
        v = int(m.group(1))
        if 1 <= v <= 180:
            return v
    return None


# Commute-time primitives now live in core.commute (shared verbatim with the
# area recommender). Re-exported under the historical private names so the rest
# of this module — step 4 annotation, the similar-but-over-budget fallback — is
# unchanged.
from core.commute import (
    COMMUTE_SANITY_CAP as _COMMUTE_SANITY_CAP,
    LONDON_BBOX as _LONDON_BBOX,
    parse_geo as _parse_geo,
    in_london as _in_london,
    coord_commute_minutes as _coord_commute_minutes,
)


_WORD_NUMS = {"studio": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _extract_bedrooms(text: str):
    """Pull an explicit bedroom count out of the current message.
    Returns (min_beds, max_beds) or None. 'studio' -> (0, 0); 'N bed[room]' and
    the spelled-out one..five -> (N, N). Deterministic so a Manchester 1-bed
    query drives an exact min=max=1 filter on the live source (no studios/2-beds
    leaking in, the original demo-data bug)."""
    if not text or is_criteria_consultation(text):
        return None
    t = scoped_update_text(text).lower()
    nums = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, 'six': 6, **_WORD_NUMS}
    t = re.sub(r'\bminimum bedroom count\s+(?:with|to|of|is)\s+([0-6]|one|two|three|four|five|six)\b', r'at least \1 bedrooms', t)
    pattern = r'(?<!\d)([0-6零一二两三四五六]|one|two|three|four|five|six)\s*-?\s*(?:bed(?:room)?s?\b|(?:间)?(?:独立)?卧(?:室)?|居(?:室)?)'
    candidates = []
    for m in re.finditer(pattern, t):
        clause = clause_at(t, m.start(), m.end())
        if re.search(r'不要改变|不要修改|不变|don.t change|unchanged', clause, re.I):
            continue
        if not mention_is_requirement(t, m.start(), m.end()):
            if not re.search(r'不要改变\s*$', t[:m.start()]):
                continue
        before = re.split(r'[。.!?;；\n，,:：]', t[:m.start()])[-1][-55:]
        after = t[m.end():m.end() + 22]
        # Allocation per occupant is not the whole home's bedroom count. Never
        # multiply it by an assumed household size, or let it win over a total.
        if (re.search(r'(?:各(?:自)?(?:有|住|占|要|需(?:要)?)?|每人(?:有|住|要|需(?:要)?)?|一人(?:有|住|要|需(?:要)?)?)\s*$'
                      r'|\beach(?:\s+\w+){0,4}\s*$', before, re.I)
                or re.match(r'\s*(?:each|per (?:person|tenant)|apiece)\b', after, re.I)):
            continue
        n = int(m.group(1)) if m.group(1).isdigit() else nums[m.group(1)]
        minimum = bool(re.search(r'(?:至少|最少|at least|minimum|min\.?)[^，。;；]{0,10}$', before))
        minimum = minimum or bool(re.match(r'\s*(?:minimum|or more|以上)', after, re.I))
        total = bool(re.search(r'正好|恰好|总共|总计|合计|整套|整租|\b(?:exactly|total(?: of)?|whole (?:home|property|flat))\b', before, re.I))
        candidates.append((int(total), m.start(), (n, 6 if minimum else n)))
    if candidates:
        # Preserve historical first-mention behavior at equal strength; an
        # explicitly scoped total outranks an earlier incidental room count.
        return max(candidates, key=lambda item: (item[0], -item[1]))[2]
    if _extract_room_type(t) == 'studio':
        return (0, 0)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Move-in / availability date (Deliverable: optional move-in criterion + fit).
# ─────────────────────────────────────────────────────────────────────────────
# 期望入住日期抽取 + 房源可入住日期归一化 + 匹配标注。全部确定性、仅 stdlib。
# 约定（数据契约）：
#   available_from      : ""(未知) | "Available now"(即可入住) | "YYYY-MM-DD"
#   availability_status : ""(无期望入住或未知) | "✅ 可入住"(≤期望日) | "⚠️ YYYY-MM-DD 起租"(晚于期望日)
# availability_status 采用与 budget_status 一致的"硬编码中文 emoji"约定。

# 月份词 -> 月序号（英文；中文另表）。最长优先，避免 "sep" 抢在 "september" 前。
_EN_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_EN_MONTH_RE = "|".join(sorted(_EN_MONTHS, key=len, reverse=True))
_ZH_MONTHS = {
    "一月": 1, "二月": 2, "三月": 3, "四月": 4, "五月": 5, "六月": 6, "七月": 7,
    "八月": 8, "九月": 9, "十月": 10, "十一月": 11, "十二月": 12,
}

# 入住/起租语境词。仅当出现语境词时才允许把"月份"解读为期望入住日（避免无关的
# 裸月份/数字误触发）。英文 "may" 的裸月份路径额外要求前置介词，避免情态动词 "may" 误判。
_MOVE_CTX_EN = re.compile(
    r"\b(move[\s\-]?in|moving[\s\-]?in|move|start(?:ing)?|available|avail|from|"
    r"occupy|tenancy)\b",
    re.I,
)
_MOVE_CTX_ZH = ("入住", "搬", "起租", "入伙", "搬进", "搬入", "开始租")


def _iso_or_none(y, m, d):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except (ValueError, TypeError):
        return None


def _next_first_of_month(month: int, today: date) -> Optional[str]:
    """First day of `month`'s NEXT occurrence relative to `today` (this year if still
    upcoming, else next year)."""
    cand = _iso_or_none(today.year, month, 1)
    if cand and cand < today.isoformat():
        return _iso_or_none(today.year + 1, month, 1)
    return cand


def _resolve_move_ymd(year, month, day, today: date) -> Optional[str]:
    """(maybe-yearless) day/month -> ISO. With a year, that exact date; without one,
    the next occurrence of that day/month relative to `today`."""
    if year:
        return _iso_or_none(year, month, day)
    cand = _iso_or_none(today.year, month, day)
    if cand and cand < today.isoformat():
        return _iso_or_none(today.year + 1, month, day)
    return cand


def _extract_move_in_date(text: str, *, now: date | None = None) -> Optional[str]:
    """Deterministic move-in / tenancy-start date from a user message -> ISO
    'YYYY-MM-DD', or None. English + Chinese.

    Fires ONLY when the text carries a move-in/start/available/from context word (EN)
    or 入住/搬/起租 (ZH), so a bare month name in unrelated prose ("September") or a
    stray number never becomes a criterion. A month with no year resolves to the FIRST
    day of that month's NEXT occurrence relative to `now` (today); 下个月 = first of
    next month. `now` is an injectable seam for deterministic testing; production uses
    today. Exported (module level) so the router agent can accumulate per-turn updates."""
    if not text or not str(text).strip():
        return None
    today = now or date.today()
    t = scoped_update_text(text).strip()
    if date_is_cleared(t) or is_criteria_consultation(t):
        return None
    t = '，'.join(part for part in re.split(r'[。;；!?！？，,]', t) if not is_hypothetical(part))
    # The housing form has one date field. An explicit correction to that named
    # field can refer back across a semicolon without repeating "move in".
    correction = re.search(r'表单(?:入住)?日期[^。!?]{0,90}(?:改回|改为|改成)\s*(\d{4})-(\d{1,2})-(\d{1,2})', t)
    if correction:
        return _iso_or_none(*correction.groups())
    ignored_date = re.compile(r'(?:today|now|今天|现在|当前日期)\s*(?:is|是|为|:|：)?\s*$|没有说过|没要求|不是|不要|never said|didn.t (?:say|ask)', re.I)
    full_dates = re.compile(r'(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\b\d{1,2}/\d{1,2}/\d{4}\b')
    t = full_dates.sub(lambda m: ' ' * len(m.group()) if ignored_date.search(
        re.split(r'[，,。;；]', t[:m.start()])[-1][-25:]) else m.group(), t)
    tl = t.lower()

    # Keep only predicates that declare housing availability/start. "from" and
    # "start" alone also occur in journeys, so they cannot license arbitrary dates.
    contexts = []
    for sentence in re.split(r'[。;；!?！\n]|(?<!\d)\.(?!\d)', t):
        for clause in re.split(r'\band (?=(?:check|find|show|calculate|verify) (?:the )?(?:route|journey|weather))'
                               r'|\b(?:and|but|then)\s+(?=(?:(?:i|we)\s+)?(?:(?:want|plan|need|intend)\s+to\s+|will\s+)?mov(?:e|ing)[ -]?in)'
                               r'|，|,', sentence, flags=re.I):
            if is_hypothetical(clause) or re.search(r'unchanged|not (?:a |my )?move.in|never said|没有说过|没要求|不变', clause, re.I):
                continue
            housing_context = bool(re.search(
                r'\b(?:mov(?:e|ing)[ -]?in|occupy|tenancy|available|avail)\b|'
                r'\b(?:move|moving)\s+(?:on|in|by|from)\b|'
                r'^\s*(?:starting from|from)\s+(?:\d|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))|'
                r'入住|搬入|搬进|搬家|起租|入伙|开始租', clause, re.I))
            transit_context = bool(re.search(r'\b(?:journey|train|transport|arriv\w*|depart\w*|weather)\b|天气|到达|出发', clause, re.I))
            if housing_context and (not transit_context or re.search(r'mov(?:e|ing)[ -]?in|入住|起租|tenancy', clause, re.I)):
                contexts.append(clause)
    t = '，'.join(contexts)
    if not t.strip():
        return None
    tl = t.lower()
    has_ctx_en = bool(_MOVE_CTX_EN.search(tl))
    has_ctx_zh = any(w in t for w in _MOVE_CTX_ZH)

    # 1) explicit full dates within the housing predicate.
    for m in re.finditer(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", t):
        if re.search(r'(?:today|now|今天|现在|当前日期)\s*(?:is|是|为|:|：)?\s*$', t[max(0, m.start()-20):m.start()], re.I):
            continue
        iso = _iso_or_none(m.group(1), m.group(2), m.group(3))
        if iso:
            return iso
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        iso = _iso_or_none(m.group(1), m.group(2), m.group(3))
        if iso:
            return iso
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", t)  # UK day-first
    if m:
        iso = _iso_or_none(m.group(3), m.group(2), m.group(1))
        if iso:
            return iso

    # 2) Chinese month-level — requires a ZH move-in context word.
    if has_ctx_zh:
        m = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?', t)
        if m:
            return _resolve_move_ymd(None, m.group(1), m.group(2), today)
        if "下个月" in t or "下月" in t:
            nm = today.month % 12 + 1
            ny = today.year + (1 if today.month == 12 else 0)
            iso = _iso_or_none(ny, nm, 1)
            if iso:
                return iso
        m = re.search(r"(\d{1,2})\s*月", t)  # 9月
        if m:
            iso = _next_first_of_month(int(m.group(1)), today)
            if iso:
                return iso
        for token, num in sorted(_ZH_MONTHS.items(), key=lambda kv: -len(kv[0])):
            if token in t:
                iso = _next_first_of_month(num, today)
                if iso:
                    return iso

    # 3) English month-level — requires an EN move-in context word.
    if has_ctx_en:
        # day month [year]  e.g. "1 Sep", "moving in on 1st September 2026"
        m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_EN_MONTH_RE})\b\.?\s*(\d{{4}})?", tl)
        if m:
            iso = _resolve_move_ymd(m.group(3), _EN_MONTHS[m.group(2)], int(m.group(1)), today)
            if iso:
                return iso
        # month day [year]  e.g. "from sept 1st", "September 15 2026"
        m = re.search(rf"\b({_EN_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s*,?\s*(\d{{4}}))?", tl)
        if m:
            iso = _resolve_move_ymd(m.group(3), _EN_MONTHS[m.group(1)], int(m.group(2)), today)
            if iso:
                return iso
        # month year  e.g. "available September 2026"
        m = re.search(rf"\b({_EN_MONTH_RE})\s+(\d{{4}})\b", tl)
        if m:
            iso = _resolve_move_ymd(int(m.group(2)), _EN_MONTHS[m.group(1)], 1, today)
            if iso:
                return iso
        # bare month — must be immediately preceded by a connector/context word so a
        # modal "may" ("I may move in") never resolves to the month of May.
        m = re.search(rf"\b(?:from|in|on|by|of|start(?:ing)?|available)\s+({_EN_MONTH_RE})\b", tl)
        if m:
            iso = _next_first_of_month(_EN_MONTHS[m.group(1)], today)
            if iso:
                return iso
    return None


def _valid_iso_date(value) -> Optional[str]:
    """A strict 'YYYY-MM-DD' real-calendar date string, else None. Used to sanitise a
    move_in_date arriving from accumulated state / the form before it is trusted."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        return None
    try:
        date.fromisoformat(v)
    except ValueError:
        return None
    return v


def _resolve_available_from(prop: dict) -> str:
    """Normalized availability for a formatted row: 'Available now', ISO 'YYYY-MM-DD',
    or '' (unknown). Prefers the detail-page enrichment (_available_from) over the
    rich-schema 'Available From' (normalize.py stores 'Contact agent' for unknown,
    which maps to '')."""
    v = str(prop.get('_available_from') or '').strip()
    if not v:
        rich = str(prop.get('Available From') or '').strip()
        if rich and rich.lower() != 'contact agent':
            v = rich
    if not v:
        return ''
    low = v.lower()
    if low in ('available now', 'now', 'immediately', 'asap', 'available immediately'):
        return 'Available now'
    if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        return v
    # Defensive: a raw/legacy value that slipped through unparsed -> parse it once.
    try:
        from core.scraping.onthemarket import parse_availability_date as _pad
        return _pad(v) or ''
    except Exception:
        return ''


def _availability_status(available_from: str, move_in_date) -> str:
    """Fit annotation vs. the desired move-in date (contract-exact, hardcoded-zh like
    budget_status). '' when no move_in_date criterion or availability unknown;
    '✅ 可入住' when available on/before the desired date (Available now counts);
    '⚠️ YYYY-MM-DD 起租' when only available AFTER it."""
    if not move_in_date or not available_from:
        return ''
    if available_from == 'Available now' or available_from <= move_in_date:
        return '✅ 可入住'
    return f'⚠️ {available_from} 起租'


def _is_late_availability(available_from: str, move_in_date) -> bool:
    """True when the listing is only available strictly AFTER the desired move-in date
    (used to stably demote — never exclude — such listings). Unknown / 'Available now'
    are never late."""
    if not move_in_date or not available_from or available_from == 'Available now':
        return False
    return available_from > move_in_date


# ─────────────────────────────────────────────────────────────────────────────
# Area / city extraction (conversational switch + bare place name + zh cities).
# ─────────────────────────────────────────────────────────────────────────────
# SINGLE SOURCE OF TRUTH for Chinese→English UK place names. Reused by the main
# fresh-search area path so a first message like "曼彻斯特的公寓" resolves without an
# LLM round-trip. Chinese place names are distinctive enough for a safe substring
# match. Nonsense / non-UK names (火星/月球/瓦坎达 …) are deliberately absent -> None.
_ZH_AREA_MAP = {
    "\u8c61\u5821": "Elephant and Castle",
    "\u5927\u8c61\u57ce\u5821": "Elephant and Castle",
    "曼彻斯特": "Manchester", "曼城": "Manchester", "伦敦": "London",
    "利兹": "Leeds", "爱丁堡": "Edinburgh", "布里斯托尔": "Bristol",
    "布里斯托": "Bristol", "格拉斯哥": "Glasgow", "卡迪夫": "Cardiff",
    "伯明翰": "Birmingham", "利物浦": "Liverpool", "谢菲尔德": "Sheffield",
    "纽卡斯尔": "Newcastle", "诺丁汉": "Nottingham", "莱斯特": "Leicester",
    "考文垂": "Coventry", "牛津": "Oxford", "剑桥": "Cambridge",
    "布莱顿": "Brighton", "约克": "York", "南安普顿": "Southampton",
    "南安普敦": "Southampton", "阿伯丁": "Aberdeen", "邓迪": "Dundee",
    "贝尔法斯特": "Belfast", "斯旺西": "Swansea", "诺里奇": "Norwich",
    "埃克塞特": "Exeter", "普利茅斯": "Plymouth", "德比": "Derby",
    "雷丁": "Reading", "巴斯": "Bath", "杜伦": "Durham",
    "坎特伯雷": "Canterbury", "格林威治": "Greenwich", "卡姆登": "Camden",
    "肯辛顿": "Kensington", "切尔西": "Chelsea",
}

# Curated set of well-known UK cities + London boroughs/areas (canonical spelling).
# Deliberately NOT exhaustive: it EXCLUDES names that collide with common English
# words (Bath, Reading, Angel, Bow, Kew…) so a bare "bath"/"reading" in a rental
# chat is never mistaken for an area switch (a false switch is worse than a miss).
# Chinese users still reach Bath/Reading via the unambiguous zh map above.
_MAJOR_UK_CITY_NAMES = [
    # major UK cities
    "London", "Manchester", "Birmingham", "Leeds", "Liverpool", "Sheffield",
    "Bristol", "Glasgow", "Edinburgh", "Cardiff", "Newcastle", "Nottingham",
    "Leicester", "Coventry", "Bradford", "Wolverhampton", "Sunderland",
    "Brighton", "Oxford", "Cambridge", "York", "Southampton", "Portsmouth",
    "Aberdeen", "Dundee", "Belfast", "Swansea", "Norwich", "Exeter",
    "Plymouth", "Hull", "Derby", "Milton Keynes", "Preston", "Bournemouth",
    "Middlesbrough", "Luton", "Northampton", "Ipswich", "Gloucester",
    "Chester", "Canterbury", "Lancaster", "Durham", "Guildford", "Watford",
    "Slough", "Salford", "Wigan", "Bolton", "Blackpool", "Huddersfield",
    "Doncaster", "Peterborough",
]

# Named separately from the cities above, and NOT a second copy of them: several of these
# share a name with a town elsewhere in the UK, and a bare mention means the London one in
# this product. "Stratford" geocoded to Stratford-upon-Avon, 150 km away, and a POI search
# then answered about Warwickshire — so anything geocoding a bare area name needs to know
# which half of the table it came from (see LONDON_AREAS).
_LONDON_AREA_NAMES = [
    # well-known London boroughs / areas
    "Camden", "Islington", "Hackney", "Shoreditch", "Hoxton", "Dalston",
    "Brixton", "Clapham", "Peckham", "Greenwich", "Ealing", "Wimbledon",
    "Stratford", "Kensington", "Chelsea", "Fulham", "Hammersmith",
    "Notting Hill", "Whitechapel", "Bethnal Green", "Bermondsey", "Deptford",
    "Lewisham", "Wandsworth", "Battersea", "Vauxhall", "Pimlico", "Marylebone",
    "Soho", "Mayfair", "Finsbury Park", "Tottenham", "Walthamstow", "Wembley",
    "Harrow", "High Barnet", "Barnet", "Enfield", "Richmond", "Kingston", "Putney", "Balham",
    "Tooting", "Canary Wharf", "Elephant and Castle", "Highbury", "Highgate",
    "Hampstead", "Kentish Town", "Holloway", "Stoke Newington", "Mile End",
    "Clerkenwell", "Farringdon", "Acton", "Chiswick", "Streatham", "Catford",
    "Leyton", "Leytonstone", "Hackney Wick", "New Cross", "Rotherhithe",
    "Kilburn", "Willesden", "Croydon", "Bromley", "Ilford", "Woolwich",
    "Camberwell", "Kennington", "Aldgate", "Southwark", "Wapping", "Limehouse",
    "Blackheath", "Forest Hill", "Shepherd's Bush", "Earl's Court",
    "King's Cross",
]

_KNOWN_AREA_NAMES = _MAJOR_UK_CITY_NAMES + _LONDON_AREA_NAMES
# key (lowercased, apostrophes removed) -> canonical spelling.
_KNOWN_AREAS = {re.sub(r"[’']", "", n.lower()): n for n in _KNOWN_AREA_NAMES}
# Same normalisation, London half only — the set a geocoder should read "London" into.
LONDON_AREAS = {re.sub(r"[’']", "", n.lower()): n for n in _LONDON_AREA_NAMES}

# ── Postcode-district -> London BOROUGH, for reconciling a provider's own area label ──
# OnTheMarket display names carry a borough word chosen by the agent, and it is sometimes
# simply wrong: "Rugby House 6 Great Ormond Street, Islington WC1N" was returned for a
# CAMDEN search and shown to a user who had asked for Camden — WC1N is Camden (Bloomsbury),
# not Islington. The postcode is the one part of that string the agent cannot get wrong.
#
# DELIBERATELY INCOMPLETE. Only districts that sit unambiguously inside ONE borough are
# listed; every district that straddles a boundary (N1 Islington/Hackney, EC1 Islington/
# City, SE1 Southwark/Lambeth, NW6 Camden/Brent, E20, N4) is OMITTED, because a confident
# wrong correction is worse than the label we started with. Sub-district overrides come
# first for the few 4-character codes that leave their district's borough.
_LONDON_DISTRICT_BOROUGH = {
    "WC1": "Camden", "NW1": "Camden", "NW3": "Camden", "NW5": "Camden",
    "N5": "Islington", "N7": "Islington",
    "E5": "Hackney", "E8": "Hackney", "E9": "Hackney", "N16": "Hackney",
    "E1": "Tower Hamlets", "E2": "Tower Hamlets", "E3": "Tower Hamlets",
    "E14": "Tower Hamlets",
    "SE5": "Southwark", "SE15": "Southwark", "SE16": "Southwark",
    "SE8": "Lewisham", "SE13": "Lewisham", "SE14": "Lewisham",
    "SE10": "Greenwich", "SE18": "Greenwich",
    "SE11": "Lambeth", "SW2": "Lambeth", "SW4": "Lambeth", "SW9": "Lambeth",
    "SW11": "Wandsworth", "SW17": "Wandsworth", "SW18": "Wandsworth",
    "W1": "Westminster", "W2": "Westminster", "W9": "Westminster",
    "SW1": "Westminster", "NW8": "Westminster",
    "SW3": "Kensington and Chelsea", "SW5": "Kensington and Chelsea",
    "SW7": "Kensington and Chelsea", "SW10": "Kensington and Chelsea",
    "W8": "Kensington and Chelsea", "W11": "Kensington and Chelsea",
    "W6": "Hammersmith and Fulham", "W12": "Hammersmith and Fulham",
    "W14": "Hammersmith and Fulham", "SW6": "Hammersmith and Fulham",
    "EC2": "City of London", "EC3": "City of London", "EC4": "City of London",
}
# 4-character outward codes whose borough differs from their district's.
_LONDON_OUTWARD_BOROUGH = {
    "WC1X": "Islington",
    "WC1R": "Islington",
}
# The borough words a listing string may claim. A label is only ever corrected when BOTH
# sides are boroughs: a neighbourhood name ("Shoreditch", "Bloomsbury") is not a borough
# claim and is never touched.
_LONDON_BOROUGH_NAMES = {
    b.lower(): b for b in
    set(_LONDON_DISTRICT_BOROUGH.values()) | set(_LONDON_OUTWARD_BOROUGH.values())
}

_OUTWARD_IN_ADDRESS_RE = re.compile(
    r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\b(?!\s*[0-9][A-Z]{2})", re.IGNORECASE)


def borough_for_postcode(text: str) -> str | None:
    """The London borough an outward postcode in ``text`` unambiguously sits in, else None.
    Never raises."""
    try:
        m = _OUTWARD_IN_ADDRESS_RE.search(str(text or ""))
        if not m:
            m = re.search(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*[0-9][A-Z]{2}\b",
                          str(text or ""), re.IGNORECASE)
        if not m:
            return None
        outward = m.group(1).upper()
        if outward in _LONDON_OUTWARD_BOROUGH:
            return _LONDON_OUTWARD_BOROUGH[outward]
        district = re.match(r"^([A-Z]{1,2}[0-9]+)", outward)
        return _LONDON_DISTRICT_BOROUGH.get(district.group(1)) if district else None
    except Exception:
        return None


def reconcile_area_label(address: object) -> str:
    """Replace a listing string's BOROUGH word when its own postcode contradicts it.

    "Rugby House 6 Great Ormond Street, Islington WC1N" -> "... Camden WC1N". WC1N is
    Bloomsbury, i.e. Camden; the borough word in an OnTheMarket display name is chosen by
    the agent and is sometimes simply wrong, which is how a Camden search answered a Camden
    question with a listing labelled Islington.

    Only a borough word ADJACENT TO THE POSTCODE is touched — in the postcode's own comma
    part, or alone in the part just before it. A borough name inside a STREET name
    ("Camden Park Road, London, E1 4AA") is never rewritten. Untouched, too, when the
    district is not in the curated unambiguous set, when nothing claims a borough, or when
    the two already agree, so this can only remove a contradiction and never invent a
    claim. Never raises."""
    try:
        text = " ".join(str(address or "").split())
        if not text:
            return text
        borough = borough_for_postcode(text)
        if not borough:
            return text
        parts = [p.strip() for p in text.split(",")]
        pc_idx = next((i for i, part in enumerate(parts)
                       if _OUTWARD_IN_ADDRESS_RE.search(part)
                       or re.search(r"\b[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}\b",
                                    part, re.IGNORECASE)), None)
        if pc_idx is None:
            return text
        targets = {pc_idx}
        if pc_idx > 0 and parts[pc_idx - 1].lower() in _LONDON_BOROUGH_NAMES:
            targets.add(pc_idx - 1)
        changed = False
        for idx in sorted(targets):
            part = parts[idx]
            for claimed_lower, claimed in _LONDON_BOROUGH_NAMES.items():
                if claimed == borough:
                    continue
                pattern = re.compile(r"\b" + re.escape(claimed) + r"\b", re.IGNORECASE)
                if pattern.search(part):
                    part = pattern.sub(borough, part)
                    changed = True
            parts[idx] = part
        return ", ".join(parts) if changed else text
    except Exception:
        return str(address or "")


# Switch / location cues that precede a place name in a conversational area change.
# The captured span is VALIDATED against _KNOWN_AREAS, so even a weak cue can never
# invent an area — only a recognised UK place is ever returned.
_AREA_CUE_RE = re.compile(
    r"\b(?:"
    r"make it|switch(?:ing)?\s+to|change(?:\s+it)?\s+to|move(?:\s+it)?\s+to|"
    r"relocat\w*\s+to|how about|what about|let[’']?s\s+try|lets\s+try|try|"
    r"actually|instead|look(?:ing)?\s+(?:in|at|around)|search(?:ing)?\s+(?:in|around)|"
    r"live\s+in|living\s+in|stay(?:ing)?\s+in|based\s+in|"
    r"flats?\s+in|places?\s+in|rooms?\s+in|somewhere\s+in|"
    r"property\s+in|properties\s+in|apartments?\s+in|"
    r"want|prefer|choose|pick|go\s+(?:with|to|for)|"
    r"around|near|in"
    r")\s+([a-z][a-z’'\-\.\s]{1,30})",
    re.IGNORECASE,
)

# Filler tokens stripped when testing whether a message is "essentially just a place".
_AREA_BARE_FILLER = re.compile(
    r"\b(?:please|thanks?|thank you|ok|okay|yeah?|yep|yes|sure|hi|hey|hello|"
    r"the|a|an|to|in|at|of|for|let[’']?s|go|going|with|maybe|actually|now|"
    r"move|switch|change|make|it|try|i|want|need|would|like|live|search|"
    r"look|looking|flat|flats|place|places|room|rooms|apartment|apartments|"
    r"area|city|somewhere|instead)\b",
    re.IGNORECASE,
)


def _match_known_area(fragment: str):
    """Canonical name of the LONGEST known UK city / London area that appears as a
    whole word in ``fragment``, else None. Apostrophes are normalised away so
    "shepherd's bush" matches; longest match wins so "notting hill" beats a stray
    partial."""
    if not fragment:
        return None
    t = re.sub(r"[’']", "", fragment.lower())
    best_key = None
    for key in _KNOWN_AREAS:
        if re.search(r"\b" + re.escape(key) + r"\b", t):
            if best_key is None or len(key) > len(best_key):
                best_key = key
    return _KNOWN_AREAS[best_key] if best_key else None


# An explicit AREA SWITCH names both the old and the new place, and the new one is not
# the one a left-to-right scan keeps: "Move the search from Islington to Camden",
# "Let's look at Camden instead of Islington", "Search in Camden rather than Islington"
# all searched Islington again under a "verified" header, and "Switch from Islington to
# Camden" changed nothing (2026-10-19 A-01, 3/3 live). A route question ("how long from
# Camden to King's Cross") has no switch verb and is not an area switch.
_SWITCH_FROM_TO = re.compile(
    r"\b(?:move|switch|change|swap|shift|update|moving|switching|changing)\b"
    r"[^.;!?\n]{0,40}?\bfrom\s+([a-z][a-z’'\-\s]{1,30}?)\s+to\s+([a-z][a-z’'\-\s]{1,30})", re.I)
_INSTEAD_OF = re.compile(
    r"([a-z][a-z’'\-\s]{0,30}?)\s*,?\s+(?:instead\s+of|rather\s+than)\s+([a-z][a-z’'\-\s]{1,30})",
    re.I)
_NOT_ROUTE_SWITCH = re.compile(
    r"\b(?:commute|commuting|journey|travel|route|trip|how long|every day|daily|by (?:bike|bus|"
    r"tube|train|car)|for work|to work|cycle|cycling)\b", re.I)


def _area_switch_target(text: str) -> str | None:
    """The NEW area of an explicit "from X to Y" / "Y instead of X" switch, else None."""
    source = str(text or "")
    for clause in re.split(r"[.;!?\n。；！？]", source):
        match = _SWITCH_FROM_TO.search(clause)
        # Route wording is tested only between the switch verb and the new place: "Switch
        # the search from Islington to Camden, it's closer to work" is a switch; "change my
        # commute from Islington to Camden" is not.
        if match and not _NOT_ROUTE_SWITCH.search(clause[match.start():match.start(2)]):
            old, new = _match_known_area(match.group(1)), _match_known_area(match.group(2))
            if old and new and old != new:
                return new
        match = _INSTEAD_OF.search(clause)
        if match:
            new, old = _match_known_area(match.group(1)), _match_known_area(match.group(2))
            if old and new and old != new:
                return new
    return None


def _drop_city_qualifier(areas: list[str], phrase: str) -> list[str]:
    """"Hackney, London" is ONE place: a London area qualified by its city, not two search
    areas. Restoring a saved default "1-bed in Hackney, London" searched all of London and
    labelled Chigwell IG7 and Shepherd's Bush W12 "verified" for a Hackney-only default
    (2026-10-19 A-06, 4/4)."""
    if "London" not in areas or len(areas) < 2:
        return areas
    london_areas = [a for a in areas if a != "London"
                    and re.sub(r"[’']", "", a.lower()) in LONDON_AREAS]
    if london_areas and re.search(
            r"(?:" + "|".join(re.escape(a) for a in london_areas) + r")\s*,\s*london\b",
            phrase, re.I):
        return [a for a in areas if a != "London"]
    return areas


def _extract_areas(text: str) -> list[str]:
    """Explicit residential place alternatives in order; [] means no update.

    Scan a named-place sequence governed by a home/search/keep predicate, rather
    than collecting every city mentioned in work, routes or weather questions.
    """
    from core.constraint_updates import housing_update_text, is_hypothetical
    source = housing_update_text(text)
    if not source:
        return []
    switched = _area_switch_target(source)
    if switched:
        return [switched]
    names = {**_KNOWN_AREAS, **_ZH_AREA_MAP}
    alternatives = '|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    place = rf'(?<![a-z])(?:{alternatives})(?![a-z])'
    sequence = re.compile(rf'({place})(?:\s*(?:,\s*(?:and\s+)?|、|和|或(?:者)?|及|与|\band\b|\bor\b)\s*({place}))+', re.I)
    for match in sequence.finditer(source):
        before = re.split(r'[。.!?;；\n]', source[:match.start()])[-1]
        phrase = match.group()
        clause = before + phrase
        if is_hypothetical(clause):
            continue
        if not re.search(r'\b(?:search|find|live|living|look|keep|focus|choose|flats?|homes?|properties|areas?)\b|找|住|搜|改为|改成|保留|恢复', before, re.I):
            continue
        if re.search(r'\b(?:work|job|office|route|journey|weather)\b|上班|工作|天气|路线', before, re.I) and not re.search(r'\b(?:live|search|find|keep|focus|flats?|homes?|properties)\b|找房|住在|搜索', before, re.I):
            continue
        areas = []
        for found in re.finditer(place, phrase, re.I):
            raw = found.group()
            key = re.sub(r"[’']", '', raw.lower())
            canonical = names.get(raw) or names.get(key)
            if canonical and canonical not in areas:
                areas.append(canonical)
        areas = _drop_city_qualifier(areas, phrase)
        if len(areas) > 1:
            return areas
    area = _extract_area(source)
    return [area] if area else []
def most_specific_known_area(text: str) -> str | None:
    """The MOST SPECIFIC curated UK place named anywhere in ``text``, else None.

    "Most specific" is a two-level order, not a longest-match: a London area/borough
    ("Shoreditch") beats a bare city ("London"), because a city name locates nothing a
    geocoder can centre a 300 m circle on. Within a level the longest name wins, so
    "Notting Hill" beats a stray "Hill"-shaped partial and "Milton Keynes" beats a
    partial city token.

    This exists because the legacy planner had no place resolver at all: it fell back to
    the literal string "London" for a POI lookup, and "London" is rejected by the geocode
    ladder as too generic (``_is_too_generic``), so "What's near Shoreditch in London?"
    returned "Could not find coordinates for address: London" and zero POIs. Returns the
    CANONICAL English spelling so downstream geocoding works whatever the input language.
    Never raises."""
    try:
        if not text or not str(text).strip():
            return None
        t = re.sub(r"[\u2019']", "", str(text).lower())
        best_london = best_city = None
        for key in _KNOWN_AREAS:
            if not re.search(r"\b" + re.escape(key) + r"\b", t):
                continue
            if key in LONDON_AREAS:
                if best_london is None or len(key) > len(best_london):
                    best_london = key
            elif best_city is None or len(key) > len(best_city):
                best_city = key
        winner = best_london or best_city
        return _KNOWN_AREAS[winner] if winner else None
    except Exception:
        return None


def _extract_area(text: str) -> str | None:
    """A NEW area/city the user is switching to (canonical ENGLISH name), else None.

    Recognises (a) Chinese UK city names via _ZH_AREA_MAP, (b) a conversational
    switch/location cue followed by a known place ("make it Manchester", "flats in
    Glasgow", "actually London"), and (c) a bare message that is essentially just a
    known UK place ("Manchester", "Shoreditch"). Deliberately CONSERVATIVE: only
    places in the curated set are returned, so nonsense / non-UK names ("Mars",
    "Wakanda", "火星") yield None and never trigger a bogus area switch. Always
    returns the canonical English name so downstream geocoding works regardless of
    the input language."""
    from core.constraint_updates import housing_update_text
    text = housing_update_text(text)
    if not text or not text.strip():
        return None
    switched = _area_switch_target(text)
    if switched:
        return switched
    # Coordinated residential alternatives belong to the list extractor.
    for clause in re.split(r'[。.!?;；\n]', text):
        if re.search(r'\b(?:search|find|keep|live|flats?)\b|找|住|搜', clause, re.I):
            found = [name for name in _KNOWN_AREAS if re.search(r'(?<![a-z])' + re.escape(name) + r'(?![a-z])', clause, re.I)]
            if len(found) > 1 and re.search(r'\band\b|\bor\b|或|和|、', clause, re.I):
                return None
    # Chinese places can govern a trailing residential predicate: "在Camden找个
    # 住处". Bind the place to that predicate before the background city fallback
    # ("来伦敦读研"), rather than treating every locative 在 as home-search intent.
    from core.constraint_updates import mention_is_requirement
    chinese_residence = re.compile(
        r"(?:住在|在)\s*([a-z][a-z’'\-\s]{0,35})\s*(?:附近|周边|一带)?"
        r"(?:找(?:(?:个|一[个套间])?(?:住处|住房|房(?:子|源)?|公寓|住宅|地方)|(?=\s*(?:[，,。.!?;；]|$)))|"
        r"租(?:(?:个|一[套间])?(?:房(?:子|源)?|公寓|住宅)|(?=\s*(?:[，,。.!?;；]|$))))", re.I)
    residential_places = []
    for match in chinese_residence.finditer(text):
        if mention_is_requirement(text, match.start(), match.end()):
            area = _match_known_area(match.group(1).lower())
            if area:
                residential_places.append((match.start(), area))
    # Residential intent outranks a school/work city mentioned earlier in the turn.
    residence_cue = re.compile(
        r'(?:先(?:看看|看|找)|住在|找房|改为|改成|恢复|切换到|只看|只在|仅在|只选|仅选|看看|'
        r'\b(?:restore|keep|focus on|search|live in|look in|look at|search in|search around|switch to|change to|make it|instead of|replace the area with)\s+)\s*([a-z][a-z\s\-]{0,35})', re.I)
    chosen = list(residential_places)
    for m in residence_cue.finditer(text):
        if not mention_is_requirement(text, m.start(), m.end()):
            continue
        # In "Camden instead of Watford", the residence is before "instead of".
        if m.group(0).lower().startswith('instead of'):
            continue
        phrase = m.group(1).lower()
        if re.search(r'\b(?:and|or)\b', phrase):
            places = {canonical for name, canonical in _KNOWN_AREAS.items()
                      if re.search(r'(?<![a-z])' + re.escape(name) + r'(?![a-z])', phrase)}
            if len(places) > 1:
                # A single-place parser cannot half-apply a multi-area request.
                return None
        area = _match_known_area(m.group(1).lower())
        if area:
            chosen.append((m.start(), area))
    replacement = re.search(r'([a-z][a-z\s\-]{0,25})\s+instead of\s+[a-z]', text, re.I)
    if replacement:
        area = _match_known_area(replacement.group(1).lower())
        if area:
            chosen.append((replacement.start(), area))
    if chosen:
        return sorted(chosen, key=lambda item: item[0])[-1][1]
    # (a) Chinese place names — distinctive, safe substring match. The MOST SPECIFIC match
    # wins, not the first one in table order: "我想在伦敦卡姆登租..." contains both 伦敦
    # (London) and 卡姆登 (Camden), and returning the city dropped the borough the user
    # actually asked for — a Camden request then searched all of London. A London
    # area/borough beats a bare city; within a level the longest key wins.
    _zh_london = _zh_city = None
    for zh, canon in _ZH_AREA_MAP.items():
        for match in re.finditer(re.escape(zh), text):
            prefix = re.split(r'[。！？!?;；\n，,:：]', text[:match.start()])[-1][-45:]
            if is_hypothetical(clause_at(text, match.start(), match.end())) or re.search(
                    r'(?:不(?:要|是|需要|想|考虑)|取消|删除|移除|去掉|排除)[^，。；;!?]{0,14}$', prefix):
                continue
            if re.sub(r"[\u2019']", "", canon.lower()) in LONDON_AREAS:
                if _zh_london is None or len(zh) > len(_zh_london[0]):
                    _zh_london = (zh, canon)
            elif _zh_city is None or len(zh) > len(_zh_city[0]):
                _zh_city = (zh, canon)
            break
    if _zh_london or _zh_city:
        return (_zh_london or _zh_city)[1]
    t = text.lower().strip()
    if re.search(r'只(?:修改|改|调整)|(?:备用|主)方案', t):
        for key in sorted(_KNOWN_AREAS, key=len, reverse=True):
            if re.search(r'(?<![a-z])' + re.escape(key) + r'(?![a-z])', t):
                return _KNOWN_AREAS[key]
    # (b) explicit switch / location cue + a VALIDATED known place.
    for m in _AREA_CUE_RE.finditer(t):
        canon = _match_known_area(m.group(1))
        if canon:
            return canon
    # (c) bare place: strip punctuation + filler; the remainder IS a known place.
    bare = re.sub(r"[^a-z’'\-\s]", " ", t)
    bare = _AREA_BARE_FILLER.sub(" ", bare)
    bare = re.sub(r"[’']", "", bare)
    bare = " ".join(bare.split())
    if bare:
        canon = _KNOWN_AREAS.get(bare)
        if canon:
            return canon
    return None


# Phrases meaning "remove/clear any budget limit" — English + Chinese. Distinct from
# a normal budget statement ("budget is £1000", "under 1500"), which must NOT fire.
_BUDGET_CLEAR_PHRASES = (
    "no budget", "no price limit", "no budget limit", "no max budget",
    "no maximum budget",
    "remove the budget", "remove budget", "remove my budget",
    "clear the budget", "clear budget", "drop the budget", "lift the budget",
    "forget the budget", "forget budget", "forget about the budget",
    "without a budget", "without budget", "budget-free", "budget free",
    "any price", "any budget", "show all prices", "show me all prices",
    "money is no object", "cost is no object", "regardless of price",
    "regardless of budget", "whatever the price", "whatever the cost",
    # Chinese
    "预算不限", "不限预算", "没有预算限制", "无预算限制", "预算无上限",
    "价格不限", "不限价格", "不限价", "预算无所谓", "预算不是问题",
    "多少钱都行", "多少钱都可以", "价格无所谓", "不在乎预算", "不在乎价格",
    "预算没有限制",
)

# Regex forms carrying an apostrophe / word gap ("budget doesn't matter",
# "price is not a concern", "don't care about the budget", "no spending limit").
_BUDGET_CLEAR_PATTERNS = (
    r"\b(?:budget|price|cost)\s+(?:really\s+)?(?:does\s*n[’']?t|do\s*n[’']?t|does\s+not|do\s+not)\s+matter\b",
    r"\b(?:budget|price|cost)\s+(?:is\s+not|isn[’']?t|is\s+n[’']?t)\s+(?:a\s+)?(?:concern|issue|problem)\b",
    r"\bno\s+(?:budget|price|spending|cost)\s+(?:limit|cap|maximum|max)\b",
    r"\bdo\s*n[’']?t\s+(?:care|worry)\s+about\s+(?:the\s+|my\s+)?(?:budget|price|cost)\b",
)


def _extract_budget_clear(text: str) -> bool:
    """True when the user asks to REMOVE/CLEAR any budget limit ("no budget",
    "any price", "budget doesn't matter", "预算不限"), letting the caller reset an
    accumulated max_budget. Deterministic and precise: a normal budget statement
    ("budget is £1000", "under 1500") returns False."""
    if not text:
        return False
    t = text.lower()
    if any(p in t for p in _BUDGET_CLEAR_PHRASES):
        return True
    return any(re.search(p, t) for p in _BUDGET_CLEAR_PATTERNS)


# Hard filters a model is NOT allowed to originate. Each maps to the deterministic
# extractor that can ground it in the user's own words.
_GROUNDED_HARD_FIELDS = ("max_budget", "bedrooms", "room_type")


# A CEILING marker introducing the amount ("最多 2200", "under £2200"). Guards
# detect_relative_budget_delta, whose loose ZH pattern `(?:提高|…|加|涨|多)\s*(\d…)` matches the
# single character 多 INSIDE 最多, so the plain ceiling "预算每月最多2200英镑" is read as a
# +2200 DELTA. Wired into the accumulator that clears max_budget for a delta with no prior
# ceiling, that turned a first-turn Chinese budget statement into NO BUDGET FILTER AT ALL
# (reproduced live: fixtest-f4-rz2-legacy, max_budget null where the pre-change run recorded
# 2000). Its EN half (`at most|max|up to|to`) already lives in search_readiness._ABSOLUTE_TARGET;
# 最多/不超过/以内 and under/below/no more than do not, so they are covered here until F3 folds
# them into that regex — see the F3 request in the report.
_BUDGET_CEILING_MARKER = re.compile(
    r"(?:最多|至多|不超过|不多于|不高于|最高|上限|以内|以下)\s*(?:£)?\s*(\d[\d,]*(?:\.\d+)?)"
    r"|\b(?:under|below|no\s+more\s+than|not\s+more\s+than|within)\s*(?:£|gbp\s*)?"
    r"(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE)


def _states_relative_budget_edit(text) -> bool:
    """True when ``text`` states a budget DELTA ("raise the budget by £200", 「预算提高200镑」)
    rather than a ceiling.

    Wraps the one detector that knows the difference
    (``core.search_readiness.detect_relative_budget_delta``) so every budget-override site
    asks the same question, and adds ONE guard it is missing: when the amount the detector
    read is the same amount a CEILING marker introduces, the message states a ceiling, not an
    edit. False for "raise the budget TO £2400" (the detector's own guard) and for
    「预算每月最多2200英镑」 (this one). Never raises: if the detector is unavailable the
    callers keep their old behaviour rather than losing the override entirely."""
    try:
        from core.search_readiness import detect_relative_budget_delta
        delta = detect_relative_budget_delta(text)
        if delta is None:
            return False
        for match in _BUDGET_CEILING_MARKER.finditer(str(text or "")):
            raw = (match.group(1) or match.group(2) or "").replace(",", "")
            try:
                if abs(float(raw) - abs(float(delta))) < 1e-9:
                    return False
            except ValueError:
                continue
        return True
    except Exception:
        return False


def ground_hard_constraints(params: dict, accumulated: dict = None,
                            current_message: str = "") -> tuple[dict, list[str]]:
    """Strip HARD search filters the model invented rather than heard.

    ``max_budget`` / ``bedrooms`` / ``room_type`` narrow the result set to nothing when
    they are wrong, and the user never sees them — the reply just says "no matches". A
    model that fills a gap with a plausible default (£2000, 1 bedroom, a shared room)
    therefore silently answers a question nobody asked, and every downstream honesty
    guard then reports a failure against constraints the user never stated.

    A value is legitimate only when it is either already in ``accumulated`` (deterministically
    extracted on an earlier turn, or set from the form) or extractable from THIS turn's
    message by the same deterministic extractors the tool itself uses. Anything else is
    dropped, and a value that CONTRADICTS the accumulated one without appearing in this
    turn's message is reset to the accumulated value. Soft signals (features, preferences,
    sort order) are untouched — the model is free to shape those.

    Returns ``(params, dropped)`` where ``dropped`` names each corrected field, for logging.
    Not applied to ``areas``: an area is visible in the reply, and the tool already treats
    a bare place name in this turn's message as authoritative. Not applied to the form/direct
    search path either — that caller passes the user's own input and never goes through an
    injection point.

    A phrasing neither the extractor nor the upstream preference extraction caught is dropped
    too, so the failure direction is a BROADER search (more listings, visible to the user) and
    never a narrower one (an empty result the user cannot account for).
    """
    p = dict(params or {})
    acc = accumulated if isinstance(accumulated, dict) else {}
    message = str(current_message or "")
    # Re-apply only authenticated current text at this boundary, so callers that
    # have not run the state node yet still use the current explicit update.
    if message:
        from core.langgraph_agent import _apply_explicit_criteria_updates
        # A relative edit is resolved once, by the state node (see the flag's docstring).
        acc = _apply_explicit_criteria_updates(acc, message, apply_relative_budget=False)
    dropped: list[str] = []

    def _correct(field: str, allowed, fallback):
        """Keep p[field] when it matches something the user actually said."""
        if p.get(field) is None or field not in p:
            return
        if any(value is not None and value == p[field] for value in allowed):
            return
        if fallback is None:
            p.pop(field, None)
        else:
            p[field] = fallback
        dropped.append(field)

    # Monthly is the canonical unit at the model/tool boundary. Pair every
    # corrected number with its period, preventing £350/week becoming £350/month.
    fresh_budget, fresh_period = _extract_budget(message)
    # P1-1: a figure stated as a DELTA ("raise the budget by £200") is not a ceiling the
    # user said, so it must not enter the allowed set — that is how a model-supplied
    # max_budget of 200 used to be blessed as "something the user actually said". The
    # accumulated ceiling (already resolved by the graph) is the only allowed value; with
    # no accumulated ceiling the filter is DROPPED rather than set to the delta.
    if fresh_budget and _states_relative_budget_edit(message):
        fresh_budget, fresh_period = None, None
    acc_budget = acc.get("max_budget")
    allowed_budget = [fresh_budget] if fresh_budget is not None else [acc_budget]
    if fresh_budget and fresh_period in ("week", "weekly", "pw"):
        allowed_budget.append(monthly_from_weekly(fresh_budget))
    _correct("max_budget", allowed_budget, fresh_budget or acc_budget)
    if fresh_budget is not None:
        p['max_budget'] = monthly_from_weekly(fresh_budget) if fresh_period == 'week' else fresh_budget
        p['budget_period'] = 'month'
    elif acc_budget is not None and p.get('max_budget') == acc_budget:
        p['budget_period'] = acc.get('budget_period') or 'month'


    # Bedrooms: _extract_bedrooms returns (min, max); an exact count is min == max.
    fresh_beds = _extract_bedrooms(message)
    fresh_bedrooms = fresh_beds[0] if fresh_beds else None
    acc_bedrooms = acc.get("bedrooms")
    _correct("bedrooms", [fresh_bedrooms, acc_bedrooms],
             fresh_bedrooms if fresh_bedrooms is not None else acc_bedrooms)
    if fresh_beds:
        p['bedrooms'] = fresh_beds[0]
        p['bedrooms_minimum'] = fresh_beds[0] != fresh_beds[1]
    elif acc_bedrooms is not None:
        p['bedrooms'] = acc_bedrooms
        p['bedrooms_minimum'] = bool(acc.get('bedrooms_minimum'))

    # Room type: compare canonically, so 'en-suite' vs 'ensuite' is not a false mismatch.
    fresh_rt = _extract_room_type(message)
    acc_rt = _normalize_room_type(acc.get("room_type"))
    model_rt = _normalize_room_type(p.get("room_type"))
    if p.get("room_type") is not None:
        if model_rt is not None and model_rt in (fresh_rt, acc_rt):
            p["room_type"] = model_rt
        else:
            resolved = fresh_rt or acc_rt
            if resolved is None:
                p.pop("room_type", None)
            else:
                p["room_type"] = resolved
            dropped.append("room_type")

    if fresh_rt or acc_rt:
        p['room_type'] = fresh_rt or acc_rt
    if date_is_cleared(message) or ('move_in_date' in acc and acc['move_in_date'] is None):
        p.pop('move_in_date', None)
    if p.get('property_features'):
        from core.langgraph_agent import _extract_accessibility_requirements
        required = set(_extract_accessibility_requirements(message)['required'])
        allowed = {_canonical_property_feature(f) for f in acc.get('property_features') or []} | required
        p['property_features'] = [f for f in p['property_features']
                                 if not _requires_explicit_feature_evidence(_canonical_property_feature(f))
                                 or _canonical_property_feature(f) in allowed]

    from core.constraint_updates import protect_confirmed_criteria
    protected = protect_confirmed_criteria({}, acc)
    aliases = {'max_travel_time': 'max_commute_time', 'soft_preferences': 'accumulated_preferences'}
    for field, source in (acc.get('constraint_sources') or {}).items():
        if field in protected:
            parameter = aliases.get(field, field)
            if parameter not in ('destination', 'plans', 'constraint_sources'):
                if parameter in p and p[parameter] != protected[field] and parameter not in dropped:
                    dropped.append(parameter)
                p[parameter] = protected[field]
    if 'areas' in (acc.get('constraint_sources') or {}):
        p['areas'], p['area'] = protected.get('areas', []), protected.get('area')
    if 'commute_destination' in (acc.get('constraint_sources') or {}):
        p['commute_destination'] = protected.get('commute_destination')
    # A cleared criterion (no_commute's commute_destination, a removed budget, a cleared
    # move-in date) is ABSENT, never None: the tool's input contract is strict and checks
    # the authored schema, so an explicit null failed the whole search (A-02).
    from core.strict_schema import strip_null_args
    return strip_null_args(p), dropped


def _strip_memory_block(text: str) -> str:
    """Remove the long-term-memory block (and any conversation-history prefix) that
    the agent PREPENDS to ``user_query`` before calling this tool, leaving only the
    user's actual current-turn text.

    HARD search filters (area / budget / room_type / bedrooms) must never be
    extracted from a PRIOR conversation's remembered criteria — that is the
    cross-conversation bleed defect. Mirrors ``langgraph_agent._current_message`` so
    the phrasings stay in sync, and is kept LOCAL so the tool defends itself
    regardless of caller (a plain query with no memory block passes through
    unchanged)."""
    q = text or ""
    if q.startswith("What I remember about this user:"):
        sep = "\n\n"
        idx = q.find(sep)
        if idx != -1:
            q = q[idx + 2:]
    for marker in ("Current user message:", "answer to the clarification question:"):
        if marker in q:
            q = q.split(marker)[-1]
    return q.strip()


def _commute_phrase(max_commute_time, location: str) -> str:
    """User-facing commute clause. Empty when the limit is the no-limit sentinel so
    we never print 'within 999 min'."""
    try:
        if max_commute_time is None or int(max_commute_time) >= NO_COMMUTE_LIMIT:
            return ""
    except (TypeError, ValueError):
        return ""
    return f" within {int(max_commute_time)} min of {location}"


def _price_stats(results):
    """(min, median, max) monthly price across the returned listings, or None.
    Reads the numeric ``price`` each candidate carries before formatting."""
    prices = sorted(int(p.get('price')) for p in results
                    if isinstance(p, dict) and p.get('price'))
    if not prices:
        return None
    return prices[0], prices[len(prices) // 2], prices[-1]


def _found_summary(results, n_perfect: int, n_soft: int, max_budget, max_commute_time,
                   area: str, commute_target: str = None, room_type: str = None,
                   is_cjk: bool = False, move_in_date: str = None,
                   area_counts=None) -> str:
    """Informative, context-varying result headline (Deliverable 4): result count +
    area, the price range of the returned listings (min–median–max), and a one-line
    recap of the applied filters (budget / room type / commute). Localized zh/en and
    including the right-panel hint, so callers use it verbatim (no English-only
    suffix bolted on afterwards). Never emits '999 min' or '£None'.

    🆕 Multi-area: when ``area_counts`` (an ordered list of ``(area_name, count)``) has
    more than one entry, the headline names EVERY searched area with its per-area count
    — including any that returned 0 — instead of only the primary ``area`` (the Bug 4
    fix). Single-area callers pass ``area_counts=None`` and get the unchanged output."""
    # The "found N" claim counts ONLY the main, strictly-in-budget recommendations
    # (n_perfect). Over-budget soft-expansion listings (n_soft) are surfaced separately
    # as clearly-labelled alternatives and must NEVER be counted here or described as
    # satisfying the budget (product-correctness requirement H2).
    n_total = n_perfect
    stats = _price_stats(results)
    rt_label = _room_type_label(room_type, is_cjk) if room_type else None
    real_commute = commute_target and _commute_phrase(max_commute_time, commute_target)

    # --- applied-filter recap (only the criteria actually in effect) ---
    filt = []
    if is_cjk:
        if max_budget:
            filt.append(f"预算 ≤£{max_budget}/月")
        if rt_label:
            filt.append(f"房型 {rt_label}")
        if real_commute:
            filt.append(f"通勤 ≤{int(max_commute_time)} 分钟到 {commute_target}")
        elif commute_target:
            filt.append(f"通勤至 {commute_target}")
        if move_in_date:
            filt.append(f"期望入住 ≥{move_in_date}")
    else:
        if max_budget:
            filt.append(f"budget ≤£{max_budget}/month")
        if rt_label:
            filt.append(f"room type {rt_label}")
        if real_commute:
            filt.append(f"commute ≤{int(max_commute_time)} min to {commute_target}")
        elif commute_target:
            filt.append(f"commute to {commute_target}")
        if move_in_date:
            filt.append(f"move-in ≥{move_in_date}")

    # 🆕 多区域标题：列出所有搜索区域（含返回 0 套的区域）+ 每区域计数，而非只提主区域。
    if area_counts and len(area_counts) > 1:
        names = [a for a, _ in area_counts]
        if is_cjk:
            breakdown = "、".join(f"{a} {c} 套" for a, c in area_counts)
            s = "在 " + "、".join(names) + f" 为你找到 {n_total} 套当前房源"
            if rt_label:
                s += f"（{rt_label}）"
            s += f"（分区：{breakdown}）。"
            if stats:
                lo, mid, hi = stats
                s += f"价格约 £{lo}/月。" if lo == hi else f"价格区间 £{lo}–£{hi}/月（中位约 £{mid}）。"
            if filt:
                s += "已应用筛选：" + "、".join(filt) + "。"
            if n_soft and max_budget:
                s += f"另有 {n_soft} 套略超预算的备选单独列出（未计入上述结果，且不在预算内）。"
            s += "完整房源见右侧列表。"
            return s
        breakdown = ", ".join(f"{a}: {c}" for a, c in area_counts)
        s = f"I found {n_total} current listing{'s' if n_total != 1 else ''} across " + ", ".join(names)
        if rt_label:
            s += f" ({rt_label})"
        s += f" (by area — {breakdown})."
        if stats:
            lo, mid, hi = stats
            s += f" Around £{lo}/month." if lo == hi else f" Prices range £{lo}–£{hi}/month (median ~£{mid})."
        if filt:
            s += " Filters applied: " + ", ".join(filt) + "."
        if n_soft and max_budget:
            s += (f" Separately, {n_soft} over-budget option{'s' if n_soft != 1 else ''} "
                  f"(up to 15% above your budget) are listed as alternatives — not counted "
                  f"above and not within your budget.")
        s += " See the full listings in the right-hand panel."
        return s

    if is_cjk:
        s = f"在 {area} 为你找到 {n_total} 套当前房源"
        if rt_label:
            s += f"（{rt_label}）"
        s += "。"
        if stats:
            lo, mid, hi = stats
            s += f"价格约 £{lo}/月。" if lo == hi else f"价格区间 £{lo}–£{hi}/月（中位约 £{mid}）。"
        if filt:
            s += "已应用筛选：" + "、".join(filt) + "。"
        if n_soft and max_budget:
            s += f"另有 {n_soft} 套略超预算的备选单独列出（未计入上述结果，且不在预算内）。"
        s += "完整房源见右侧列表。"
        return s

    s = f"I found {n_total} current listing{'s' if n_total != 1 else ''} in {area}"
    if rt_label:
        s += f" ({rt_label})"
    s += "."
    if stats:
        lo, mid, hi = stats
        s += f" Around £{lo}/month." if lo == hi else f" Prices range £{lo}–£{hi}/month (median ~£{mid})."
    if filt:
        s += " Filters applied: " + ", ".join(filt) + "."
    if n_soft and max_budget:
        s += (f" Separately, {n_soft} over-budget option{'s' if n_soft != 1 else ''} "
              f"(up to 15% above your budget) are listed as alternatives — not counted "
              f"above and not within your budget.")
    s += " See the full listings in the right-hand panel."
    return s


def _soft_gate_question(missing, is_cjk: bool, move_in_missing: bool = False) -> str:
    """Localized soft-criteria clarification, listing ONLY the actually-missing
    recommended fields (budget / room_type / commute). When ``move_in_missing`` the
    OPTIONAL move-in date is mentioned as skippable — it never triggers the gate on
    its own, it only rides along when the gate already fires for a recommended field.

    The 'commute' clause asks ONLY whether the user commutes and WHERE TO — the maximum
    commute time is an OPTIONAL field and is deliberately never asked for or flagged (it
    never filters unless the user volunteers a real limit)."""
    if is_cjk:
        bits = []
        if 'room_type' in missing:
            bits.append("您想要什么房型（Studio / 独立卫浴 ensuite / 合租 shared）？")
        if 'budget' in missing:
            bits.append("每月预算大概多少？")
        if 'commute' in missing:
            bits.append("需要考虑通勤吗？如果需要，请告诉我通勤到哪里；如果不需要，可以说“不通勤”。")
        if move_in_missing:
            bits.append("什么时候入住（可不填）？")
        return ("在搜索之前，我想先确认几个条件：" + "".join(bits) +
                "您也可以直接说“继续搜索”，或在右侧的搜索条件面板补充后点击搜索。")
    bits = []
    if 'room_type' in missing:
        bits.append("what room type you'd like (studio / en-suite / shared)?")
    if 'budget' in missing:
        bits.append("what your monthly budget is?")
    if 'commute' in missing:
        bits.append("whether you need to consider a commute (if so, where to; "
                    "if not, just say \"no commute\")?")
    if move_in_missing:
        bits.append("when you'd like to move in (optional)?")
    return ("Before I search, could you confirm a couple of things: " + " ".join(bits) +
            " You can also just say \"continue\" to search anyway, or fill in the "
            "search-criteria panel on the right and press Search.")


def _partial_note(incomplete_areas, is_cjk: bool) -> str:
    """Explain incomplete coverage without exposing model instructions."""
    areas = [a for a in (incomplete_areas or []) if a]
    if not areas:
        return ""
    if is_cjk:
        return f"部分结果：搜索时间已用完，{'、'.join(areas)} 尚未完成检索，那里可能还有更多房源。"
    return (f"Partial results: the search time budget was reached before {', '.join(areas)} "
            "could be fully searched, so more listings may exist there.")


def _commute_note(is_cjk: bool) -> str:
    """Explain missing commute evidence to the user."""
    if is_cjk:
        return "本轮尚未核实这些房源的通勤时间；缩小区域或指定目的地后可以继续核实。"
    return ("Commute times have not been verified for these listings this round. "
            "A narrower area or a specific destination can help complete that check.")


def _no_results_message(area: str, is_cjk: bool) -> str:
    """Honest 'nothing found' text, language-aware, always pointing at the form."""
    if is_cjk:
        return (f"本次检索数据中，在 {area} 附近没有找到符合条件的房源。"
                f"请检查区域拼写，或调整预算/区域后重试（可使用右侧搜索表单）。")
    return (f"I couldn't find listings matching your criteria near {area} "
            f"in the retrieved data. Please check the area spelling, or adjust the budget/area and "
            f"try again (you can also use the search form on the right).")


def _clean_explanation(desc, travel_min, location, travel_source=None):
    """Single-source the commute time in the explanation: strip the static CSV
    Bus/Walk/Cycle/Drive minutes (which disagree with the live TfL time) and append
    the real TfL transit time so the card never shows two conflicting commute times.
    When there is no commute data (no target / no computed time) NO suffix is added,
    so a no-commute search never shows '0 min to None'.

    The LABEL must match the PROVENANCE of the number (``travel_source``, see
    ``_TRAVEL_SOURCE_*``). Only a real routing call may be presented as "TfL transit"; a
    straight-line coordinate estimate is labelled an estimate. Both the card and the model
    read this string, and an estimate presented as a measured journey time is precisely the
    claim the final commute guard then has no evidence for."""
    desc = desc or ''
    cleaned = re.sub(r'(Bus|Walk|Cycle|Drive)[ ]*[0-9]+[ ]*min[,. ]*', '', desc, flags=re.IGNORECASE)
    cleaned = ' '.join(cleaned.split()).strip().rstrip('.,').strip()[:120]
    suffix = ''
    if travel_min is not None and location:
        try:
            minutes = int(travel_min)
        except (TypeError, ValueError):
            minutes = None
        if minutes is not None:
            # Only a POSITIVELY established itinerary earns the measured label. An unset or
            # unrecognised source is not evidence of routing, and defaulting it to "TfL"
            # overstates exactly where the caller knows least.
            suffix = (f" (TfL transit: {minutes} min to {location})"
                      if travel_source == _TRAVEL_SOURCE_ROUTING
                      else f" (estimated from coordinates: ~{minutes} min to {location}, "
                           f"not a verified journey time)")
    return (cleaned + suffix).strip()


# Provenance of a listing's `travel_time`. `routing` = a real TfL itinerary; `estimate` = a
# straight-line approximation (coord_commute_minutes, or calculate_travel_time's own
# no-route fallback). Carried on the row as `travel_time_source` so the label, the
# model-facing payload and any downstream evidence check all read the same fact.
_TRAVEL_SOURCE_ROUTING = 'routing'
_TRAVEL_SOURCE_ESTIMATE = 'estimate'


def _travel_provenance(origin, destination, minutes, mode: str = 'transit'):
    """Classify a minutes figure as measured or estimated — from the basis, never by assumption.

    ``calculate_travel_time`` returns a bare int on BOTH of its branches: a real TfL itinerary,
    and a calibrated straight-line guess when TfL has no route for the pair
    (``maps_service.calculate_travel_time``). Treating every non-None return as routed is
    exactly how a haversine guess reaches a card labelled "TfL transit: 17 min".

    ``routing`` is therefore returned only when the cached basis payload for this pair proves a
    real itinerary. A cache miss, an unrecognised source or a stubbed producer all fall to
    ``estimate``: the label understates rather than overstates, which is the only safe
    direction for a figure a user reads.
    """
    if minutes is None:
        return None
    try:
        from core.commute_basis import is_measured
        from core.maps_service import travel_basis_if_known
        details = travel_basis_if_known(origin, destination, mode)
    except Exception:
        return _TRAVEL_SOURCE_ESTIMATE
    if not isinstance(details, dict) or not is_measured(details.get('source')):
        return _TRAVEL_SOURCE_ESTIMATE
    # The basis must vouch for THIS figure, not merely for the pair. A cached itinerary of 17
    # minutes says nothing about a 9 the caller is holding — different mode, a refit, or a
    # stubbed producer all produce that mismatch, and labelling it "TfL transit" would attach
    # a real itinerary's authority to a number that itinerary never produced.
    measured = details.get('duration_minutes')
    if measured is None:
        return _TRAVEL_SOURCE_ESTIMATE
    try:
        if int(measured) != int(minutes):
            return _TRAVEL_SOURCE_ESTIMATE
    except (TypeError, ValueError):
        return _TRAVEL_SOURCE_ESTIMATE
    return _TRAVEL_SOURCE_ROUTING


def _travel_time_label(minutes, target, source) -> str:
    """Card/model text for a listing's commute minutes, marked with its provenance.

    Unknown provenance reads as an estimate, never as a measured journey — same direction as
    ``commute_basis.is_measured``, which treats anything unrecognised as not measured.
    """
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return ''
    if source == _TRAVEL_SOURCE_ROUTING:
        return f"{value} min to {target}"
    return f"~{value} min to {target} (estimated, unverified)"


_RAG_COORDINATOR = None
# Native providers share their encoder and fork per-request indexes. The lock
# protects older injected providers while each read rebinds its request snapshot.
_RAG_LOCK = threading.Lock()

# Construction/prewarm is serialized independently from request-local recall.
_RAG_BUILD_LOCK = threading.Lock()

# Idempotent one-shot guard for the background embedding-store pre-warm.
_PREWARM_LOCK = threading.Lock()
_PREWARM_STARTED = False


class _DeterministicPropertyStore:
    """Minimal in-process replacement used when optional RAG dependencies fail."""

    def __init__(self):
        self.rows = []

    def build_index(self, rows):
        self.rows = list(rows or [])

    def search(self, _query, top_k=10):
        """Return the deterministic source-order prefix expected by similar recall."""
        return list(self.rows)[:top_k]


class _DeterministicRAGCoordinator:
    """Preserve the RAG interface while returning live listings in source order."""

    def __init__(self):
        self.property_store = _DeterministicPropertyStore()

    def for_search(self):
        return type(self)()

    def enhanced_search(self, _query, _criteria):
        return list(self.property_store.rows), "", {}


def set_rag_coordinator(coordinator):
    """Inject the process-wide coordinator built by the composition root."""
    global _RAG_COORDINATOR
    _RAG_COORDINATOR = coordinator


def _get_rag_coordinator():
    """Cached RAGCoordinator (sentence-transformers model loaded once and reused —
    reloading it cost ~15-20s per call). The FAISS index is intentionally NOT
    pre-built from a bundled dataset here: each search rebuilds the index over the
    real, city-correct listings it fetched on demand, so the semantic ranking and
    the 'similar' fallback can never surface stale/other-city demo rows."""
    global _RAG_COORDINATOR
    if _RAG_COORDINATOR is None:
        # Serialise construction so the pre-warm thread and a first real search don't both
        # pay the (~18-20s) model load. Assign only AFTER RAGCoordinator() fully returns, so
        # a concurrent readiness probe sees "not ready" until the model is actually loaded.
        with _RAG_BUILD_LOCK:
            if _RAG_COORDINATOR is None:
                try:
                    from rag.rag_coordinator import RAGCoordinator
                    _RAG_COORDINATOR = RAGCoordinator()
                except Exception as exc:
                    # A missing FAISS/embedding dependency must not turn a valid live
                    # listing response into a false "no results" outcome.
                    print(f"[search] RAG unavailable; using deterministic ranking error_type={type(exc).__name__}")
                    _RAG_COORDINATOR = _DeterministicRAGCoordinator()
    return _RAG_COORDINATOR


def _embedding_store_ready() -> bool:
    """Cheap, side-effect-free probe: is the RAG coordinator ALREADY constructed with its
    sentence-transformer model loaded? NEVER triggers construction — probing must not pay
    the ~18-20s cold load we are trying to keep off the deadline path. Contract:
      * coordinator not yet built (singleton is None)      -> False (cold / loading);
      * a deterministic-fallback coordinator (no model)    -> True (nothing to load);
      * a store exposing is_ready()                         -> its verdict;
      * any other already-constructed coordinator          -> True (assume warm).
    """
    coord = _RAG_COORDINATOR
    if coord is None:
        return False
    store = getattr(coord, "property_store", None)
    probe = getattr(store, "is_ready", None)
    if callable(probe):
        try:
            return bool(probe())
        except Exception:
            return False
    return True


def _prewarm_embedding_store():
    """Background target: construct the coordinator (loading the sentence-transformer model)
    so the FIRST search in a fresh process doesn't pay the cold load inside its deadline.
    Errors are swallowed — a failed pre-warm just means the lazy path pays later."""
    try:
        _get_rag_coordinator()
    except Exception as exc:  # defensive: _get_rag_coordinator already swallows, but never raise
        print(f"[search] embedding-store pre-warm failed; error_type={type(exc).__name__}")


def start_embedding_prewarm() -> bool:
    """Kick the embedding-store pre-warm onto a background daemon thread AT MOST ONCE per
    process (idempotent). Returns True iff THIS call started the thread; False if it was
    already started or is disabled via SEARCH_EMBED_PREWARM=0. Safe to call from import /
    tool-registration time: the model load happens off the calling thread."""
    global _PREWARM_STARTED
    if os.getenv("SEARCH_EMBED_PREWARM", "1") == "0":
        return False
    with _PREWARM_LOCK:
        if _PREWARM_STARTED:
            return False
        _PREWARM_STARTED = True
    try:
        threading.Thread(target=_prewarm_embedding_store,
                         name="embed-store-prewarm", daemon=True).start()
    except Exception as exc:
        print(f"[search] could not start embedding pre-warm thread; error_type={type(exc).__name__}")
        return False
    return True


class PropertyFilter:
    """严格的过滤器 - 用户必须满足的条件（预算/通勤均为可选，缺失时不设门槛）"""

    @staticmethod
    def apply_hard_filters(
        properties: List[Dict],
        budget: int,
        max_commute: int,
        location_keywords: str,
        care_about_safety: bool = False,
        commute_filter: bool = True,
    ) -> tuple[List[Dict], List[Dict]]:
        """
        应用硬过滤器和软过滤器 —— 但只在对应条件存在时才生效。

        返回: (完全符合, 轻微超预算的)

        规则（零工作流先验，能降级就降级）:
        - budget 为 None/0：不做价格过滤，全部计为完全符合，budget_status 留空，
          不存在 "soft_violation" 概念。
        - budget 存在：价格 ≤ budget 为完全符合；≤ budget×1.15 为轻微超预算；更高排除。
        - commute_filter=False 或 max_commute 为 None：不做通勤过滤（可能仅做标注）。
        - commute_filter=True 且房源有 travel_time：travel_time ≤ max_commute 才保留。
          缺失 travel_time 的房源在开启通勤过滤时被丢弃（无法证明满足）。
        """
        perfect_match = []
        soft_violation = []  # 超预算但通勤符合
        has_budget = budget is not None and budget > 0

        for prop in properties:
            try:
                price = float(prop.get('price', float('inf')))

                # ⚠️ 硬过滤：通勤时间（仅在启用且有真实上限时）
                if commute_filter and max_commute is not None:
                    commute_raw = prop.get('travel_time')
                    if commute_raw is None:
                        continue  # 开启通勤过滤但无通勤时间 -> 无法证明满足，丢弃
                    try:
                        if float(commute_raw) > float(max_commute):
                            continue
                    except (TypeError, ValueError):
                        continue

                score = PropertyFilter.calculate_score(
                    price, prop.get('travel_time'), budget, max_commute
                )

                if not has_budget:
                    # 无预算门槛：一切通过通勤过滤者皆为匹配
                    perfect_match.append({
                        **prop,
                        'match_type': 'perfect',
                        'budget_status': '',
                        'price_diff': 0,
                        'price_diff_percentage': 0.0,
                        'commute_status': '',
                        'recommendation_score': score,
                    })
                elif price <= budget:
                    # ✅ 完全符合
                    perfect_match.append({
                        **prop,
                        'match_type': 'perfect',
                        'budget_status': '✅ 在预算内',
                        'price_diff': 0,
                        'price_diff_percentage': 0.0,
                        'commute_status': '✅ 通勤符合' if commute_filter else '',
                        'recommendation_score': score,
                    })
                elif price <= budget * C.BUDGET_SOFT_MULTIPLIER:  # 允许超预算最多15%
                    # ⚠️ 轻微超预算（可推荐但需说明）
                    price_diff = price - budget
                    price_diff_percentage = round((price_diff / budget) * 100, 1)
                    soft_violation.append({
                        **prop,
                        'match_type': 'soft_violation',
                        'budget_status': f'⚠️ 超预算 £{int(price_diff)}',
                        'price_diff': price_diff,
                        'price_diff_percentage': price_diff_percentage,
                        'commute_status': '✅ 通勤符合' if commute_filter else '',
                        'recommendation_score': score,
                    })
                # else: 超过软过滤阈值，完全排除

            except (ValueError, TypeError) as exc:
                logger.warning(
                    "property_filter.invalid_row",
                    extra={"error_type": type(exc).__name__,
                           "has_address": bool(prop.get("address") or prop.get("Address"))},
                )
                continue

        return perfect_match, soft_violation

    @staticmethod
    def calculate_score(price, commute, budget, max_commute) -> float:
        """
        计算推荐分数 (0-100) = 价格匹配度(50) + 通勤匹配度(50)。

        任一维度缺失（budget=None 或无通勤上限/通勤时间）时，该维度取中性 50 分，
        绝不对 None 做算术（避免 TypeError）。
        """
        # 价格匹配度：0-50 分；无预算 -> 中性 50
        if budget is None or budget <= 0:
            price_match = 50
        else:
            try:
                p = float(price)
                price_match = max(0, 50 * (1 - (p - budget) / budget)) if p >= budget else 50
            except (TypeError, ValueError):
                price_match = 50

        # 通勤匹配度：0-50 分；无上限/无通勤时间/无限上限 -> 中性 50
        commute_match = 50
        try:
            if (commute is not None and max_commute is not None
                    and float(max_commute) > 0 and float(max_commute) < NO_COMMUTE_LIMIT):
                commute_match = max(0, 50 * (1 - float(commute) / float(max_commute)))
        except (TypeError, ValueError):
            commute_match = 50

        return round(price_match + commute_match, 1)


def _as_text_list(value) -> list | None:
    """A list argument as a list of clean strings, or None when nothing usable was sent.

    A model that sends ``"Camden"`` where ``areas: list`` is declared used to have the
    string iterated character by character. Never raises."""
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = clean_arg_text(value)
        return [cleaned] if cleaned else None
    if isinstance(value, (list, tuple, set)):
        out = [clean_arg_text(item) for item in value]
        out = [item for item in out if item]
        return out or None
    cleaned = clean_arg_text(value)
    return [cleaned] if cleaned else None


async def search_properties_impl(
    user_query: str = "",
    area: str = None,                 # 🆕 想住/搜索的区域（首选）
    areas: list = None,               # 🆕 多区域：一次搜索多个居住区域（含 area）
    location: str = None,             # 遗留别名：见下方解析规则
    commute_destination: str = None,  # 🆕 通勤目的地（可选）
    max_budget: float = None,
    max_commute_time: int = None,
    no_commute: bool = False,         # 🆕 用户明确表示不通勤
    bedrooms: int = None,             # 🆕 明确的卧室数（0=studio）
    bedrooms_minimum: bool = False,
    min_budget: int = None,
    radius_miles: float = 2.0,
    limit: int = 10,
    care_about_safety: bool = False,
    sort_by: str = "value",
    property_features: list = None,   # 🆕 累积的房产特征（如 studio, private）
    accumulated_preferences: list = None,  # 🆕 累积的软性偏好
    budget_period: str = "month",     # 🆕 预算周期：'week' 或 'month'
    current_message: str = "",        # 🆕 仅本轮原始消息（用于显式预算/通勤覆盖，避免误抓注入记忆）
    room_type: str = None,            # 🆕 房型：'studio' | 'ensuite' | 'shared' | None(不限)
    move_in_date: str = None,         # 🆕 期望入住/起租日 'YYYY-MM-DD'|None（可选，永不阻塞搜索）
    confirmed: bool = False,          # 🆕 用户已确认/表单直搜 —— 跳过软性条件门
    criteria_gate_shown: bool = False,  # 🆕 本会话软性条件门是否已出现过（至多触发一次）
    reply_language: str = None,       # 🆕 显式回复语言 'zh'|'en'：覆盖基于消息的 is_cjk 推断
    _deadline_monotonic: float | None = None,  # 🆕 INJECTED 绝对 time.monotonic() 截止时间（不在模型可见 schema 中）
    **kwargs  # 接受 LLM 可能传递的任何额外参数（如 property_type）
) -> dict:
    """
    完整的房源搜索工具 - RAG + 过滤器（零工作流先验）。

    核心原则（三个用户报告缺陷的修复）：
    - 只有"搜索区域"是必需的（甚至可由通勤目的地推导）。预算、通勤时间都可选，
      缺失时绝不设门槛、绝不循环追问。
    - "想住哪"(area/search_area) 与 "通勤去哪"(commute_destination) 是两个独立概念。
      no_commute=True 表示"我不通勤，我单纯住着" —— 一个此前无法表达的合法状态。

    解析规则：
    - search_area = area or location or (commute_destination 经 classify_place().slug 推导)。
    - 通勤标注目标 = commute_destination；否则 location（当其 classify_place kind==university，
      兼容 location="UCL" 之类遗留调用）；否则 None。
    - no_commute=True 覆盖一切通勤逻辑：不计算、不过滤、绝不追问。
    - max_commute_time 为 None/0 或 >= NO_COMMUTE_LIMIT：视为"无上限"，有目标时仅做标注、
      绝不过滤。
    - max_budget 为 None/0：不做预算过滤。

    Returns:
        包含搜索结果或（仅在 search_area 无法确定时）澄清问题的字典。
    """
    # ── Argument boundary ──────────────────────────────────────────────────
    # Everything below this line may assume the free-text arguments ARE strings and the
    # list arguments ARE lists. A model can send None, a number, a bare string where a
    # list is declared, or text carrying a lone UTF-16 surrogate; the first statements of
    # this function (memory stripping, budget parsing) run OUTSIDE the big try/except
    # below, so a malformed argument used to escape as an unhandled TypeError instead of
    # the structured {'success': False, 'error': ...} this tool promises.
    user_query = clean_arg_text(user_query)
    current_message = clean_arg_text(current_message)
    area = clean_arg_text(area) or None
    location = clean_arg_text(location) or None
    commute_destination = clean_arg_text(commute_destination) or None
    room_type = clean_arg_text(room_type) or None
    move_in_date = clean_arg_text(move_in_date) or None
    reply_language = clean_arg_text(reply_language) or None
    sort_by = clean_arg_text(sort_by) or "value"
    budget_period = clean_arg_text(budget_period) or "month"
    areas = _as_text_list(areas)
    property_features = _as_text_list(property_features)
    accumulated_preferences = _as_text_list(accumulated_preferences)

    # Memory-stripped view of user_query. The agent PREPENDS a long-term-memory
    # block to user_query; a HARD search filter (area/budget/room_type/bedrooms)
    # must NEVER be extracted from a prior conversation's remembered criteria
    # (cross-conversation bleed). Every deterministic hard-criteria extraction below
    # runs on THIS turn's real text only: current_message (this turn, memory-free)
    # is preferred; stripped_query is the defensive fallback when the caller omits
    # current_message. Long-term memory may still inform soft context elsewhere, but
    # it can no longer SET a hard filter the user didn't state this conversation.
    stripped_query = _strip_memory_block(user_query)

    # ── Deadline-aware degradation (latency round) ─────────────────────────
    # 绝不让本工具成为吃掉 fc-loop 20s batch 预算、从而被整体 ABANDON（=零结果、
    # "没有房源数据" 的诚实失败，H2）的元凶。执行器注入一个绝对 time.monotonic() 截止时间
    # (_deadline_monotonic)；未注入时（旧调用/MCP 路径）自行按 SEARCH_TOOL_BUDGET_S 设一个
    # 保守自限。整条流水线按此截止时间做"到点即返回已有部分结果"的降级（见步骤 3 与其后各段）。
    try:
        _self_budget_s = float(os.getenv("SEARCH_TOOL_BUDGET_S", "18"))
    except (TypeError, ValueError):
        _self_budget_s = 18.0
    # SEARCH_RETURN_MARGIN_S — safety headroom subtracted from whatever deadline governs
    # this call (the injected executor deadline, or the self-imposed budget). The tool
    # must RETURN before the executor's abandon axe, not race it: aiming to finish exactly
    # at the deadline loses to any jitter and gets the whole tool ABANDONED (executed=∅,
    # the H2 "no data" honesty failure). Shrinking the EFFECTIVE internal deadline by this
    # margin makes EVERY pacing stage below (_time_left, scrape_left, reco cap, degraded
    # check, and the per-stage network guards) automatically target returning ≥ margin
    # seconds early — the −0.2/−0.3 pads those stages already carry now sit safely INSIDE
    # this margin. Applies to the self-imposed default path too.
    try:
        _return_margin_s = float(os.getenv("SEARCH_RETURN_MARGIN_S", "1.2"))
    except (TypeError, ValueError):
        _return_margin_s = 1.2
    if _return_margin_s < 0:
        _return_margin_s = 0.0
    _deadline_injected = _deadline_monotonic is not None
    _raw_deadline = (float(_deadline_monotonic) if _deadline_injected
                     else time.monotonic() + _self_budget_s)
    _deadline = _raw_deadline - _return_margin_s

    def _time_left() -> float:
        return _deadline - time.monotonic()

    # ── 语言：中文用户用中文回复（澄清/无结果/摘要文案） ──────────────────
    # 显式 reply_language（'zh'|'en'）优先，覆盖基于消息的推断：/api/search_direct 无消息、
    # "search anyway" 路径也常无当前消息可判断语言，故由上游（app 按前端 UI 语言/本轮消息
    # 定好回复语言）显式传入。未传时退回既有的按消息推断（保持旧测试/旧调用行为不变）。
    if reply_language in ('zh', 'en'):
        is_cjk = (reply_language == 'zh')
    else:
        is_cjk = _has_cjk(current_message) or _has_cjk(stripped_query)

    # 🆕 初始化累积的特征和偏好
    # 修复：确保输入是列表，不是字符串
    if isinstance(property_features, list):
        _raw_property_features = list(property_features)
    elif isinstance(property_features, str):
        _raw_property_features = [property_features] if property_features else []
    else:
        _raw_property_features = []
    all_property_features = []
    for _feature in _raw_property_features:
        _raw_feature = str(_feature or "").strip()
        if not _raw_feature:
            continue
        _canonical_feature = _canonical_property_feature(_raw_feature)
        # Canonicalise accessibility aliases so the request and evidence use the
        # same vocabulary. Preserve every unrelated legacy tag byte-for-byte.
        _stored_feature = (_canonical_feature if _canonical_feature in _ACCESSIBILITY_FEATURES
                           else _raw_feature)
        if _stored_feature not in all_property_features:
            all_property_features.append(_stored_feature)

    # 🔧 修复：正确处理 accumulated_preferences，避免 list(string) 变成字符列表
    if isinstance(accumulated_preferences, list):
        all_soft_preferences = []
        for item in accumulated_preferences:
            if isinstance(item, str) and len(item) > 1:  # 排除单个字符
                all_soft_preferences.append(item)
            elif isinstance(item, str) and len(item) == 1:
                continue
    elif isinstance(accumulated_preferences, str) and accumulated_preferences:
        all_soft_preferences = [accumulated_preferences]
    else:
        all_soft_preferences = []

    if kwargs:
        logger.info(
            "property_search.extra_params",
            extra={"param_keys": sorted(str(key) for key in kwargs), "param_count": len(kwargs)},
        )

    try:
        from core.scraping.on_demand import classify_place, get_listings
        # 目的地判定（大学/公司）。契约由 on_demand 提供 is_destination；若并行 builder
        # 尚未落地该符号，用与契约同义的本地兜底（university/workplace 即目的地），绝不因
        # 导入缺失而让整个搜索工具崩溃。落地后自动改用真实实现。
        try:
            from core.scraping.on_demand import is_destination
        except ImportError:
            def is_destination(kind_or_result):
                kind = (kind_or_result.get("kind")
                        if isinstance(kind_or_result, dict) else kind_or_result)
                return kind in ("university", "workplace")
        # 消息级目的地扫描：当消息把"公司/工作地 + 裸城市"同时说出时（"Google office in
        # London"），裸城市 area 抓取会短路掉逐名目的地判定，丢失通勤目标。此扫描在原始
        # 消息上找回被命名的目的地并锁为通勤目标（详见步骤 1.4a）。若并行 builder 尚未
        # 落地该符号，兜底为 None（绝不因导入缺失让搜索崩溃）。
        try:
            from core.scraping.on_demand import extract_destination_from_text
        except ImportError:
            def extract_destination_from_text(_text):
                return None

        logger.info(
            "property_search.start",
            extra={
                "query_chars": len(user_query or ""),
                "has_area": bool(area or location),
                "has_commute_destination": bool(commute_destination),
                "has_budget": max_budget is not None,
                "has_commute_limit": max_commute_time is not None,
                "no_commute": bool(no_commute),
                "property_feature_count": len(all_property_features),
                "soft_preference_count": len(all_soft_preferences),
            },
        )

        # ================================================================
        # 步骤 0: 本轮消息里的显式信号优先（预算/通勤/不通勤），但绝不重新引入门槛
        # ================================================================
        # 只在"本轮真实消息"上做硬条件抽取（memory-stripped），绝不从注入的长期记忆块里
        # 抓取区域/预算/房型/卧室（跨会话泄漏修复）。
        msg_for_extraction = scoped_update_text(current_message or stripped_query)
        # 房型：规范化传入值；缺失时从本轮消息里抽取（"我要ensuite" 亦生效）。
        room_type = _normalize_room_type(room_type)
        if not room_type:
            room_type = _extract_room_type(msg_for_extraction)
        # 期望入住日：先净化传入值（累积状态/表单）；再看本轮消息是否显式声明，显式声明优先
        # （与预算"本轮覆盖累积"一致）。可选条件，绝不阻塞搜索。
        move_in_date = _valid_iso_date(move_in_date)
        if date_is_cleared(msg_for_extraction):
            move_in_date = None
        _fresh_move_in = _extract_move_in_date(msg_for_extraction)
        if _fresh_move_in and _fresh_move_in != move_in_date:
            logger.info(
                "property_search.move_in_updated",
                extra={"had_previous_value": bool(move_in_date)},
            )
            move_in_date = _fresh_move_in
        if not no_commute and _extract_no_commute(msg_for_extraction):
            no_commute = True
            logger.info("property_search.no_commute_detected")
        if msg_for_extraction:
            fresh_budget, fresh_period = _extract_budget(msg_for_extraction)
            # P1-1: "raise the budget by £200" states a DELTA. The graph has already folded
            # it into the ceiling it hands us, so re-reading the raw figure here would either
            # re-install £200 as the ceiling (the legacy bug) or, if we applied the delta a
            # second time, overshoot. A relative edit therefore states NO ceiling as far as
            # this override is concerned: whatever the caller passed stands, and an absent
            # ceiling stays absent (no budget filter) rather than becoming the delta.
            # detect_relative_budget_delta returns None for "raise the budget TO £2400", so
            # an absolute restatement still overrides exactly as before. Idempotent by
            # construction — it never adds anything.
            if fresh_budget and _states_relative_budget_edit(msg_for_extraction):
                logger.info(
                    "property_search.relative_budget_not_a_ceiling",
                    extra={"had_previous_value": max_budget is not None},
                )
                fresh_budget = None
            if fresh_budget and fresh_budget != max_budget:
                logger.info(
                    "property_search.budget_updated",
                    extra={
                        "had_previous_value": max_budget is not None,
                        "budget_period": fresh_period or budget_period,
                    },
                )
                max_budget = fresh_budget
                budget_period = fresh_period or budget_period
            fresh_commute = _extract_commute_minutes(msg_for_extraction)
            if fresh_commute and fresh_commute != max_commute_time:
                logger.info(
                    "property_search.commute_limit_updated",
                    extra={"had_previous_value": max_commute_time is not None},
                )
                max_commute_time = fresh_commute

        # ================================================================
        # 步骤 1: 解析搜索区域（唯一必需项）与通勤标注目标
        # ================================================================
        def _clean_area(v):
            return v.strip() if isinstance(v, str) and v.strip() else None

        search_area = _clean_area(area) or _clean_area(location)
        # 🆕 多区域：清洗/去重显式传入的 areas 列表。缺省单区域时以 areas[0] 作主区域，
        # 使目的地判定/区域派生（步骤 1.4）照常在主区域上运行。最终搜索区域列表
        # search_areas 在门通过、search_area 定稿后于步骤 3 前构建；此处先置空，供
        # 早退路径上的 _criteria()/_known_criteria() 闭包安全引用。
        req_areas: list = []
        for _a in (areas or []):
            _ca = _clean_area(_a)
            if _ca and _ca.lower() not in {x.lower() for x in req_areas}:
                req_areas.append(_ca)
        if not search_area and req_areas:
            search_area = req_areas[0]
        fresh_areas = _extract_areas(msg_for_extraction)
        if fresh_areas:
            req_areas = fresh_areas
            search_area = fresh_areas[0]
        search_areas: list = []
        # 记录 search_area 是否由 commute_destination 推导（而非用户当作"住哪"输入的 token）。
        # 由 commute_destination 推导出的 slug 绝不参与"区域即目的地"的重分类（步骤 1.4）。
        search_area_from_commute_dest = False
        if not search_area and commute_destination:
            _slug = classify_place(commute_destination).get("slug")
            search_area = _slug or None
            search_area_from_commute_dest = bool(search_area)

        # 确定性区域识别（SINGLE SOURCE OF TRUTH，含 zh→English 城市映射），在 NL/LLM
        # 回退之前：一个裸/切换式地名 —— 英文或中文（"曼彻斯特的公寓"）—— 无需 LLM
        # 往返即可解析。仅在 memory-stripped 的本轮消息上运行，不会读取注入的记忆块。
        if not search_area:
            _area_guess = _extract_area(msg_for_extraction)
            if _area_guess:
                search_area = _area_guess
                logger.info(
                    "property_search.area_extracted",
                    extra={"area_chars": len(str(search_area)), "source": "deterministic"},
                )

        # 仅当仍无法确定区域，且有自由文本时，才回退到 NL 抽取来"找回一个区域"。
        # （零先验：预算/通勤缺失不触发抽取；抽取只为补齐区域，顺带带回预算/通勤/特征。）
        # 注意：使用 memory-stripped 文本，避免 LLM 从注入的长期记忆块里读取旧区域/预算。
        if not search_area and (stripped_query or current_message):
            print(f"\n📝 [SEARCH] 无区域，使用 NL 抽取尝试找回位置...")
            enhanced_query = stripped_query or current_message
            if all_property_features:
                enhanced_query = f"Looking for {', '.join(all_property_features)} property. {enhanced_query}"
            try:
                from core.llm_interface import clarify_and_extract_criteria
                criteria_response = clarify_and_extract_criteria(enhanced_query) or {}
            except Exception as _e:
                logger.warning(
                    "property_search.nl_extract_failed",
                    extra={"error_type": type(_e).__name__},
                )
                criteria_response = {}
            logger.info(
                "property_search.nl_extract_completed",
                extra={
                    "field_names": sorted(str(key) for key in criteria_response),
                    "field_count": len(criteria_response),
                    "has_destination": bool(criteria_response.get("destination")),
                },
            )

            new_features = criteria_response.get('property_tags', []) or []
            if isinstance(new_features, str):
                new_features = [new_features] if new_features else []
            for feat in new_features:
                if feat and feat not in all_property_features:
                    all_property_features.append(feat)

            new_prefs = criteria_response.get('soft_preferences', '')
            if isinstance(new_prefs, str) and new_prefs:
                if new_prefs not in all_soft_preferences:
                    all_soft_preferences.append(new_prefs)
            elif isinstance(new_prefs, list):
                for pref in new_prefs:
                    if pref and pref not in all_soft_preferences:
                        all_soft_preferences.append(pref)

            extracted_dest = criteria_response.get('destination')
            if extracted_dest and not _clean_area(area) and not _clean_area(location):
                # 遗留语义：抽取的 destination 走 location 通道（可能是大学=通勤目标）。
                location = extracted_dest
            if max_budget is None:
                max_budget = criteria_response.get('max_budget')
            if max_commute_time is None:
                max_commute_time = criteria_response.get('max_travel_time')
            _ebp = criteria_response.get('budget_period')
            if _ebp:
                budget_period = _ebp

            # 重新解析区域
            search_area = _clean_area(area) or _clean_area(location)
            search_area_from_commute_dest = False
            if not search_area and commute_destination:
                _slug = classify_place(commute_destination).get("slug")
                search_area = _slug or None
                search_area_from_commute_dest = bool(search_area)

        # 0 是无效值（NL 抽取 JSON 模板的默认占位，调用方也可能传 0）——统一
        # 规范化为 None，使过滤逻辑与 known_criteria/search_criteria 载荷一致，
        # 且绝不让 0 进入抓取价格带（max_price=0 会得到空结果）。
        if not max_budget:
            max_budget = None
        if not max_commute_time:
            max_commute_time = None

        # ================================================================
        # 步骤 1.4: 若"想住的区域"其实是一个目的地（大学/公司），锁为通勤目标
        # ----------------------------------------------------------------
        # 用户可能把一个目的地（"UCL"、某公司）当作"住哪"来说。此时应：锁定它为通勤
        # 目标、默认通勤模式（下面步骤 2.6 绝不再问"是否通勤"），并清空 search_area 以
        # 反问"想住哪"。真实居住区（Camden/Manchester → classify_place kind=="area"）
        # 不受影响。仅重分类"用户当作住哪输入的"token，绝不动由 commute_destination
        # 推导出的 slug（步骤 1 的 search_area_from_commute_dest 记录了这一点）。
        # 位置：必须早于下面的硬门（步骤 2）与软门（步骤 2.6），故置于二者之前、且在
        # commute_target 计算之前——这样锁定的目的地能被 commute_target 直接采用。
        locked_commute_dest = None
        # 🆕 目的地一旦锁定，记录它自身可搜索的居住 slug 与城市，供非阻塞默认（步骤 2）
        # 把"住哪"默认为目的地所在区域、并供区域推荐器做城市污染防护。
        locked_dest_slug = None
        locked_dest_city = None

        # 步骤 1.4a: 消息里"命名"了一个目的地（公司/大学）时，其优先级高于裸城市 area 抓取。
        # ----------------------------------------------------------------
        # "find me a place near the Google office in London" 会被 _extract_area 抓成
        # area="London"（裸城市），从而短路掉逐名目的地判定，丢失"Google 办公室"这个通勤
        # 目标。此处在 memory-stripped 的本轮原始消息上做保守的目的地扫描（仅当 classify_place
        # 确认为 university/workplace 才命中；纯居住区/裸城市绝不命中），命中即锁为通勤目标。
        # 仅在用户未显式给出 commute_destination 且未声明 no_commute 时运行。
        if (not no_commute and not commute_destination and msg_for_extraction):
            _msg_dest = extract_destination_from_text(msg_for_extraction)
            if _msg_dest and is_destination(_msg_dest):
                _dest_name = _msg_dest.get("name") or search_area or "your destination"
                commute_destination = _msg_dest.get("address") or _dest_name
                no_commute = False
                locked_commute_dest = _dest_name  # 人类可读目的地名，用于门文案
                _dest_city = str(_msg_dest.get("city") or "").strip().lower()
                locked_dest_slug = _msg_dest.get("slug")
                locked_dest_city = _dest_city or None
                # 区分"目的地自身的城市/token"（上下文，非用户选定的家 → 不作 search_area）与
                # "用户另给的、与目的地不同的真实居住区"（如 "...near the Google office, live in
                # Camden" → 保留 Camden，只锁通勤）。仅动用户当作住哪输入的 token；由
                # commute_destination 推导出的 slug 不在此列。清空时，若调用方另给了一个不同的
                # 真实居住区（area/location），像步骤 1.4b 一样把它找回作 search_area。
                if search_area and not search_area_from_commute_dest:
                    _sa = search_area.strip().lower()
                    if is_destination(classify_place(search_area)) or _sa == _dest_city:
                        _residential = None
                        for _cand in (_clean_area(area), _clean_area(location)):
                            _cl = _cand.strip().lower() if _cand else None
                            if (_cl and _cl != _sa and _cl != _dest_city
                                    and not is_destination(classify_place(_cand))):
                                _residential = _cand
                                break
                        search_area = _residential
                logger.info(
                    "property_search.destination_locked",
                    extra={
                        "source": "message",
                        "destination_chars": len(str(commute_destination or "")),
                        "has_residential_area": bool(search_area),
                    },
                )

        # 步骤 1.4b: 若"想住的区域"其实是一个目的地 token（用户把 UCL/某公司当住哪说），
        # 且上面 1.4a 尚未锁定，则在此重分类为通勤目标。
        if not locked_commute_dest and search_area and not search_area_from_commute_dest:
            _place = classify_place(search_area)
            if is_destination(_place):
                _token = search_area
                commute_destination = _place.get("address") or _token
                no_commute = False
                locked_commute_dest = _token  # 人类可读的目的地名（如 "UCL"），用于门文案
                locked_dest_slug = _place.get("slug")
                locked_dest_city = str(_place.get("city") or "").strip().lower() or None
                # 用户若另给了一个"不同的"真实居住区，保留之为 search_area；
                # 否则清空，触发下方"想住哪"硬门。
                _residential = None
                for _cand in (_clean_area(area), _clean_area(location)):
                    if (_cand and _cand != _token
                            and not is_destination(classify_place(_cand))):
                        _residential = _cand
                        break
                search_area = _residential
                logger.info(
                    "property_search.destination_locked",
                    extra={
                        "source": "area_reclassified",
                        "destination_chars": len(str(commute_destination or "")),
                        "has_residential_area": bool(search_area),
                    },
                )

        # 通勤标注目标（no_commute 覆盖一切）
        commute_target = None
        if not no_commute:
            if commute_destination:
                commute_target = commute_destination
            elif location and is_destination(classify_place(location)):
                commute_target = location

        # ================================================================
        # 步骤 1.5: 解析卧室数量（显式 bedrooms 优先，其次本轮文本/特征）
        # ================================================================
        min_beds, max_beds = 0, 6  # An unspecified count must include family homes too.
        feats_lower = [str(f).lower() for f in all_property_features]
        if isinstance(bedrooms, int) and 0 <= bedrooms <= 6:
            min_beds, max_beds = bedrooms, (6 if bedrooms_minimum else bedrooms)
            resolved_bedrooms = bedrooms
        else:
            beds = _extract_bedrooms(current_message or stripped_query or "")
            if beds is None:
                for _v in kwargs.values():
                    beds = _extract_bedrooms(str(_v))
                    if beds is not None:
                        break
            if beds is not None:
                min_beds, max_beds = beds
                resolved_bedrooms = min_beds
                bedrooms_minimum = min_beds != max_beds
            elif 'studio' in feats_lower or room_type == 'studio':
                min_beds = max_beds = 0
                resolved_bedrooms = 0
            else:
                resolved_bedrooms = None

        # 供两种载荷统一复用的 search_criteria 构造器（同时带 canonical + legacy 键）。
        def _criteria():
            return {
                # canonical（新消费者）
                'area': search_area,
                'areas': list(search_areas) if search_areas else ([search_area] if search_area else []),
                'commute_destination': commute_target,
                'no_commute': no_commute,
                'bedrooms': resolved_bedrooms,
                'bedrooms_minimum': bedrooms_minimum,
                'budget_period': budget_period,
                'room_type': room_type,
                'move_in_date': move_in_date,
                # legacy（update_search_criteria / 旧 UI 仍在读）
                'destination': commute_target or search_area,
                'max_budget': max_budget,
                'max_travel_time': max_commute_time,
                'monthly_total_budget': kwargs.get('monthly_total_budget'),
                'office_days_per_week': kwargs.get('office_days_per_week'),
                'max_changes': kwargs.get('max_changes'),
                'property_features': all_property_features,
                'soft_preferences': all_soft_preferences,
            }

        # Full "what we know" snapshot for BOTH clarification gates (area + soft
        # criteria). Includes room_type so the frontend panel can reflect it.
        def _known_criteria():
            return {
                'area': search_area,
                'areas': list(search_areas) if search_areas else ([search_area] if search_area else []),
                'commute_destination': commute_target,
                'max_budget': max_budget,
                'max_travel_time': max_commute_time,
                'monthly_total_budget': kwargs.get('monthly_total_budget'),
                'office_days_per_week': kwargs.get('office_days_per_week'),
                'max_changes': kwargs.get('max_changes'),
                'no_commute': no_commute,
                'bedrooms': resolved_bedrooms,
                'bedrooms_minimum': bedrooms_minimum,
                'budget_period': budget_period,
                'room_type': room_type,
                'move_in_date': move_in_date,
                'property_features': all_property_features,
                'soft_preferences': all_soft_preferences,
            }

        # Legacy merge shape the graph folds back into accumulated criteria.
        def _extracted_so_far(extra=None):
            base = {
                'destination': commute_target,
                'max_budget': max_budget,
                'max_travel_time': max_commute_time,
                'monthly_total_budget': kwargs.get('monthly_total_budget'),
                'office_days_per_week': kwargs.get('office_days_per_week'),
                'max_changes': kwargs.get('max_changes'),
                'room_type': room_type,
                'move_in_date': move_in_date,
                'property_features': all_property_features,
                'soft_preferences': all_soft_preferences,
            }
            if extra:
                base.update(extra)
            return base

        # ================================================================
        # 步骤 2: 澄清 —— 仅当"搜索区域"无法确定时才追问（问『住哪』，绝不问『通勤去哪』）。
        # 🆕 非阻塞默认：若已锁定通勤目的地且知其可搜索的居住 slug，则不再追问，直接把
        # "住哪"默认为目的地所在区域并开搜；步骤 3 会并发生成"附近推荐居住区"，供用户一键
        # 添加（多区域）。仅当既无区域、又无可用默认时才追问。
        # ================================================================
        default_area_from_dest = False
        if not search_area and locked_commute_dest and locked_dest_slug:
            search_area = locked_dest_slug
            default_area_from_dest = True
            logger.info(
                "property_search.destination_default_area",
                extra={
                    "destination_chars": len(str(locked_commute_dest or "")),
                    "area_chars": len(str(search_area or "")),
                },
            )

        hard_readiness = assess_search_readiness(
            resolved_area=search_area,
            max_budget=max_budget,
            room_type=room_type,
            no_commute=no_commute,
            commute_destination=commute_target,
            criteria_gate_shown=criteria_gate_shown,
            confirmed=confirmed,
            user_message=msg_for_extraction,
            move_in_date=move_in_date,
        )
        if hard_readiness.status == "missing_hard":
            if locked_commute_dest:
                # 目的地已锁定但无法派生可搜索 slug（罕见）：仍回退到追问"想住哪"。
                if is_cjk:
                    question = (f"好的 —— 我会按到 {locked_commute_dest} 的通勤来帮你找房。"
                                f"你想住在哪个区域或城市？（有预算的话也一并告诉我）")
                else:
                    question = (f"Got it — I'll plan your commute to {locked_commute_dest}. "
                                f"Which area or city would you like to live in? "
                                f"(and your monthly budget, if you have one)")
            elif is_cjk:
                question = ("你想住在哪个区域或城市？（例如 Camden、Manchester，或大学如 UCL）。"
                            "也可以直接使用右侧的搜索表单。")
            else:
                question = ("Which area or city would you like to live in? "
                            "(e.g. Camden, Manchester — or a university like UCL). "
                            "You can also fill in the search form on the right.")
            return {
                'success': False,
                'status': 'need_clarification',
                'clarification_kind': 'missing_area',
                'question': question,
                'missing_fields': list(hard_readiness.missing_hard),
                'known_criteria': _known_criteria(),
                'extracted_so_far': _extracted_so_far(),  # 保留旧 merge 代码期待的形状
            }

        # ================================================================
        # 步骤 2.5: 预算模式（可选）—— 缺失即无门槛
        # ================================================================
        has_budget = bool(max_budget) and int(max_budget) > 0
        if has_budget and budget_period and budget_period.lower() == 'week':
            # Preserve the full weekly budget at the monthly filter boundary; the
            # formula and penny rounding live in one shared helper.
            max_budget = monthly_from_weekly(max_budget)
            budget_period = 'month'
            logger.info(
                "property_search.budget_period_converted",
                extra={"conversion_applied": True, "from_period": "week", "to_period": "month"},
            )

        # 通勤开关：有目标才标注；有目标且有真实上限才过滤。
        def _real_commute_limit(mct):
            try:
                return mct is not None and 0 < int(mct) < NO_COMMUTE_LIMIT
            except (TypeError, ValueError):
                return False

        commute_annotation_enabled = (not no_commute) and (commute_target is not None)
        commute_filter_enabled = commute_annotation_enabled and _real_commute_limit(max_commute_time)

        # ================================================================
        # 步骤 2.6: 软性条件门（Deliverable 1）—— 仅聊天路径，且至多触发一次
        # ----------------------------------------------------------------
        # 推荐但可选的条件：房型 room_type、预算 budget、通勤 commute（满足条件 =
        # 有真实通勤目标+上限，或明确 no_commute）。区域 area 仍是上面的硬门。
        # 触发：area 已确定 且 ≥1 推荐字段缺失 且 本会话尚未出现过此门 且 本轮未确认继续。
        # 绕过：confirmed=True（表单直搜 /api/search_direct）、criteria_gate_shown=True、
        #      或本轮消息含“继续/continue”等继续意图。缺失即列出，让用户补充或继续。
        # 持久化：criteria_gate_shown 经 extracted_so_far → update_search_criteria →
        #      _write_back_turn 落到 accumulated_search_criteria（按会话、可跨进程重启）。
        # 已知通勤目标即视为满足（仅标注、不强制过滤）：一旦锁定/给出目的地，就绝不再
        # 追问"是否通勤"。真实上限缺失时仍不过滤（commute_annotation_enabled 只标注，
        # commute_filter_enabled 只有在有真实上限时才为 True）。预算/房型软门行为不变。
        readiness = assess_search_readiness(
            resolved_area=search_area,
            max_budget=max_budget,
            room_type=room_type,
            no_commute=no_commute,
            commute_destination=commute_target,
            criteria_gate_shown=criteria_gate_shown,
            confirmed=confirmed,
            user_message=msg_for_extraction,
            move_in_date=move_in_date,
        )
        soft_missing = list(readiness.missing_soft)
        move_in_missing = "move_in" in readiness.missing_optional

        if readiness.status == "ask_soft_once":
            print(f"   🚪 [SOFT GATE] 缺失推荐条件 {soft_missing}"
                  f"{'（+可选 move_in）' if move_in_missing else ''}，先确认再搜索")
            return {
                'success': False,
                'status': 'need_clarification',
                'clarification_kind': 'soft_criteria',
                'question': _soft_gate_question(soft_missing, is_cjk, move_in_missing),
                'missing_fields': soft_missing,
                # OPTIONAL missing fields exposed separately so the REQUIRED-recommended
                # 'missing_fields' contract (asserted by tests) stays exactly as-is while
                # the frontend can still surface the optional move-in prompt.
                'missing_optional_fields': list(readiness.missing_optional),
                'known_criteria': _known_criteria(),
                'search_criteria': _criteria(),
                # 持久化“门已展示”标记（至多触发一次）。
                'extracted_so_far': _extracted_so_far({'criteria_gate_shown': True}),
            }

        # ================================================================
        # 步骤 3: 按需抓取真实、城市正确的 OnTheMarket 房源（带持久缓存）
        # ================================================================
        from core.data_loader import parse_price

        if has_budget:
            scrape_min = max(0, min(int(min_budget or 100), int(max_budget)))
            scrape_max = int(max_budget * C.BUDGET_SOFT_MULTIPLIER)
        else:
            scrape_min = max(0, int(min_budget or 100))
            scrape_max = int(os.getenv("SEARCH_DEFAULT_MAX_PRICE", "10000"))

        # 🆕 定稿多区域搜索列表：主区域（可能是目的地默认）在前，其后接显式传入的其它
        # 居住区域（去重、排除目的地类 token），上限 SEARCH_MAX_AREAS。
        MAX_SEARCH_AREAS = int(os.getenv("SEARCH_MAX_AREAS", "4"))
        search_areas = []
        if search_area:
            search_areas.append(search_area)
        for _ca in req_areas:
            if _ca.lower() in {x.lower() for x in search_areas}:
                continue
            if is_destination(classify_place(_ca)):
                continue  # 目的地不是居住区域，不纳入搜索
            search_areas.append(_ca)
        search_areas = search_areas[:MAX_SEARCH_AREAS] or ([search_area] if search_area else [])

        loop = asyncio.get_event_loop()

        # 🆕 并发生成"附近推荐居住区"（web 搜索→LLM 推理→逐个校验→本地缓存）。仅在有通勤
        # 目标时有意义；与抓取并行跑，首次未命中缓存会稍慢（用户已认可"首次慢、之后即时"）。
        _reco_task = None
        if commute_target and os.getenv("AREA_RECOS_ENABLED", "1") != "0":
            try:
                from core.recommend_areas import recommend_areas as _recommend_areas
            except Exception as _e:
                logger.warning(
                    "property_search.area_recommender_unavailable",
                    extra={"error_type": type(_e).__name__},
                )
                _recommend_areas = None
            if _recommend_areas:
                _reco_city = locked_dest_city
                if not _reco_city and commute_destination:
                    try:
                        _reco_city = classify_place(commute_destination).get('city')
                    except Exception:
                        _reco_city = None
                _reco_task = asyncio.create_task(_recommend_areas(
                    commute_target,
                    city=_reco_city,
                    max_commute_time=max_commute_time,
                    exclude_slugs={a.lower() for a in search_areas},
                    limit=int(os.getenv("AREA_RECO_LIMIT", "4")),
                ))

        RANK_HEADROOM_S = float(os.getenv("SEARCH_RANK_HEADROOM_S", "1.5"))
        PER_AREA_EST_S = float(os.getenv("SEARCH_PER_AREA_SCRAPE_EST_S", "6.0"))
        from core.scraping.on_demand import SCRAPE_BUDGET_S as _SCRAPE_BUDGET_S
        from core.scraping.on_demand import (
            is_unsearchable_city_area as _is_unsearchable_city_area,
        )

        # 🆕 ISSUE #78: 裸城市 "London" + 通勤目标 → 改搜目的地附近的真实居住区。
        # ----------------------------------------------------------------
        # OnTheMarket 的 /to-rent/property/london/ 页面是存在的，抓取也确实有结果——但
        # canonical 抓取只取该页 CANONICAL_SCRAPE_LIMIT 行的顶部切片，而那一页并不按地理
        # 排序，于是这几十行散落在 Feltham / Hayes / Barking / Harrow。任何"到某校 ≤N 分钟"
        # 的过滤都会把它们几乎全部滤掉，用户被告知"伦敦没有房源"，而同样条件换成一个真实
        # 居住区 slug 却能返回满满一页。这是 _band_rescue 已经处理过的"价格段被切掉"的
        # 通勤版孪生问题。
        #
        # 用户写"London，≤20 分钟到 UCL"，真实意图就是"UCL 附近能住的地方"。所以当区域是
        # 一个大到无法整体抓取的城市、且本轮有通勤目标时，改用推荐器解出的目的地邻近区域来搜。
        # 仅限伦敦：Manchester / Edinburgh 这类城市页本身就装得下整城，展开只会把一次能用的
        # 搜索换成一次更慢的搜索。推荐器没有及时给出结果时静默退回原样，最坏情况就是今天的行为。
        _city_expanded_to = []
        if _reco_task is not None and len(search_areas) == 1 and \
                _is_unsearchable_city_area(search_areas[0]):
            _expand_cap = min(
                float(os.getenv("SEARCH_CITY_EXPAND_TIMEOUT_S", "20")),
                max(0.0, _time_left() - RANK_HEADROOM_S - PER_AREA_EST_S),
            )
            # asyncio.wait (NOT wait_for): on timeout it leaves the task pending instead of
            # cancelling it, so the area-recommendation collector further down can still
            # await the same task. A cancelled task would raise CancelledError there —
            # a BaseException, which its `except Exception` would NOT catch.
            if _expand_cap > 0:
                _done, _ = await asyncio.wait({_reco_task}, timeout=_expand_cap)
                if _reco_task in _done:
                    try:
                        _city_expanded_to = _reco_task.result() or []
                    except Exception as _e:
                        logger.warning(
                            "property_search.city_expansion_failed",
                            extra={"error_type": type(_e).__name__},
                        )
                        _city_expanded_to = []
            if _city_expanded_to:
                _expanded = []
                for _item in _city_expanded_to:
                    _name = (_item.get('slug') or _item.get('name') or '').strip()
                    if _name and _name.lower() not in {x.lower() for x in _expanded}:
                        _expanded.append(_name)
                if _expanded:
                    logger.info(
                        "property_search.city_expanded",
                        extra={
                            "expanded_area_count": min(len(_expanded), MAX_SEARCH_AREAS),
                            "destination_chars": len(str(commute_target or "")),
                        },
                    )
                    search_areas = _expanded[:MAX_SEARCH_AREAS]
                    # _criteria() closes over BOTH names and is what the frontend writes
                    # back into the search form, so the primary area has to move too —
                    # otherwise the form keeps saying "London" while every result came
                    # from Bloomsbury, which is the same lie in a different pane.
                    search_area = search_areas[0]

        # 🆕 多区域 DEADLINE-AWARE 抓取；每行打上来源区域标签（_search_area），合并后统一排序/
        # 标注/过滤。绝不因单个冷缓存区域的慢抓取而让整个工具被 batch 预算 ABANDON（H2）：
        #   Phase 1（近乎免费）：仅查缓存（cache_only）——已缓存的区域总是被服务；
        #   Phase 2（有界）：仅在"截止时间(减去排序余量)仍能容纳一次保守的 per-area 估算"时才对
        #                    未命中的区域发起抓取，且每次抓取自身有界（min(SCRAPE_BUDGET_S, 剩余)），
        #                    使一个慢区域吃不掉整个窗口；来不及抓的区域标记为 incomplete（≠ empty）。
        logger.info(
            "property_search.scrape_start",
            extra={
                "area_count": len(search_areas),
                "min_bedrooms": min_beds,
                "max_bedrooms": max_beds,
                "has_budget": has_budget,
            },
        )

        def _tag(_a, res):
            for _r in res.get('rows', []):
                _r.setdefault('_search_area', _a)
            return res

        def _incomplete_result():
            return {"rows": [], "meta": {"requested_city": None, "stale": False,
                                         "source": "none", "count": 0, "timed_out": True}}

        _res_by_area: dict = {}
        cache_hits = 0
        cache_misses = 0
        incomplete_areas: list = []

        # Phase 1: cache-only pass (no scrape). Already-cached areas are served for near-free.
        _missed_areas: list = []
        for _a in search_areas:
            _cres = await loop.run_in_executor(
                None, lambda _x=_a: get_listings(_x, min_beds, max_beds, scrape_min,
                                                 scrape_max, limit=15, cache_only=True))
            if _cres.get('rows'):
                _res_by_area[_a] = _tag(_a, _cres)
                cache_hits += 1
            else:
                cache_misses += 1
                _missed_areas.append(_a)

        # Phase 2: scrape the missed areas that still fit the deadline (bounded per-area).
        _scrape_left = _time_left() - RANK_HEADROOM_S
        if _missed_areas and _scrape_left >= PER_AREA_EST_S:
            _per_area_budget = max(1.0, min(_SCRAPE_BUDGET_S, _scrape_left - 0.3))
            _scrape_tasks = {
                _a: asyncio.ensure_future(loop.run_in_executor(
                    None, lambda _x=_a: get_listings(_x, min_beds, max_beds, scrape_min,
                                                     scrape_max, limit=15,
                                                     budget_s=_per_area_budget)))
                for _a in _missed_areas
            }
            _done, _pending = await asyncio.wait(list(_scrape_tasks.values()),
                                                 timeout=max(0.2, _scrape_left))
            for _a, _task in _scrape_tasks.items():
                if _task in _done:
                    try:
                        _sres = _task.result()
                    except Exception as _se:
                        logger.warning(
                            "property_search.area_scrape_failed",
                            extra={
                                "area_chars": len(str(_a or "")),
                                "error_type": type(_se).__name__,
                            },
                        )
                        incomplete_areas.append(_a)
                        _res_by_area[_a] = _incomplete_result()
                        continue
                    _smeta = _sres.get('meta', {})
                    if _sres.get('rows'):
                        _res_by_area[_a] = _tag(_a, _sres)          # complete-with-results
                    elif (_smeta.get('timed_out') or _smeta.get('source_status') in
                          FAILED_SOURCE_STATES):
                        incomplete_areas.append(_a)                 # deadline hit -> incomplete
                        _res_by_area[_a] = _sres
                    else:
                        _res_by_area[_a] = _tag(_a, _sres)          # genuine complete-empty
                else:
                    # Still running at the window: abandon (result discarded) -> incomplete.
                    _task.cancel()
                    _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                    incomplete_areas.append(_a)
                    _res_by_area[_a] = _incomplete_result()
        elif _missed_areas:
            # Deadline can't fit even one scrape: every missed area is incomplete (served
            # nothing) — NEVER scraped, so we reserve the remaining time for ranking/format.
            logger.info(
                "property_search.scrape_skipped_deadline",
                extra={
                    "remaining_seconds": round(_scrape_left, 3),
                    "required_seconds": PER_AREA_EST_S,
                    "incomplete_area_count": len(_missed_areas),
                },
            )
            for _a in _missed_areas:
                incomplete_areas.append(_a)
                _res_by_area[_a] = _incomplete_result()

        # Per-area served-row counts (pre-filter) drive the crisp per-area status contract:
        # 'results' (complete, had listings) | 'empty' (complete, genuinely none) | 'incomplete'.
        _area_rowcount = {_a: len(_res_by_area.get(_a, {}).get('rows', []) or [])
                          for _a in search_areas}
        _source_failure = source_failure_summary(_res_by_area, language='zh' if is_cjk else 'en')
        _area_status = {
            _a: (_res_by_area[_a]['meta']['source_status']
                 if (_res_by_area.get(_a, {}).get('meta') or {}).get('source_status') in FAILED_SOURCE_STATES
                 else 'incomplete' if _a in incomplete_areas
                 else 'results' if _area_rowcount.get(_a) else 'empty')
            for _a in search_areas
        }

        live_rows = []
        _metas = []
        for _a in search_areas:
            res = _res_by_area.get(_a) or {"rows": [], "meta": {}}
            live_rows.extend(res.get('rows', []))
            _metas.append(res.get('meta', {}))
        # 聚合 meta：主区域（search_areas[0]）的 city 用于步骤 4 的 London 判定；
        # stale/source/count 做跨区域合并。
        primary_meta = _metas[0] if _metas else {}
        _any_scraped = any(m.get('source') == 'scraped' for m in _metas)
        _all_cached = bool(_metas) and all(m.get('source') in ('hit', 'stale-cache') for m in _metas)
        listing_meta = {
            'requested_city': primary_meta.get('requested_city'),
            'stale': any(m.get('stale') for m in _metas),
            'source': primary_meta.get('source'),
            'count': sum(int(m.get('count') or 0) for m in _metas),
        }
        possibly_outdated = bool(listing_meta.get('stale'))
        data_source = 'OnTheMarket' + (' (cached)' if (_all_cached and not _any_scraped) else '')
        print(f"   ✅ 实时房源 {listing_meta.get('count', 0)} 个 "
              f"(areas={len(search_areas)}, cached={_all_cached})")

        # 🆕 收集区域推荐（有界等待；命中缓存即时返回）。等待上限再被截止时间夹一层：绝不为可选的
        # 区域推荐把整个工具拖过预算（未完成时 area_recommendations=[]，任务转入后台并落缓存供下次）。
        area_recommendations = []
        if _reco_task is not None:
            _reco_cap = min(float(os.getenv("AREA_RECO_INLINE_TIMEOUT", "30")),
                            max(0.0, _time_left() - 0.2))
            try:
                area_recommendations = await asyncio.wait_for(
                    asyncio.shield(_reco_task), timeout=_reco_cap
                ) or []
            except asyncio.CancelledError:
                # The parent search was externally cancelled.  shield deliberately keeps
                # the cache-filling recommender alive, so retain/consume that detached task
                # before preserving the parent's cancellation semantics.
                _detach_background_area_recommendation(_reco_task)
                raise
            except asyncio.TimeoutError:
                # This is optional precomputation with a durable cache.  Detach it from the
                # foreground wait, retain a strong reference, and consume its eventual
                # result/exception instead of wait_for cancelling the underlying task.
                _detach_background_area_recommendation(_reco_task)
                logger.info(
                    "property_search.area_recommendation_continues_in_background",
                    extra={"inline_timeout_seconds": round(_reco_cap, 3)},
                )
                area_recommendations = []
            except Exception as _e:
                logger.warning(
                    "property_search.area_recommendation_failed",
                    extra={"error_type": type(_e).__name__},
                )
                area_recommendations = []

        # 到达此处若截止时间余量已不足以跑完（语义排序 + 通勤标注 + 详情丰富）这些 post-scrape 段，
        # 则降级为"确定性价格升序"结果——绝不返回空。降级只在时间告急时触发（有界抓取通常会预留
        # RANK_HEADROOM_S 余量），并跳过网络重的通勤标注/详情丰富，保证到点即返回已有部分结果。
        # Commute honesty (FIX 2): does the user actually have a commute constraint this
        # turn? If so, and commute never gets verified below (deadline-tight degrade, or a
        # per-stage time guard skips annotation), the payload must SAY commute was not
        # verified and the listings must carry NO commute fields — the model must not echo
        # or promise a duration the tool never computed. `commute_verified` flips True only
        # when the annotation stage actually runs.
        _has_commute_constraint = (not no_commute) and (
            commute_target is not None or _real_commute_limit(max_commute_time))
        commute_verified = False

        degraded = _time_left() < RANK_HEADROOM_S
        if degraded:
            print(f"   ⏱️ 截止余量不足（{_time_left():.1f}s < {RANK_HEADROOM_S:.1f}s）→ "
                  f"降级为价格升序结果（跳过语义排序/通勤/详情丰富）")
            # Disable commute annotation AND filtering: annotation is network-heavy (skipped),
            # and leaving the filter ON would make apply_hard_filters drop every un-annotated
            # row (no travel_time) — turning a time-degrade into a false empty. Price-only.
            commute_annotation_enabled = False
            commute_filter_enabled = False

        # Cold embedding-store guard (latency round, H2 repeat): semantic ranking constructs
        # PropertyEmbeddingStore on first use, whose sentence-transformer model load BLOCKS
        # ~18-20s — a single stage the SEARCH_RETURN_MARGIN_S pacing cannot preempt (it was
        # assumed cheap). If the store is not already warm (the background pre-warm hasn't
        # finished) and the time left doesn't comfortably cover that cold load, degrade THIS
        # call to the deterministic price-sorted path — reusing the existing degradation
        # machinery — instead of risking the whole tool being abandoned at the batch window.
        # Probing NEVER triggers the load. When the store IS warm, or time genuinely fits a
        # relaxed warm-up budget, ranking proceeds unchanged. Applies ONLY when a deadline was
        # actually injected — i.e. the fc-loop path with the hard 20s abandon axe. The
        # self-imposed budget path (legacy / MCP / direct calls) has no external axe and can
        # afford to build the store (as it always did), so it is never cold-degraded here.
        if _deadline_injected and not degraded and not _embedding_store_ready():
            try:
                _EMBED_INIT_EST_S = float(os.getenv("SEARCH_EMBED_INIT_EST_S", "20.0"))
            except (TypeError, ValueError):
                _EMBED_INIT_EST_S = 20.0
            if _time_left() < _EMBED_INIT_EST_S:
                print(f"   ⏱️ 嵌入模型尚未预热且余量不足（{_time_left():.1f}s < "
                      f"{_EMBED_INIT_EST_S:.1f}s，冷加载约需 ~18-20s）→ 本次降级为价格升序"
                      f"（跳过语义排序/通勤/详情丰富）")
                degraded = True
                commute_annotation_enabled = False
                commute_filter_enabled = False

        # Merge the deadline/partial contract into any post-scrape payload (found, no_results,
        # similar). cache_stats is ALWAYS present; partial/incomplete_areas/partial_note reflect
        # areas the time budget prevented us from fully searching (incomplete ≠ empty).
        def _augment(payload: dict) -> dict:
            out = dict(payload)
            out['partial'] = bool(incomplete_areas or _source_failure)
            out['incomplete_areas'] = list(incomplete_areas)
            out['partial_note'] = _partial_note(incomplete_areas, is_cjk) if incomplete_areas else ""
            out['cache_stats'] = {"hits": cache_hits, "misses": cache_misses}
            out['area_status'] = dict(_area_status)
            out.update(_source_failure)
            out['retrieval_outcome'] = (
                'source_unavailable' if _source_failure and not live_rows else
                'stale_fallback' if _source_failure and live_rows else
                'incomplete' if incomplete_areas else
                'no_matching_candidates' if payload.get('status') == 'no_results' and live_rows else
                'no_source_candidates' if payload.get('status') == 'no_results' else 'candidates_returned')
            if _source_failure:
                out['message'] = _source_failure['source_message']
                out['partial_note'] = _source_failure['source_message']
            elif payload.get('status') == 'no_results' and not incomplete_areas:
                note = ('未获得可用候选数据，不能据此判断市场上没有房源。' if not live_rows else
                        '本次候选中没有满足当前筛选的房源；这不代表市场上没有房源。') if is_cjk else (
                        'No usable source candidates were retrieved; this does not establish that the market is empty.' if not live_rows else
                        'No retrieved candidates match the current filters; this does not establish that the market is empty.')
                out['message'] = f"{out.get('message') or ''} {note}".strip()
            # Commute honesty (FIX 2): the user had a commute constraint but commute was never
            # verified this round. `commute_note` ALWAYS rides with the results (independent of
            # the partial note) and forbids stating/promising any commute duration; the returned
            # listings carry no commute fields in this case (annotation was disabled), so there
            # is nothing stale for the model to echo.
            _commute_unverified = bool(_has_commute_constraint and not commute_verified)
            out['commute_unverified'] = _commute_unverified
            out['commute_note'] = _commute_note(is_cjk) if _commute_unverified else ""
            return out

        # 没有任何真实房源 —— 诚实返回（语言感知），绝不使用 demo 假数据。
        # Provider area pages are candidate generators, not proof of proximity.
        # Validate every listing against the requested area's centroid before it
        # can enter RAG, ranking, the similar fallback, or the UI.
        geo_rejected = []
        geo_validation_enabled = os.getenv("SEARCH_GEO_VALIDATION_ENABLED", "1") != "0"
        if live_rows and geo_validation_enabled and not degraded:
            from core.geography import (
                filter_properties_by_radius,
                known_area_centroid,
                parse_coordinates,
            )
            from core.maps_service import geocode_address

            area_centres = {}
            for _idx, _a in enumerate(search_areas):
                _centre = known_area_centroid(_a)
                if _centre is None:
                    _city = (_metas[_idx].get('requested_city')
                             if _idx < len(_metas) else None)
                    _query = str(_a).replace('-', ' ')
                    if _city and _city.lower() not in _query.lower():
                        _query = f"{_query}, {_city}, UK"
                    try:
                        _geo = await loop.run_in_executor(None, geocode_address, _query)
                    except Exception as _geo_exc:
                        logger.warning(
                            "property_search.geocode_failed",
                            extra={
                                "area_chars": len(str(_a or "")),
                                "error_type": type(_geo_exc).__name__,
                            },
                        )
                        _geo = None
                    _centre = parse_coordinates(_geo)
                area_centres[_a] = _centre

            try:
                _geo_radius = float(radius_miles)
            except (TypeError, ValueError):
                _geo_radius = 2.0
            if not math.isfinite(_geo_radius) or _geo_radius <= 0:
                _geo_radius = 2.0

            # One radius cannot serve both granularities. The 2-mile disc is right for a
            # neighbourhood ("Camden") and is the WRONG TEST for a whole city: on
            # 2026-07-27 a real "London" search logged
            #   [GEO] Verified 0/2 listings within 2 miles of requested area(s)
            # and the user got nothing — both listings were in London, just not within two
            # miles of the single point the geocoder returns for "London". A city's real
            # containment test is on_demand._wrong_city, applied at scrape time by
            # _clean(); this disc stays only as a coarse fail-closed backstop, so for a
            # city-level area it is widened to city scale instead of being switched off
            # (a None centre fails CLOSED here, which would reject everything).
            from core.geography import _coerce_radius
            from core.scraping.on_demand import is_city_level_area

            _city_radius = _coerce_radius(
                os.getenv("SEARCH_CITY_RADIUS_MILES", "20.0"), 20.0)
            _area_radii = {_a: _city_radius for _a in search_areas
                           if is_city_level_area(_a) and _city_radius > _geo_radius}
            if _area_radii:
                logger.info(
                    "property_search.city_radius_applied",
                    extra={
                        "city_area_count": len(_area_radii),
                        "city_radius_miles": _city_radius,
                        "default_radius_miles": _geo_radius,
                    },
                )

            _before_geo = len(live_rows)
            # Haversine over every fetched listing is pure CPU that scales with the
            # candidate pool (hundreds of rows across multi-area) — offload so it never
            # stalls the loop/batch-budget timer. Deterministic: same in/out as inline.
            live_rows, geo_rejected = await asyncio.to_thread(
                filter_properties_by_radius, live_rows, area_centres, _geo_radius,
                _area_radii,
            )
            listing_meta['count'] = len(live_rows)
            listing_meta['location_filtered_count'] = len(geo_rejected)
            print(
                f"   [GEO] Verified {len(live_rows)}/{_before_geo} listings within "
                f"{_geo_radius:g} miles of requested area(s); "
                f"rejected {len(geo_rejected)}"
            )

        if not live_rows:
            # When the emptiness is because the time budget prevented searching some areas,
            # the message must NOT claim "no listings" for them (H2 honesty) — surface the
            # partial note instead. A genuinely-searched empty stays the honest no-results.
            _empty_msg = (_partial_note(incomplete_areas, is_cjk) if incomplete_areas
                          else _no_results_message(search_area, is_cjk))
            return _augment({
                'success': True,
                'status': 'no_results',
                'message': _empty_msg,
                'recommendations': [],
                'data_source': data_source,
                'search_criteria': _criteria(),
                'known_criteria': _known_criteria(),
                'area_recommendations': area_recommendations,
            })

        # 🆕 构建搜索查询（无预算时避免 "£None"）。抽成小工具，便于多区域时按区域各自召回。
        def _build_search_query(_area_name):
            if has_budget:
                q = f"Find flat near {_area_name} under £{max_budget}"
                if all_property_features:
                    q = f"Find {', '.join(all_property_features)} flat near {_area_name} under £{max_budget}"
            else:
                q = f"Find flat near {_area_name}"
                if all_property_features:
                    q = f"Find {', '.join(all_property_features)} flat near {_area_name}"
            return q

        search_query = _build_search_query(search_area)

        # 传给 RAG 的 criteria 用安全值，避免 _hybrid_rank 对 None 做算术。
        criteria = {
            'destination': commute_target or search_area,
            'max_budget': max_budget if has_budget else 10_000_000,
            'max_travel_time': max_commute_time if commute_filter_enabled else NO_COMMUTE_LIMIT,
            'property_features': all_property_features,
            'soft_preferences': all_soft_preferences,
        }

        # Both recall paths use one request-owned snapshot, including across awaits.
        from core.listing_retrieval import ListingRetrievalSession

        retrieval_session = None

        def _normalize_and_rank():
            nonlocal retrieval_session
            for prop in live_rows:
                prop['parsed_price'] = parse_price(prop.get('Price'))
                _rt = str(prop.get('Room_Type_Category', ''))
                prop.setdefault('Type', _rt or 'Flat')
                _bm = re.search(r'(\d+)\s*bed', _rt, re.I)
                if _bm:
                    prop['Bedrooms'] = int(_bm.group(1))
                elif 'studio' in _rt.lower():
                    prop['Bedrooms'] = 'Studio'
                if possibly_outdated:
                    prop['possibly_outdated'] = True
            retrieval_session = ListingRetrievalSession(
                _get_rag_coordinator(), live_rows, _RAG_LOCK,
            )
            return retrieval_session.rank(
                [(area, _build_search_query(area)) for area in search_areas], criteria,
            )

        def _degraded_rank():
            # Deadline is too close for semantic ranking: normalize the rows and order them
            # deterministically by price (ascending). Still surfaces real, city-correct
            # listings — never nothing — and stays cheap/off-network.
            for prop in live_rows:
                prop['parsed_price'] = parse_price(prop.get('Price'))
                _rt = str(prop.get('Room_Type_Category', ''))
                prop.setdefault('Type', _rt or 'Flat')
                _bm = re.search(r'(\d+)\s*bed', _rt, re.I)
                if _bm:
                    prop['Bedrooms'] = int(_bm.group(1))
                elif 'studio' in _rt.lower():
                    prop['Bedrooms'] = 'Studio'
                if possibly_outdated:
                    prop['possibly_outdated'] = True
            return sorted(live_rows,
                          key=lambda p: (p.get('parsed_price') if p.get('parsed_price') else float('inf')))

        if degraded:
            ranked_properties, past_context, area_info = _degraded_rank(), "", {}
        else:
            ranked_properties, past_context, area_info = await asyncio.to_thread(_normalize_and_rank)
        print(f"   ✅ RAG 返回 {len(ranked_properties)} 个候选房源")

        # 🆕 根据房产特征过滤结果（注意：不要遮蔽函数级的 room_type 参数）
        if all_property_features:
            logger.info(
                "property_search.feature_filter",
                extra={"feature_count": len(all_property_features)},
            )
            filtered_by_features = []
            for prop in ranked_properties:
                matches = True
                for feature in all_property_features:
                    canonical = _canonical_property_feature(feature)
                    evidence = _property_feature_evidence(prop, canonical)
                    if _requires_explicit_feature_evidence(canonical):
                        # Missing disclosure is not proof that an accessibility
                        # feature is absent. Keep unknown rows visible and let the
                        # candidate contract label them unverified; only an explicit
                        # structured denial removes a row here.
                        if evidence == "absent":
                            matches = False
                            break
                    elif canonical in {"studio", "private", "en-suite"} and evidence != "verified":
                        matches = False
                        break
                if matches:
                    filtered_by_features.append(prop)
            if filtered_by_features:
                print(f"   ✅ 特征过滤后剩余 {len(filtered_by_features)} 个房源")
                ranked_properties = filtered_by_features
            else:
                print(f"   ⚠️ 特征过滤后无结果，保留原始结果并在说明中提及")

        # 🆕 根据房型过滤（studio / ensuite / shared）—— 复用 _matches_room_type
        if room_type:
            print(f"\n🔍 [SEARCH] 根据房型过滤 room_type_chars={len(str(room_type))}")
            rt_matched = [p for p in ranked_properties if _matches_room_type(p, room_type)]
            if rt_matched:
                print(f"   ✅ 房型过滤后剩余 {len(rt_matched)} 个房源")
                ranked_properties = rt_matched
            else:
                print(f"   ⚠️ 房型过滤后无结果，保留原始结果并在说明中提及")

        # ================================================================
        # 步骤 4: 通勤时间（仅在有标注目标且未声明不通勤时计算/过滤）
        # ================================================================
        # A requested room type is an explicit constraint. Do not show another
        # type merely to fill the result page.
        if room_type and not rt_matched:
            return _augment({
                'success': True,
                'status': 'no_results',
                'message': _no_results_message(search_area, is_cjk),
                'recommendations': [],
                'data_source': data_source,
                'search_criteria': _criteria(),
                'known_criteria': _known_criteria(),
                'area_recommendations': area_recommendations,
            })

        candidates = ranked_properties[:15]
        dest_coords = None
        london_dest = False

        # Commute annotation is the last network-heavy post-scrape stage (a geocode plus a
        # per-candidate TfL/geo estimate). Even on the non-degraded path, the intervening
        # geo-validation + RAG ranking can have eroded the budget since `degraded` was
        # decided — so gate this stage on its OWN conservative estimate too. If the time
        # left no longer covers it, skip annotation + filtering exactly like the degraded
        # path (leave commute unverified) rather than risk overrunning the return margin.
        try:
            _COMMUTE_ANNOTATE_EST_S = float(os.getenv("SEARCH_COMMUTE_ANNOTATE_EST_S", "3.0"))
        except (TypeError, ValueError):
            _COMMUTE_ANNOTATE_EST_S = 3.0
        if commute_annotation_enabled and not degraded and _time_left() < _COMMUTE_ANNOTATE_EST_S:
            print(f"   ⏱️ 通勤标注余量不足（{_time_left():.1f}s < {_COMMUTE_ANNOTATE_EST_S:.1f}s）→ "
                  f"跳过通勤标注/过滤（通勤未核实）")
            commute_annotation_enabled = False
            commute_filter_enabled = False

        if commute_annotation_enabled and not degraded:
            logger.info(
                "property_search.commute_annotation",
                extra={
                    "destination_chars": len(str(commute_target or "")),
                    "filter_enabled": commute_filter_enabled,
                },
            )
            from core.maps_service import calculate_travel_time, geocode_address
            loop = asyncio.get_event_loop()
            dest_coords = await loop.run_in_executor(None, geocode_address, commute_target)
            london_dest = (listing_meta.get('requested_city') == 'london') or _in_london(dest_coords)

            if london_dest:
                travel_time_tasks = [
                    loop.run_in_executor(None, calculate_travel_time, prop.get('Address', ''), commute_target)
                    for prop in candidates
                ]
                tfl_times = await asyncio.gather(*travel_time_tasks, return_exceptions=True)
            else:
                tfl_times = [None] * len(candidates)

            # 通勤标注装配：坐标估算 _coord_commute_minutes（纯 CPU）以及非 London 分支里
            # 的同步 calculate_travel_time（逐候选、串行网络调用）都会阻塞事件循环。整段离线到
            # 工作线程（London 的 TfL 时间已在上面 gather 并发取回，此处仅按 tfl_times 装配），
            # 行为逐字不变（相同的净化/回退/过滤顺序）。
            def _annotate():
                annotated = []
                for prop, tfl in zip(candidates, tfl_times):
                    if isinstance(tfl, Exception):
                        tfl = None
                    travel_time = tfl if (isinstance(tfl, (int, float)) and 0 < tfl <= _COMMUTE_SANITY_CAP) else None
                    # The bare producer answers from a TfL itinerary OR from its own
                    # straight-line fallback; only the basis payload can tell them apart.
                    travel_source = _travel_provenance(
                        prop.get('Address', ''), commute_target, travel_time)
                    if travel_time is None:
                        travel_time = _coord_commute_minutes(
                            prop.get('geo_location') or prop.get('Geo_Location'), dest_coords
                        )
                        if travel_time is not None:
                            travel_source = _TRAVEL_SOURCE_ESTIMATE
                    if travel_time is None and not london_dest:
                        try:
                            travel_time = calculate_travel_time(prop.get('Address', ''), commute_target)
                        except Exception:
                            travel_time = None
                        travel_source = _travel_provenance(
                            prop.get('Address', ''), commute_target, travel_time)
                    if travel_time is not None:
                        prop['travel_time_minutes'] = travel_time
                        prop['travel_time'] = travel_time
                        prop['travel_time_source'] = travel_source
                    if commute_filter_enabled:
                        if travel_time is not None and travel_time <= max_commute_time:
                            annotated.append(prop)
                        # 否则：超出上限，丢弃
                    else:
                        annotated.append(prop)  # 仅标注，不过滤
                return annotated

            candidates = await asyncio.to_thread(_annotate)
            # The annotation stage ran to completion: commute is now genuinely verified for
            # the surfaced listings (they carry a real travel_time), so the honesty note is
            # not needed.
            commute_verified = True
            print(f"   ✅ 通勤处理后: {len(candidates)} 个房源 (dest_in_london={london_dest})")

        # ================================================================
        # 步骤 5: 价格过滤和评分（预算/通勤缺失时相应维度降级为中性）
        # ================================================================
        print(f"\n💰 [SEARCH] 应用过滤 (预算={'开' if has_budget else '关'})...")
        for prop in candidates:
            if 'price' not in prop or not prop['price']:
                prop['price'] = prop.get('parsed_price', parse_price(prop.get('Price', '')))

        perfect_match, soft_violation = PropertyFilter.apply_hard_filters(
            properties=candidates,
            budget=max_budget if has_budget else None,
            max_commute=max_commute_time if commute_filter_enabled else None,
            location_keywords=search_area,
            care_about_safety=care_about_safety,
            commute_filter=commute_filter_enabled,
        )
        print(f"   ✅ 完全符合: {len(perfect_match)} | ⚠️ 超预算可考虑: {len(soft_violation)}")

        perfect_match.sort(key=lambda p: -p.get('recommendation_score', 0))
        soft_violation.sort(key=lambda p: -p.get('recommendation_score', 0))
        # RAG is useful for candidate recall, but the final page needs a single
        # transparent objective. Re-score eligible listings with continuous
        # price/commute utilities, evidence-aware normalisation, and light MMR
        # diversification. RANKER_V2_ENABLED=0 restores the previous ordering. Skipped when
        # degraded (deadline-tight): the price-sorted order already computed above stands.
        if os.getenv('RANKER_V2_ENABLED', '1') != '0' and not degraded:
            from core.ranking import rank_and_diversify

            rank_kwargs = {
                'max_budget': max_budget if has_budget else None,
                'max_commute': max_commute_time if commute_filter_enabled else None,
                'requested_features': all_property_features,
                'move_in_date': move_in_date,
            }

            # MMR re-rank is O(n²) over each pool with per-listing utility maths;
            # offload both pools in a single thread hop (kept off the loop / batch timer).
            def _rank_both():
                return (rank_and_diversify(perfect_match, **rank_kwargs),
                        rank_and_diversify(soft_violation, **rank_kwargs))

            perfect_match, soft_violation = await asyncio.to_thread(_rank_both)

        # ================================================================
        # 步骤 5.5: 无匹配时的回退
        #   - 有预算：尝试 RAG "相似但略超" 建议（此路径仅在有预算时有意义）
        #   - 无预算：直接给出诚实的 no_results（语言感知）
        # ================================================================
        if not perfect_match and not soft_violation:
            # The similar-but-over-budget fallback runs an embedding search (property_store.search),
            # so it needs a WARM store. Skip it while degraded (deadline-tight or cold store): we
            # must not construct/query the model here — fall through to the honest no_results.
            if has_budget and not degraded:
                print(f"\n⚠️ [SEARCH] 无符合结果，尝试 RAG 相似房源...")

                # The fallback reads this request's index, even after another search.
                def _similar_recall():
                    sims = retrieval_session.similar(
                        f"flat apartment near {search_area} budget {max_budget}", top_k=10,
                    )
                    out = []
                    for prop in (sims or [])[:6]:
                        try:
                            travel_time = None
                            travel_source = None
                            if commute_annotation_enabled:
                                from core.maps_service import calculate_travel_time
                                if london_dest:
                                    _tt = calculate_travel_time(prop.get('Address', ''), commute_target)
                                    travel_time = _tt if (isinstance(_tt, (int, float)) and 0 < _tt <= _COMMUTE_SANITY_CAP) else None
                                    travel_source = _travel_provenance(
                                        prop.get('Address', ''), commute_target, travel_time)
                                if travel_time is None:
                                    travel_time = _coord_commute_minutes(
                                        prop.get('geo_location') or prop.get('Geo_Location'), dest_coords
                                    )
                                    if travel_time is not None:
                                        travel_source = _TRAVEL_SOURCE_ESTIMATE
                            keep = True
                            if commute_filter_enabled:
                                keep = travel_time is not None and travel_time <= max_commute_time * C.SIMILAR_COMMUTE_SLACK
                            if keep:
                                if travel_time is not None:
                                    prop['travel_time'] = travel_time
                                    prop['travel_time_source'] = travel_source
                                prop['price'] = prop.get('parsed_price', parse_price(prop.get('Price', '')))
                                out.append(prop)
                        except Exception:
                            continue
                    return out

                similar_with_commute = await asyncio.to_thread(_similar_recall)
                if similar_with_commute:
                        similar_with_commute.sort(key=lambda x: x.get('price', float('inf')))
                        closest_3 = similar_with_commute[:3]
                        min_price_needed = min(p.get('price', 0) for p in closest_3)
                        suggested_budget = int(min_price_needed * C.SUGGESTED_BUDGET_MARGIN)
                        budget_increase = suggested_budget - max_budget
                        # This fallback recalls SIMILAR listings, not over-budget ones: the
                        # embedding recall is not budget-filtered and the exact-match pool can
                        # be empty for reasons that have nothing to do with price (room type,
                        # area, commute, a scrape that fell back to cache). So a recalled row
                        # is frequently UNDER budget, and the old unconditional
                        # `price - max_budget` then rendered "Over budget by £-267".
                        #
                        # The headline is decided PER ROW, never from the cheapest row or from
                        # `suggested_budget` (which is min_price x a margin, so it can exceed a
                        # budget every row is already under: £1,900 cheapest vs a £1,950 budget
                        # suggests £1,995 and would have claimed a raise was needed). Three
                        # distinct cases, because a mixed set is neither "raise your budget"
                        # nor "all within budget" — with prices £1,900/£2,100/£2,200 against
                        # £2,000, both of those sentences contradict the per-row badges.
                        n_over = sum(1 for p in closest_3
                                     if int(p.get('price', 0)) > max_budget)
                        needs_more_budget = n_over == len(closest_3)
                        all_within_budget = n_over == 0

                        similar_formatted = []
                        for i, prop in enumerate(closest_3, 1):
                            price = int(prop.get('price', 0))
                            over_budget = max(0, price - max_budget)
                            over_percentage = (round((over_budget / max_budget) * 100, 1)
                                               if max_budget and over_budget else 0.0)
                            budget_status = (f"⚠️ Over budget by £{over_budget} ({over_percentage}%)"
                                             if over_budget else "✅ Within budget")
                            images = prop.get('Images', prop.get('images', []))
                            if isinstance(images, str):
                                images = [images] if images else []
                            geo_location = prop.get('Geo_Location', prop.get('geo_location', ''))
                            row = {
                                'rank': i,
                                # The provider's own borough word can contradict the listing's own postcode
                                # ("... Islington WC1N" for a Camden listing). Show the reconciled label;
                                # keep the raw provider string for traceability.
                                'address': reconcile_area_label(
                                    prop.get('Address', prop.get('address', 'Unknown'))),
                                'provider_address': prop.get('Address', prop.get('address', 'Unknown')),
                                'price': f"£{price}/month",
                                'budget_status': budget_status,
                                'price_raw': price,
                                'over_budget': over_budget,
                                'similarity_score': round(prop.get('similarity_score', 0) * 100, 1),
                                'property_type': prop.get('Type', prop.get('type', 'Flat')),
                                'bedrooms': prop.get('Bedrooms', prop.get('bedrooms', 'N/A')),
                                'match_type': 'similar_suggestion',
                                'source': data_source,
                                'url': prop.get('URL', prop.get('url', '')),
                                'images': images,
                                'geo_location': geo_location,
                                'distance_miles': prop.get('distance_miles'),
                                'explanation': _clean_explanation(
                                    prop.get('Description', ''),
                                    prop.get('travel_time') if commute_annotation_enabled else None,
                                    commute_target,
                                    prop.get('travel_time_source'),
                                ),
                                'area': prop.get('_search_area'),
                            }
                            # 🆕 可入住日期：相似回退房源同样诚实标注（这些行未做详情页丰富，
                            # 仅取 rich-schema 的 'Available From'；未知则留空）。
                            _sim_avail = _resolve_available_from(prop)
                            row['available_from'] = _sim_avail
                            row['availability_status'] = _availability_status(_sim_avail, move_in_date)
                            _tt = prop.get('travel_time')
                            if commute_annotation_enabled and _tt is not None:
                                _tsrc = prop.get('travel_time_source')
                                row['travel_time'] = _travel_time_label(_tt, commute_target, _tsrc)
                                row['travel_time_source'] = _tsrc
                            similar_formatted.append(row)

                        _cp = _commute_phrase(max_commute_time, commute_target) if commute_target else ""
                        # The headline must describe the ACTUAL miss. Claiming "nothing within
                        # your budget" while listing three cheaper flats is the same defect as
                        # the negative over-budget figure, one layer up.
                        _cheapest = int(closest_3[0].get('price', 0))
                        if needs_more_budget and is_cjk:
                            _msg = (f"在 £{max_budget}/月的预算内"
                                    f"{f'（{_cp.strip()}）' if _cp else f'、{search_area} 附近'}"
                                    f"没有找到房源。")
                            _suggestion = (f"不过找到了 {len(closest_3)} 套相近的房源，"
                                           f"最接近的一套是 £{_cheapest}/月。"
                                           f"是否考虑把预算提高约 £{budget_increase}"
                                           f"（到 £{suggested_budget}/月）？")
                        elif needs_more_budget:
                            _msg = (f"No properties were found within your budget of £{max_budget}/month"
                                    f"{_cp or f' near {search_area}'}.")
                            _suggestion = (f"However, I found {len(closest_3)} similar properties. "
                                           f"The closest match is £{_cheapest}/month. "
                                           f"Would you consider increasing your budget by approximately "
                                           f"£{budget_increase} (to £{suggested_budget}/month)?")
                        elif is_cjk:
                            _msg = (f"没有找到同时满足全部条件的房源"
                                    f"{f'（{_cp.strip()}）' if _cp else f'（{search_area} 附近）'}。")
                            _budget_note = (f"且都在 £{max_budget}/月的预算之内"
                                            f"（最低 £{_cheapest}/月）"
                                            if all_within_budget else
                                            f"其中 {len(closest_3) - n_over} 套在 £{max_budget}/月"
                                            f"的预算之内、{n_over} 套超出预算"
                                            f"（最低 £{_cheapest}/月）")
                            _suggestion = (f"不过找到了 {len(closest_3)} 套相近的房源，"
                                           f"{_budget_note}。"
                                           f"它们在另一项条件上与搜索不一致，请逐套对照您最在意的条件。")
                        else:
                            _msg = (f"No listing matched every condition"
                                    f"{_cp or f' near {search_area}'}.")
                            _budget_note = (f"all within your £{max_budget}/month budget "
                                            f"(from £{_cheapest}/month)"
                                            if all_within_budget else
                                            f"{len(closest_3) - n_over} within your "
                                            f"£{max_budget}/month budget and {n_over} over it "
                                            f"(from £{_cheapest}/month)")
                            _suggestion = (f"However, I found {len(closest_3)} similar properties, "
                                           f"{_budget_note}. "
                                           f"They differ from the search on another condition, so "
                                           f"please check each one against what matters to you.")
                        return _augment({
                            'success': True,
                            'status': 'no_exact_match_but_similar',
                            'message': _msg,
                            'suggestion': _suggestion,
                            'similar_properties': similar_formatted,
                            'suggested_budget': suggested_budget if needs_more_budget else max_budget,
                            'budget_increase_needed': budget_increase if needs_more_budget else 0,
                            'search_criteria': _criteria(),
                            'known_criteria': _known_criteria(),
                            'recommendations': similar_formatted,
                            'area_recommendations': area_recommendations,
                        })

            # 无预算，或相似回退也没结果 —— 诚实的 no_results（语言感知，提示表单）。
            _empty_msg = (_partial_note(incomplete_areas, is_cjk) if incomplete_areas
                          else _no_results_message(search_area, is_cjk))
            return _augment({
                'success': True,
                'status': 'no_results',
                'message': _empty_msg,
                'recommendations': [],
                'data_source': data_source,
                'search_criteria': _criteria(),
                'known_criteria': _known_criteria(),
                'area_recommendations': area_recommendations,
            })

        # ================================================================
        # 步骤 6: 格式化结果
        # ----------------------------------------------------------------
        # 硬预算是产品正确性要求（缺陷 H2）：主推荐 recommendations 必须严格满足
        # monthly_rent <= max_budget（perfect_match，价格已归一化）。软扩展（≤ 预算×1.15
        # 的"略超预算"房源，soft_violation）绝不混入主推荐、绝不计入"找到 N 套"的声明，
        # 只作为单独、明确双语标注的 over_budget_alternatives 备选列出，摘要也绝不声称其在预算内。
        # ================================================================
        main_matches = perfect_match[:limit]
        # over-budget 备选仅在有预算时存在（apply_hard_filters 契约：无预算时 soft 恒为空）。
        over_budget_matches = soft_violation[:3]
        display_pool = main_matches + over_budget_matches

        # 🆕 用房源详情页丰富"将要展示的候选"（主推荐 + 备选）：完整描述 + 真实可入住日期
        # （有界=仅待展示项；并发；一次抓取取回两者，按 URL 缓存）。让 Agent 拿到真实房源
        # 文本以回答后续问题，并让每套房都能诚实标注"何时可入住"。
        # Detail enrichment is an optional network-heavy post-stage (concurrent detail-page
        # GETs). Guard it on its own conservative estimate so it can never erode the return
        # margin; when time is short we simply skip it — availability falls back to the
        # honest "unknown / contact agent", never a guess.
        try:
            _DESC_ENRICH_EST_S = float(os.getenv("SEARCH_DESC_ENRICH_EST_S", "2.0"))
        except (TypeError, ValueError):
            _DESC_ENRICH_EST_S = 2.0
        if (os.getenv("DESC_ENRICH_ENABLED", "1") != "0" and display_pool
                and not degraded and _time_left() >= _DESC_ENRICH_EST_S):
            try:
                from core.scraping.onthemarket import fetch_listing_details as _fetch_details
            except Exception:
                _fetch_details = None
            if _fetch_details:
                _to_enrich = display_pool
                # Bound concurrency to a few workers; the actual OTM politeness is
                # enforced by the shared rate limiter inside fetch_listing_details
                # (>=1.2s between detail GETs across ALL threads). The semaphore just
                # caps how many executor threads/sessions are live at once.
                _enrich_sema = asyncio.Semaphore(3)

                async def _enrich_one(_url):
                    async with _enrich_sema:
                        # Re-read the ABSOLUTE deadline only after acquiring the semaphore:
                        # queued workers must not inherit a stale/full budget.  The sync
                        # detail function applies this total budget to limiter wait + HTTP.
                        _remaining = _time_left()
                        if _remaining <= 0:
                            return None
                        _call = partial(_fetch_details, _url, budget_s=_remaining)
                        return await loop.run_in_executor(None, _call)

                _enrich_tasks = [asyncio.create_task(
                    _enrich_one(p.get('URL') or p.get('url') or '')
                ) for p in _to_enrich]
                # Bound the AWAIT itself by the same absolute deadline.  Worker functions
                # also receive the remaining budget above, because cancelling an asyncio
                # wrapper cannot stop a function already running in an executor thread.
                try:
                    _done, _pending = await asyncio.wait(
                        _enrich_tasks, timeout=max(0.0, _time_left()))
                except BaseException:
                    # asyncio.wait never cancels its inputs.  If the parent search is
                    # cancelled (or otherwise unwinds), explicitly cancel and consume every
                    # child wrapper before propagating the original BaseException.
                    for _task in _enrich_tasks:
                        if not _task.done():
                            _task.cancel()
                        _task.add_done_callback(_consume_task_exception)
                    raise
                for _task in _pending:
                    _task.cancel()
                    _task.add_done_callback(_consume_task_exception)

                _details_list = []
                for _task in _enrich_tasks:
                    if _task not in _done or _task.cancelled():
                        _details_list.append(None)
                        continue
                    try:
                        _details_list.append(_task.result())
                    except Exception as _detail_exc:
                        logger.warning(
                            "property_search.detail_enrichment_failed",
                            extra={"error_type": type(_detail_exc).__name__},
                        )
                        _details_list.append(None)
                if _pending:
                    logger.info(
                        "property_search.detail_enrichment_deadline",
                        extra={
                            "completed_count": len(_done),
                            "timed_out_count": len(_pending),
                        },
                    )
                for _p, _d in zip(_to_enrich, _details_list):
                    if not isinstance(_d, dict):
                        continue
                    if _d.get('description'):  # 非空串才算有效（"" = 已知无描述，避免每次重抓）
                        _p['_full_description'] = _d['description']
                    if _d.get('available_from'):  # 真实可入住日期（"" = 未知，诚实留空）
                        _p['_available_from'] = _d['available_from']
                print(f"   📝 已丰富 {sum(1 for p in _to_enrich if p.get('_full_description'))}/"
                      f"{len(_to_enrich)} 个房源描述，"
                      f"{sum(1 for p in _to_enrich if p.get('_available_from'))} 个含可入住日期")

        # 🆕 入住日期：为每个待展示房源解析规范化可入住日期（供排序与卡片显示）。
        for prop in display_pool:
            prop['_resolved_available_from'] = _resolve_available_from(prop)

        # 🆕 入住匹配降级：给定期望入住日期时，把"仅晚于期望日"的房源稳定下沉到末尾（在既有
        # 分数/预算排序之上做稳定排序：可入住或未知在前、晚于期望日在后），但绝不排除。主推荐与
        # over-budget 备选各自独立降级。
        def _demote_late(props):
            out = list(props)
            if move_in_date:
                out.sort(key=lambda p: 1 if _is_late_availability(
                    p.get('_resolved_available_from', ''), move_in_date) else 0)
            return out

        main_display = _demote_late(main_matches)
        alt_display = _demote_late(over_budget_matches)

        def _format_rows(props):
            rows = []
            for i, prop in enumerate(props, 1):
                images = prop.get('Images', prop.get('images', []))
                if isinstance(images, str):
                    images = [images] if images else []
                geo_location = prop.get('Geo_Location', prop.get('geo_location', ''))
                _avail_from = prop.get('_resolved_available_from', '')
                _verified_room_type = (room_type if room_type and _matches_room_type(prop, room_type)
                                       else None)
                _verified_features = []
                _unverified_features = []
                _absent_features = []
                for _feature in all_property_features:
                    _normal = _canonical_property_feature(_feature)
                    _evidence = _property_feature_evidence(prop, _normal)
                    if _evidence == 'verified':
                        _verified_features.append(_normal)
                    elif _requires_explicit_feature_evidence(_normal) and _evidence == 'unverified':
                        _unverified_features.append(_normal)
                    elif _requires_explicit_feature_evidence(_normal) and _evidence == 'absent':
                        _absent_features.append(_normal)
                row = {
                    'rank': i,
                    # The provider's own borough word can contradict the listing's own postcode
                    # ("... Islington WC1N" for a Camden listing). Show the reconciled label;
                    # keep the raw provider string for traceability.
                    'address': reconcile_area_label(
                        prop.get('Address', prop.get('address', 'Unknown'))),
                    'provider_address': prop.get('Address', prop.get('address', 'Unknown')),
                    'price': f"£{int(prop.get('price', 0))}/month",
                    'budget_status': prop.get('budget_status', ''),
                    'score': prop.get('recommendation_score', 0),
                    'score_breakdown': prop.get('score_breakdown', {}),
                    'property_type': prop.get('Type', prop.get('type', 'Flat')),
                    'room_type': _verified_room_type,
                    'bedrooms': prop.get('Bedrooms', prop.get('bedrooms', 'N/A')),
                    'match_type': prop.get('match_type', 'perfect'),
                    'source': data_source,
                    'possibly_outdated': bool(prop.get('possibly_outdated', False)),
                    'url': prop.get('URL', prop.get('url', '')),
                    'images': images,
                    'geo_location': geo_location,
                    'distance_miles': prop.get('distance_miles'),
                    'explanation': _clean_explanation(
                        prop.get('Description', prop.get('description', '')),
                        prop.get('travel_time') if commute_annotation_enabled else None,
                        commute_target,
                        prop.get('travel_time_source'),
                    ),
                    # 🆕 多区域：该房源来自哪个搜索区域（前端卡片徽标）。
                    'area': prop.get('_search_area'),
                    # 🆕 OnTheMarket 详情页完整描述（未截断；供 Agent 后续问答）。
                    'description': prop.get('_full_description', ''),
                    # 🆕 可入住日期：""(未知→前端显示"Contact agent") | "Available now" | "YYYY-MM-DD"。
                    'available_from': _avail_from,
                    # 🆕 与期望入住日的匹配标注（无期望日/未知 → ""）。
                    'availability_status': _availability_status(_avail_from, move_in_date),
                    # Only exact structured fields enter the hard-constraint contract;
                    # free-text descriptions never prove that a feature is present.
                    'verified_features': _verified_features,
                    # Accessibility disclosure is commonly incomplete. These two
                    # explicit states prevent missing text from becoming either a
                    # false pass or a false hard failure downstream.
                    'unverified_features': _unverified_features,
                    'absent_features': _absent_features,
                }
                # 无通勤目标/无通勤时间时，完全省略 travel_time 字段（不出现 "0 min to None"）。
                _tt = prop.get('travel_time')
                if commute_annotation_enabled and _tt is not None:
                    _tsrc = prop.get('travel_time_source')
                    row['travel_time'] = _travel_time_label(_tt, commute_target, _tsrc)
                    # Structured provenance beside the display string: a coordinate estimate
                    # must never be consumed (by the model or by a downstream check) as a
                    # measured journey time.
                    row['travel_time_source'] = _tsrc
                rows.append(row)
            return rows

        formatted_results = _format_rows(main_display)
        over_budget_alternatives = _format_rows(alt_display)
        # 备选行显式结构化标记（即使前端不渲染 budget_status，也能识别其为超预算备选）。
        for _r in over_budget_alternatives:
            _r['alternative'] = True

        # 🆕 多区域：按来源区域(_search_area)统计"主推荐（严格在预算内）"的每区域套数，供摘要
        # 列出所有搜索区域（含返回 0 套者）。与标题的 n_total（=主推荐数）一致；over-budget
        # 备选不计入。单区域时传 None，_found_summary 输出与旧版逐字一致。
        _area_counts = None
        if len(search_areas) > 1:
            _cnt = {a.lower(): 0 for a in search_areas}
            for _p in perfect_match:
                _k = str(_p.get('_search_area', '')).lower()
                if _k in _cnt:
                    _cnt[_k] += 1
            _area_counts = [(a, _cnt.get(a.lower(), 0)) for a in search_areas]

        # 摘要的"找到 N"= 主推荐（严格在预算内）计数；over-budget 备选仅作为单独区块提及。
        _summary = _found_summary(main_matches, len(perfect_match), len(over_budget_matches),
                                  max_budget if has_budget else None,
                                  max_commute_time if commute_filter_enabled else None,
                                  search_area, commute_target, room_type, is_cjk,
                                  move_in_date, area_counts=_area_counts)
        if possibly_outdated:
            _summary += (" （部分为近期缓存房源，实时刷新暂不可用，可能已过期。）" if is_cjk
                         else " (Showing recent cached listings — a live refresh wasn't "
                              "available just now, so some may be outdated.)")
        # Honesty: when some areas were not fully searched, the per-area breakdown could read
        # a still-unsearched area's "0" as complete-empty. Append the partial note so the model
        # never claims those areas have no listings (incomplete ≠ empty, H2).
        if incomplete_areas:
            _summary += " " + _partial_note(incomplete_areas, is_cjk)
        # Honesty: if the user had a commute constraint but commute was never verified this
        # round (degraded / time-guarded), append the note so the headline itself never
        # implies a checked commute (the CR5 failure).
        if _has_commute_constraint and not commute_verified:
            _summary += " " + _commute_note(is_cjk)

        # over-budget 备选的双语区块标签（reply_language / is_cjk 决定语言）。空列表时留空串。
        over_budget_label = (
            "超预算备选（最多高出预算 15%；未计入上述结果，且不在预算内）" if is_cjk else
            "Over-budget alternatives (up to 15% above your budget; not counted in the "
            "results above and not within your budget)"
        ) if over_budget_alternatives else ''

        return _augment({
            'success': True,
            'status': 'found',
            # "找到 N 套" = 严格在预算内的主推荐数（绝不含 over-budget 备选）。
            'total_found': len(perfect_match),
            'data_source': data_source,
            'possibly_outdated': possibly_outdated,
            'search_criteria': _criteria(),
            # Populated snapshot of the ACTUAL resolved criteria so the frontend can
            # mirror state after a search (previously absent on the search path, so a
            # consumer reading known_criteria on a found turn got {}).
            'known_criteria': _known_criteria(),
            # 主推荐：严格满足 monthly_rent <= max_budget（前端读取此键，行为不变）。
            'recommendations': formatted_results,
            # 🆕 additive：软扩展的超预算备选，明确双语标注，绝不混入 recommendations、
            # 绝不计入 total_found。前端可选渲染；旧消费者忽略该键即可。
            'over_budget_alternatives': over_budget_alternatives,
            'over_budget_alternatives_label': over_budget_label,
            'perfect_count': len(perfect_match),
            'soft_count': len(soft_violation),
            'summary': _summary,
            'area_recommendations': area_recommendations,
        })

    except Exception as e:
        logger.error(
            "property_search.failed",
            extra={"error_type": type(e).__name__},
        )
        return {
            'success': False,
            'status': 'error',
            'error': 'Property search failed'
        }


# 创建工具实例
search_properties_tool = Tool(
    name="search_properties",

    description=SEARCH_TOOL_DESCRIPTION,

    func=search_properties_impl,

    parameters={
        'type': 'object',
        'properties': {
            'user_query': {
                'type': 'string',
                'description': 'The user\'s natural language query about finding properties. Used only as a fallback to recover an area/criteria when structured params are missing.'
            },
            'area': {
                'type': 'string',
                'description': 'Where the user wants to LIVE / the search area (e.g. Camden, Manchester, or a university like UCL). This (or commute_destination) is the only thing truly needed.'
            },
            'areas': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'OPTIONAL multiple residential areas to search at once (e.g. ["Camden", "Islington"]). Use when the user wants to compare several neighbourhoods. The single "area" is treated as the first of these; results are tagged with the area each listing came from.'
            },
            'location': {
                'type': 'string',
                'description': 'LEGACY alias. Treated as the search area; if it names a university (e.g. UCL) it also becomes the commute-annotation target. Prefer "area" and/or "commute_destination".'
            },
            'commute_destination': {
                'type': 'string',
                'description': 'Where the user commutes TO (e.g. UCL, London Bridge). Optional. When given, commute times are annotated (and filtered only if max_commute_time is a real limit). If no area is given, the search area is derived from this.'
            },
            'no_commute': {
                'type': 'boolean',
                'description': 'Set true only when the user explicitly does not commute at all. Working from home on some days does not remove an office commute. Disables commute computation and filtering.',
                'default': False
            },
            'bedrooms': {
                'type': 'integer',
                'description': 'Explicit bedroom count (0 = studio). Optional; when omitted the tool infers from the message or defaults to a broad range.'
            },
            'bedrooms_minimum': {'type': 'boolean', 'description': 'True when bedrooms is a minimum, rather than an exact count.'},
            'max_budget': {
                'type': 'number',
                'description': 'OPTIONAL maximum monthly rent in GBP, preserving pennies (e.g., 1900, 2166.67). Omit for no budget filter — never blocks the search.'
            },
            'monthly_total_budget': {
                'type': 'number', 'minimum': 0,
                'description': 'Separate monthly rent-plus-commuting affordability ceiling in GBP. It is not the rent ceiling, and cannot be verified without candidate-linked travel cost evidence.'
            },
            'office_days_per_week': {
                'type': 'integer', 'minimum': 0, 'maximum': 7,
                'description': 'Explicit office attendance days per week; retain this when testing the separate total budget.'
            },
            'max_changes': {
                'type': 'integer', 'minimum': 0, 'maximum': 30,
                'description': 'Explicit maximum public-transport changes. Missing candidate-linked route evidence leaves this constraint unverified.'
            },
            'max_commute_time': {
                'type': 'integer',
                'description': 'OPTIONAL maximum commute time in minutes. ONLY provide if the user explicitly states a limit AND has a commute destination. Omit for no commute filter — never blocks the search.'
            },
            'care_about_safety': {
                'type': 'boolean',
                'description': 'Whether user cares about area safety/crime rates.',
                'default': False
            },
            'limit': {
                'type': 'integer',
                'description': 'Maximum number of results to return.',
                'default': 10
            },
            # These are injected by the agent from conversational/accumulated state.
            # They MUST be declared here: the tool's pydantic input model drops any
            # undeclared kwarg (extra='ignore'), which previously silently discarded
            # current_message — killing the in-message budget/commute override so a
            # mid-conversation "my budget is now £1000" was ignored (D2).
            'current_message': {
                'type': 'string',
                'description': 'The raw text of ONLY this turn\'s user message (no injected memory/history). Used to let an explicit budget/commute stated this turn override accumulated values.'
            },
            'property_features': {
                'type': 'array',
                'description': 'Accumulated property features (e.g. studio, en-suite) carried across turns.'
            },
            'accumulated_preferences': {
                'type': 'array',
                'description': 'Accumulated soft preferences carried across turns.'
            },
            'budget_period': {
                'type': 'string',
                'description': "Budget period: 'week' or 'month' (default month)."
            },
            'min_budget': {
                'type': 'integer',
                'description': 'Minimum monthly budget in GBP.'
            },
            'room_type': {
                'type': 'string',
                'description': "OPTIONAL preferred room type: 'studio', 'ensuite', or "
                               "'shared', or 'whole' for a self-contained flat/house (omit for any). Studio also implies a 0-bedroom search."
            },
            'move_in_date': {
                'type': 'string',
                'description': "OPTIONAL desired move-in / tenancy-start date as 'YYYY-MM-DD'. "
                               "Never blocks the search: when omitted, every listing still "
                               "shows its availability; when provided, listings are annotated "
                               "for fit (available on/before it) and later-only ones are "
                               "demoted but not excluded. The tool also parses an explicit "
                               "move-in date stated in the current message."
            },
            'confirmed': {
                'type': 'boolean',
                'description': 'Set true when the user has explicitly confirmed to proceed '
                               '(chat "continue"/"搜索") or when called from the criteria-panel '
                               'Search button (/api/search_direct). Bypasses the soft criteria gate.',
                'default': False
            },
            'criteria_gate_shown': {
                'type': 'boolean',
                'description': 'Injected by the agent from accumulated criteria: true once the '
                               'soft criteria gate has already been shown in this conversation '
                               '(so it fires at most once).',
                'default': False
            },
            'reply_language': {
                'type': 'string',
                'description': "OPTIONAL reply-language override: 'zh' or 'en'. When set it "
                               "FORCES every user-facing string (found summary, gate question, "
                               "no-results, similar-fallback) into that language instead of "
                               "inferring from the current message. The app sets it from the "
                               "reply-language policy (current message CJK, else the frontend UI "
                               "language); omit to keep the message-based inference."
            }
        },
        'required': []  # 没有必须参数 - 工具内部会处理
    },

    max_retries=2
)


# Import-time pre-warm is opt-in because constructing the embedding model may
# perform network/cache discovery. Deployments with a pre-populated model cache
# can explicitly enable it without slowing or destabilising offline startup.
if (
    "pytest" not in sys.modules
    and os.getenv("SEARCH_EMBED_PREWARM_ON_IMPORT", "0").strip().lower()
    in {"1", "true", "yes", "on"}
):
    start_embedding_prewarm()
