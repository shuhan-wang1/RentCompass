"""Small, deterministic boundaries for explicit changes to housing requirements.

These helpers deliberately return no update for questions and hypothetical examples.
They do not infer personal attributes or silently broaden a hard requirement.
"""
from __future__ import annotations

import re


_CLAUSE_START = r'(?<![^。.!?;；\n])'
_WITHDRAWN_CLAUSE = re.compile(
    _CLAUSE_START + r'[^。.!?;；\n]*(?:\b(?:discard|disregard|ignore|withdraw)\b|作废|撤销|丢弃)'
    r'[^。.!?;；\n]*(?:[。.!?;；\n]|$)', re.I)
_KEPT_OUT_CLAUSE = re.compile(
    _CLAUSE_START + r'[^。.!?;；\n]*\bkeep\b[^。.!?;；\n]*\bout of (?:this|the) (?:decision|search)\b'
    r'[^。.!?;；\n]*(?:[。.!?;；\n]|$)', re.I)


# Someone else's place is not the user's criterion (stress A-08): "…, my friend lives in
# Peckham, …" switched the search area to Peckham. A clause whose SUBJECT is another person
# and that says nothing about the user ("I", "me", "we", "us") is a reference, like a withdrawn
# plan. The vocabulary is the memory layer's (rag.agent_memory._THIRD_PARTY) minus the people
# who act on the user's OWN terms: "My employer reduced office attendance to two days per week"
# and "my landlord wants £1,800" are about the user's commute and rent.
_THIRD_PARTY_NOUN = (
    r"partner|wife|husband|spouse|son|daughter|child|children|kids?|"
    r"friend|mother|mum|mom|father|dad|parents?|brother|sister|colleague|flatmate|roommate|"
    r"housemate|girlfriend|boyfriend|fianc[ée]e?")
_THIRD_PARTY_CLAUSE = re.compile(
    r"(?:^|(?<=[,，;；.。!?！？\n:：]))(\s*(?:(?:and|but|also|plus|while|whereas)\s+)?"
    r"(?:my|our|his|her|their|a)\s+(?:(?:best|close|old|older|younger|little|big)\s+)?"
    r"(?:" + _THIRD_PARTY_NOUN + r")(?:'s|’s|s)?\b)([^,，;；.。!?！？\n]*)"
    r"|(?:^|(?<=[,，;；。！？\n]))(\s*(?:我|我的|我们的?)?"
    r"(?:伴侣|老公|老婆|丈夫|妻子|孩子|儿子|女儿|朋友|父母|同事|室友))([^,，;；。！？\n]*)",
    re.I)
_FIRST_PERSON_WORD = re.compile(r"\b(?:i|i'm|i’m|i've|i'd|i'll|me|we|we're|us)\b|我(?!的?(?:朋友|父母|同事|室友))", re.I)


_ADOPTS_PLACE = re.compile(
    r"\b(?:there|near\s+(?:him|her|them)|close\s+to\s+(?:him|her|them)|(?:the\s+)?same\s+area|"
    r"that\s+area|nearby)\b|那里|那边|附近", re.I)


def without_third_party_clauses(text: str) -> str:
    """``text`` without clauses about another person ("my friend lives in Peckham").

    "My partner and I need a 2-bed" and "my sister and I are moving to Hackney" stay: the
    user is part of the subject."""
    value = str(text or '')

    def _keep_or_drop(match):
        rest = match.group(2) if match.group(1) is not None else match.group(4)
        return match.group(0) if _FIRST_PERSON_WORD.search(rest or '') else ''
    stripped = _THIRD_PARTY_CLAUSE.sub(_keep_or_drop, value)
    # "My friend lives in Peckham, find me a flat there": the user adopts that place.
    if stripped != value and _ADOPTS_PLACE.search(stripped):
        return value
    return stripped


# A conditional stretch is not a new ceiling (stress A-08): "my budget could stretch to £2,100
# for the right place" raised max_budget 1,900 -> 2,100. Narrower than the memory layer's
# _SOFT_BOUND on purpose: "Could you find flats under £2,000?" is a ceiling.
# Full-width digits were folded long ago; zh numerals never were: 两千五百镑, 二千镑, 两千五, 三千以内 and
# 一千八百 gave no budget, the search ran unfiltered and later replies still quoted the figure (W-06).
# A digit after 千 with no unit counts hundreds (两千五 is 2500), after 百 tens. 三千多, 几千 and 两三千
# are not figures anyone can search on, so they stay as they are.
_ZH_DIGITS = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_ZH_NUMERAL_BEFORE = r'(?<![一二两三四五六七八九十百千万零几数\d])'
_ZH_NUMERAL_AFTER = r'(?![一二两三四五六七八九十百千万零几多来余])'
_ZH_MONEY = re.compile(
    _ZH_NUMERAL_BEFORE + r'(?:(?P<wan>[一二两三])万(?P<wan_k>[一二两三四五六七八九])?'
    r'|(?P<k>[一二两三四五六七八九])千(?:零(?P<k_ten>[一二三四五六七八九])十'
    r'|(?P<k_h>[一二两三四五六七八九])(?:百(?:(?P<k_t>[一二三四五六七八九])十?)?)?)?'
    r'|(?P<h>[二两三四五六七八九])百(?:(?P<h_t>[一二三四五六七八九])十?)?)' + _ZH_NUMERAL_AFTER)
_DIGIT_THOUSANDS = re.compile(r'(?<![\d.])(\d)千(?:(\d)百?)?(?![\d多来余])')


def _zh_money_value(match) -> str:
    def digit(name):
        return _ZH_DIGITS.get(match.group(name) or '零', 0)
    return str(digit('wan') * 10000 + digit('wan_k') * 1000 + digit('k') * 1000 + digit('k_h') * 100
               + digit('k_ten') * 10 + digit('k_t') * 10 + digit('h') * 100 + digit('h_t') * 10)


def zh_money_digits(text: str) -> str:
    """``text`` with zh numerals of 200 and over written in digits, for the budget parsers."""
    value = str(text or '')
    if not re.search(r'[百千万]', value):
        return value
    value = _DIGIT_THOUSANDS.sub(lambda m: str(int(m.group(1)) * 1000 + int(m.group(2) or 0) * 100), value)
    return _ZH_MONEY.sub(_zh_money_value, value)


_STRETCH_CUE = re.compile(
    r"\b(?:stretch\w*|at\s+a\s+push|for\s+the\s+right\s+(?:place|flat|home|property|one)|"
    r"wiggle\s+room|leeway|willing\s+to\s+go|would\s+consider\s+(?:going|paying)|"
    # "I can pay up to £2,300" states a ceiling; "could / might go up to" is the stretch.
    r"(?:could|might)\s+(?:go|push|pay)\s+(?:up\s+to|as\s+high\s+as)|can\s+push\s+(?:up\s+)?to|"
    r"if\s+(?:needed|necessary|need\s+be|i\s+(?:have|had)\s+to|it'?s\s+worth\s+it|pushed))\b|"
    r"上浮|放宽到|实在不行|必要时|合适的话|勉强", re.I)
_STRETCH_CLAUSE_SPLIT = re.compile(
    r"(?<!\d)[,，]|[,，](?!\d)|[;；。!?！？\n]|\.(?!\d)|\bbut\b|\s+and\s+(?=(?:it\s+|i\s+|that\s+)?(?:could|might|can|would)\b)",
    re.I)
_MONEY_FIGURE = re.compile(r"£\s?\d|\d\s?(?:k\b|pcm|p/m|per\s+month|a\s+month|pw|per\s+week|镑|英镑)|\d{3,}", re.I)


# "Actually I can stretch to £480pw." moves the ceiling: a plain "can", the message's only figure and
# no condition, so there is nothing for it to stretch beside and no case for keeping the old one.
_FIRM_STRETCH = re.compile(r"\b(?:i|we)\s+can\s+(?:now\s+)?stretch\s+(?:up\s+)?to\s+£?\s?\d", re.I)


def _stretch_clauses(text: str) -> list[str]:
    value = str(text or '')[:4000]
    if (_FIRM_STRETCH.search(value) and len(_STRETCH_CUE.findall(value)) == 1
            and len(re.findall(r"£\s?\d|(?<![\d,.])\d{3,}", value)) == 1):
        return []
    return [part.strip() for part in _STRETCH_CLAUSE_SPLIT.split(value)
            if part and _STRETCH_CUE.search(part) and _MONEY_FIGURE.search(part)]


def without_conditional_stretch(text: str) -> str:
    """``text`` without the clauses that state a conditional stretch of a figure."""
    value = str(text or '')
    for clause in _stretch_clauses(value):
        value = value.replace(clause, ' ')
    return value


def conditional_stretch_notes(text: str) -> list[str]:
    """The user's own stretch clauses, kept as soft notes beside the ceiling."""
    notes = []
    for clause in _stretch_clauses(without_third_party_clauses(text)):
        note = re.sub(r"^(?:and|but|also|so)\s+", '', clause, flags=re.I).strip(' .')
        if note and note not in notes:
            notes.append(note[:160])
    return notes


# A place the user only mentions does not replace a commute destination already set:
# "…, and I study part-time at Birkbeck." (a "nothing to search" background turn) replaced
# the Canary Wharf workplace with Malet Street (stress A-08). A destination change says so.
# "Use my saved budget and commute to find a 1-bed flat in Angel" stored the rest of the sentence as
# the destination, and every commute lookup failed to geocode it (G-08). A place does not open with
# a verb or name a home.
_NOT_A_DESTINATION = re.compile(
    r"^\W*(?i:find(?:ing)?|search(?:ing)?|look(?:ing)?|get|see|show|use|using|rent|renting|live|living|buy)\b"
    r"|\b(?:flats?|apartments?|studios?|bedrooms?|rooms?|houses?|homes?)\b|\b\d\s*-?\s*bed\b")


_AREA_NAMED_CHANGE = re.compile(
    r"\b(?:change|switch|move|set|update)\b[^.!?;]{0,24}\b(?:area|location|search)\b[^.!?;]{0,12}\bto\b"
    r"|(?:区域|地区|位置)(?:改成|改为|换成|换到)", re.I)
_SWITCH_ONLY = re.compile(r"^\W*(?:(?:ok(?:ay)?|actually|no|sorry)\W+)*(?:switch|change|move|go)\s+(?:it\s+)?(?:back\s+)?to\b", re.I)


def moves_area_only(text: str, no_commute: bool = False) -> bool:
    """True when the message moves where to live and says nothing of work. "Switch to Canary Wharf"
    reads so only for someone who has said they do not commute."""
    return bool(_AREA_NAMED_CHANGE.search(text or '') or (no_commute and _SWITCH_ONLY.search(text or '')))


def is_destination_text(text) -> bool:
    """False for a clause or a request stored in the place of a destination."""
    return not _NOT_A_DESTINATION.search(str(text or ''))


_DESTINATION_CHANGE_CUE = re.compile(
    r"\b(?:commut\w*|work\w*|office|workplace|job|destination|travel\w*\s+to|get(?:ting)?\s+to|"
    r"near|nearby|close\s+to|around|walking\s+distance|instead|now|actually|chang\w*|switch\w*|"
    r"new|mov\w*)\b|通勤|上班|工作|附近|改为|改成|换成|现在", re.I)


_SEARCH_REQUEST = re.compile(r"\b(?:find|search|look\s+for|show\s+me)\b|找房|帮我找|搜索", re.I)
_NO_SEARCH = re.compile(
    r"\bnothing\s+to\s+search\b|\b(?:don.t|do\s+not|no\s+need\s+to|not)\s+(?:\w+\s+)?search|"
    r"\bno\s+action\b|\bfor\s+context\s+only\b|不要搜索|先不(?:用)?搜|不用搜", re.I)


def replaces_destination(text: str, name: str) -> bool:
    """True when the clause naming ``name`` presents it as the (new) commute destination."""
    value = str(text or '')
    key = re.sub(r"[.,;:!?]+$", '', str(name or '').strip())
    found = re.search(re.escape(key), value, re.I) if key else None
    clause = clause_at(value, found.start(), found.end()) if found else value
    if _DESTINATION_CHANGE_CUE.search(clause):
        return True
    # "I study at UCL, find me a studio nearby": a search asked for in the same message is
    # built around the place; "…, still nothing to search: …" is not.
    return bool(_SEARCH_REQUEST.search(value) and not _NO_SEARCH.search(value))


_WITHDRAW_VERB = re.compile(r'\b(?:discard|disregard|ignore|withdraw)\b|作废|撤销|丢弃', re.I)
# A comma between thousands ("£3,500") does not end a part.
_PART_BOUNDARY = re.compile(r',(?!\d{3}(?!\d))|，|\s+[-–—]\s+|\s+but\s+', re.I)
# "Ignore Stratford, use Poplar" states its replacement in words (G-02).
_REPLACEMENT_CUE = re.compile(r"^\s*(?:and\s+|then\s+|please\s+|just\s+)*(?:use|search|try|look\s+in|go\s+with)\b", re.I)


def _replacement_after_withdrawal(match) -> str:
    """What a withdrawn clause still states after the withdrawal: a new money figure or a
    replacement in words.

    "Actually ignore my saved rent maximum, I can pay up to £3,500 this time" and "Ignore the
    budget I mentioned before, now it's £3,500 max" dropped the whole sentence, so the search
    ran with no ceiling at all while the reply said "within your £3,500 limit" (R3-04). A
    retraction followed by a new value in the same sentence is a replacement; the withdrawn
    part itself, and anything else after it, stays a reference."""
    clause = match.group(0)
    parts = _PART_BOUNDARY.split(clause)
    first = next((i for i, part in enumerate(parts) if _WITHDRAW_VERB.search(part)), None)
    if first is None:
        return ''
    kept = [re.sub(r'^use\b(?=\s+[^£\d])', 'search', part.strip(), flags=re.I) for part in parts[first + 1:]
            if (_MONEY_FIGURE.search(part) or _REPLACEMENT_CUE.search(part))
            and not _WITHDRAW_VERB.search(part)]
    if not kept:
        return ''
    end = clause.rstrip()[-1:] if clause.rstrip()[-1:] in '。.!?;；' else ''
    return ', '.join(kept) + (end or '.') + ' '


# Full-width digits, Latin letters and the full-width pound sign ("１-bed", "￡２０００") read as
# nothing to every parser, so an English message silently lost its bedrooms and budget
# (R3-17). Only those forms are folded; CJK punctuation stays as the zh patterns expect it.
_FULLWIDTH_ASCII = {code: code - 0xFEE0 for code in (*range(0xFF10, 0xFF1A), *range(0xFF21, 0xFF3B),
                                                   *range(0xFF41, 0xFF5B))}
_FULLWIDTH_ASCII.update({0xFFE1: ord('£'), 0xFF04: ord('$'), 0xFF0D: ord('-'), 0x3000: ord(' ')})


def fold_fullwidth_ascii(text) -> str:
    """``text`` with full-width digits, Latin letters, ￡ and the ideographic space folded."""
    return str(text or '').translate(_FULLWIDTH_ASCII)


# A quoted passage introduced as someone else's text ("Here is a listing note: '...'", "the
# agent wrote: \"...\"") or carrying its own save cue or role label is not the user's
# criteria: the pasted note's "£300" became the search's max_budget (S-04; R3-08 kept it out
# of memory only).
_PASTE_LEAD_IN = re.compile(
    r"\b(?:listing|agent|agent'?s|landlord|landlord'?s|letting\s+agent|advert|ad|someone|he|she|they)\b"
    r"[^.!?;\n\"'“‘「]{0,20}\b(?:note|notes|wrote|writes|said|says|message|text|description|reads?)\b"
    r"[^.!?;\n\"'“‘「]{0,6}[:：]?\s*$", re.I)


def without_pasted_text(text: str) -> str:
    """``text`` without quoted third-party passages and pasted instructions (S-04)."""
    value = str(text or '')
    if not re.search(r"[\"'“‘「『]|\b(?:system|assistant|developer)\s*:", value, re.I):
        return value
    from core.memory_gate import (_FOREIGN_CUE, _QUOTED_SPAN, _ROLE_LABEL_PASTE,
                                  _THIRD_PERSON_REMEMBER)

    def _quoted(match):
        lead = value[max(0, match.start() - 60):match.start()]
        if _FOREIGN_CUE.search(match.group(2)) or _PASTE_LEAD_IN.search(lead):
            return ' '
        return match.group(0)
    value = _QUOTED_SPAN.sub(_quoted, value)
    value = _ROLE_LABEL_PASTE.sub(' ', value)
    return _THIRD_PERSON_REMEMBER.sub(' ', value)


def scoped_update_text(text: str) -> str:
    text = without_pasted_text(str(text or ''))
    text = without_third_party_clauses(text)
    # Withdrawn experiments and other people's plans are references, not updates.
    # Each clause pattern is anchored at a clause START (``_CLAUSE_START``). Unanchored, the
    # leading ``[^…]*`` let the engine retry from every character of a long clause, each
    # attempt re-scanning to its end: O(n²) on a long paste with no full stop, run several
    # times per turn on the event loop (2026-10-19, B-01 class). Leftmost-match semantics
    # already started every real match at a clause start, so the result is unchanged.
    text = _WITHDRAWN_CLAUSE.sub(_replacement_after_withdrawal, text)
    text = _KEPT_OUT_CLAUSE.sub('', text)
    # A comparison can quote another plan without changing it. Keep the explicit
    # modification, excluding the clause marked as unchanged.
    parts = re.split(r'[;；\n]', text)
    if len(parts) > 1 and re.search(r'只(?:修改|改|调整)|only (?:change|update)', text, re.I):
        parts = [p for p in parts if not re.search(r'不变|保持原|unchanged|leave .* as is', p, re.I)]
    return '; '.join(parts)


def is_hypothetical(text: str) -> bool:
    return bool(re.search(
        r'\b(?:what if|suppose|hypothetically|for example|if (?:a|the|we|i|it|my|there))\b|假设|假如|举例|比如说|如果', text, re.I))


def explicit_criteria_revision(text: str) -> bool:
    """A positive instruction replacing old defaults is an update, not a conflict."""
    pattern = re.compile(
        r'(?:替换|覆盖|取代|重设|重置)(?:我(?:的)?)?(?:(?:此前|之前|原来|原有|先前|旧)(?:的)?)?'
        r'(?:长期)?(?:默认(?:租房)?(?:偏好|条件|设置)?|租房偏好|预算|上限|筛选条件)'
        r'|\b(?:replace|supersede|override|reset|overwrite)\s+(?:(?:my|the|our)\s+)?'
        r'(?:(?:previous|old|existing|earlier|current)\s+)?(?:(?:saved|rental|search)\s+)?'
        r'(?:defaults?|preferences|criteria|filters|(?:rent )?budget)\b', re.I)
    for match in pattern.finditer(text or ''):
        clause = clause_at(text, match.start(), match.end())
        if (is_hypothetical(clause) or re.search(r'[?？]|是否|会不会|能否|\b(?:should|would|could|can)\b', clause, re.I)):
            continue
        if mention_is_requirement(text, match.start(), match.end()):
            return True
    return False


_ASKS_FOR_HOMES = re.compile(
    r"^\s*is\s+there\s+(?:any|anything)\b[^.!?]{0,24}\b(?:housing|accommodation|flats?|homes?|rooms?|studios?|"
    r"apartments?)\b", re.I)


def asks_for_homes(text: str) -> bool:
    """A search phrased as a question: "Is there any housing near SW7 2AZ under £900?"."""
    return bool(_ASKS_FOR_HOMES.search(text or ''))


def is_criteria_consultation(text: str) -> bool:
    """An explicit read-only request must also bypass cached-result refinement."""
    if re.search(r"(?:don.t|do not|please don.t) change (?:my |the )?(?:search )?(?:criteria|filters)"
                 r'|leave (?:my |the )?(?:housing |search |housing search )?(?:criteria|filters) unchanged'
                 r'|不要(?:改变|修改|调整)(?:我的|当前|搜索)?(?:筛选|条件|需求)', text, re.I):
        return True
    if re.search(r'(?:housing|search) (?:must|should|will) (?:stay|remain)|(?:only|just) a weather question|'
                 r'(?:找房|住房|搜索)(?:条件|区域|需求)?(?:必须|应当|仍然|还是|保持)[^。.!?]{0,25}(?:不变|维持|watford)', text, re.I):
        return True
    if re.search(r'\b(?:save|favourite|favorite)\b|收藏', text, re.I) and re.search(r'\bfavou?rites?\b|收藏', text, re.I) and not re.search(
            r'\b(?:find|search|set|change|raise|lower|remove|drop|switch|restore)\b|改为|改回|更新|找房|搜索', text, re.I):
        return True
    # "Is there any housing near SW7 2AZ under £900?" asks for a search: as a read-only question it
    # kept neither the budget nor the area, and the gate asked for the budget it had just read (G-10).
    update = (re.search(r'\b(?:set|change|raise|lower|remove|drop|find|search|show|available)\b|改为|改回|更新|取消|删除|移除|找房|搜索',
                        text, re.I) or asks_for_homes(text))
    if re.search(r'\b(?:details?|furniture|furnished|bills|availability|tenancy|epc)\b|房源详情|家具|账单', text, re.I) and re.search(
            r'^\s*(?:please|can |could |what|tell |explain|get |check |verify|for the)|请|这套|这个房源|告诉|查询|[?？]', text, re.I) and not re.search(
            r'\b(?:find|search|set|change|raise|lower|remove|drop|switch|restore)\b|改为|改回|更新|取消|删除|移除|找房|搜索', text, re.I):
        return True
    # "Please don't search; give me a summary" is still a read-only request.
    cleaned = re.sub(r'请?不要搜索|don.t search|do not search', '', text, flags=re.I)
    if re.search(r'摘要|需求总结|\b(?:summary|recap)\b', cleaned, re.I) and not re.search(
            r'改为|改回|更新|取消|删除|移除|\b(?:set|change|raise|lower|remove|drop)\b', cleaned, re.I):
        return True
    return bool(re.search(r'^\s*(?:is|does|has)\b', text, re.I) and not update
                and not _states_a_ceiling_outside_a_question(text))


# "Is £2,100 a month reasonable? I can pay up to £2,300." opens with a question, but its
# second sentence states the ceiling; the whole message was read-only (r21 variant).
_CEILING_STATEMENT = re.compile(
    r"\b(?:i\s+can\s+(?:only\s+)?(?:pay|afford|spend)|(?:my\s+)?budget\s+(?:is|of|=|:)|"
    r"up\s+to|under|below|no\s+more\s+than|max(?:imum)?|at\s+most)\b[^.!?;\n]{0,25}£\s?\d", re.I)


def _states_a_ceiling_outside_a_question(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?;\n])\s+", str(text or "")[:4000]):
        if sentence.strip() and not sentence.strip().endswith(("?", "？")) \
                and _CEILING_STATEMENT.search(sentence):
            return True
    return False


_CLAUSE_BOUNDARY = re.compile(r'[。！？!?;；\n，,:：]|(?<!\d)\.(?!\d)')
# How far a clause may extend either side of a mention. Every caller runs once per MENTION,
# and scanning the whole prefix each time made a long unpunctuated paste O(mentions x
# length) — 11 s for 28 000 characters of repeated Chinese criteria, on the event loop
# (2026-10-19, B-01 class). No real clause is longer than this.
_CLAUSE_WINDOW = 400


def clause_at(text: str, start: int, end: int) -> str:
    lo = max(0, start - _CLAUSE_WINDOW)
    # finditer(text, pos, endpos) reports absolute offsets.
    left = max([m.end() for m in _CLAUSE_BOUNDARY.finditer(text, lo, start)] or [lo])
    # The tail is searched in a SLICE, exactly as before: with pos=end the look-behind
    # would see the digit before `end`, so "My budget is 1500. If I could ..." stopped
    # treating that full stop as a boundary and read the next sentence's hypothetical.
    tail = _CLAUSE_BOUNDARY.search(text[end:end + _CLAUSE_WINDOW])
    right = end + tail.start() if tail else min(len(text), end + _CLAUSE_WINDOW)
    return text[left:right]


_WITHDRAWN_BEFORE_MENTION = re.compile(
    r"(?<!n't )(?<!n’t )(?<!\bnot )(?<!\bnever )"
    r"\b(?:never\s*mind|forget(?:\s+about)?|scrap|ditch|drop|skip|retract|withdraw|cancel|remove|delete|undo"
    r"|no\s+longer\s+(?:want|need))"
    r"(?:\s+(?:the|a|an|my|that|this))?\s+$", re.I)


def withdraws_room_type(text: str, room_type) -> bool:
    """True when ``text`` withdraws the standing ``room_type`` ("never mind studio",
    "forget the studio", "no longer need a studio", "不要studio了"), or one of several."""
    return bool(withdrawn_room_types(text, room_type))


def withdrawn_room_types(text: str, room_type) -> tuple:
    """The parts of the standing ``room_type`` (one type, or several joined by '+') that
    ``text`` withdraws; "never mind the studio" against 'studio+ensuite' withdraws only
    the studio."""
    from core.tools.search_properties import _ROOM_TYPE_SYNONYMS, room_type_parts
    synonyms = dict(_ROOM_TYPE_SYNONYMS)
    value = str(text or '')
    withdrawn = []
    for part in room_type_parts(room_type) or (str(room_type or ''),):
        if _mentions_withdrawn(value, synonyms.get(part, ())):
            withdrawn.append(part)
    return tuple(withdrawn)


def _mentions_withdrawn(value: str, needles) -> bool:
    for needle in needles:
        for found in re.finditer(r'(?<![a-z])' + re.escape(needle) + r'(?![a-z])', value, re.I):
            before = _CLAUSE_BOUNDARY.split(value[max(0, found.start() - 60):found.start()])[-1][-45:]
            after = value[found.end():found.end() + 12]
            if (_WITHDRAWN_BEFORE_MENTION.search(before)
                    or re.search(r'(?:不要|不用|不需要|不考虑)\s*$', before)
                    and re.match(r'\s*(?:了|也行|吧)', after)):
                return True
    return False


def mention_is_requirement(text: str, start: int, end: int) -> bool:
    clause = clause_at(text, start, end)
    # Negation belongs to this clause. In particular, "not all London: a whole
    # one-bedroom flat" negates the area, not the room type or bedroom count.
    # Only the last 45 characters are read, so a bounded window is exactly equivalent.
    window = text[max(0, start - _CLAUSE_WINDOW):start]
    before = _CLAUSE_BOUNDARY.split(window)[-1][-45:]
    if is_hypothetical(clause):
        return False
    if re.search(r'\b(?:don.t|do not) assume\b|不要假设|不能假设|out of (?:this|the) decision', clause, re.I):
        return False
    if re.search(r'\b(?:on its own|alone)\b[^.!?;\n]{0,35}\b(?:not|cannot|can.t|doesn.t|wouldn.t)\b'
                 r'[^.!?;\n]{0,35}\b(?:answer|prove|establish|mean|satisfy|guarantee)\b', clause, re.I):
        return False
    if re.search(r'\b(?:must|should|can) not (?:be )?(?:treated|assumed|considered|taken)|'
                 r'\bdoes not (?:mean|prove|guarantee)|不能(?:视为|证明)|不代表', clause, re.I):
        return False
    # "I don't need a balcony after all" recorded "Must have balcony" (stress A-04 replay).
    if re.search(r'(?:not(?: a| an)?|no|without|remove|exclude|avoid|delete|don.t want'
                 r'|(?:don.t|do not|doesn.t|does not) need|no need for)\s+(?:\w+\s+){0,3}$'
                 # "1 bed rather than studio" stored the studio beside the count.
                 r'|\b(?:rather\s+than|instead\s+of)\s+(?:(?:a|an|the|my)\s+)?$'
                 r'|(?:不(?:要|是|需要|想|考虑)|取消|删除|移除|去掉|排除)[^，。；;!?]{0,14}$', before, re.I):
        return False
    # "Never mind studio, 2 beds" kept the studio, so every later search asked for a studio
    # with two bedrooms (R3-05). "Don't forget the studio" is still a requirement.
    if _WITHDRAWN_BEFORE_MENTION.search(before):
        return False
    if re.search(r'是否|有没有|能否|\b(?:does it|is there|are there|do they|can you explain)\b', clause, re.I):
        return False
    if re.search(r'是什么|怎么样|允许.*[吗？]|\b(?:is this|is that|does this|does that|tell me about|what does)\b', clause, re.I):
        return False
    return True


# "Which of the listings you showed me in Bethnal Green was the cheapest?" asks about results
# already on the page; it moved the search area, and the next edit searched there (r06 e07e26d4).
_SHOWN_RESULTS = re.compile(
    r"\byou(?:['’]ve|\s+have|\s+had|\s+just|\s+already)*\s+"
    r"(?:showed|shown|listed|found|gave|given|recommended|suggested|mentioned|sent|returned)\b"
    r"|\b(?:shown|listed|recommended)\s+(?:earlier|before|above|previously)\b"
    r"|你(?:刚才|刚刚|之前|前面|上次)?(?:给我|帮我|为我)?(?:看|展示|列出|找到|推荐|搜到|发)(?:过|了)?的", re.I)
_SHOWN_RESULTS_ACTION = re.compile(
    r"\b(?:show\s+me|go(?:ing)?\s+back|back\s+to|switch|move|search|look\s+in|again)\b"
    # "I need to commute to King's Cross, how long from the first area you showed?" states a need.
    r"|\bI\s+(?:need|want|work|commute)\b|回到|切换|换到|再搜|我要|我需要", re.I)


def without_shown_results_questions(text: str) -> str:
    """``text`` without the sentences that only ask about results already shown."""
    sentences = re.findall(r"[^.!?;；。！？\n]+[.!?;；。！？\n]*", text or '')
    kept = [sentence for sentence in sentences if not (
        _SHOWN_RESULTS.search(sentence) and not _SHOWN_RESULTS_ACTION.search(sentence))]
    return ''.join(kept) if len(kept) != len(sentences) else text


def housing_update_text(text: str) -> str:
    """Exclude standalone ancillary requests from the housing mutation channel."""
    text = without_shown_results_questions(scoped_update_text(text))
    # Negative/preservation clauses in an ancillary question are not housing
    # instructions. Require a positive housing action before accepting its places.
    if is_criteria_consultation(text):
        return ''
    auxiliary = re.search(r'天气|气温|票价|线路状态|\b(?:weather|temperature|fare|travelcard|line status|journey)\b', text, re.I)
    if auxiliary:
        from core.tools.search_properties import _CUE_ONLY_AREAS, _KNOWN_AREAS
        affirmative = []
        for clause in re.split(r'[。.!?;；\n]', text):
            if re.search(r'\b(?:don.t|do not|never)\b|不要|无需|不用|别', clause, re.I):
                continue
            if re.search(r'\b(?:unchanged|remain|stay|only a weather|only weather)\b|不变|保持原', clause, re.I):
                continue
            area_action = any(any(re.match(re.escape(name) + r'(?![a-z])', match.group(1), re.I)
                                  for name in (*_KNOWN_AREAS, *_CUE_ONLY_AREAS))
                for match in re.finditer(
                    r'\b(?:search(?: in| around)?|keep|restore|(?:switch|change|move)'
                    r'(?:\s+(?:the\s+|my\s+)?(?:area|search|location))?\s+to)\s+([a-z][a-z\s\-]{0,35})', clause, re.I))
            explicit_slot = re.search(
                r'\b(?:rent|budget|ceiling|bedrooms?|whole (?:flat|property)|self.contained|workplace)\b'
                r'|\b(?:commute|commuting|office)\b.{0,65}\b(?:days?|minutes?|change|workplace)\b'
                r'|\b(?:my (?:job|work)|a job)\b.{0,35}\b(?:near|next to|at|in)\b'
                r'|\b(?:wheelchair|step.free|doorways?|corridors?|bathroom|cats?|pets?|landlord|permission|consent|lifts?|elevators?|balcony|garden)\b'
                r'|\bfocus on\b|月租|租金|预算|卧室|整租|到岗|入住|起租|轮椅|无台阶|猫|许可|电梯|阳台|花园', clause, re.I)
            # "Show me Angel flats under £2100 and tell me the weather there." searched the old
            # area (R4-07, 18/18): a "show me" request and "<homes> under £N" are housing too.
            if area_action or explicit_slot or re.search(r'\b(?:search|find|rent|show(?:\s+me)?|look(?:ing)?\s+for|get\s+me)\b'
                         r'.{0,35}\b(?:flat|home|house|property|properties|studio|bedroom|apartment|places|listings)|'
                         r'\b(?:flats?|homes?|houses?|studios?|apartments?|places|listings)\b.{0,25}'
                         r'\b(?:under|below|up\s+to|max(?:imum)?)\s*£?\s*\d|'
                         r'\b(?:set|change|raise|lower|remove|drop)\b.{0,30}\b(?:budget|bedroom|area|room type|commute destination|my office)|'
                         r'找房|搜索房源|找.{0,12}(?:公寓|卧|住处|居室|studio|单间)|预算改|改为|改回|恢复', clause, re.I):
                affirmative.append(clause)
        if not affirmative:
            return ''
        text = '; '.join(affirmative)
    if is_criteria_consultation(text):
        return ''
    housing_intent = re.search(
        r'找房|帮我找|寻找|搜(?:索|三套|房)|看(?:看)?房|整租|预算|月租|房租|改为|改回|改成|切换|恢复|备用方案|主方案'
        r'|\b(?:find|search|show|rent|budget|ceiling|bedroom|move in|restore|switch|instead|make it|change|set|keep|remove|drop)\b', text, re.I)
    if not housing_intent and re.search(
        r'天气|气温|犯罪|治安|票价|票卡|线路状态|\b(?:weather|temperature|crime|safety|fare|travelcard|line status|journey)\b', text, re.I):
        return ''
    return text


def explicit_feature_requirement_text(text: str) -> str:
    """Declarative hard property needs may reaffirm missing slots during a review.

    Only these sentences are returned. Questions about a listing, hypothetical
    requirements and explicit instructions to preserve criteria remain read-only.
    """
    selected = []
    for match in re.finditer(r'[^。.!?;；\n]+[。.!?;；\n]*', text or ''):
        sentence = match.group().strip()
        if (is_hypothetical(sentence) or re.search(r'[?？]|\b(?:does|is|has) (?:this|that|the|either)\b|是否|有没有|能否', sentence, re.I)):
            continue
        hard = re.search(r'\b(?:must|needs?|require(?:s|d)?|requirements?|essential|mandatory)\b|必须|必需|需要|硬条件', sentence, re.I)
        feature = re.search(r'wheelchair|step.free|doorways?|corridors?|bathroom|cats?|pets?|permission|consent|lifts?|elevators?'
                            r'|轮椅|无台阶|猫|许可|电梯', sentence, re.I)
        if not (hard and feature):
            continue
        if re.search(r'\b(?:don.t|do not) (?:assume|add|require|need)\b|不要假设|不要添加|不再需要', sentence, re.I):
            continue
        selected.append(sentence)
    return ' '.join(selected)


_SAVED_VALUE = (r"(?:saved|stored|remembered|usual)\s+(?:search\s+|default\s+|rent\s+)?"
                r"(?:budget|ceiling|criteria|preferences|requirements|settings|filters?|details)")


# The user's budget, named as what to search by, without the word "saved": "use my default budget",
# "Find me a 1-bed under my budget", "applying my budget", "stick to my budget", "Use the budget you
# have saved for me" applied nothing while the reply talked of a budget (G-08, F-03).
_OWN_BUDGET_REFERENCE = re.compile(
    r"\b(?:use|using|apply|applying|with|under|within|stick(?:ing)?\s+to|keep(?:ing)?\s+to|respect(?:ing)?)\s+"
    r"(?:my|the)\s+(?:(?:default|normal|usual|current|set)\s+)?budget\b"
    r"(?!\s+(?:is|was|of|to|at|by|could|can|for|£|\d))"
    r"|\b(?:use|using|apply|applying)\s+(?:the\s+|my\s+)?budget\s+(?:that\s+)?(?:you(?:\s+have|['’]ve)?|i(?:\s+have|['’]ve)?)\s+"
    r"(?:saved|stored|got|gave|told)\b", re.I)


def references_own_budget(text: str) -> bool:
    """True for that reference outside a question about something: "Is it under my budget?"."""
    for sentence in re.split(r'(?<=[.!?。！？])\s*', str(text or '')[:2000]):
        if (_OWN_BUDGET_REFERENCE.search(sentence)
                and not (sentence.rstrip().endswith(('?', '？')) and _ASKS_ABOUT_IT.search(sentence))):
            return True
    return False


def references_saved_criteria(text: str) -> bool:
    if references_own_budget(text):
        return True
    return bool(re.search(
        r'(?:按|用|恢复|复述|记得)[^。！？]{0,35}(?:长期|默认|保存|偏好|记住|存的)|长期默认|'
        r'saved (?:search |default )?(?:criteria|preferences)|'
        r'(?:use|apply|restore|recall|repeat)[^.!?]{0,35}(?:defaults|remembered|preferences)|'
        # "Find me a flat in Bow using my saved budget and preferences." (W-09).
        r'\b' + _SAVED_VALUE + r'\b', text or '', re.I))


# What a saved value is taken for. "show" only with a home noun: "Show my saved budget" is a recall.
_RESTORE_CUE = re.compile(
    r'搜|找|恢复|使用|按|用|\b(?:use|using|apply|applying|restore|find|search|look\s+for|with|stick|keep|'
    r'show\s+(?:me\s+)?(?:flats?|homes?|listings?|properties|places|options))\b', re.I)
_ONLY_THE_BUDGET = re.compile(r'\b(?:saved|stored|remembered|usual)\s+(?:rent\s+)?(?:budget|ceiling)\b|保存的预算|记住的预算', re.I)
_SAVED_OTHER_THAN_BUDGET = re.compile(r'\b(?:preferences|criteria|requirements|settings|filters?|details|defaults)\b|偏好|条件|默认', re.I)
_SAVED_COMMUTE_TOO = re.compile(r'\b(?:commute|workplace|office)\b', re.I)
_COMMUTE_FIELDS = {'commute_destination', 'destination', 'no_commute', 'max_travel_time'}
_SAVED_FIELDS = {'area', 'areas', 'max_budget', 'budget_period', 'room_type', 'bedrooms',
                 'bedrooms_minimum', 'commute_destination', 'destination', 'no_commute',
                 'max_travel_time', 'property_features', 'soft_preferences', 'move_in_date',
                 'monthly_total_budget', 'office_days_per_week'}
_BUDGET_FIELDS = {'max_budget', 'budget_period', 'monthly_total_budget'}


def restore_saved_criteria(accumulated: dict, memories: list, message: str) -> dict:
    """Use only this authenticated user's retrieved memory, on an explicit request.

    Prompt recollection alone does not enforce filters. Project the selected semantic
    preference into the same structured state the search executor validates. A saved default
    in one row wins as a whole; otherwise each field comes from the newest row that states it,
    so a plain "max budget £2,000" counts (W-09). "my saved budget" takes the budget only.
    """
    if not references_saved_criteria(message) or not _RESTORE_CUE.search(message):
        return accumulated
    from core.langgraph_agent import _apply_explicit_criteria_updates
    # A reflection is the assistant's inference ("fixed budget of around £2,300/month"),
    # never a saved criterion (R2-06).
    candidates = sorted((m for m in memories or [] if m.get('mtype') != 'reflection'),
                        key=lambda m: str(m.get('created_at') or ''), reverse=True)
    candidates.sort(key=lambda m: (m.get('mtype') != 'semantic',
        not bool(re.search(r'默认|长期|\bdefaults?\b|saved preferences', str(m.get('text') or ''), re.I))))
    own_budget = references_own_budget(message)
    budget_only = bool((_ONLY_THE_BUDGET.search(message) or own_budget)
                       and not _SAVED_OTHER_THAN_BUDGET.search(message))
    wanted = (_BUDGET_FIELDS | _COMMUTE_FIELDS if budget_only and _SAVED_COMMUTE_TOO.search(message)
              else _BUDGET_FIELDS)
    if own_budget and budget_only:
        # "my budget" is the one this conversation holds; a saved figure fills in only when it
        # has one meaning, the rule of restore_saved_budget (E-02).
        figures = {_apply_explicit_criteria_updates({}, str(m.get('text') or '')).get('max_budget')
                   for m in candidates if m.get('mtype') == 'semantic'} - {None}
        if (accumulated or {}).get('max_budget') is not None or len(figures) != 1:
            return accumulated
    merged, owners = {}, {}

    def _restored(fields: dict, owner_of) -> dict:
        restored = dict(accumulated)
        restored.update(fields)
        restored['constraint_sources'] = {
            **(accumulated.get('constraint_sources') or {}),
            **{k: {'source': 'saved_preference', 'memory_id': owner_of(k), 'value': v}
               for k, v in fields.items()},
        }
        return _apply_explicit_criteria_updates(restored, message)

    for memory in candidates:
        text = str(memory.get('text') or '')
        parsed = _apply_explicit_criteria_updates({}, text)
        fields = {k: v for k, v in parsed.items() if k in _SAVED_FIELDS}
        if fields.get('max_travel_time'):
            fields.setdefault('no_commute', False)
        if not budget_only and any(k in fields for k in ('room_type', 'bedrooms')) and bool(
                fields.get('max_budget') or fields.get('monthly_total_budget')):
            return _restored(fields, lambda _key: memory.get('id'))
        if memory.get('mtype') != 'semantic':
            continue
        for key, value in fields.items():
            if key not in merged and value not in (None, [], '') and (not budget_only or key in wanted):
                merged[key], owners[key] = value, memory.get('id')
    return _restored(merged, owners.get) if merged else accumulated


# "Budget back to what it was when I started" and 把预算恢复成最开始的数字 kept the current ceiling
# while the reply listed results as if it had moved (W-05). A reference to an earlier value resolves
# from this conversation's own constraint_history.
_BUDGET_FIRST = re.compile(
    r"\b(?:original|initial|first|starting|opening)\s+(?:budget|ceiling|figure|amount|limit)\b"
    r"|\bbudget\s+(?:i|we)\s+(?:started|began)\s+with\b"
    r"|\bbudget\b[^.!?\n]{0,40}\b(?:what\s+it\s+was\s+)?(?:when\s+(?:i|we)\s+(?:started|began)|"
    r"at\s+the\s+(?:start|beginning)|originally|to\s+begin\s+with|at\s+first)\b"
    r"|\bbudget\b[^.!?\n]{0,40}\bwhat\s+(?:i|we)\s+(?:started|began)\s+with\b"
    r"|(?:最开始|一开始|刚开始|最初|起初|当初|最早)的?(?:预算|数字|金额|价格|上限|那个)"
    r"|预算[^，。！？,.!?]{0,12}(?:恢复|改回|回到|换回|调回|还原|退回)[成到为]?(?:最开始|一开始|刚开始|最初|起初|当初|最早)", re.I)
_BUDGET_PREVIOUS = re.compile(
    r"\b(?:the|my)\s+(?:previous|earlier|old|prior|last)\s+(?:budget|ceiling|figure|amount)\b"
    r"|\bbudget\s+i\s+had\s+before\b"
    r"|\bbudget\b[^.!?\n]{0,40}\bwhat\s+it\s+was\s+(?:before|earlier|previously)\b"
    r"|(?:之前|原来|上一次|上次|先前|以前)的预算"
    r"|预算[^，。！？,.!?]{0,12}(?:恢复|改回|回到|换回|调回|还原|退回)[成到为]?(?:之前|原来|上一次|上次|先前|以前)", re.I)
_BUDGET_RESTORE_CUE = re.compile(
    r"\b(?:back|revert\w*|restor\w*|return\w*|reset|go|use|using|put|set|change|switch\w*|apply|please|instead|again)\b"
    r"|恢复|改回|回到|换回|调回|还原|退回|用|按", re.I)
# "Forget my old budget, use £2,300 instead" names the old budget to get rid of it. Read as a
# reference to it, the turn restored the empty slot from before the old budget (G-02).
_BUDGET_DISOWNED = re.compile(
    r"\b(?:forget(?:\s+about)?|ignore|disregard|scrap|ditch|drop|delete|remove|lose|never\s*mind(?:\s+about)?)\s+"
    r"(?:the|my)\s+(?:previous|earlier|old|prior|last|original|initial|first|starting|opening)\s+"
    r"(?:budget|ceiling|figure|amount|limit)\b"
    r"|(?:忘掉|忘记|删除|删掉|去掉|不要)(?:我的)?(?:之前|原来|上一次|上次|先前|以前|最开始|一开始|最初)的预算", re.I)
_ASKS_ABOUT_IT = re.compile(r"^\W*(?:what|which|when|why|how|did|do|does|is|are|was|were|has|have)\b", re.I)
_ZH_ASKS_ABOUT_IT = re.compile(r"多少|什么|几|吗|么[？?]")


def budget_reference(text: str) -> str | None:
    """'first' or 'previous' when the message asks for an earlier budget, else None."""
    value = str(text or '')[:2000]
    for sentence in re.split(r'(?<=[!?？。！])\s*|(?<=\.)\s+(?=\D)', value):
        which = 'first' if _BUDGET_FIRST.search(sentence) else 'previous' if _BUDGET_PREVIOUS.search(sentence) else None
        if (not which or is_hypothetical(sentence) or not _BUDGET_RESTORE_CUE.search(sentence)
                or _BUDGET_DISOWNED.search(sentence)):
            continue
        if (sentence.rstrip().endswith(('?', '？')) and _ASKS_ABOUT_IT.search(sentence)) or _ZH_ASKS_ABOUT_IT.search(sentence):
            continue
        return which
    return None


def budget_reference_target(criteria: dict, which: str):
    """The earlier budget ``which`` refers to in ``criteria``'s own history, else None."""
    values = list(((criteria or {}).get('constraint_history') or {}).get('max_budget') or [])
    if not values:
        return None
    if which == 'first':
        return values[0]
    current = (criteria or {}).get('max_budget')
    at = max((i for i, value in enumerate(values) if value == current), default=None)
    if at is None:
        return values[-1]
    return values[at - 1] if at > 0 else None


def restore_saved_budget(accumulated: dict, memories: list, message: str) -> dict:
    """The one budget this user saved, for an earlier budget a new conversation does not hold.

    "Budget back to what it was when I started" in a fresh conversation searched with no budget,
    and the reply said it had set £2,000, that £2,000 was not applied, and that it had no earlier
    budget (r05 8a4045f5). With exactly one saved figure the request has one meaning. With none or
    several, nothing is set and ``budget_reference_note`` says so."""
    which = budget_reference(message)
    if (not which or (accumulated or {}).get('max_budget') is not None
            or budget_reference_target(accumulated, which) is not None):
        return accumulated
    from core.langgraph_agent import _apply_explicit_criteria_updates
    figures = {}
    for memory in memories or []:
        if memory.get('mtype') != 'semantic':
            continue
        budget = _apply_explicit_criteria_updates({}, str(memory.get('text') or '')).get('max_budget')
        if budget:
            figures.setdefault(budget, memory.get('id'))
    if len(figures) != 1:
        return accumulated
    ((budget, owner),) = figures.items()
    restored = dict(accumulated or {}, max_budget=budget, budget_period='month')
    restored['constraint_sources'] = {
        **((accumulated or {}).get('constraint_sources') or {}),
        'max_budget': {'source': 'saved_preference', 'memory_id': owner, 'value': budget}}
    return restored


def budget_reference_note(message: str, criteria: dict, language: str = 'en') -> str:
    """The sentence that goes with an earlier budget this conversation does not hold: the reply
    must not carry on as if it had been applied."""
    which = budget_reference(message)
    if not which:
        return ''
    source = ((criteria or {}).get('constraint_sources') or {}).get('max_budget') or {}
    if source.get('source') == 'user_undo' and source.get('text') == str(message)[:800]:
        return ''
    if source.get('source') == 'saved_preference':
        return ''
    if budget_reference_target(criteria, which) is not None:
        return ''
    current = (criteria or {}).get('max_budget')
    if language == 'zh':
        return (f'这次对话里没有更早的预算记录，所以预算仍是£{current:,.0f}。' if current
                else '这次对话里没有更早的预算记录，请告诉我要用的预算。')
    return (f"I don't have an earlier budget in this conversation, so it stays at £{current:,.0f}." if current
            else "I don't have an earlier budget in this conversation. What ceiling should I use?")


_BUDGET_SET_CLAIM = re.compile(
    r"[^.!?。！？\n]*(?:\b(?:set|put|moved?|changed?|reset|restored?|reverted?|returned)\b[^.!?\n]{0,30}\bbudget\b"
    r"[^.!?\n]{0,24}\b(?:back|to\s+£)|\bbudget\b[^.!?\n]{0,12}\b(?:is|now)\s+back\b"
    r"|(?:已|已经|帮你)?把?预算[^。！？\n]{0,10}(?:恢复|改回|调回|设回|设为|改为))[^.!?。！？\n]*[.!?。！？]?\s*", re.I)


def without_budget_set_claim(text: str) -> str:
    """``text`` without a sentence saying a budget was set back, for a turn that set none."""
    return _BUDGET_SET_CLAIM.sub('', str(text or '')).strip()


def resolve_clarification_reply(message: str, messages: list) -> str:
    """Interpret only a value from this conversation's latest pending question.

    Options may carry opaque UI values. Its stored display label supplies the
    meaning, without trusting client-supplied criteria or another conversation.
    """
    for item in reversed(messages or []):
        if item.get('role') != 'assistant':
            continue
        if item.get('response_type') not in ('clarification', 'ask_user'):
            return message
        choices = item.get('options') or (item.get('metadata') or {}).get('options') or []
        for choice in choices:
            if isinstance(choice, dict) and choice.get('value') == message:
                return str(choice.get('label') or message)
        return message
    return message


def validate_current_criteria_recap(response: str, criteria: dict, *, message: str = '', language: str = 'en') -> str:
    """Correct narrow claims about current state from the persisted state itself."""
    text = str(response or '')
    days = (criteria or {}).get('office_days_per_week')
    words = dict(one=1, two=2, three=3, four=4, five=5, six=6, seven=7)
    corrected = False
    lines = []
    for line in text.splitlines():
        match = re.search(
            r"(?:criteria block|current criteria|current work pattern|office attendance|your work pattern|you(?:'re| are) (?:still )?commuting)"
            r'[^.!\n]{0,90}?\b([0-7]|one|two|three|four|five|six|seven)\s+(?:office\s+|work\s+)?days?\s+(?:a|per)\s+week', line, re.I)
        if match and isinstance(days, (int, float)):
            value = match.group(1).lower()
            count = int(value) if value.isdigit() else words[value]
            if count != days:
                corrected = True
                continue
        if _asks_to_save_listing(message) and re.search(
                r'favou?rite.{0,60}(?:yourself|visit|listing page|onthemarket)|(?:yourself|visit).{0,60}favou?rite', line, re.I) and re.search(r'onthemarket|external|外部', line, re.I):
            continue
        lines.append(line)
    if corrected:
        lines.append(f'当前已保存的到岗频率是每周 {days:g} 天。' if language == 'zh' else
                     f'Your current criteria record {days:g} office days per week.')
    if _asks_to_save_listing(message) and not re.search(r'heart|Saved view|Save button|收藏按钮|爱心', '\n'.join(lines), re.I):
        lines.append('可点击房源卡片的爱心收藏按钮，再到“收藏夹”查看；聊天助手不能代您点击。' if language == 'zh' else
                     'Use the heart/Save button on the listing card, then open the Saved view. The chat assistant cannot click it for you.')
    return '\n'.join(lines).strip()


def _asks_to_save_listing(message: str) -> bool:
    """Saved-listing (favourites) talk, not the adjective: "my favourite area is Angel" and
    "my favourite fruit" got "Use the heart/Save button on the listing card" (R2-29)."""
    text = str(message or '')
    return bool(re.search(
        r'\b(?:save|add|put|move)\b[^.!?]{0,40}\b(?:to|in|into)\s+(?:my\s+)?favou?rites\b|'
        r'\b(?:save|mark|keep)\b[^.!?]{0,40}\bas\s+(?:a\s+|my\s+)?favou?rite\b|'
        r'\bfavou?rites?\s+(?:list|folder|page|view|tab)\b|\bmy\s+favou?rites\b|'
        r'\b(?:saved|favourited|favorited)\s+(?:listings?|flats?|homes?|properties)\b|'
        r'\bfavou?rite\s+(?:it|this|that|them|the\s+\w+\s+one)\b|收藏', text, re.I))


def commute_query_forbidden(text: str) -> bool:
    """A per-turn tool boundary, independent of the user's lasting commute filter."""
    return bool(re.search(
        r'(?:don.t|do not|never)\s+(?:run|calculate|check|query|call|recalculate)(?:\s+\w+){0,3}\s+(?:commut\w*|journey)'
        r'|(?:不要|别|无需|不用)(?:再|运行|调用|进行|计算|查询|查|算){0,4}通勤', text or '', re.I))


def cat_permission_update(text: str) -> bool | None:
    """Cat ownership is a fact; requiring landlord permission is a housing slot.

    Permission may precede or follow the animal and the hard cue. Inspect each
    statement independently so a later evidence warning cannot negate a requirement.
    None means unchanged, False an explicit withdrawal, True a requirement.
    """
    for clause in re.split(r'[。.!?;；\n]', text or ''):
        # "pets" too: "I do not need pets allowed any more" left the hard "cats allowed"
        # filter in place (2026-10-19 A-07). A withdrawal may name pets generally; a
        # requirement still has to pass the hard-cue test below.
        animal = re.search(r'\b(?:cats?|pets?)\b|猫|宠物', clause, re.I)
        permission = re.search(r'permission|permitt|allow|accept|consent|许可|允许|同意|养猫|带猫', clause, re.I)
        if not (animal and permission):
            continue
        if re.search(r'(?:remove|drop|clear|(?:no longer|don.t|do not|doesn.t|does not) (?:need|require))'
                     r'[^.!?]{0,50}(?:permission|requirement|filter|cats?|pets?)'
                     r'|(?:cats?|pets?)\s+allowed\s+(?:is\s+)?(?:no longer|not)\s+(?:needed|necessary|required)'
                     r'|(?:取消|去掉|不再需要)[^。；]{0,18}(?:猫|宠物)(?:许可|筛选|条件)', clause, re.I):
            return False
        if not re.search(r'\bcats?\b|猫', clause, re.I):
            continue        # a pet requirement without a cat is not this slot
        if is_hypothetical(clause) or re.search(
                r'\b(?:unknown|whether|does|can you|is it|do not assume|don.t assume|no permission|not required)\b'
                r'|是否|未知|不要|暂时不|没有.{0,15}(?:许可|同意)', clause, re.I):
            continue
        hard = re.search(r'\b(?:must|needs?|require(?:s|d)?|essential|mandatory|hard requirements?)\b|必须|必需|需要|硬条件', clause, re.I)
        if hard or re.search(r'\b(?:permission|consent) for (?:this |that |our |my |a |the )?(?:specific )?cat\b', clause, re.I):
            return True
    return None


# A zh withdrawal is bound to the feature it names, in either order: 「算了，不要宠物要求了」,
# 「不需要宠物了」 and 「宠物要求算了」 kept the filter while the reply said it was dropped (W-02), and
# 「算了，不用阳台了」 says nothing about pets. 「不要宠物」 alone can be a wish for a pet-free building, so
# 不要 needs 了 or a word like 要求 after the feature.
_ZH_DROP_BEFORE = re.compile(
    r"(?P<verb>不要|不用|不需要|不必|无需|不再需要|不再要|去掉|取消|删掉|删除|移除|忽略|撤销|撤回)"
    r"(?:有|带|养|允许|可以养|能养|靠近)*的?\s*$")
_ZH_REQUIREMENT_WORD = r"(?:的)?(?:事|这个|这条|这项|这一条|这一项)?(?:要求|条件|筛选|需求|限制)?"
_ZH_DROP_TAIL = re.compile(r"\s*(?:的)?(?:事|这个|这条|这项)?(?:要求|条件|筛选|需求|限制)|\s*(?:的事)?\s*(?:了|吧)")
_ZH_DROP_AFTER = re.compile(
    r"\s*" + _ZH_REQUIREMENT_WORD + r"(?:也|就|都)?"
    r"(?:不要了|不用了|不需要了|不必了|算了|不重要了|无所谓|不管了|不考虑了|取消|去掉|删掉|删除|移除|撤销)")
_ZH_QUESTION_AFTER = re.compile(r"[^，。！？；,.!?;\n]{0,14}(?:吗|么|[？?])")


def zh_withdrawn_at(text: str, start: int, end: int) -> bool:
    """True when the zh words around ``text[start:end]`` withdraw that feature (W-02)."""
    if _ZH_QUESTION_AFTER.match(text, end):
        return False
    before = _ZH_DROP_BEFORE.search(text[max(0, start - 12):start])
    if before and (before.group('verb') != '不要' or _ZH_DROP_TAIL.match(text, end)):
        return True
    return bool(_ZH_DROP_AFTER.match(text, end))


def zh_feature_withdrawn(text: str, words) -> bool:
    """True when ``text`` withdraws a feature named by one of the zh ``words``."""
    value = str(text or '')
    return any(zh_withdrawn_at(value, found.start(), found.end())
               for word in words for found in re.finditer(re.escape(word), value))


_PET_WORD = r'\b(?:cats?|kittens?|pets?|dogs?|pupp(?:y|ies))\b|猫|狗|宠物'
_PET_WITHDRAWAL = re.compile(
    r'\b(?:forget|never\s*mind|ignore|drop|remove|scrap|skip|disregard|cancel)\b[^.!?;]{0,30}'
    r'(?:\b(?:cats?|pets?|dogs?)\b|pet[- ]friendly)'
    r'|\bno\s+longer\s+(?:have|own|got|need|require|want|keep)\b[^.!?;]{0,25}\b(?:cats?|pets?|dogs?|kittens?)\b'
    r'|\b(?:don.t|do\s+not)\s+(?:have|own)\s+(?:a\s+|any\s+|the\s+)?(?:cats?|pets?|dogs?)\s+any\s*(?:more|longer)\b'
    r'|\b(?:cats?|pets?|dogs?)\b[^.!?;]{0,25}\b(?:doesn.t|does\s+not|don.t|do\s+not|no\s+longer)\s+matter'
    r'|\b(?:the\s+)?(?:cat|pet|dog)\s+(?:is|has)\s+(?:gone|rehomed|no\s+longer\s+with\s+me)\b'
    r'|\bno\s+(?:need\s+for\s+)?pets?\s+(?:needed|required|requirement)\b'
    # "I don't need pets allowed", "No need for it to be pet friendly", "I'm not bringing a pet".
    r"|\b(?:don.t|do\s+not|doesn.t|does\s+not|no)\s+(?:need|require)\s+(?:for\s+)?(?:it\s+to\s+be\s+)?"
    r"(?:a\s+|any\s+|the\s+)?(?:cats?|pets?|dogs?)\b"
    r"|\bnot\s+(?:bringing|having|taking|getting)\s+(?:a\s+|any\s+|the\s+|my\s+)?(?:cats?|pets?|dogs?)\b"
    r'|不再养|不养(?:猫|狗|宠物)了|没有(?:猫|狗|宠物)了'
    r'|(?:猫|狗|宠物)(?:的事|的要求|的条件|这个条件)?(?:不用|不需要|算了|不重要了|取消)'
    r'|(?:取消|去掉|删除|忽略)[^。；]{0,10}(?:猫|狗|宠物)'
    # A correction in a message of its own: "Sorry, no cat, I was thinking of someone else.",
    # "No cat after all.", "抱歉，我没有狗，不需要宠物条件。" kept the pet filter (R1-02). A bare
    # "no pets in the building" is still a building rule, not a withdrawal.
    r'|\b(?:sorry|actually|oops|my\s+mistake|scratch\s+that|correction|wait)\b[\s,]+(?:i\s+)?'
    r'(?:no|don.t\s+have\s+an?|do\s+not\s+have\s+an?|haven.t\s+got\s+an?)\s+(?:cats?|pets?|dogs?|kittens?)\b'
    r'|\b(?:no|(?:don.t|do\s+not)\s+have\s+(?:an?|any))\s+(?:cats?|pets?|dogs?|kittens?)\s+after\s+all\b'
    r'|(?:抱歉|不好意思|其实|说错了|搞错了)[^。；]{0,6}没(?:有)?(?:养)?(?:猫|狗|宠物)'
    r'|(?:不用|不需要|不要求|不必|无需)(?:允许)?(?:养)?(?:猫|狗|宠物)(?:的)?(?:条件|要求|许可)'
    r'|不要求(?:允许)?(?:养)?(?:猫|狗|宠物)', re.I)


# An attribute written directly before the home being asked for ("pet-friendly furnished 1
# bed", "a furnished studio") is part of the request, as its "must" form is (R3-07). Up to
# three other attributes may sit between it and the home word.
_ATTRIBUTE_OF_HOME = (
    r"(?<![a-z-]){word}\s+(?:(?:pet[- ]friendly|(?:fully\s+)?furnished|modern|spacious|quiet|bright|"
    r"large|small|cosy|cozy|new|self[- ]contained|ground[- ]floor|top[- ]floor|"
    r"(?:\d|one|two|three|four)\s*-?\s*bed(?:room)?(?:ed)?)\s+){{0,3}}"
    r"(?:flats?|apartments?|studios?|houses?|homes?|places?|maisonettes?|(?:\d|one|two|three|four)\s*-?\s*bed"
    r"(?:room)?s?\b)")
_SOFT_WORDING = re.compile(
    r"\b(?:ideally|preferably|if\s+possible|would\s+(?:like|prefer|be\s+nice)|nice\s+to\s+have|"
    r"bonus|not\s+essential|don.t\s+mind|either\s+way|prefer)\b|最好|如果可以|优先", re.I)


def furnished_requirement_update(text: str) -> bool | None:
    """An explicit FURNISHED requirement (True), its withdrawal (False), else None (R2-16).

    "Furnished only please - I don't want anything unfurnished." and "It must be furnished."
    stayed a soft preference, so an Eden Grove listing with no furnishing evidence was shown
    under "Meets all declared conditions (verified)". Like a pet requirement (R1-06), the
    requirement needs evidence. Wanting UNfurnished is a different requirement and is not
    read here."""
    for match in re.finditer(r'[^。.!?;；\n]+[。.!?;；\n]*', text or ''):
        clause = match.group()
        if not re.search(r'furnish|家具|家私', clause, re.I):
            continue
        if zh_feature_withdrawn(clause, ('家具', '家私')):
            return False
        if re.search(r"\b(?:unfurnished|furnished)\s+(?:is\s+)?(?:fine|ok(?:ay)?)\b|"
                     r"\b(?:furnish\w*|furniture)\b[^.!?]{0,25}\b(?:doesn.t|does not|don.t|do not)\s+"
                     r"(?:matter|need|mind)|\b(?:don.t|do not|no longer)\s+(?:need|require|care about)\s+"
                     r"(?:it\s+(?:to\s+be\s+)?)?furnish|家具无所谓|不需要家具|不用带家具|不必带家具", clause, re.I):
            return False
        if is_hypothetical(clause) or re.search(r'[?？]|是否|吗|\bwhether\b', clause, re.I):
            continue
        wants_unfurnished = re.search(r'\b(?:want|need|prefer|must\s+be|only)\s+(?:an?\s+)?unfurnished\b|'
                                      r'\bunfurnished\s+only\b', clause, re.I)
        if wants_unfurnished:
            continue
        if re.search(_ATTRIBUTE_OF_HOME.format(word=r"(?:fully\s+)?furnished"), clause, re.I) \
                and not _SOFT_WORDING.search(clause):
            # "Furnished pet-friendly 1 bed with a garden in Camden" names the attribute of
            # the home it asks for, like "it must be furnished" (R3-07).
            return True
        if re.search(r"\bfurnished\s+only\b|\bonly\s+(?:fully\s+)?furnished\b|"
                     r"\b(?:must|has\s+to|have\s+to|needs?\s+to|should)\s+be\s+(?:fully\s+)?furnished\b|"
                     r"\b(?:don.t|do\s+not)\s+want\s+(?:anything\s+|an?\s+)?unfurnished\b|"
                     r"\bno\s+unfurnished\b|\b(?:need|require)s?\s+(?:an?\s+)?(?:fully\s+)?furnished\b|"
                     r"必须(?:带|有)家具|(?:要|需要)(?:带|有)家具|只要(?:带)?家具", clause, re.I):
            return True
    return None


def pet_soft_wish(text: str) -> bool:
    """"I'd like pets allowed if possible": no filter, but listed as not checked (V-08)."""
    for match in re.finditer(r'[^。.!?;；\n]+[。.!?;；\n]*', text or ''):
        clause = match.group()
        if (_SOFT_WORDING.search(clause) and re.search(
                r'\b(?:pets?|cats?|dogs?)\s+(?:are\s+)?(?:allowed|welcome|accepted|ok(?:ay)?)\b|'
                r'\bpet[- ]friendly\b|允许(?:养)?(?:宠物|猫|狗)|可以养|能养|可养', clause, re.I)
                and not is_hypothetical(clause) and not re.search(r'[?？]|是否|吗|\bwhether\b', clause, re.I)):
            return True
    return False


def pet_requirement_update(text: str) -> bool | None:
    """A pet-permission requirement or its withdrawal, in the user's own words.

    True: "It must allow pets." / "Find a 1-bed in Hackney that allows pets" — the home
    has to permit pets, so a listing without that evidence is unverified, never a
    verified match (R1-06). False: "Actually forget the pets thing, I no longer have a
    cat." — every pet requirement and pet preference goes (R1-05). None: unchanged.
    Cats keep their own, stricter slot (``cat_permission_update``)."""
    for match in re.finditer(r'[^。.!?;；\n]+[。.!?;；\n]*', text or ''):
        clause = match.group()
        if not re.search(_PET_WORD, clause, re.I):
            continue
        if _PET_WITHDRAWAL.search(clause) or zh_feature_withdrawn(clause, ('宠物', '猫', '狗')):
            return False
        if is_hypothetical(clause) or re.search(
                r'[?？]|是否|吗|\b(?:do not assume|don.t assume|unknown|whether)\b', clause, re.I):
            continue
        if re.search(r'\b(?:no|without|don.t want|do not want)\s+(?:any\s+)?(?:pets?|dogs?|cats?)\b', clause, re.I):
            continue    # "no pets in the building please" is not a permission requirement
        permission = re.search(r'\b(?:allow(?:s|ed|ing)?|accept(?:s|ed|ing)?|permit(?:s|ted)?|friendly|ok(?:ay)?\s+with)\b'
                               r'|允许|可以养|能养|接受|可养', clause, re.I)
        if not permission:
            continue
        hard = re.search(r'\b(?:must|has\s+to|have\s+to|required?|requires|essential|mandatory|only)\b'
                         r"|(?<!no\s)(?<!t\s)\bneeds?\b|必须|一定要|需要|要求", clause, re.I)
        restrictive = (re.search(r'\b(?:that|which|where)\s+(?:allows?|accepts?|permits?)\s+(?:pets?|cats?|dogs?)\b'
                                 # "... with a garden and pets allowed" (V-08)
                                 r'|\b(?:pets?|cats?|dogs?)\s+(?:are\s+)?(?:allowed|welcome|accepted|ok(?:ay)?)\b'
                                 r'|\bpet[- ]friendly\s+(?:flat|home|place|property|apartment|studio|house|\d|one|two)'
                                 r'|允许养|可以养|能养|可养', clause, re.I)
                       and re.search(r'\b(?:find|search|show|look(?:ing)?\s+for|want|need)\b|找|搜|要', clause, re.I))
        if hard or restrictive:
            return True
        if re.search(_ATTRIBUTE_OF_HOME.format(word=r"pet[- ]friendly"), clause, re.I) \
                and not _SOFT_WORDING.search(clause):
            return True     # "Pet-friendly furnished 1 bed in Hackney" (R3-07)
    return None


def precise_property_requirements(text: str, required: list[str]) -> list[str]:
    """Keep explicitly stated fit/written-permission requirements independently.

    A wheelchair's stated width is not a measured doorway clearance. The fit tag
    remains unknown until a source verifies usable space for that wheelchair.
    """
    from decimal import Decimal
    features = []
    if 'wheelchair-accessible' in required:
        width = None
        patterns = (
            r"\bwheelchair\s+(?:(?:that(?:'s| is)|is|measures|with a width of)\s+)?(\d{1,3}(?:\.\d+)?)\s*(?:cm|centimet(?:er|re)s?)\s+wide\b",
            r"\b(\d{1,3}(?:\.\d+)?)\s*(?:cm|centimet(?:er|re)s?)\s+(?:wide\s+)?wheelchair\b",
            r'轮椅\s*宽(?:度)?(?:为|是)?\s*(\d{1,3}(?:\.\d+)?)\s*(?:cm|厘米)',
        )
        for pattern in patterns:
            match = re.search(pattern, text or '', re.I)
            if match:
                width = format(Decimal(match.group(1)).normalize(), 'f')
                break
        if width:
            features.append(f'wheelchair_space_{width}cm')
    if 'cats allowed' in required and re.search(r'\bwritten (?:consent|permission)\b|\b(?:consent|permission)\b[^.!?;]{0,25}\bin writing\b|书面(?:许可|同意)|业主书面', text or '', re.I):
        features.append('cat_written_consent')
    return features


def extract_office_days(text: str) -> int | None:
    """Read a declared office frequency or named office weekdays, never WFH days."""
    words = dict(one=1, two=2, three=3, four=4, five=5, six=6, seven=7)
    weekday = r'(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)'
    indexes = dict(mon=0, tue=1, wed=2, thu=3, fri=4, sat=5, sun=6)
    for clause in re.split(r'[。.!?;；\n]', text or ''):
        if not re.search(r'\b(?:office|commut\w*|workplace|on.site)\b|\bi work (?:at|in|near)\b|到岗|到公司|通勤', clause, re.I):
            continue
        # WFH occupies a different predicate even when linked with "and".
        office = re.split(r'\band (?:i )?(?:work(?:ing)? from home|wfh)\b|其余.*(?:在家|居家)', clause, maxsplit=1, flags=re.I)[0]
        match = re.search(r'\b([1-7]|one|two|three|four|five|six|seven)\s+(?:office\s+)?days?\s+(?:a|per)\s+week\b', office, re.I)
        if match:
            value = match.group(1).lower()
            return int(value) if value.isdigit() else words[value]
        match = re.search(rf'\b({weekday})\s*(?:to|through|[-–—])\s*({weekday})\b', office, re.I)
        if match:
            first, last = (indexes[match.group(i).lower()[:3]] for i in (1, 2))
            return (last - first) % 7 + 1
        named = {indexes[m.group().lower()[:3]] for m in re.finditer(rf'\b{weekday}\b', office, re.I)}
        if named:
            return len(named)
    return None


def extract_max_changes(text: str) -> int | None:
    """Explicit transfer upper bound. None means absent, zero means direct only."""
    words = dict(zero=0, one=1, two=2, three=3)
    match = re.search(r'\b(?:at most|no more than|up to|maximum(?: of)?)\s+(\d|zero|one|two|three)\s+(?:changes?|transfers?)\b', text or '', re.I)
    if match:
        value = match.group(1).lower()
        return int(value) if value.isdigit() else words[value]
    if re.search(r'\b(?:direct only|no (?:changes?|transfers?))\b|不换乘|无需换乘', text or '', re.I):
        return 0
    match = re.search(r'(?:最多|至多|不超过)\s*([0-3零一二两三])\s*次?换乘', text or '')
    if match:
        value = match.group(1)
        return int(value) if value.isdigit() else {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3}[value]
    return None


def canonical_criteria(criteria: dict) -> dict:
    """Project the residential and destination aliases from their canonical slots.

    Missing keys carry no update. Explicit None remains a clear, including stale
    legacy aliases; ordinary tool payloads cannot decide to clear user criteria.
    """
    result = dict(criteria or {})
    if 'areas' in result and isinstance(result['areas'], list):
        result['areas'] = list(dict.fromkeys(a.strip() for a in result['areas'] if isinstance(a, str) and a.strip()))
        result['area'] = result['areas'][0] if result['areas'] else None
    elif result.get('area'):
        result['areas'] = [result['area']]
    if 'commute_destination' in result:
        result['destination'] = result['commute_destination']
    elif 'destination' in result:
        result['commute_destination'] = result['destination']
    return result


def protect_confirmed_criteria(result: dict, confirmed: dict) -> dict:
    """Tool extraction is evidence, not authority to revise user-confirmed slots."""
    result = dict(result or {})
    for key, source in (confirmed.get('constraint_sources') or {}).items():
        if isinstance(source, dict) and source.get('source') in ('user_message', 'saved_preference', 'user_form', 'criteria_recovery') and key in confirmed:
            value = confirmed[key]
            result[key] = list(value) if isinstance(value, list) else value
    # A confirmed area list and destination own both of their compatibility aliases.
    if result.get('no_commute') and result.get('commute_destination') is None and 'commute_destination' not in (confirmed.get('constraint_sources') or {}):
        # Legacy no-commute payloads use destination as the search area only.
        residential = canonical_criteria({k: v for k, v in result.items() if k in ('area', 'areas')})
        result.update(residential)
        return result
    return canonical_criteria(result)


def date_is_cleared(text: str) -> bool:
    return bool(re.search(
        r'(?:取消|清空|删除|去掉|不(?:要|需要))(?:任何)?(?:入住|起租|搬入)(?:日期|时间)?'
        r'|(?:remove|clear|drop|no) (?:the |any )?(?:move.in|tenancy.start|start) date'
        r'|(?:入住|起租|搬入)(?:日|日期|时间)(?:暂(?:时)?|尚|还)?(?:未确定|未定|没确定|不确定|待定)', text, re.I))


def cleared_fields(text: str) -> set[str]:
    result = set()
    for field, names in {
        'room_type': r'房型|room.type',
        'bedrooms': r'卧室(?:数(?:量)?)?|bedrooms?',
        'property_features': r'设施|房屋特征|property.features|amenities',
        'area': r'(?:居住|搜索)?区域|(?:residential |search )?areas?',
        'commute_destination': r'通勤目的地|commute destination',
    }.items():
        if re.search(r'(?:取消|清空|删除|去掉|不限|不限制)[^，。;；]{0,6}(?:' + names + r')'
                     r'|(?:remove|clear|drop|no) (?:the |any )?(?:' + names + r')(?: filter| limit| requirement)?', text, re.I):
            result.add(field)
    return result


# A whole topic withdrawn in one phrase (R4-14): "Forget everything we have said in this
# conversation about location." changed nothing, so view_state kept area=Camden and the
# Canary Wharf destination while the reply said "Location: none set", and the next search
# still carried the destination (A-F5). The topic word must be the direct object of the
# withdrawal ("forget [everything we said] [about] [the] location"); "Don't forget the
# area" and "ignore my friend's area" are not withdrawals.
# "Retract the commute again." and "Retract the area" changed nothing (R4-05, R4-02).
_TOPIC_VERB = (r"(?<!n't )(?<!\bnot )(?<!\bnever )"
               r"\b(?:forget|disregard|ignore|never\s*mind|scrap|drop|clear|reset|cancel|ditch|"
               r"retract|withdraw|remove|undo|lose|delete)\b")
_TOPIC_EVERYTHING = (r"(?:\s+about)?(?:\s+(?:everything|anything|all))?"
                     r"(?:\s+(?:that\s+)?(?:we|i|you)(?:\s+(?:have|'ve|had))?\s+"
                     r"(?:said|discussed|talked\s+about|mentioned|covered|agreed)"
                     r"(?:\s+(?:so\s+far|earlier|before|previously|"
                     r"in\s+(?:this|the)\s+(?:conversation|chat)))*)?")
_TOPIC_OBJECT = r"(?:\s+(?:about|regarding|on|re))?(?:\s+(?:the|my|our|any|that|this))?\s+"
_TOPIC_RE = {
    'location': re.compile(_TOPIC_VERB + _TOPIC_EVERYTHING + _TOPIC_OBJECT
                           + r"(?:locations?|where\s+(?:i|we)\s+(?:want\s+to\s+|would\s+|should\s+)?live)\b"
                           r"|\blocations?\s+(?:doesn.t|does\s+not|no\s+longer)\s+matters?\b"
                           r"|(?:忘掉|忘记|忘了|不管|别管|不考虑|不用考虑|不要考虑)[^。，,.!?；;]{0,12}(?:位置|地点|地段)",
                           re.I),
    'area': re.compile(_TOPIC_VERB + _TOPIC_EVERYTHING + _TOPIC_OBJECT
                       + r"(?:areas?|neighbou?rhoods?)\b(?!\s+(?:like|near|around|with)\b)"
                       r"|(?:忘掉|忘记|忘了|不管|别管|不考虑|不用考虑|不要考虑|撤销|撤回|取消|去掉)"
                       r"[^。，,.!?；;]{0,12}(?:区域|地区)"
                       # "I don't care about the area any more, anywhere in London." kept Bow
                       # and answered from the old results as "outside the requested areas"
                       # (R4-02): the user widens the search to anywhere.
                       r"|\b(?:don.t|do\s+not|no\s+longer)\s+care\s+(?:about\s+)?(?:the\s+|which\s+|what\s+)?"
                       r"(?:areas?|neighbou?rhoods?)\b"
                       r"|\b(?:areas?|neighbou?rhoods?)\s+(?:doesn.t|does\s+not|no\s+longer|don.t)\s+matters?\b"
                       r"|\bany\s+(?:area|neighbou?rhood|part\s+of\s+(?:london|the\s+city))\s+"
                       r"(?:is|will\s+do|works)\b"
                       r"|\banywhere(?=\s+in\s+(?:london|the\s+city)\b|\s+(?:is\s+fine|is\s+ok(?:ay)?|will\s+do|"
                       r"works|at\s+all)\b|\s*(?:[.,!;]|$))"
                       r"|哪里都(?:行|可以|好)|哪儿都(?:行|可以|好)|哪都(?:行|可以)|(?:伦敦)?任何(?:地方|区域|地区)都?(?:行|可以)?"
                       r"|(?:区域|地区)(?:无所谓|不限|都行|都可以)|不限(?:区域|地区)",
                       re.I),
    'commute': re.compile(_TOPIC_VERB + _TOPIC_EVERYTHING + _TOPIC_OBJECT
                          + r"(?:commut(?:e|es|ing)|journey\s+to\s+work)\b"
                          r"(?!\s+(?:time|limit|filter|cap|max|times|duration))"
                          # 「撤销通勤要求，在Hackney找」 (R4-04); 取消通勤时间限制 lifts only the limit.
                          r"|(?:撤销|撤回|取消|去掉|删除|删掉)[^。，,.!?；;]{0,6}通勤(?!时间|时长|限制)",
                          re.I),
}


def _withdrawal_spans(text: str) -> list[tuple[str, int, int]]:
    """(topic, start, end) of each topic withdrawal; questions/hypotheticals withdraw nothing."""
    spans: list[tuple[str, int, int]] = []
    value = str(text or '')[:4000]
    for topic, pattern in _TOPIC_RE.items():
        for match in pattern.finditer(value):
            clause = clause_at(value, match.start(), match.end())
            sentence_end = re.search(r'[.!?。！？\n]', value[match.end():match.end() + 300])
            if is_hypothetical(clause) or (sentence_end and sentence_end.group() in '?？'):
                continue
            spans.append((topic, match.start(), match.end()))
    return spans


def withdrawn_topics(text: str) -> set[str]:
    """{'location', 'area', 'commute'} topics this message withdraws as a whole.

    'location' covers where to live AND where the user commutes to; 'area' only the former."""
    return {topic for topic, _start, _end in _withdrawal_spans(text)}


def without_withdrawals(text: str) -> str:
    """``text`` minus its topic withdrawals, so what follows them still applies:
    "Disregard the area, search Stratford." searches Stratford (the withdrawn-clause filter
    of scoped_update_text would otherwise drop the whole sentence)."""
    value = str(text or '')
    spans = sorted((start, end) for _topic, start, end in _withdrawal_spans(value))
    if not spans:
        return value
    out, position = [], 0
    for start, end in spans:
        if start < position:
            continue
        out.append(value[position:start])
        position = end
    out.append(value[position:])
    return ''.join(out)


_STUDIO_OR_COUNT = re.compile(
    r"\bstudios?\s+or\s+(?:an?\s+)?(?P<after>(?:\d|one|two)[\s-]*bed(?:room)?s?(?:\s+(?:flat|apartment))?)\b"
    r"|\b(?P<before>(?:\d|one|two)[\s-]*bed(?:room)?s?(?:\s+(?:flat|apartment))?)\s+or\s+(?:an?\s+)?studios?\b", re.I)


def studio_alternative_note(text: str) -> str:
    """The count a "studio or a 1-bed" leaves unsearched, as a soft note, or ""."""
    match = _STUDIO_OR_COUNT.search(str(text or ''))
    if not match or is_hypothetical(clause_at(match.string, match.start(), match.end())):
        return ''
    return f"Also open to a {' '.join((match.group('after') or match.group('before')).split())}"


# "Reset all my criteria and start from scratch." left every slot standing: the reset words
# named no field, so nothing read them (U-06). They name the whole search, not one field: "clear
# my budget" and "reset the search area" are not this, and long-term memory is not touched.
_CRITERIA_NOUN = (r"(?:(?:search\s+)?(?:criteria|criterion|filters?|preferences|requirements|conditions|"
                  r"constraints|settings)\b|search\b(?!\s+(?:area|radius|budget|location|distance)))")
_RESET_DETERMINER = (r"(?:my|the|our|these|those|your|this)?\s*(?:current\s+|saved\s+)?")
# "start over in Hackney" resets; "start again in May" is a date.
_RESET_ENDS = (r"(?=\s*(?:$|[.,!?;:，。！？：]|and\b|with\b|please\b|(?:in|from|around|near)\s+"
               r"(?!(?:the\s+)?(?:\d|new\b|next\b|(?:spring|summer|autumn|fall|winter|january|february|"
               r"march|april|may|june|july|august|sept?(?:ember)?|october|november|december|monday|"
               r"tuesday|wednesday|thursday|friday|saturday|sunday)\b))))")
_RESET_ALL_CRITERIA = re.compile(
    r"\b(?:reset|restart|clear|wipe|scrap|erase)\s+(?:out\s+)?(?:all\s+(?:of\s+)?)?" + _RESET_DETERMINER + _CRITERIA_NOUN
    + r"|\b(?:forget|remove|delete|drop)\s+all\s+(?:of\s+)?" + _RESET_DETERMINER + _CRITERIA_NOUN
    + r"|\b(?:reset|clear|wipe)\s+(?:out\s+)?everything\b(?!\s+(?:about|you|i\b))"
    r"|\b(?:(?:start|begin)(?:ing)?\s+(?:all\s+)?(?:again|over|afresh|anew)\b"
    r"(?:\s+from\s+(?:the\s+beginning|scratch))?|(?<!the\s)(?<!a\s)(?<!after\s)restart(?:ing)?\b)" + _RESET_ENDS +
    r"|\b(?:start|begin|search|do\s+(?:it|this))\s+(?:all\s+over\s+)?from\s+scratch\b"
    r"|重新开始(?!上|工作|读|学|租约|计时)|从头(?:开始|来)|从零开始|全部重来|重来一次|重新来过?"
    r"|(?:清空|重置|清除|清掉)(?:掉|一下)?(?:我的|所有的?|全部的?)*(?:搜索|筛选|查询|找房)?的?"
    r"(?:条件|筛选|要求|标准|偏好|设置|搜索)"
    r"|(?:删除|删掉|取消|去掉)(?:掉|一下)?(?:我的)?(?:所有的?|全部的?)(?:搜索|筛选|查询|找房)?的?(?:条件|筛选|要求|标准|偏好)",
    re.I)
_RESET_NEGATED = re.compile(
    r"(?:\b(?:don['’]?t|do\s+not|never|not\s+to|without)\s+(?:\w+\s+){0,2}|不要|别|不用|不想|无需|不必)$", re.I)
_RESET_QUESTION = re.compile(r"^\s*(?:how|what|why|when|where|does|do|did|should|will|is|are)\b|怎么|如何|为什么", re.I)


def _criteria_reset_spans(text: str) -> list[tuple[int, int]]:
    value = str(text or '')[:4000]
    spans = []
    for match in _RESET_ALL_CRITERIA.finditer(value):
        clause = clause_at(value, match.start(), match.end())
        if (is_hypothetical(clause) or _RESET_QUESTION.search(clause)
                or _RESET_NEGATED.search(value[:match.start()])):
            continue
        spans.append(match.span())
    return spans


def resets_all_criteria(text: str) -> bool:
    """True when this message resets the whole search ("Clear my filters", 清空条件)."""
    return bool(_criteria_reset_spans(text))


def history_after_reset(history: list, message: str = '') -> list:
    """What the model may still read of ``history``: nothing from before the last reset. "Can we
    start from scratch with Hackney?" left a clean state, and the old turns gave the model "the
    filters I sent (1-bed, under £2,200)" to write about and a user_query to fill in, past two
    instructions not to (D-04, F-01). The reset turn itself stays: it states what comes next."""
    turns = list(history or [])
    if resets_all_criteria(message):
        return []
    for index in range(len(turns) - 1, -1, -1):
        if isinstance(turns[index], dict) and resets_all_criteria(turns[index].get('user') or ''):
            return turns[index:]
    return turns


# What opens the rest of a reset: "start from scratch with Hackney" names a place, which only
# the words after a cue reveal once the reset is gone (V-02). "with a 2-bed" is no place.
_RESET_PLACE_LEAD = re.compile(r"\s*(?:with|from|using)\s+(?!(?:a|an|the|my|our|\d+)\b)", re.I)


def without_criteria_reset(text: str) -> str:
    """``text`` minus its reset phrases, so what follows still applies: "Start again, 2-bed in
    Hackney." searches that, and "Start from scratch with Hackney" searches Hackney."""
    value = str(text or '')
    out, position = [], 0
    for start, end in _criteria_reset_spans(value):
        out.append(value[position:start])
        position = end
        lead = _RESET_PLACE_LEAD.match(value, end)
        if lead:
            out.append(' in ')
            position = lead.end()
    out.append(value[position:])
    return ''.join(out)


# "commute to UCL not needed" stored UCL as the destination (W-04): the place is named only to say
# that the user does not go there.
_COMMUTE_DISOWNED = re.compile(
    r"\bcommut\w*(?:\s+(?:time\s+)?(?:to|into)\s+[^,.;!?\n]{1,40}?)?\s+(?:is\s+|are\s+)?"
    r"(?:(?:not|no\s+longer)\s+(?:needed|required|necessary|relevant|important)"
    r"|isn.t\s+(?:needed|required|necessary|relevant|important)|unnecessary)\b"
    r"|\b(?:no|without\s+a|(?:don.t|do\s+not)\s+need\s+a)\s+commut\w*\s+(?:to|into)\b"
    r"|\b(?:don.t|do\s+not|won.t|will\s+not|no\s+need\s+to)\s+(?:need\s+to\s+|have\s+to\s+)?commute\s+(?:to|into)\b"
    r"|(?:不用|不需要|无需|不必)通勤(?:到|去)", re.I)


def commute_disowned(text: str) -> bool:
    """True when the message says the commute it names does not apply."""
    return bool(_COMMUTE_DISOWNED.search(str(text or '')[:4000]))


def commute_withdrawal_drops_destination(text: str) -> bool:
    """A no-commute statement withdraws the destination too, unless it only lifts the limit.

    "Never mind about commute." / "forget the commute, I will work from home" leave nothing to
    commute to (NEW-4); "remove the commute limit" / "取消通勤时间" keep the destination for the
    per-listing travel times."""
    return not re.search(
        r'\bcommut\w*\s+(?:times?|durations?|limits?|filters?|caps?|max(?:imum)?|thresholds?)\b'
        r'|\btime\s+limit\b'
        r'|通勤(?:时间|时长)|时间限制|不限制通勤', str(text or ''), re.I)


def lifts_only_commute_limit(text: str) -> bool:
    """True when the message lifts the commute TIME LIMIT and does not say the user stops
    commuting: "Forget the commute limit", "取消通勤时间限制" (R3-11)."""
    value = str(text or '')
    if re.search(r"\b(?:don.t|do\s+not|won.t|never|no\s+longer)\s+(?:need\s+to\s+)?commute\b|"
                 r"\bno\s+commut\w*\s+(?:needed|required|at\s+all)\b|\bwork(?:ing)?\s+from\s+home\b|"
                 r"\bwfh\b|\bremote(?:ly)?\b|不(?:需要|用)?通勤|在家(?:办公|工作)", value, re.I):
        return False
    return not commute_withdrawal_drops_destination(value)


def withdrawn_by_user(accumulated: dict, field: str) -> bool:
    """True when the user (message or form) explicitly cleared ``field`` and it is still clear."""
    acc = accumulated if isinstance(accumulated, dict) else {}
    source = (acc.get('constraint_sources') or {}).get(field)
    return (acc.get(field) in (None, [], '') and isinstance(source, dict)
            and source.get('value') in (None, [], '')
            and source.get('source') in ('user_message', 'user_form', 'form', 'manual'))


def _names_place(message: str, place: str) -> bool:
    key = re.sub(r"[’']", '', str(place or '').split(',')[0].strip().lower())
    text = re.sub(r"[’']", '', str(message or '').lower())
    return bool(key) and re.search(r'(?<![a-z])' + re.escape(key) + r'(?![a-z])', text) is not None


def drop_withdrawn_places(params: dict, accumulated: dict, message: str) -> tuple[dict, list[str]]:
    """Remove a model-supplied destination / legacy location the user withdrew (R4-14).

    After "Forget everything we have said about location." the model still sent the old
    Canary Wharf destination from the conversation it remembers. A place this turn's
    message names again is the user's choice and stays."""
    p = dict(params or {})
    dropped = []
    if commute_disowned(message) and isinstance(p.get('commute_destination'), str):
        p.pop('commute_destination', None)      # "commute to UCL not needed" names it to refuse it
        dropped.append('commute_destination')
    for field, cleared in (('commute_destination', 'commute_destination'), ('location', 'area')):
        value = p.get(field)
        if (isinstance(value, str) and value.strip() and withdrawn_by_user(accumulated, cleared)
                and not _names_place(message, value)):
            p.pop(field, None)
            dropped.append(field)
    return p, dropped


# A question about how far a SHOWN listing is from a place (R3-10): "Out of curiosity how
# far is the first one from UCL?" set UCL as the commute destination for every later
# search, over an explicit "no commute needed", and "第一套离国王十字远吗？" moved the search
# area to King's Cross. The place is the end of one journey, not a criterion.
_LISTING_DEICTIC = re.compile(
    r"\b(?:the\s+)?(?:first|second|third|fourth|fifth|last|cheapest|top|other)\s+"
    r"(?:one|flat|listing|place|property|home|option|studio)\b"
    r"|\b(?:this|that|these|those)\s+(?:one|ones|flats?|listings?|places?|propert(?:y|ies)|homes?|apartments?)\b"
    r"|\b(?:number|no\.|#)\s*\d\b|\bis\s+it\b|\bare\s+they\b|\bit\s+(?:to|from)\b"
    r"|第[一二三四五六七八九十\d]+(?:套|个|间|处)|[这那](?:套|个|间|处|几套|些)", re.I)
_DISTANCE_QUESTION = re.compile(
    r"\bhow\s+(?:far|long|close|near)\b|\b(?:distance|far|close|near)\s+(?:away\s+)?(?:from|to)\b"
    r"|\b(?:commute|journey|trip)\s+(?:time\s+)?(?:from|to)\b"
    r"|离[^，。？?！!]{1,20}(?:远|近|多远|多久|多长时间)|到[^，。？?！!]{1,20}(?:要)?(?:多久|多长时间|多远|远吗|近吗)"
    # 「从这些房子到伦敦大学学院哪个最近？」 compares the listings; it states no commute (U-09).
    r"|到[^，。？?！!]{1,20}(?:哪个|哪套|哪间|哪一个|哪一套)[^，。？?！!]{0,4}(?:最近|最远|最快|更近|更快)", re.I)
_OWN_COMMUTE_STATEMENT = re.compile(
    r"\b(?:i|we)\s+(?:\w+\s+)?(?:work|study|studies|commute|go\s+to\s+work)\b"
    r"|\bmy\s+(?:office|workplace|work|job|school|university|college|campus|commute)\b"
    r"|我[^，。？?！!]{0,8}(?:上班|上学|工作|通勤|读书|实习)", re.I)


def one_off_distance_question(text: str) -> bool:
    """True when the message asks how far a shown listing is from a place and states no
    commute of the user's own."""
    value = str(text or '')[:2000]
    if _OWN_COMMUTE_STATEMENT.search(value):
        return False
    for sentence in re.split(r'(?<=[!?？。！])\s*|(?<=\.)\s+', value):
        question = (sentence.rstrip().endswith(('?', '？'))
                    or re.match(r'\s*(?:out\s+of\s+curiosity,?\s+)?(?:how|is|are|what|which)\b', sentence, re.I)
                    or re.search(r'[吗呢]\s*$|多远|多久', sentence))
        if question and _DISTANCE_QUESTION.search(sentence) and _LISTING_DEICTIC.search(sentence):
            return True
    return False


_TRANSPORT_QUESTION = re.compile(
    r"\b(?:fares?|zones?|travel\s*cards?|oyster|tickets?|how\s+(?:do|can|would)\s+i\s+get|"
    r"(?:tube|train|bus|dlr)\s+(?:from|to|between)|transport\s+links?|connected)\b"
    r"|票价|几区|怎么去|交通(?:怎么样|方便吗)", re.I)


def one_off_transport_question(text: str) -> bool:
    """True when the message asks about a fare, zone or journey and states no commute of the
    user's own: "What are the fares and zones for Clapham to Canary Wharf?" made Canary
    Wharf the search's commute destination for the rest of the conversation (R4-06)."""
    value = str(text or '')[:2000]
    if _OWN_COMMUTE_STATEMENT.search(value):
        return False
    for sentence in re.split(r'(?<=[!?？。！])\s*|(?<=\.)\s+', value):
        question = (sentence.rstrip().endswith(('?', '？'))
                    or re.match(r'\s*(?:how|is|are|what|which|does|do)\b', sentence, re.I)
                    or re.search(r'[吗呢]\s*$', sentence))
        if question and _TRANSPORT_QUESTION.search(sentence):
            return True
    return False


def independent_memory_task(message: str) -> bool:
    """A positive independent request survives a memory-only turn boundary.

    Negated search/compare predicates do not become tasks merely because their
    verb occurs. Contrast/please clauses remain separate, preserving real mixed
    requests such as "do not search, but compare these two areas".
    """
    action = (r'复述|总结|摘要|逐项|列出|解释|恢复|怎么|如何|计算|比较|推荐|搜索|找房'
              r'|\b(?:summari[sz]e|recap|repeat|list|explain|restore|recover|calculate|compare|find|search|show)\b')
    ancillary_topic = (r'天气|气温|天气预报|票价|线路状态|服务状态|'
                       r'\b(?:weather|forecast|temperature|fares?|line status|service status|disruptions?)\b')
    ancillary_action = (r'(?:查(?:询|看)?|看看|核实|获取|\b(?:check|look up|get|what|how)\b)'
                        r'(?=[^。.!?;；\n，,]{0,65}(?:' + ancillary_topic + r'))')
    action = '(?:' + action + ')|(?:' + ancillary_action + ')'
    for clause in re.split(r'[。.!?;；\n，,]|\bbut\b|\b(?:and|then) please\b|但是|不过|但|并(?:且)?请', message or '', flags=re.I):
        for match in re.finditer(action, clause, re.I):
            prefix = clause[:match.start()]
            if re.search(r'(?:不要|别|无需|不用|不必)[^，。;；!?]{0,14}$'
                         r'|\b(?:do not|don.t|never|no need to|no)\s+(?:\w+\s+){0,8}$', prefix, re.I):
                continue
            return True
    return False
