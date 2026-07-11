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
from typing import Optional, List, Dict
import pandas as pd
import asyncio
import math
import os
from pathlib import Path
import json
import re
from uk_rent_agent.domain import constants as C

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
    "no commute", "not commuting", "don't commute", "dont commute",
    "i don't need to commute", "just living", "just live",
    "work from home", "working from home", "wfh", "remote work", "fully remote",
    "不通勤", "不用通勤", "不需要通勤", "无需通勤",
    "单纯住", "就是住", "只是住", "不上班", "在家办公", "居家办公", "远程工作",
)


def _extract_no_commute(text: str) -> bool:
    """True when the message explicitly says the user does not commute.
    Deterministic so "我不通勤我单纯住着" / "I just live there, wfh" disables all
    commute logic (no computation, no filter, never ask)."""
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in _NO_COMMUTE_PHRASES)


# Phrases meaning "just run the search with what we have" — used to bypass the soft
# criteria gate (D1). CJK phrases match the raw text; English phrases match on word
# boundaries so short tokens ('go on') can't fire mid-word. Kept deliberately small
# and specific (a proceed signal, not a general affirmation).
_PROCEED_PHRASES_ZH = (
    "继续搜索", "继续搜", "继续找", "继续", "就这样吧", "就这样", "直接搜索", "直接搜",
    "可以了", "没事", "不用了继续", "都行", "先搜", "随便搜",
)
_PROCEED_PATTERNS_EN = (
    r"\bcontinue\b", r"\bgo ahead\b", r"\bsearch anyway\b", r"\bjust search\b",
    r"\bsearch now\b", r"\bproceed\b", r"\bthat'?s fine\b", r"\bthat is fine\b",
    r"\bgo on\b", r"\bkeep going\b", r"\bit'?s fine\b",
)


def _is_proceed_intent(text: str) -> bool:
    """True when the user is telling us to go ahead and search despite missing
    recommended criteria (soft gate bypass)."""
    if not text:
        return False
    if any(p in text for p in _PROCEED_PHRASES_ZH):
        return True
    t = text.lower()
    return any(re.search(p, t) for p in _PROCEED_PATTERNS_EN)


# Room-type synonyms -> canonical value ('studio' | 'ensuite' | 'shared'). Substring
# match (CJK + EN). 'studio' is a distinct property form (implies 0 bedrooms), so it is
# checked first and never shadowed by the room-in-a-share types.
_ROOM_TYPE_SYNONYMS = (
    ("studio", ("studio", "单间公寓", "一室户", "开间")),
    ("ensuite", ("ensuite", "en-suite", "en suite", "独立卫浴", "独卫", "套间", "带独卫")),
    ("shared", ("shared room", "flatshare", "flat share", "house share", "houseshare",
                "shared", "合租房", "合租", "共享房间")),
)


def _extract_room_type(text: str):
    """Canonical room type from a message: 'studio' | 'ensuite' | 'shared' | None.
    Deterministic so a chat answer after the soft gate ("我要ensuite") updates the
    accumulated room_type even without any 'find'/'search' verb."""
    if not text:
        return None
    t = text.lower()
    for canonical, needles in _ROOM_TYPE_SYNONYMS:
        if any(n in t for n in needles):
            return canonical
    return None


def _normalize_room_type(value):
    """Coerce an incoming room_type (from the form, accumulated state, or free text)
    to a canonical value or None. Unknown/free-text values are run through the
    synonym extractor so 'en-suite' or 'a studio please' still map correctly."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower()
    if v in ("studio", "ensuite", "shared"):
        return v
    return _extract_room_type(v)


# Display labels for room type, localized (en, zh).
_ROOM_TYPE_LABELS = {
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
    if room_type == 'studio':
        return 'studio' in rt or 'studio' in desc
    if room_type == 'ensuite':
        return 'en-suite' in blob or 'ensuite' in blob or 'en suite' in blob
    if room_type == 'shared':
        return 'shar' in rt or 'shar' in desc or 'flatshare' in blob or 'flat share' in blob
    return True


def _extract_budget(text: str):
    """Pull an explicit monthly/weekly budget out of the *current* user message.
    Returns (amount:int, period:'week'|'month') or (None, None). Deterministic regex
    so a conversational update ("my budget is now 1800") overrides accumulated state
    without an extra LLM round-trip."""
    if not text:
        return None, None
    t = text.lower().replace(',', '')
    amount = None
    # 1) currency-tagged amount: "£1800", "£ 1800"
    m = re.search(r'£\s?(\d{3,5})\b', t)
    # 2) amount followed by a budget unit: "1800 pcm", "1800 per month", "1800 a week"
    if not m:
        m = re.search(r'\b(\d{3,5})\s*(?:pcm|pm|pw|/\s*(?:month|week|wk)|per\s+(?:month|week)|a\s+(?:month|week)|pounds?)\b', t)
    # 3) budget keyword + amount: "budget is now 1800", "budget of 1800", "max budget 1800"
    if not m:
        m = re.search(r'budget\b[^£\d]{0,20}£?\s?(\d{3,5})\b', t)
    # 4) budget-INTENT phrasing (esp. clarification answers): "under 1000", "up to 1200",
    #    "max 900", "around 1500", and Chinese "1000以内/以下/左右", "预算1000", "月租1000".
    if not m:
        m = re.search(r'\b(?:under|below|max(?:imum)?|up\s+to|around|about|no\s+more\s+than|less\s+than)\s*£?\s?(\d{3,5})\b', t)
    if not m:
        m = re.search(r'(\d{3,5})\s*(?:以内|以下|左右|块|镑|元|英镑)', t)
    if not m:
        m = re.search(r'(?:预算|月租|租金|房租)\s*[^\d]{0,6}(\d{3,5})', t)
    if m:
        val = int(m.group(1))
        if 200 <= val <= 20000:
            amount = val
    if amount is None:
        return None, None
    period = 'week' if re.search(r'\b(?:pw|/\s*w(?:k|eek)?|per\s+week|a\s+week)\b', t) else 'month'
    return amount, period


def _extract_commute_minutes(text: str):
    """Pull an explicit commute-time limit (in minutes) out of the current message.
    Returns int minutes or None. Handles '40 min', 'within 30 minutes', 'half an hour',
    'an hour'."""
    if not text:
        return None
    t = text.lower()
    if re.search(r'\bhalf\s+an?\s+hour\b', t):
        return 30
    if re.search(r'\b(?:an?|1)\s+hour\b', t):
        return 60
    m = re.search(r'\b(\d{1,3})\s*(?:-)?\s*(?:min|mins|minute|minutes)\b', t)
    if not m:
        m = re.search(r'\b(\d{1,3})\s*hours?\b', t)
        if m:
            v = int(m.group(1)) * 60
            return v if 1 <= v <= 180 else None
    if m:
        v = int(m.group(1))
        if 1 <= v <= 180:
            return v
    return None


# Any address-based commute estimate above this (minutes) is treated as a
# geocoding glitch and replaced by the coordinate-based estimate.
_COMMUTE_SANITY_CAP = 240
# Greater-London bounding box — inside it we trust TfL Journey Planner; outside
# it (Manchester, Leeds, ...) TfL has no route and street-address geocoding is
# unreliable, so we estimate from the listing's own exact lat/lon instead.
_LONDON_BBOX = (51.28, 51.70, -0.55, 0.30)  # (lat_min, lat_max, lng_min, lng_max)


def _parse_geo(geo) -> tuple[float, float] | None:
    """'53.4415, -2.2159' -> (53.4415, -2.2159); tolerant of blanks/junk."""
    if not geo:
        return None
    m = re.findall(r"-?\d+\.?\d*", str(geo))
    if len(m) < 2:
        return None
    try:
        lat, lng = float(m[0]), float(m[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return (lat, lng)
    return None


def _in_london(coords: dict | None) -> bool:
    if not coords:
        return False
    la, lo = coords.get("lat"), coords.get("lng")
    if la is None or lo is None:
        return False
    return _LONDON_BBOX[0] <= la <= _LONDON_BBOX[1] and _LONDON_BBOX[2] <= lo <= _LONDON_BBOX[3]


def _coord_commute_minutes(geo_str, dest_coords: dict | None) -> int | None:
    """Distance-based transit estimate (minutes) from a listing's exact
    coordinates to the destination. Mirrors maps_service.estimate_travel_time_simple
    (1.3x route factor, 20 km/h transit, short wait) but uses the scraped lat/lon
    directly, so it never depends on flaky street-address geocoding."""
    o = _parse_geo(geo_str)
    if not o or not dest_coords:
        return None
    dla, dlo = dest_coords.get("lat"), dest_coords.get("lng")
    if dla is None or dlo is None:
        return None
    R = 6371.0
    dlat = math.radians(dla - o[0])
    dlng = math.radians(dlo - o[1])
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(o[0])) * math.cos(math.radians(dla)) * math.sin(dlng / 2) ** 2)
    dist_km = R * 2 * math.asin(math.sqrt(a))
    actual = dist_km * 1.3
    return int((actual / 20.0) * 60 + min(10, dist_km * 2))


_WORD_NUMS = {"studio": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _extract_bedrooms(text: str):
    """Pull an explicit bedroom count out of the current message.
    Returns (min_beds, max_beds) or None. 'studio' -> (0, 0); 'N bed[room]' and
    the spelled-out one..five -> (N, N). Deterministic so a Manchester 1-bed
    query drives an exact min=max=1 filter on the live source (no studios/2-beds
    leaking in, the original demo-data bug)."""
    if not text:
        return None
    t = text.lower()
    if re.search(r"\bstudio\b", t):
        return (0, 0)
    m = re.search(r"\b(\d)\s*(?:-)?\s*bed(?:room)?s?\b", t)
    if m:
        n = int(m.group(1))
        if 0 <= n <= 6:
            return (n, n)
    m = re.search(r"\b(one|two|three|four|five)\s*(?:-)?\s*bed(?:room)?s?\b", t)
    if m:
        n = _WORD_NUMS[m.group(1)]
        return (n, n)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Area / city extraction (conversational switch + bare place name + zh cities).
# ─────────────────────────────────────────────────────────────────────────────
# SINGLE SOURCE OF TRUTH for Chinese→English UK place names. Reused by the main
# fresh-search area path so a first message like "曼彻斯特的公寓" resolves without an
# LLM round-trip. Chinese place names are distinctive enough for a safe substring
# match. Nonsense / non-UK names (火星/月球/瓦坎达 …) are deliberately absent -> None.
_ZH_AREA_MAP = {
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
_KNOWN_AREA_NAMES = [
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
    # well-known London boroughs / areas
    "Camden", "Islington", "Hackney", "Shoreditch", "Hoxton", "Dalston",
    "Brixton", "Clapham", "Peckham", "Greenwich", "Ealing", "Wimbledon",
    "Stratford", "Kensington", "Chelsea", "Fulham", "Hammersmith",
    "Notting Hill", "Whitechapel", "Bethnal Green", "Bermondsey", "Deptford",
    "Lewisham", "Wandsworth", "Battersea", "Vauxhall", "Pimlico", "Marylebone",
    "Soho", "Mayfair", "Finsbury Park", "Tottenham", "Walthamstow", "Wembley",
    "Harrow", "Barnet", "Enfield", "Richmond", "Kingston", "Putney", "Balham",
    "Tooting", "Canary Wharf", "Elephant and Castle", "Highbury", "Highgate",
    "Hampstead", "Kentish Town", "Holloway", "Stoke Newington", "Mile End",
    "Clerkenwell", "Farringdon", "Acton", "Chiswick", "Streatham", "Catford",
    "Leyton", "Leytonstone", "Hackney Wick", "New Cross", "Rotherhithe",
    "Kilburn", "Willesden", "Croydon", "Bromley", "Ilford", "Woolwich",
    "Camberwell", "Kennington", "Aldgate", "Southwark", "Wapping", "Limehouse",
    "Blackheath", "Forest Hill", "Shepherd's Bush", "Earl's Court",
    "King's Cross",
]
# key (lowercased, apostrophes removed) -> canonical spelling.
_KNOWN_AREAS = {re.sub(r"[’']", "", n.lower()): n for n in _KNOWN_AREA_NAMES}

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
    if not text or not text.strip():
        return None
    # (a) Chinese city names — distinctive, safe substring match.
    for zh, canon in _ZH_AREA_MAP.items():
        if zh in text:
            return canon
    t = text.lower().strip()
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
                   is_cjk: bool = False) -> str:
    """Informative, context-varying result headline (Deliverable 4): result count +
    area, the price range of the returned listings (min–median–max), and a one-line
    recap of the applied filters (budget / room type / commute). Localized zh/en and
    including the right-panel hint, so callers use it verbatim (no English-only
    suffix bolted on afterwards). Never emits '999 min' or '£None'."""
    n_total = n_perfect + n_soft
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
    else:
        if max_budget:
            filt.append(f"budget ≤£{max_budget}/month")
        if rt_label:
            filt.append(f"room type {rt_label}")
        if real_commute:
            filt.append(f"commute ≤{int(max_commute_time)} min to {commute_target}")
        elif commute_target:
            filt.append(f"commute to {commute_target}")

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
            s += f"其中 {n_soft} 套略超预算。"
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
        s += f" {n_soft} of these are slightly over budget."
    s += " See the full listings in the right-hand panel."
    return s


def _soft_gate_question(missing, is_cjk: bool) -> str:
    """Localized soft-criteria clarification, listing ONLY the actually-missing
    recommended fields (budget / room_type / commute)."""
    if is_cjk:
        bits = []
        if 'room_type' in missing:
            bits.append("您想要什么房型（Studio / 独立卫浴 ensuite / 合租 shared）？")
        if 'budget' in missing:
            bits.append("每月预算大概多少？")
        if 'commute' in missing:
            bits.append("需要考虑通勤吗？如果需要，请告诉我通勤到哪里、最多多少分钟；如果不需要，可以说“不通勤”。")
        return ("在搜索之前，我想先确认几个条件：" + "".join(bits) +
                "您也可以直接说“继续搜索”，或在右侧的搜索条件面板补充后点击搜索。")
    bits = []
    if 'room_type' in missing:
        bits.append("what room type you'd like (studio / en-suite / shared)?")
    if 'budget' in missing:
        bits.append("what your monthly budget is?")
    if 'commute' in missing:
        bits.append("whether you need to consider a commute (if so, to where and the max "
                    "minutes; if not, just say \"no commute\")?")
    return ("Before I search, could you confirm a couple of things: " + " ".join(bits) +
            " You can also just say \"continue\" to search anyway, or fill in the "
            "search-criteria panel on the right and press Search.")


def _no_results_message(area: str, is_cjk: bool) -> str:
    """Honest 'nothing found' text, language-aware, always pointing at the form."""
    if is_cjk:
        return (f"目前在 {area} 附近没有找到符合条件的实时房源。"
                f"请检查区域拼写，或调整预算/区域后重试（可使用右侧搜索表单）。")
    return (f"I couldn't find any current listings matching your criteria near {area} "
            f"right now. Please check the area spelling, or adjust the budget/area and "
            f"try again (you can also use the search form on the right).")


def _clean_explanation(desc, travel_min, location):
    """Single-source the commute time in the explanation: strip the static CSV
    Bus/Walk/Cycle/Drive minutes (which disagree with the live TfL time) and append
    the real TfL transit time so the card never shows two conflicting commute times.
    When there is no commute data (no target / no computed time) NO suffix is added,
    so a no-commute search never shows '0 min to None'."""
    desc = desc or ''
    cleaned = re.sub(r'(Bus|Walk|Cycle|Drive)[ ]*[0-9]+[ ]*min[,. ]*', '', desc, flags=re.IGNORECASE)
    cleaned = ' '.join(cleaned.split()).strip().rstrip('.,').strip()[:120]
    suffix = ''
    if travel_min is not None and location:
        try:
            suffix = f" (TfL transit: {int(travel_min)} min to {location})"
        except (TypeError, ValueError):
            suffix = ''
    return (cleaned + suffix).strip()


_RAG_COORDINATOR = None


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
        from rag.rag_coordinator import RAGCoordinator
        _RAG_COORDINATOR = RAGCoordinator()
    return _RAG_COORDINATOR


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

            except (ValueError, TypeError):
                print(f"   ⚠️ 跳过房源 {prop.get('address', 'Unknown')}: 数据格式错误")
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


async def search_properties_impl(
    user_query: str = "",
    area: str = None,                 # 🆕 想住/搜索的区域（首选）
    location: str = None,             # 遗留别名：见下方解析规则
    commute_destination: str = None,  # 🆕 通勤目的地（可选）
    max_budget: int = None,
    max_commute_time: int = None,
    no_commute: bool = False,         # 🆕 用户明确表示不通勤
    bedrooms: int = None,             # 🆕 明确的卧室数（0=studio）
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
    confirmed: bool = False,          # 🆕 用户已确认/表单直搜 —— 跳过软性条件门
    criteria_gate_shown: bool = False,  # 🆕 本会话软性条件门是否已出现过（至多触发一次）
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
    # Memory-stripped view of user_query. The agent PREPENDS a long-term-memory
    # block to user_query; a HARD search filter (area/budget/room_type/bedrooms)
    # must NEVER be extracted from a prior conversation's remembered criteria
    # (cross-conversation bleed). Every deterministic hard-criteria extraction below
    # runs on THIS turn's real text only: current_message (this turn, memory-free)
    # is preferred; stripped_query is the defensive fallback when the caller omits
    # current_message. Long-term memory may still inform soft context elsewhere, but
    # it can no longer SET a hard filter the user didn't state this conversation.
    stripped_query = _strip_memory_block(user_query)

    # ── 语言：中文用户用中文回复（澄清/无结果文案） ──────────────────────
    is_cjk = _has_cjk(current_message) or _has_cjk(stripped_query)

    # 🆕 初始化累积的特征和偏好
    # 修复：确保输入是列表，不是字符串
    if isinstance(property_features, list):
        all_property_features = list(property_features)
    elif isinstance(property_features, str):
        all_property_features = [property_features] if property_features else []
    else:
        all_property_features = []

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
        print(f"   ℹ️ 收到额外参数: {kwargs}")

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

        print(f"\n{'='*60}")
        print(f"🏠 [SEARCH TOOL] 开始执行房源搜索")
        print(f"   user_query: {user_query}")
        print(f"   area: {area} | location: {location} | commute_destination: {commute_destination}")
        print(f"   max_budget: {max_budget} | max_commute_time: {max_commute_time} | no_commute: {no_commute}")
        print(f"   property_features (累积): {all_property_features}")
        print(f"   soft_preferences (累积): {all_soft_preferences}")
        print(f"{'='*60}")

        # ================================================================
        # 步骤 0: 本轮消息里的显式信号优先（预算/通勤/不通勤），但绝不重新引入门槛
        # ================================================================
        # 只在"本轮真实消息"上做硬条件抽取（memory-stripped），绝不从注入的长期记忆块里
        # 抓取区域/预算/房型/卧室（跨会话泄漏修复）。
        msg_for_extraction = current_message or stripped_query
        # 房型：规范化传入值；缺失时从本轮消息里抽取（"我要ensuite" 亦生效）。
        room_type = _normalize_room_type(room_type)
        if not room_type:
            room_type = _extract_room_type(msg_for_extraction)
        if not no_commute and _extract_no_commute(msg_for_extraction):
            no_commute = True
            print(f"   🔕 本轮消息判定为『不通勤』，禁用全部通勤逻辑")
        if msg_for_extraction:
            fresh_budget, fresh_period = _extract_budget(msg_for_extraction)
            if fresh_budget and fresh_budget != max_budget:
                print(f"   🔄 当前消息更新预算: £{max_budget} → £{fresh_budget}/{fresh_period}")
                max_budget = fresh_budget
                budget_period = fresh_period or budget_period
            fresh_commute = _extract_commute_minutes(msg_for_extraction)
            if fresh_commute and fresh_commute != max_commute_time:
                print(f"   🔄 当前消息更新通勤上限: {max_commute_time} → {fresh_commute} min")
                max_commute_time = fresh_commute

        # ================================================================
        # 步骤 1: 解析搜索区域（唯一必需项）与通勤标注目标
        # ================================================================
        def _clean_area(v):
            return v.strip() if isinstance(v, str) and v.strip() else None

        search_area = _clean_area(area) or _clean_area(location)
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
                print(f"   🧭 确定性区域识别: {search_area}")

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
                print(f"   ⚠️ NL 抽取不可用: {_e}")
                criteria_response = {}
            print(f"   NL 返回: {json.dumps(criteria_response, ensure_ascii=False)[:400]}")

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
                # 区分"目的地自身的城市"（上下文，非用户选定的家 → 清空以反问住哪）与"用户
                # 另给的、与目的地不同的真实居住区"（如 "...near the Google office, live in
                # Camden" → 保留 Camden，只锁通勤）。仅动用户当作住哪输入的 token；由
                # commute_destination 推导出的 slug 不在此列。
                if search_area and not search_area_from_commute_dest:
                    _sa = search_area.strip().lower()
                    if is_destination(classify_place(search_area)) or _sa == _dest_city:
                        search_area = None
                print(f"   🎯 消息命名目的地：锁定通勤目标『{locked_commute_dest}』"
                      f"(commute_destination={commute_destination})，改问住哪 "
                      f"(residential={search_area})")

        # 步骤 1.4b: 若"想住的区域"其实是一个目的地 token（用户把 UCL/某公司当住哪说），
        # 且上面 1.4a 尚未锁定，则在此重分类为通勤目标。
        if not locked_commute_dest and search_area and not search_area_from_commute_dest:
            _place = classify_place(search_area)
            if is_destination(_place):
                _token = search_area
                commute_destination = _place.get("address") or _token
                no_commute = False
                locked_commute_dest = _token  # 人类可读的目的地名（如 "UCL"），用于门文案
                # 用户若另给了一个"不同的"真实居住区，保留之为 search_area；
                # 否则清空，触发下方"想住哪"硬门。
                _residential = None
                for _cand in (_clean_area(area), _clean_area(location)):
                    if (_cand and _cand != _token
                            and not is_destination(classify_place(_cand))):
                        _residential = _cand
                        break
                search_area = _residential
                print(f"   🎯 区域即目的地：锁定通勤目标『{locked_commute_dest}』"
                      f"(commute_destination={commute_destination})，改问住哪 "
                      f"(residential={search_area})")

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
        min_beds, max_beds = 0, 2  # 未指定时放宽
        feats_lower = [str(f).lower() for f in all_property_features]
        if isinstance(bedrooms, int) and 0 <= bedrooms <= 6:
            min_beds = max_beds = bedrooms
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
                resolved_bedrooms = min_beds if min_beds == max_beds else None
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
                'commute_destination': commute_target,
                'no_commute': no_commute,
                'bedrooms': resolved_bedrooms,
                'budget_period': budget_period,
                'room_type': room_type,
                # legacy（update_search_criteria / 旧 UI 仍在读）
                'destination': commute_target or search_area,
                'max_budget': max_budget,
                'max_travel_time': max_commute_time,
                'property_features': all_property_features,
                'soft_preferences': all_soft_preferences,
            }

        # Full "what we know" snapshot for BOTH clarification gates (area + soft
        # criteria). Includes room_type so the frontend panel can reflect it.
        def _known_criteria():
            return {
                'area': search_area,
                'commute_destination': commute_target,
                'max_budget': max_budget,
                'max_travel_time': max_commute_time,
                'no_commute': no_commute,
                'bedrooms': resolved_bedrooms,
                'budget_period': budget_period,
                'room_type': room_type,
                'property_features': all_property_features,
                'soft_preferences': all_soft_preferences,
            }

        # Legacy merge shape the graph folds back into accumulated criteria.
        def _extracted_so_far(extra=None):
            base = {
                'destination': commute_target,
                'max_budget': max_budget,
                'max_travel_time': max_commute_time,
                'room_type': room_type,
                'property_features': all_property_features,
                'soft_preferences': all_soft_preferences,
            }
            if extra:
                base.update(extra)
            return base

        # ================================================================
        # 步骤 2: 澄清 —— 仅当"搜索区域"无法确定时才追问（问『住哪』，绝不问『通勤去哪』）
        # ================================================================
        if not search_area:
            if locked_commute_dest:
                # 区域即目的地：默认通勤模式已锁定，只需再问"想住哪"（顺带 nudge 预算）。
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
                'missing_fields': ['area'],
                'known_criteria': _known_criteria(),
                'extracted_so_far': _extracted_so_far(),  # 保留旧 merge 代码期待的形状
            }

        # ================================================================
        # 步骤 2.5: 预算模式（可选）—— 缺失即无门槛
        # ================================================================
        has_budget = bool(max_budget) and int(max_budget) > 0
        if has_budget and budget_period and budget_period.lower() == 'week':
            original_budget = max_budget
            max_budget = int(max_budget * C.WEEKS_PER_MONTH)
            print(f"\n💱 [BUDGET] 周租转月租: £{original_budget}/week → £{max_budget}/month")

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
        commute_satisfied = bool(no_commute) or (commute_target is not None)
        soft_missing = []
        if not has_budget:
            soft_missing.append('budget')
        if not room_type:
            soft_missing.append('room_type')
        if not commute_satisfied:
            soft_missing.append('commute')

        proceed_confirmed = bool(confirmed) or _is_proceed_intent(msg_for_extraction)
        if soft_missing and not criteria_gate_shown and not proceed_confirmed:
            print(f"   🚪 [SOFT GATE] 缺失推荐条件 {soft_missing}，先确认再搜索")
            return {
                'success': False,
                'status': 'need_clarification',
                'clarification_kind': 'soft_criteria',
                'question': _soft_gate_question(soft_missing, is_cjk),
                'missing_fields': soft_missing,
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

        print(f"\n🌐 [SEARCH] 抓取实时房源: area={search_area}, beds={min_beds}-{max_beds}, "
              f"£{scrape_min}-{scrape_max}/month")
        loop = asyncio.get_event_loop()
        listing_result = await loop.run_in_executor(
            None,
            lambda: get_listings(search_area, min_beds, max_beds, scrape_min, scrape_max, limit=15),
        )
        live_rows = listing_result['rows']
        listing_meta = listing_result['meta']
        possibly_outdated = bool(listing_meta.get('stale'))
        _src = listing_meta.get('source')
        data_source = 'OnTheMarket' + (' (cached)' if _src in ('hit', 'stale-cache') else '')
        print(f"   ✅ 实时房源 {listing_meta.get('count', 0)} 个 "
              f"(source={_src}, {listing_meta.get('elapsed_s')}s)")

        # 没有任何真实房源 —— 诚实返回（语言感知），绝不使用 demo 假数据。
        if not live_rows:
            return {
                'success': True,
                'status': 'no_results',
                'message': _no_results_message(search_area, is_cjk),
                'recommendations': [],
                'data_source': data_source,
                'search_criteria': _criteria(),
                'known_criteria': _known_criteria(),
            }

        # 规范化真实行：解析价格、推断卧室/房型、标记过期。
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

        # 在"仅这些城市正确的真实行"上重建语义索引。
        rag_coordinator = _get_rag_coordinator()
        rag_coordinator.property_store.build_index(live_rows)

        # 🆕 构建搜索查询（无预算时避免 "£None"）
        if has_budget:
            search_query = f"Find flat near {search_area} under £{max_budget}"
            if all_property_features:
                search_query = f"Find {', '.join(all_property_features)} flat near {search_area} under £{max_budget}"
        else:
            search_query = f"Find flat near {search_area}"
            if all_property_features:
                search_query = f"Find {', '.join(all_property_features)} flat near {search_area}"

        # 传给 RAG 的 criteria 用安全值，避免 _hybrid_rank 对 None 做算术。
        criteria = {
            'destination': commute_target or search_area,
            'max_budget': max_budget if has_budget else 10_000_000,
            'max_travel_time': max_commute_time if commute_filter_enabled else NO_COMMUTE_LIMIT,
            'property_features': all_property_features,
            'soft_preferences': all_soft_preferences,
        }

        ranked_properties, past_context, area_info = rag_coordinator.enhanced_search(
            search_query, criteria
        )
        print(f"   ✅ RAG 返回 {len(ranked_properties)} 个候选房源")

        # 🆕 根据房产特征过滤结果（注意：不要遮蔽函数级的 room_type 参数）
        if all_property_features:
            print(f"\n🔍 [SEARCH] 根据房产特征过滤: {all_property_features}")
            filtered_by_features = []
            for prop in ranked_properties:
                prop_rt = prop.get('Room_Type_Category', '').lower()
                description = prop.get('Description', '').lower()
                amenities = prop.get('Detailed_Amenities', '').lower()
                matches = True
                for feature in all_property_features:
                    feature_lower = feature.lower()
                    if feature_lower in ['studio', 'private', 'en-suite', 'ensuite']:
                        if feature_lower == 'studio' and 'studio' not in prop_rt:
                            matches = False
                            break
                        if feature_lower == 'private' and 'private' not in prop_rt and 'private' not in description:
                            matches = False
                            break
                        if feature_lower in ['en-suite', 'ensuite'] and 'en-suite' not in prop_rt and 'en-suite' not in amenities:
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
            print(f"\n🔍 [SEARCH] 根据房型过滤: {room_type}")
            rt_matched = [p for p in ranked_properties if _matches_room_type(p, room_type)]
            if rt_matched:
                print(f"   ✅ 房型过滤后剩余 {len(rt_matched)} 个房源")
                ranked_properties = rt_matched
            else:
                print(f"   ⚠️ 房型过滤后无结果，保留原始结果并在说明中提及")

        # ================================================================
        # 步骤 4: 通勤时间（仅在有标注目标且未声明不通勤时计算/过滤）
        # ================================================================
        candidates = ranked_properties[:15]
        dest_coords = None
        london_dest = False

        if commute_annotation_enabled:
            print(f"\n⏱️ [SEARCH] 计算通勤时间到 {commute_target} "
                  f"(过滤={'开' if commute_filter_enabled else '关'})...")
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

            annotated = []
            for prop, tfl in zip(candidates, tfl_times):
                if isinstance(tfl, Exception):
                    tfl = None
                travel_time = tfl if (isinstance(tfl, (int, float)) and 0 < tfl <= _COMMUTE_SANITY_CAP) else None
                if travel_time is None:
                    travel_time = _coord_commute_minutes(
                        prop.get('geo_location') or prop.get('Geo_Location'), dest_coords
                    )
                if travel_time is None and not london_dest:
                    try:
                        travel_time = calculate_travel_time(prop.get('Address', ''), commute_target)
                    except Exception:
                        travel_time = None
                if travel_time is not None:
                    prop['travel_time_minutes'] = travel_time
                    prop['travel_time'] = travel_time
                if commute_filter_enabled:
                    if travel_time is not None and travel_time <= max_commute_time:
                        annotated.append(prop)
                    # 否则：超出上限，丢弃
                else:
                    annotated.append(prop)  # 仅标注，不过滤
            candidates = annotated
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

        # ================================================================
        # 步骤 5.5: 无匹配时的回退
        #   - 有预算：尝试 RAG "相似但略超" 建议（此路径仅在有预算时有意义）
        #   - 无预算：直接给出诚实的 no_results（语言感知）
        # ================================================================
        if not perfect_match and not soft_violation:
            if has_budget:
                print(f"\n⚠️ [SEARCH] 无符合结果，尝试 RAG 相似房源...")
                similar_properties = rag_coordinator.property_store.search(
                    f"flat apartment near {search_area} budget {max_budget}", top_k=10
                )
                if similar_properties:
                    similar_with_commute = []
                    for prop in similar_properties[:6]:
                        try:
                            travel_time = None
                            if commute_annotation_enabled:
                                from core.maps_service import calculate_travel_time
                                if london_dest:
                                    _tt = calculate_travel_time(prop.get('Address', ''), commute_target)
                                    travel_time = _tt if (isinstance(_tt, (int, float)) and 0 < _tt <= _COMMUTE_SANITY_CAP) else None
                                if travel_time is None:
                                    travel_time = _coord_commute_minutes(
                                        prop.get('geo_location') or prop.get('Geo_Location'), dest_coords
                                    )
                            keep = True
                            if commute_filter_enabled:
                                keep = travel_time is not None and travel_time <= max_commute_time * C.SIMILAR_COMMUTE_SLACK
                            if keep:
                                if travel_time is not None:
                                    prop['travel_time'] = travel_time
                                prop['price'] = prop.get('parsed_price', parse_price(prop.get('Price', '')))
                                similar_with_commute.append(prop)
                        except Exception:
                            continue

                    if similar_with_commute:
                        similar_with_commute.sort(key=lambda x: x.get('price', float('inf')))
                        closest_3 = similar_with_commute[:3]
                        min_price_needed = min(p.get('price', 0) for p in closest_3)
                        suggested_budget = int(min_price_needed * C.SUGGESTED_BUDGET_MARGIN)
                        budget_increase = suggested_budget - max_budget

                        similar_formatted = []
                        for i, prop in enumerate(closest_3, 1):
                            price = int(prop.get('price', 0))
                            over_budget = price - max_budget
                            over_percentage = round((over_budget / max_budget) * 100, 1) if max_budget else 0.0
                            images = prop.get('Images', prop.get('images', []))
                            if isinstance(images, str):
                                images = [images] if images else []
                            geo_location = prop.get('Geo_Location', prop.get('geo_location', ''))
                            row = {
                                'rank': i,
                                'address': prop.get('Address', prop.get('address', 'Unknown')),
                                'price': f"£{price}/month",
                                'budget_status': f"⚠️ Over budget by £{over_budget} ({over_percentage}%)",
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
                                'explanation': _clean_explanation(
                                    prop.get('Description', ''),
                                    prop.get('travel_time') if commute_annotation_enabled else None,
                                    commute_target,
                                ),
                            }
                            _tt = prop.get('travel_time')
                            if commute_annotation_enabled and _tt is not None:
                                row['travel_time'] = f"{int(_tt)} min to {commute_target}"
                            similar_formatted.append(row)

                        _cp = _commute_phrase(max_commute_time, commute_target) if commute_target else ""
                        return {
                            'success': True,
                            'status': 'no_exact_match_but_similar',
                            'message': (f"No properties were found within your budget of £{max_budget}/month"
                                        f"{_cp or f' near {search_area}'}."),
                            'suggestion': (f"However, I found {len(closest_3)} similar properties. "
                                           f"The closest match is £{int(closest_3[0].get('price', 0))}/month. "
                                           f"Would you consider increasing your budget by approximately "
                                           f"£{budget_increase} (to £{suggested_budget}/month)?"),
                            'similar_properties': similar_formatted,
                            'suggested_budget': suggested_budget,
                            'budget_increase_needed': budget_increase,
                            'search_criteria': _criteria(),
                            'known_criteria': _known_criteria(),
                            'recommendations': similar_formatted,
                        }

            # 无预算，或相似回退也没结果 —— 诚实的 no_results（语言感知，提示表单）。
            return {
                'success': True,
                'status': 'no_results',
                'message': _no_results_message(search_area, is_cjk),
                'recommendations': [],
                'data_source': data_source,
                'search_criteria': _criteria(),
                'known_criteria': _known_criteria(),
            }

        # ================================================================
        # 步骤 6: 格式化结果
        # ================================================================
        perfect_limited = perfect_match[:limit]
        soft_limited = soft_violation[:3]
        all_results = perfect_limited + soft_limited

        formatted_results = []
        for i, prop in enumerate(all_results[:limit], 1):
            images = prop.get('Images', prop.get('images', []))
            if isinstance(images, str):
                images = [images] if images else []
            geo_location = prop.get('Geo_Location', prop.get('geo_location', ''))
            if not geo_location:
                address = prop.get('Address', prop.get('address', ''))
                if 'London' in address:
                    geo_location = '51.5074,-0.1278'  # London center fallback

            row = {
                'rank': i,
                'address': prop.get('Address', prop.get('address', 'Unknown')),
                'price': f"£{int(prop.get('price', 0))}/month",
                'budget_status': prop.get('budget_status', ''),
                'score': prop.get('recommendation_score', 0),
                'property_type': prop.get('Type', prop.get('type', 'Flat')),
                'bedrooms': prop.get('Bedrooms', prop.get('bedrooms', 'N/A')),
                'match_type': prop.get('match_type', 'perfect'),
                'source': data_source,
                'possibly_outdated': bool(prop.get('possibly_outdated', False)),
                'url': prop.get('URL', prop.get('url', '')),
                'images': images,
                'geo_location': geo_location,
                'explanation': _clean_explanation(
                    prop.get('Description', prop.get('description', '')),
                    prop.get('travel_time') if commute_annotation_enabled else None,
                    commute_target,
                ),
            }
            # 无通勤目标/无通勤时间时，完全省略 travel_time 字段（不出现 "0 min to None"）。
            _tt = prop.get('travel_time')
            if commute_annotation_enabled and _tt is not None:
                row['travel_time'] = f"{int(_tt)} min to {commute_target}"
            formatted_results.append(row)

        _summary = _found_summary(all_results, len(perfect_match), len(soft_violation),
                                  max_budget if has_budget else None,
                                  max_commute_time if commute_filter_enabled else None,
                                  search_area, commute_target, room_type, is_cjk)
        if possibly_outdated:
            _summary += (" （部分为近期缓存房源，实时刷新暂不可用，可能已过期。）" if is_cjk
                         else " (Showing recent cached listings — a live refresh wasn't "
                              "available just now, so some may be outdated.)")

        return {
            'success': True,
            'status': 'found',
            'total_found': len(all_results),
            'data_source': data_source,
            'possibly_outdated': possibly_outdated,
            'search_criteria': _criteria(),
            # Populated snapshot of the ACTUAL resolved criteria so the frontend can
            # mirror state after a search (previously absent on the search path, so a
            # consumer reading known_criteria on a found turn got {}).
            'known_criteria': _known_criteria(),
            'recommendations': formatted_results,
            'perfect_count': len(perfect_match),
            'soft_count': len(soft_violation),
            'summary': _summary,
        }

    except Exception as e:
        print(f"   ❌ 搜索房源出错: {e}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'status': 'error',
            'error': str(e)
        }


# 创建工具实例
search_properties_tool = Tool(
    name="search_properties",

    description="""Search for SPECIFIC rental properties in the UK database.

⚠️ USE THIS TOOL ONLY WHEN:
- User explicitly wants to FIND/SEARCH for a specific property they can rent
- User provides search criteria like budget, location, commute time
- User says things like "帮我找房", "I want to find a flat", "search for apartments", "找房子"

❌ DO NOT USE THIS TOOL FOR:
- General questions about rent prices or averages ("租房价格多少")
- Questions about living costs, transport costs, food costs
- Questions about areas, neighborhoods, or safety
- "租房价格怎么样" = asking about rent prices → use web_search
- "介绍租房信息" = asking about renting info → use web_search

WHAT IS REQUIRED:
- ONLY an area to live in (or a commute destination it can be derived from). Budget
  and commute time are OPTIONAL — the tool degrades gracefully and NEVER loops asking
  for them. Missing budget → no budget filter; missing/no commute → no commute filter.
- "area" = where the user wants to LIVE. "commute_destination" = where they commute to.
  These are DISTINCT. If the user says they don't commute (e.g. "我不通勤我单纯住着",
  "work from home"), set no_commute=true.

WORKFLOW:
1. Call this tool with whatever criteria exist (at minimum an area or commute destination).
2. Only if NO area can be determined does the tool return a clarification question.
3. Otherwise it returns property recommendations.

For GENERAL INFORMATION questions about rent, use web_search instead.""",

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
                'description': 'Set true when the user explicitly does NOT commute / just lives there / works from home (e.g. "我不通勤我单纯住着", "wfh"). Disables all commute computation, filtering and questions.',
                'default': False
            },
            'bedrooms': {
                'type': 'integer',
                'description': 'Explicit bedroom count (0 = studio). Optional; when omitted the tool infers from the message or defaults to a broad range.'
            },
            'max_budget': {
                'type': 'integer',
                'description': 'OPTIONAL maximum monthly budget in GBP (e.g., 1500, 2000). Omit for no budget filter — never blocks the search.'
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
                               "'shared' (omit for any). Studio also implies a 0-bedroom search."
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
            }
        },
        'required': []  # 没有必须参数 - 工具内部会处理
    },

    max_retries=2
)
