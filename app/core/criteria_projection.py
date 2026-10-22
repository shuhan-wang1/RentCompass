"""What the browser may see of where a criterion came from (r6-C1).

``constraint_sources[field]['text']`` keeps, on the server, the user's message a value was
parsed from (up to 800 characters). Server-side logic depends on that full text:
``criteria_recovery`` re-parses it and matches it against the conversation's stored
messages, and ``criteria_contract.synchronize_monthly_budget_alias`` re-parses it to prove
that a ``user_message`` budget is the user's own figure. It therefore stays in the snapshot,
the session and the checkpoints unchanged.

It used to leave the server too, verbatim, in ``view_state.criteria`` and the response's
``search_criteria`` — including the clauses the memory and criteria layers deliberately
keep out (R4-08 / NEW-E: income, benefits, health, disability, immigration, contact
details, children's names and schools). r10 rid 93630cf9 shipped "I use a wheelchair, I'm
on Universal Credit,. Find a step-free 1-bed in Camden under £1,800." eight times in one
payload. The frontend reads nothing from it.

The browser projection carries, per source, only the sentence that supports that field's
value, with every sensitive clause removed (the same clause classifier as automatic
memory, ``rag.agent_memory.redact_sensitive``), bounded to ``_MAX_EXCERPT`` characters. When
no sentence can be identified, or nothing non-sensitive is left, the excerpt is omitted.
Deterministic, offline and regex-only: no parser run, so a projection costs microseconds.
"""
from __future__ import annotations

import re
from copy import deepcopy

_MAX_EXCERPT = 200
_MARKER = re.compile(r"\[(?:personal|contact) detail not stored\]")
# A full stop after a common abbreviation, or inside a number, does not end a sentence.
_ABBREVIATION = re.compile(r"\b(St|Dr|Mt|Rd|Ave|Sq|Mr|Mrs|Ms|No|approx|vs)\.", re.I)
_SENTENCE_END = re.compile(r"[!?;。！？；\n]+|\.(?!\d)")
_CLAUSE_BREAK = re.compile(r"[,，、;；]|\s[–—-]\s")
_CONTENT = re.compile(r"[0-9A-Za-z一-鿿]")

# Words that name a slot's topic, used when the value itself is not quoted in the text
# (a weekly budget stored monthly, a bedroom count, a cleared field).
_CUES = {
    "budget": (r"£|\$|€|\d\s*k\b|\bp\.?c\.?m\b|\bp/?w\b|\bp/?m\b|\bbudget|\brent\b|\bmonthly\b|"
               r"\bweekly\b|\bper\s+(?:calendar\s+)?(?:month|week)\b|\ba\s+(?:month|week)\b|"
               r"/\s*(?:month|mo|week|wk)\b|\bprice|\bcost|\bafford|\bspend|"
               r"预算|英镑|镑|租金|月租|每月|每周"),
    "rooms": (r"\bstudios?\b|\bbed(?:room)?s?\b|\d\s*-?\s*b(?:ed|r)\b|"
              r"\b(?:one|two|three|four|five|six)[- ]bed|\brooms?\b|\bshar(?:e|ed|ing)\b|"
              r"flat\s?share|house\s?share|en[- ]?suite|\bwhole\b|\bentire\b|self[- ]contained|"
              r"\bflat\b|\bapartment\b|\bhouse\b|卧|居室|整租|合租|单间|开间"),
    "commute": (r"commut|\bwork(?:s|ing)?\b|\boffice\b|\bjob\b|\bjourney|\btravel|\bminutes?\b|"
                r"\bmins?\b|\bhours?\b|\bremote(?:ly)?\b|\bwfh\b|\bfrom home\b|\bdays?\b|"
                r"\bchanges?\b|\binterchanges?\b|\bdirect\b|\bstation\b|\bget to\b|"
                r"通勤|上班|办公|分钟|小时|换乘|天"),
    "date": (r"\bmov(?:e|ing)\b|\bstart|\bfrom\b|\bavailab|\basap\b|\bimmediately\b|"
             r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
             r"\d{1,2}(?:st|nd|rd|th)\b|\d{4}-\d{2}-\d{2}|\bnext\s+(?:week|month)|入住|搬|月|号"),
    "area": (r"\bareas?\b|\bin\b|\bnear\b|\baround\b|\blocation|\bneighbou?rhood|\bplace\b|"
             r"\bsearch|\blook|区|附近|地区|位置"),
    "features": (r"\blifts?\b|\belevators?\b|step[- ]?free|level access|wheelchair|accessib|"
                 r"ground floor|\bpets?\b|\bcats?\b|\bdogs?\b|furnish|furniture|garden|balcon|"
                 r"parking|\bbills\b|washing|dishwasher|\bquiet|\bgyms?\b|\bparks?\b|"
                 r"宠物|猫|狗|电梯|无障碍|家具|阳台|花园|停车"),
}
_CUE_RES = {name: re.compile(pattern, re.I) for name, pattern in _CUES.items()}
_FIELD_TOPIC = {
    "max_budget": "budget", "max_budget_monthly": "budget", "budget_period": "budget",
    "monthly_total_budget": "budget",
    "bedrooms": "rooms", "bedrooms_minimum": "rooms", "room_type": "rooms",
    "commute_destination": "commute", "destination": "commute", "max_travel_time": "commute",
    "max_commute_time": "commute", "no_commute": "commute", "office_days_per_week": "commute",
    "max_changes": "commute",
    "move_in_date": "date",
    "area": "area", "areas": "area",
    "property_features": "features", "soft_preferences": "features",
}
# Value words that say nothing about where the value came from.
_FILLER_WORDS = frozenset(
    "allowed required must have near with would like prefer preferred need needs access "
    "friendly good nice close the and for".split())


def _key(text: str) -> str:
    return re.sub(r"[’']", "", str(text or "").casefold())


def _sentences(text: str) -> list[str]:
    protected = _ABBREVIATION.sub(lambda m: m.group(1) + "\x00", text)
    out, start = [], 0
    for match in _SENTENCE_END.finditer(protected):
        out.append(protected[start:match.end()])
        start = match.end()
    out.append(protected[start:])
    return [s.replace("\x00", ".").strip() for s in out if _CONTENT.search(s)]


def _value_patterns(field: str, value) -> list[re.Pattern]:
    """Patterns matching a literal statement of ``value`` (normalised text)."""
    patterns: list[re.Pattern] = []
    topic = _FIELD_TOPIC.get(field)
    if (topic == "budget" and isinstance(value, (int, float)) and not isinstance(value, bool)
            and value >= 50 and float(value).is_integer()):
        amount = int(value)
        forms = {str(amount), f"{amount:,}"}
        if amount >= 1000 and amount % 100 == 0:
            forms.add(f"{amount / 1000:g}k")
        patterns.append(re.compile(r"(?<![\d.,])(?:" + "|".join(re.escape(f) for f in forms)
                                   + r")(?![\d])"))
        return patterns
    if field in ("room_type", "budget_period") or topic is None:
        return patterns
    values = [value] if isinstance(value, str) else (
        [v for v in value if isinstance(v, str)] if isinstance(value, list) else [])
    for item in values:
        item = _key(item).strip()
        if not item:
            continue
        terms = {item, item.split(",")[0].strip()}
        if topic == "features":
            terms |= {word.rstrip("s") for word in re.findall(r"[a-z]{3,}", item)
                      if word not in _FILLER_WORDS}
        for term in terms:
            if len(term) >= 3:
                patterns.append(re.compile(r"(?<![a-z0-9])" + re.escape(term)
                                           + ("" if topic == "features" else r"(?![a-z0-9])")))
    return patterns


def _first_match(patterns, sentence: str) -> int | None:
    """Position (in ``sentence``) of the earliest literal statement of the value, else None."""
    chars, index = [], []
    for position, char in enumerate(sentence):
        if char in "’'":
            continue
        for lowered in char.lower():
            chars.append(lowered)
            index.append(position)
    normal = "".join(chars)
    found = [m.start() for m in (p.search(normal) for p in patterns) if m]
    return index[min(found)] if found and index else None


def _narrow(sentence: str, position: int | None) -> str:
    """A long sentence is cut to the clause around the supporting words."""
    if len(sentence) <= _MAX_EXCERPT or not position:
        return sentence
    start = 0
    for brk in _CLAUSE_BREAK.finditer(sentence, 0, position):
        start = brk.end()
    return sentence[start:].strip()


def _without_sensitive(text: str) -> str | None:
    try:
        from rag.agent_memory import redact_sensitive
    except Exception:
        return None               # no classifier, no excerpt: fail closed
    redacted, categories = redact_sensitive(text)
    value = _MARKER.sub(" ", redacted) if categories else redacted
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"([,;:，；：])(?:\s*[,;:，；：])+", r"\1", value)       # ", ," -> ","
    value = re.sub(r"[,;:，；：]\s*(?=[.!?。！？])", "", value)             # ",." -> "."
    value = re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", value)
    value = re.sub(r"^[\s,;:，；：.–—-]+|[\s,;:，；：–—-]+$", "", value)
    value = re.sub(r"^(?:and|but|so)\s+|\s+(?:and|but|so)$", "", value, flags=re.I).strip(" ,;:")
    if not _CONTENT.search(value) or redact_sensitive(value)[1]:
        return None
    if len(value) > _MAX_EXCERPT:
        value = value[:_MAX_EXCERPT].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return value


def source_excerpt(field: str, value, text) -> str | None:
    """The sentence of ``text`` that supports ``field = value``, sensitive clauses removed."""
    if not isinstance(text, str) or not text.strip():
        return None
    sentences = _sentences(text[:2000])
    if not sentences:
        return None
    patterns = _value_patterns(field, value)
    cue = _CUE_RES.get(_FIELD_TOPIC.get(field, ""))
    candidates = []          # (rank, sentence, position of the supporting words)
    for sentence in sentences:
        position = _first_match(patterns, sentence) if patterns else None
        if position is not None:
            candidates.append((0, sentence, position))
        elif cue is not None:
            cue_match = cue.search(sentence)
            if cue_match:
                candidates.append((1, sentence, cue_match.start()))
    if len(sentences) == 1 and not candidates:
        candidates = [(2, sentences[0], None)]     # the only sentence it can have come from
    if not candidates:
        return None
    best = min(rank for rank, _sentence, _position in candidates)
    # The last supporting sentence wins, as the parser's last statement does.
    for rank, sentence, position in reversed(candidates):
        if rank != best:
            continue
        excerpt = _without_sensitive(_narrow(sentence, position))
        if excerpt:
            return excerpt
    return None


def _project_source(field: str, source):
    if not isinstance(source, dict):
        return deepcopy(source)
    projected = {}
    for key, item in source.items():
        if key == "text":
            excerpt = source_excerpt(field, source.get("value"), item)
            if excerpt:
                projected["text"] = excerpt
        elif key == "original_source":
            projected[key] = _project_source(field, item)
        else:
            projected[key] = deepcopy(item)
    return projected


def browser_payload(value):
    """A copy of ``value`` in which every ``constraint_sources`` mapping, at any depth
    (``view_state.criteria``, ``search_criteria``, a stored plan's criteria), carries only
    the bounded, sensitive-free excerpt of each source's text."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "constraint_sources" and isinstance(item, dict):
                out[key] = {field: _project_source(field, source) for field, source in item.items()}
            else:
                out[key] = browser_payload(item)
        return out
    if isinstance(value, list):
        return [browser_payload(item) for item in value]
    return value
