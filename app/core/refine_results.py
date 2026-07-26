"""Deterministic in-place refinement of the listing set already on screen.

WHY
---
A follow-up like *"drop anything over £2000, then sort the rest by distance to the
tube"* is a **narrowing** of the set the previous turn already produced: a filter and
a re-sort over records we still hold in conversation state. Both of the routes such a
message could previously take were wrong:

  • routed to ``search_properties`` it pays for a live scrape + embedding + FAISS +
    commute pass, and hands the results panel a DIFFERENT set of listings than the
    prose is talking about;
  • routed to a chat / listing-advice answer it emits no ``recommendations`` at all,
    so ``/api/alex`` returns a ``chat`` payload and the panel silently keeps rendering
    the pre-refinement set — the prose says "only one qualifies", the panel still
    shows six.

WHAT
----
:func:`plan_refinement` is the single entry point. It returns ``(spec, refined)`` when —
and only when — the message is a pure narrowing the cached set can serve, and ``None``
otherwise. Everything that is NOT a narrowing falls through to normal routing so a
genuine search still runs:

  • a widening budget ("up to £3000" when nothing on screen costs that much), an
    explicit raise ("bump the budget"), or a budget clear ("any price");
  • a new / changed area (an area that is not already represented on screen);
  • an explicit new-search verb ("find me", "show me more", "搜索房源");
  • a sort key the cached records cannot support **on its own** (there is nothing to
    recompute without new data);
  • a filter that would empty the panel — the cached top-N simply cannot answer it, and
    a fresh search at the tighter constraint genuinely can.

Pure module: no I/O, no LLM, no graph / Flask imports. Everything here is a function of
the message text plus the previous recommendation records.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════
# Record field readers — recommendation records as produced by
# search_properties._format_rows (price "£1800/month", travel_time
# "16 min to UCL", bedrooms int|"N/A", …). Every reader is total: an
# unreadable field yields None and the record is treated as "unknown"
# by the filters (never silently dropped, never silently kept).
# ═══════════════════════════════════════════════════════════════════

_NUM_RE = re.compile(r'(\d[\d,]*)')
_MINUTES_RE = re.compile(r'(\d{1,3})\s*min')


def record_price(rec: Any) -> Optional[int]:
    """Monthly price of a recommendation record ("£1,800/month" -> 1800)."""
    if not isinstance(rec, dict):
        return None
    raw = rec.get('price')
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    m = _NUM_RE.search(str(raw or ''))
    if not m:
        return None
    try:
        return int(m.group(1).replace(',', ''))
    except ValueError:
        return None


def record_commute_minutes(rec: Any) -> Optional[int]:
    """Commute minutes from a record's ``travel_time`` ("16 min to UCL" -> 16)."""
    if not isinstance(rec, dict):
        return None
    raw = rec.get('travel_time')
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    m = _MINUTES_RE.search(str(raw or '').lower())
    return int(m.group(1)) if m else None


def record_bedrooms(rec: Any) -> Optional[int]:
    """Bedroom count; a studio reads as 0. Unknown / "N/A" -> None."""
    if not isinstance(rec, dict):
        return None
    raw = rec.get('bedrooms')
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw or '').strip().lower()
    if not s or s == 'n/a':
        # A studio listing often carries no bedroom count at all.
        return 0 if 'studio' in str(rec.get('property_type') or '').lower() else None
    if 'studio' in s:
        return 0
    m = re.search(r'\d+', s)
    return int(m.group(0)) if m else None


def record_score(rec: Any) -> float:
    if not isinstance(rec, dict):
        return 0.0
    try:
        return float(rec.get('score') or 0)
    except (TypeError, ValueError):
        return 0.0


def _record_text(rec: Any) -> str:
    """Everything about a record that a room-type test may legitimately read."""
    if not isinstance(rec, dict):
        return ''
    return ' '.join(str(rec.get(k) or '') for k in (
        'property_type', 'address', 'name', 'description', 'explanation')).lower()


def _record_areas(rec: Any) -> str:
    if not isinstance(rec, dict):
        return ''
    return f"{rec.get('area') or ''} {rec.get('address') or ''}".lower()


# ═══════════════════════════════════════════════════════════════════
# Message parsing
# ═══════════════════════════════════════════════════════════════════

# An explicit "go and find something else" — never a refinement of what is on screen.
_NEW_SEARCH_RE = re.compile(
    r'(?:find\s+me|find\s+another|search\s+for|look\s+for|show\s+me\s+(?:more|other)|'
    r'more\s+options|other\s+(?:areas?|cities|towns|options?|places?|listings?|properties)|'
    r'another\s+area|different\s+area|somewhere\s+else|'
    r'new\s+search|search\s+again|re-?search|start\s+over|'
    r'找房|搜房|搜索房源|重新搜|再搜一|换个(?:区|地方|城市)|其他区域|别的区域|更多(?:房源|选择))',
    re.IGNORECASE)

# An explicit LOOSENING of the price constraint — needs new data by definition.
_WIDEN_RE = re.compile(
    r'(?:raise|increase|bump|up)\s+(?:the\s+|my\s+)?budget|'
    r'(?:higher|bigger|larger)\s+budget|'
    r'more\s+expensive\s+(?:ones?|options?|places?)|'
    r'提高预算|预算(?:提高|涨到|加到|放宽)|放宽预算',
    re.IGNORECASE)

# ── price ──────────────────────────────────────────────────────────
_AMOUNT = r'£?\s?(\d[\d,]{2,6})'
_DROP_VERB = (r'(?:drop|remove|exclude|filter\s+out|get\s+rid\s+of|ditch|lose|cut|'
              r'去掉|删掉|排除|砍掉|剔除|不要)')
_ABOVE = r'(?:over|above|more\s+than|greater\s+than|exceeding|beyond|超过|高于|大于)'
_BELOW_EN = (r'(?:under|below|less\s+than|cheaper\s+than|no\s+more\s+than|not\s+more\s+than|'
             r'at\s+most|within|up\s+to|max(?:imum)?(?:\s+of)?)')

# "drop anything over £2000" / "去掉超过2000的"
_DROP_ABOVE_RE = re.compile(
    _DROP_VERB + r'[^.。;；\n]{0,32}?' + _ABOVE + r'\s*' + _AMOUNT, re.IGNORECASE)
# "超过2000的都去掉" (Chinese puts the verb last)
_ZH_DROP_ABOVE_RE = re.compile(
    r'(?:超过|高于|大于)\s*(\d[\d,]{2,6})[^\n]{0,12}?(?:去掉|删掉|排除|不要|砍掉|剔除)')
# "drop anything under £1200" — a floor is a narrowing too.
_DROP_BELOW_RE = re.compile(
    _DROP_VERB + r'[^.。;；\n]{0,32}?' + _BELOW_EN + r'\s*' + _AMOUNT, re.IGNORECASE)
_ZH_DROP_BELOW_RE = re.compile(
    r'(?:低于|少于|不到)\s*(\d[\d,]{2,6})[^\n]{0,12}?(?:去掉|删掉|排除|不要|砍掉|剔除)')
# "under £2000" / "no more than 2000"
_KEEP_BELOW_RE = re.compile(_BELOW_EN + r'\s*' + _AMOUNT, re.IGNORECASE)
_ZH_KEEP_BELOW_RE = re.compile(
    r'(?:不超过|不高于|低于|最多)\s*£?\s?(\d[\d,]{2,6})|'
    r'(\d[\d,]{2,6})\s*(?:镑|元|块|英镑|磅)?\s*(?:以下|以内|之下)')
# "budget down to 2000" / "预算降到2000"
_BUDGET_DOWN_RE = re.compile(
    r'(?:budget|预算)[^\d\n]{0,12}(?:down\s+to|降到|降至|减到|改成)\s*£?\s?(\d[\d,]{2,6})',
    re.IGNORECASE)
# "only the ones over £1500" — a floor with no drop verb.
_KEEP_ABOVE_RE = re.compile(
    r'(?:over|above|more\s+than|at\s+least|超过|高于|至少)\s*' + _AMOUNT, re.IGNORECASE)

_PRICE_MIN, _PRICE_MAX = 200, 20000


def _amount(match) -> Optional[int]:
    """First non-empty numeric group of a match, sanity-bounded."""
    if not match:
        return None
    for g in match.groups():
        if not g:
            continue
        try:
            val = int(str(g).replace(',', ''))
        except ValueError:
            continue
        if _PRICE_MIN <= val <= _PRICE_MAX:
            return val
    return None


def _parse_price_bound(text: str) -> Tuple[Optional[int], Optional[int]]:
    """(max_price, min_price) implied by the message, or (None, None).

    Ordered ladder: an explicit drop-verb form is read FIRST so "drop anything over
    £2000" is a ceiling, not the floor its bare "over £2000" substring would suggest.
    """
    cap = _amount(_DROP_ABOVE_RE.search(text)) or _amount(_ZH_DROP_ABOVE_RE.search(text))
    if cap is not None:
        return cap, None
    floor = _amount(_DROP_BELOW_RE.search(text)) or _amount(_ZH_DROP_BELOW_RE.search(text))
    if floor is not None:
        return None, floor
    cap = (_amount(_KEEP_BELOW_RE.search(text))
           or _amount(_ZH_KEEP_BELOW_RE.search(text))
           or _amount(_BUDGET_DOWN_RE.search(text)))
    if cap is not None:
        return cap, None
    floor = _amount(_KEEP_ABOVE_RE.search(text))
    return (None, floor) if floor is not None else (None, None)


# ── room type ──────────────────────────────────────────────────────
# Phrasings mirror search_properties._ROOM_TYPE_SYNONYMS (single source of truth for
# the vocabulary); they are re-declared here because a refinement needs the match
# POSITION to tell "only the ensuite ones" (keep) from "drop the shared ones" (exclude),
# which the tool's polarity-free extractor cannot express.
_ROOM_TYPE_NEEDLES = (
    ("studio", ("studio", "单间公寓", "一室户", "开间")),
    ("ensuite", ("ensuite", "en-suite", "en suite", "独立卫浴", "独卫", "套间", "带独卫")),
    ("shared", ("shared room", "flatshare", "flat share", "house share", "houseshare",
                "shared", "合租房", "合租", "共享房间")),
)
_EXCLUDE_CUE_RE = re.compile(
    _DROP_VERB + r'|\bno\b|\bnot\b|\bwithout\b|\bnon-?\b|\bavoid\b|别|不想|无|避开',
    re.IGNORECASE)
# Deliberately short: a longer window lets an unrelated earlier clause supply the cue
# ("no more than £2000 and keep the ensuite ones" must NOT read as "exclude ensuite").
_EXCLUDE_CUE_WINDOW = 20


def _parse_room_type(text: str) -> Tuple[Optional[str], Optional[str]]:
    """(keep_room_type, exclude_room_type). Polarity comes from an exclusion cue in the
    characters immediately preceding the room-type word ("drop the shared ones")."""
    low = text.lower()
    for canonical, needles in _ROOM_TYPE_NEEDLES:
        for needle in needles:
            pos = low.find(needle)
            if pos < 0:
                continue
            prefix = low[max(0, pos - _EXCLUDE_CUE_WINDOW):pos]
            if _EXCLUDE_CUE_RE.search(prefix):
                return None, canonical
            return canonical, None
    return None, None


def record_matches_room_type(rec: Any, room_type: str) -> bool:
    """Whether a recommendation record satisfies a room type. Mirrors the tool's
    ``_matches_room_type`` but reads the record's own fields (the scraped column names
    it inspects do not survive into a recommendation row)."""
    if not room_type:
        return True
    blob = _record_text(rec)
    if room_type == 'studio':
        return 'studio' in blob
    if room_type == 'ensuite':
        return any(n in blob for n in ('en-suite', 'ensuite', 'en suite'))
    if room_type == 'shared':
        return any(n in blob for n in ('shar', 'flatshare', 'flat share', 'house share'))
    return True


# ── area subset ────────────────────────────────────────────────────

def _parse_area_subset(text: str, previous: List[dict]) -> Tuple[Optional[str], Optional[str]]:
    """(keep_area, exclude_area) chosen from the areas the SHOWN set already covers.

    Only the set's own ``area`` labels are candidates, so this can never be read as a
    change of area — the worst case is a no-op filter, which :func:`plan_refinement`
    then rejects. Overlapping labels collapse to the longest ("south kensington" beats a
    stray "kensington"); genuinely DIFFERENT labels in one message ("keep Bloomsbury and
    Camden") are declined rather than half-applied.
    """
    low = text.lower()
    labels = {str(r.get('area')).strip().lower()
              for r in previous if isinstance(r, dict) and r.get('area')}
    hits = [lab for lab in labels
            if lab and re.search(r'\b' + re.escape(lab) + r'\b', low)]
    if not hits:
        return None, None
    label = max(hits, key=len)
    if any(h not in label for h in hits):
        return None, None
    pos = low.find(label)
    prefix = low[max(0, pos - _EXCLUDE_CUE_WINDOW):pos]
    if _EXCLUDE_CUE_RE.search(prefix):
        return None, label
    return label, None


# ── bedrooms ───────────────────────────────────────────────────────
_MIN_BEDS_RE = re.compile(
    r'(?:at\s+least|minimum(?:\s+of)?|min\.?|>=)\s*(\d)\s*(?:\+\s*)?(?:bed|bedroom)|'
    r'(\d)\s*\+\s*(?:bed|bedroom)|'
    r'(?:至少|不少于)\s*(\d)\s*(?:室|卧|房)', re.IGNORECASE)
_EXACT_BEDS_RE = re.compile(
    r'(?:only|just|keep)\s+(?:the\s+)?(\d)\s*[- ]?(?:bed|bedroom)|'
    r'(?:只(?:要|留|保留))\s*(\d)\s*(?:室|卧|房)', re.IGNORECASE)


# ── sort ───────────────────────────────────────────────────────────
_SORT_TRIGGER_RE = re.compile(
    r'\b(?:sort|order|re-?order|rank|arrange)\b'
    r'(?:\s+(?:the|these|those|all|remaining)\s+)*'
    r'(?:\s*\b(?:them|these|those|it|rest|others?|remainder|results?|listings?|'
    r'properties|options?|ones|places?|flats?)\b)?'
    r'\s*(?:by|on|according\s+to)\s+(?P<key>[^.,;!?\n]{1,48})', re.IGNORECASE)
_ZH_SORT_RE = re.compile(r'(?:按|依|以)\s*(?P<key>[^，。；!?\n]{1,16}?)\s*(?:来)?(?:排序|排列|排一下|排序一下)')
_FIRST_SORT_RE = re.compile(
    r'\b(?P<key>cheapest|most\s+expensive|priciest|closest|nearest|quickest|fastest|'
    r'biggest|largest|smallest|shortest\s+commute)\b[^.,;!?\n]{0,16}\bfirst\b',
    re.IGNORECASE)

# (pattern over the captured sort phrase) -> (record key, default descending?)
_SORT_KEYS = (
    (re.compile(r'most\s+expensive|priciest|dearest|从贵|价格从高', re.IGNORECASE), 'price', True),
    (re.compile(r'cheap|price|cost|rent\b|价格|价钱|租金|便宜', re.IGNORECASE), 'price', False),
    (re.compile(r'commute|travel\s*time|journey\s*time|quickest|fastest|'
                r'shortest\s+commute|通勤|车程', re.IGNORECASE), 'commute', False),
    (re.compile(r'bedroom|beds?\b|biggest|largest|smallest|卧室|房间数|大小', re.IGNORECASE),
     'bedrooms', True),
    (re.compile(r'match|relevance|score|recommend|匹配|推荐度', re.IGNORECASE), 'score', True),
)
_DESC_CUE_RE = re.compile(
    r'desc(?:ending)?|high(?:est)?\s+(?:to\s+low|first)|从高到低|从大到小|从贵到便宜',
    re.IGNORECASE)
_ASC_CUE_RE = re.compile(
    r'asc(?:ending)?|low(?:est)?\s+(?:to\s+high|first)|从低到高|从小到大|从便宜到贵',
    re.IGNORECASE)
# Sort keys that read naturally but are NOT derivable from a cached listing record.
_SMALLEST_CUE_RE = re.compile(r'smallest|从小|最小', re.IGNORECASE)


def _parse_sort(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """((key, desc) spec, unsupported_phrase). At most one of the two is non-None."""
    m = _SORT_TRIGGER_RE.search(text) or _ZH_SORT_RE.search(text) or _FIRST_SORT_RE.search(text)
    if not m:
        return None, None
    phrase = (m.group('key') or '').strip()
    if not phrase:
        return None, None
    for pattern, key, default_desc in _SORT_KEYS:
        if pattern.search(phrase):
            desc = default_desc
            if _DESC_CUE_RE.search(text):
                desc = True
            elif _ASC_CUE_RE.search(text):
                desc = False
            elif key == 'bedrooms' and _SMALLEST_CUE_RE.search(phrase):
                desc = False
            return {'key': key, 'desc': desc, 'phrase': phrase}, None
    # A real sort request over a dimension the cached records do not carry
    # (e.g. "distance to the tube"). Reported, never silently faked.
    return None, phrase


# ── top-N ──────────────────────────────────────────────────────────
_WORD_N = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
           '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6}
_TOP_N_RE = re.compile(
    r'\b(?:top|first)\s+(\d{1,2}|two|three|four|five|six)\b(?!\s*(?:bed|bedroom|min|'
    r'month|week|pound))', re.IGNORECASE)
_ZH_TOP_N_RE = re.compile(r'前\s*(\d{1,2}|[二两三四五六])\s*(?:个|套|条|间)?')


def _parse_limit(text: str) -> Optional[int]:
    for pattern in (_TOP_N_RE, _ZH_TOP_N_RE):
        m = pattern.search(text)
        if not m:
            continue
        token = m.group(1).lower()
        n = _WORD_N.get(token)
        if n is None:
            try:
                n = int(token)
            except ValueError:
                continue
        if 1 <= n <= 50:
            return n
    return None


# ═══════════════════════════════════════════════════════════════════
# Spec building + application
# ═══════════════════════════════════════════════════════════════════

def parse_refinement(message: str, previous: List[dict]) -> Optional[Dict[str, Any]]:
    """Parse ``message`` into a refinement spec, or None when it is not a pure narrowing.

    ``previous`` is only read to decide whether a named area is already represented on
    screen (an area the set does not contain means the user changed area, which needs a
    real search). Applying the spec is :func:`apply_refinement`'s job.
    """
    if not message or not previous:
        return None
    text = str(message).strip()
    if not text:
        return None

    # Hard bail-outs: this is a new/wider search, not a narrowing of what is shown.
    if _NEW_SEARCH_RE.search(text) or _WIDEN_RE.search(text):
        return None
    from core.tools.search_properties import _extract_area, _extract_budget_clear
    if _extract_budget_clear(text):
        return None

    filters: List[Dict[str, Any]] = []

    # A named area is a SUBSET filter when the shown set already covers it, and a
    # change-of-area (fresh search) when it does not.
    named_area = _extract_area(text)
    if named_area:
        if not any(named_area.lower() in _record_areas(r) for r in previous):
            return None
        filters.append({'kind': 'area', 'value': named_area})
    else:
        # _extract_area only fires on a switch/location cue ("just the ones IN Camden");
        # a bare subset phrasing ("only the Camden ones") is caught by matching the shown
        # set's OWN area labels, which by construction cannot be a change of area.
        subset_area, exclude_area = _parse_area_subset(text, previous)
        if subset_area:
            filters.append({'kind': 'area', 'value': subset_area})
        elif exclude_area:
            filters.append({'kind': 'not_area', 'value': exclude_area})

    max_price, min_price = _parse_price_bound(text)
    if max_price is not None:
        filters.append({'kind': 'max_price', 'value': max_price})
    if min_price is not None:
        filters.append({'kind': 'min_price', 'value': min_price})

    keep_rt, drop_rt = _parse_room_type(text)
    if keep_rt:
        filters.append({'kind': 'room_type', 'value': keep_rt})
    if drop_rt:
        filters.append({'kind': 'not_room_type', 'value': drop_rt})

    m = _MIN_BEDS_RE.search(text)
    if m:
        beds = next((int(g) for g in m.groups() if g), None)
        if beds is not None:
            filters.append({'kind': 'min_bedrooms', 'value': beds})
    m = _EXACT_BEDS_RE.search(text)
    if m:
        beds = next((int(g) for g in m.groups() if g), None)
        if beds is not None:
            filters.append({'kind': 'bedrooms', 'value': beds})

    sort, unsupported_sort = _parse_sort(text)
    limit = _parse_limit(text)
    if limit is not None and limit >= len(previous):
        limit = None  # "top 10" over six listings changes nothing

    if not filters and sort is None and limit is None:
        # Nothing actionable — including the case where the ONLY ask was a sort key the
        # cached records cannot support. Let normal routing handle it.
        return None

    return {
        'filters': filters,
        'sort': sort,
        'limit': limit,
        'unsupported_sort': unsupported_sort,
    }


def _passes(rec: Any, flt: Dict[str, Any]) -> bool:
    """One filter against one record. A record whose value is UNKNOWN is kept: dropping a
    listing because our parser could not read its price would silently lie to the user."""
    kind, value = flt.get('kind'), flt.get('value')
    if kind == 'max_price':
        price = record_price(rec)
        return price is None or price <= value
    if kind == 'min_price':
        price = record_price(rec)
        return price is None or price >= value
    if kind == 'room_type':
        return record_matches_room_type(rec, value)
    if kind == 'not_room_type':
        return not record_matches_room_type(rec, value)
    if kind == 'min_bedrooms':
        beds = record_bedrooms(rec)
        return beds is None or beds >= value
    if kind == 'bedrooms':
        beds = record_bedrooms(rec)
        return beds is None or beds == value
    if kind == 'area':
        return str(value).lower() in _record_areas(rec)
    if kind == 'not_area':
        return str(value).lower() not in _record_areas(rec)
    return True


_SORT_READERS = {
    'price': record_price,
    'commute': record_commute_minutes,
    'bedrooms': record_bedrooms,
    'score': record_score,
}


def apply_refinement(previous: List[dict], spec: Dict[str, Any]) -> List[dict]:
    """Apply a spec to the previous records. Stable: records the spec says nothing about
    keep their existing relative order, and records with an unreadable sort key sink to
    the end rather than being reordered arbitrarily."""
    out = [r for r in (previous or []) if isinstance(r, dict)]
    for flt in spec.get('filters') or []:
        out = [r for r in out if _passes(r, flt)]

    sort = spec.get('sort')
    if sort:
        reader = _SORT_READERS.get(sort.get('key'))
        if reader is not None:
            desc = bool(sort.get('desc'))

            def _key(rec, _reader=reader, _desc=desc):
                val = _reader(rec)
                # Unknown always sorts last, in BOTH directions.
                return (1, 0) if val is None else (0, -float(val) if _desc else float(val))

            out = sorted(out, key=_key)

    limit = spec.get('limit')
    if limit:
        out = out[:limit]
    return out


def describe_refinement(spec: Dict[str, Any], previous_count: int, kept_count: int) -> str:
    """One-line, emoji-free English description of what the refinement did. Fed to the
    answer generator as evidence so the prose can only describe the operation actually
    performed (in particular it must not claim a sort that was not applied)."""
    parts: List[str] = []
    for flt in spec.get('filters') or []:
        kind, value = flt.get('kind'), flt.get('value')
        if kind == 'max_price':
            parts.append(f"kept only listings at or under £{value}/month")
        elif kind == 'min_price':
            parts.append(f"kept only listings at or above £{value}/month")
        elif kind == 'room_type':
            parts.append(f"kept only {value} listings")
        elif kind == 'not_room_type':
            parts.append(f"removed {value} listings")
        elif kind == 'min_bedrooms':
            parts.append(f"kept only listings with at least {value} bedroom(s)")
        elif kind == 'bedrooms':
            parts.append(f"kept only {value}-bedroom listings")
        elif kind == 'area':
            parts.append(f"kept only listings in {value}")
        elif kind == 'not_area':
            parts.append(f"removed listings in {value}")
    sort = spec.get('sort')
    if sort:
        direction = 'highest first' if sort.get('desc') else 'lowest first'
        parts.append(f"re-sorted by {sort.get('key')} ({direction})")
    if spec.get('limit'):
        parts.append(f"kept the top {spec['limit']}")
    body = "; ".join(parts) if parts else "re-ordered the existing listings"
    return (f"Refined the {previous_count} listing(s) already shown "
            f"({previous_count - kept_count} removed, {kept_count} remain): {body}.")


def plan_refinement(message: str, previous: List[dict]) -> Optional[Tuple[Dict[str, Any], List[dict]]]:
    """``(spec, refined_records)`` when ``message`` is a pure narrowing that the already
    shown listings can serve, else ``None``.

    Returns None (so the caller falls through to its normal routing, and a real search
    can still run) when:

      • the message is not a narrowing at all — see :func:`parse_refinement`;
      • the filters remove nothing AND there is no re-sort or top-N cut, i.e. the request
        is a no-op over this set and therefore was not really about it;
      • the filters remove EVERYTHING. The cached set is the previous search's top-N, not
        the whole market: "nothing here is under £2000" is not the same as "nothing is
        under £2000", and emptying the panel would be both unhelpful and unsound.
    """
    if not previous:
        return None
    spec = parse_refinement(message, previous)
    if spec is None:
        return None
    refined = apply_refinement(previous, spec)
    if not refined:
        return None
    if len(refined) == len(previous) and spec.get('sort') is None and not spec.get('limit'):
        return None
    return spec, refined
