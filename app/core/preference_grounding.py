"""What a search may carry as a soft preference or flag.

Two rules, applied where a search is assembled (``agent_loop._inject_search_params``, the
legacy executor) and again at the search tool's own boundary:

* Traceable (R4-12). A soft preference or flag the MODEL supplies must be traceable to the
  user's own words in this conversation or to a saved semantic fact, never to the
  assistant's own inferences. r06 dadc0162: the user asked once, in another conversation,
  "Is the area safe?"; a reflection then said "pet-friendliness (cat) and area safety are
  deal-breakers" and the next search carried ``accumulated_preferences ["pet friendly
  (cat)", "safe area"]`` and ``care_about_safety: true``. The cat is a saved fact; the
  safety concern is only an inference, so it no longer travels.
* Requirement, not identity (NEW-E). A sensitive personal detail (the R4-08 categories of
  ``rag.agent_memory.redact_sensitive``) never becomes a search preference. When it names a
  concrete housing requirement it becomes that requirement instead: "wheelchair user -
  step-free access needed" is the ``step-free`` property feature; "Universal Credit tenant"
  and "child attending Oakfield Primary" are dropped (r08 45f4be46).

Deterministic and offline, except ``saved_fact_texts``, which reads this user's own saved
facts from the local memory store when a value is not traceable to the conversation.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Concepts a preference can name in several words or languages. A preference naming a
# concept is traceable when any of the concept's words occurs in the user's evidence.
_CONCEPTS = (
    ("safety", r"saf(?:e|er|est|ety)|unsafe|secure|security|crimes?|criminal|danger\w*|"
               r"burglar\w*|thefts?|安全|治安|犯罪|危险"),
    ("pets", r"pets?|cats?|dogs?|kittens?|pupp(?:y|ies)|animals?|宠物|猫|狗"),
    ("quiet", r"quiet\w*|noise|noisy|peace\w*|calm|tranquil|安静|噪音|吵|清静"),
    ("transport", r"transport\w*|tube|underground|stations?|trains?|rail\w*|bus|buses|"
                  r"overground|dlr|metro|交通|地铁|车站|公交"),
    ("green", r"green\s+spaces?|parks?|green\w*|gardens?|outdoors?|公园|绿地|花园|绿化"),
    ("shops", r"shops?|shopping|supermarkets?|grocer\w*|amenit\w*|超市|商店|购物"),
    ("gym", r"gyms?|fitness|健身"),
    ("balcony", r"balcon(?:y|ies)|terraces?|阳台|露台"),
    ("light", r"bright\w*|sunny|sunlight|natural light|采光|明亮|阳光"),
    ("modern", r"modern\w*|renovated|refurbished|new[- ]?build|现代|装修"),
    ("furnished", r"furnish\w*|furniture|家具"),
    ("parking", r"parking|car park|停车"),
    ("nightlife", r"nightlife|bars?|pubs?|restaurants?|vibrant|lively|夜生活|热闹|餐厅"),
    ("space", r"spacious|space|roomy|宽敞|空间"),
    ("schools", r"schools?|学校"),
    ("commute", r"commut\w*|travel time|journey|通勤"),
    ("accessibility", r"lifts?|elevators?|step[- ]free|level access|wheelchair\w*|"
                      r"accessib\w*|电梯|无障碍|轮椅|无台阶"),
    ("study", r"study|studying|desk|学习"),
    ("laundry", r"laundry|washing machine|washer|dryer|洗衣"),
)
_CONCEPT_RES = tuple((name, re.compile(r"(?<![a-z])(?:" + pattern + r")(?![a-z])", re.I))
                     for name, pattern in _CONCEPTS)

# Words that carry no traceable content of their own ("a good area", "would like", "1 bed").
_GENERIC_WORDS = frozenset("""
a an the and or of to in on at for with without near nearby close closer by from into within
under over around about good great nice decent well very really quite more less most least
lots plenty high low level quality area areas neighbourhood neighborhood neighbourhoods
neighborhoods location locations place places property properties flat flats apartment
apartments home homes house houses building buildings room rooms bedroom bedrooms bed beds
studio studios unit units accommodation rental rentals rent renting let would like likes
liked prefer prefers preferred preference preferences want wants wanted need needs needed
require requires required requirement requirements important essential must have has had
should ideally ideal looking look user users tenant tenants friendly access whole entire self
contained private shared ensuite suite month monthly per pcm week weekly budget price prices
pricing cost costs max maximum min minimum minutes mins min hour hours one two three four
five any some all not no non nice-to-have nice to have be is are was were it its this that
these those their there they them his her he she you your yours me my mine our we us i
also just only still yet so too please thanks want wanted links link connections
connection connected options option environment vibe feel street streets road roads side
distance local short quick fast easy easily long big small large little lot
""".split())
_GENERIC_CJK = re.compile(
    r"的|地方|区域|地区|附近|周边|好的|比较|一些|希望|需要|喜欢|最好|想要|想|要|房子|房源|公寓|"
    r"住处|一个|有|和|或|离|近|方便|好|很|更|最|用户|租客|可以|能|是")

_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_FACT_LINE = re.compile(r"^\[FACT\] ?(.*)$", re.M)


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").lower().replace("’", "'")))


def _word_evidenced(word: str, evidence_words: set[str]) -> bool:
    stem = word.rstrip("s")
    for other in evidence_words:
        if other == word or other.rstrip("s") == stem:
            return True
        short, long_ = (other, word) if len(other) <= len(word) else (word, other)
        if len(short) >= 4 and long_.startswith(short[:max(4, len(short) - 2)]):
            return True
    return False


def preference_is_traceable(preference: str, evidence: list[str]) -> bool:
    """True when every concept and content word of ``preference`` occurs in ``evidence``.

    A preference with no content of its own ("good area", "1 bedroom") is not traceable:
    it says nothing the structured criteria do not already hold."""
    text = str(preference or "")
    joined = "\n".join(str(item or "") for item in evidence or [])
    if not text.strip() or not joined.strip():
        return False
    residue = text.lower()
    units = 0
    for _name, pattern in _CONCEPT_RES:
        if pattern.search(residue):
            units += 1
            if not pattern.search(joined):
                return False
            residue = pattern.sub(" ", residue)
    evidence_words = _words(joined)
    for word in _words(residue):
        if word in _GENERIC_WORDS or word.isdigit() or len(word) < 3:
            continue
        units += 1
        if not _word_evidenced(word, evidence_words):
            return False
    for run in _CJK_RE.findall(residue):
        rest = _GENERIC_CJK.sub(" ", run)
        grams = {piece[i:i + 2] for piece in rest.split() for i in range(max(0, len(piece) - 1))}
        if not grams:
            continue
        units += 1
        if not any(gram in joined for gram in grams):
            return False
    return units > 0


def safety_is_traceable(evidence: list[str]) -> bool:
    """``care_about_safety`` needs the user, or a saved fact, to have raised safety."""
    pattern = dict(_CONCEPT_RES)["safety"]
    return any(pattern.search(str(item or "")) for item in evidence or [])


def conversation_evidence(state: dict) -> list[str]:
    """The user's own words in this conversation, and the criteria parsed from them.

    Assistant turns, the rolling summary, episodic rows and reflections are NOT evidence:
    a question the user once asked ("Is the area safe?") is not a standing preference, and
    an inference is not something the user said."""
    state = state if isinstance(state, dict) else {}
    ec = state.get("extracted_context") or {}
    evidence = [str(ec.get("current_message") or "")]
    for turn in ec.get("history") or []:
        if isinstance(turn, dict) and turn.get("user"):
            evidence.append(str(turn["user"]))
    acc = state.get("accumulated_search_criteria") or {}
    for key in ("property_features", "areas"):
        evidence.extend(str(v) for v in acc.get(key) or [] if isinstance(v, str))
    for key in ("area", "commute_destination"):
        if isinstance(acc.get(key), str):
            evidence.append(acc[key])
    # The [FACT] lines of this turn's memory block are saved semantic facts.
    evidence.extend(line.strip() for line in
                    _FACT_LINE.findall(str(state.get("memory_context") or "")))
    return [item for item in evidence if item.strip()]


def saved_fact_texts(user_id) -> list[str]:
    """This user's saved semantic facts (local store, read-only); [] on any failure."""
    if not isinstance(user_id, str) or not user_id.strip():
        return []
    try:
        from rag.agent_memory import get_agent_memory
        return [str(record.get("text") or "")
                for record in get_agent_memory().semantic_records(user_id=user_id)
                if isinstance(record, dict)]
    except Exception as exc:
        logger.info("preference_grounding.saved_facts_unavailable type=%s", type(exc).__name__)
        return []


def ground_soft_signals(params: dict, state: dict, *, saved_facts=None
                        ) -> tuple[dict, list[str]]:
    """Drop model-supplied soft preferences and the safety flag the user never stated.

    Runs on the MODEL's arguments, before the harness fills in accumulated criteria (which
    were parsed from the user's own messages). Returns ``(params, dropped_field_names)``."""
    p = dict(params or {})
    prefs = p.get("accumulated_preferences")
    if isinstance(prefs, str):
        prefs = [prefs] if prefs.strip() else []
    prefs = [v for v in prefs if isinstance(v, str) and v.strip()] if isinstance(prefs, list) else []
    wants_safety = p.get("care_about_safety") is True
    if not prefs and not wants_safety:
        if "accumulated_preferences" in p and not prefs:
            p.pop("accumulated_preferences", None)
        return p, []
    evidence = conversation_evidence(state)
    pending = [v for v in prefs if not preference_is_traceable(v, evidence)]
    if pending or (wants_safety and not safety_is_traceable(evidence)):
        # Only now read the saved facts: most values trace to the conversation itself.
        loader = saved_facts or saved_fact_texts
        evidence = evidence + list(loader((state or {}).get("user_id")) or [])
    dropped: list[str] = []
    slots = _hard_slot_words(p, (state or {}).get("accumulated_search_criteria") or {})
    kept = [v for v in prefs
            if preference_is_traceable(v, evidence) and not _restates_hard_slot(v, slots)]
    if len(kept) != len(prefs):
        dropped.append("accumulated_preferences")
    if kept:
        p["accumulated_preferences"] = kept
    else:
        p.pop("accumulated_preferences", None)
    if wants_safety and not safety_is_traceable(evidence):
        p.pop("care_about_safety", None)
        dropped.append("care_about_safety")
    return p, dropped


def _hard_slot_words(params: dict, accumulated: dict) -> set[str]:
    words: set[str] = set()
    for source in (params, accumulated):
        for key in ("area", "commute_destination", "location"):
            if isinstance(source.get(key), str):
                words |= _words(source[key])
        for value in source.get("areas") or []:
            if isinstance(value, str):
                words |= _words(value)
    return words


def _restates_hard_slot(preference: str, slot_words: set[str]) -> bool:
    """True for a "preference" that only repeats the area or destination ("Islington",
    "commute to King's Cross under 30 minutes"). It goes stale the moment the user switches
    area, and the stress round's view_state still listed "Islington" after the switch to
    Camden (A-04). A preference naming anything else ("quiet street in Islington",
    "near Camden market") is not a restatement."""
    residue = str(preference or "").lower()
    commute = dict(_CONCEPT_RES)["commute"]
    residue = commute.sub(" ", residue)
    if any(pattern.search(residue) for _name, pattern in _CONCEPT_RES):
        return False
    content = {w for w in _words(residue)
               if w not in _GENERIC_WORDS and not w.isdigit() and len(w) >= 3}
    return bool(content) and content <= slot_words


# ── NEW-E: sensitive details become a requirement or nothing ──────────────────────────
_WHEELCHAIR = re.compile(r"wheelchair|轮椅", re.I)
_MARKERS = re.compile(r"\[(?:personal|contact) detail not stored\]")


def split_sensitive_preferences(values) -> tuple[list, list[str]]:
    """``(preferences, housing_features)``: each sensitive preference replaced by the housing
    requirement it names, if any, using the R4-08 classifier (``redact_sensitive``).

    "wheelchair user - step-free access needed" -> feature ``step-free``; "Universal Credit
    tenant", "child attending Oakfield Primary", a diagnosis, income or visa status -> nothing.
    The product has no evidence for a "housing benefit accepted" listing filter, so benefit
    status maps to no requirement. Non-sensitive preferences are returned unchanged."""
    from rag.agent_memory import redact_sensitive
    if isinstance(values, str):
        values = [values]
    kept: list = []
    features: list[str] = []
    for item in values or []:
        if not isinstance(item, str):
            continue
        redacted, categories = redact_sensitive(item)
        if not categories:
            if item not in kept:
                kept.append(item)
            continue
        for feature in _housing_features(item, categories):
            if feature not in features:
                features.append(feature)
        # Clause-level redaction: an unrelated housing clause in the same item survives
        # ("quiet street, earns £30k" -> "quiet street").
        rest = " ".join(_MARKERS.sub(" ", redacted).split()).strip(" ,;:-–—")
        if rest and _has_content(rest) and not redact_sensitive(rest)[1] and rest not in kept:
            kept.append(rest)
    return kept, features


def _housing_features(text: str, categories) -> list[str]:
    from core.tools.search_properties import _accessibility_feature_mentions
    found = [feature for feature, _s, _e in _accessibility_feature_mentions(text)]
    if not found and "disability" in categories and _WHEELCHAIR.search(text):
        found = ["step-free"]
    return found


def _has_content(text: str) -> bool:
    return any(w not in _GENERIC_WORDS and not w.isdigit() and len(w) >= 3
               for w in _words(text)) or bool(_CJK_RE.search(text))


def apply_preference_policy(params: dict) -> tuple[dict, bool]:
    """The search arguments with sensitive preferences turned into requirements (NEW-E).

    Returns ``(params, changed)``; derived features are appended to ``property_features``.
    A "DSS / housing benefit accepted" feature is dropped: no listing evidence can satisfy
    it, and it would persist the user's benefit status in the criteria (r6-C2)."""
    p = dict(params or {})
    dropped = False
    features = p.get("property_features")
    if isinstance(features, list) and any(benefit_acceptance_feature(f) for f in features):
        p["property_features"] = [f for f in features if not benefit_acceptance_feature(f)]
        dropped = True
    prefs = p.get("accumulated_preferences")
    if not prefs:
        return p, dropped
    kept, features = split_sensitive_preferences(prefs)
    original = [prefs] if isinstance(prefs, str) else list(prefs)
    changed = kept != original or dropped
    if kept:
        p["accumulated_preferences"] = kept
    else:
        p.pop("accumulated_preferences", None)
    if features:
        existing = p.get("property_features")
        existing = list(existing) if isinstance(existing, list) else []
        for feature in features:
            if feature not in existing:
                existing.append(feature)
                changed = True
        p["property_features"] = existing
    return p, changed


def stored_soft_preferences(values) -> list:
    """What criteria may persist as soft preferences: no health reason (R2-04) and no
    sensitive detail (NEW-E). Requirements a sensitive item names are NOT added here."""
    from rag.agent_memory import sanitize_soft_preferences
    return split_sensitive_preferences(sanitize_soft_preferences(values))[0]


# ── r6-C2: "DSS / housing benefit accepted" is a requirement the search cannot check ───
# No listing source records whether a landlord accepts housing benefit or Universal
# Credit, so round 5 maps benefit status to no requirement and memory never stores it.
# The user must still be told: a request for such listings gets a plain notice that the
# search cannot filter for it, worded about the listings, never about the user.
_BENEFIT_TERM = (r"(?:dss|housing\s+benefits?|universal\s+credit|uc|local\s+housing\s+allowance|lha|"
                 r"(?:state\s+)?benefits?(?:\s+(?:tenants?|claimants?))?|"
                 r"(?:tenants?|people|renters)\s+(?:on|claiming|receiving)\s+benefits?)")
_NO_NEW_CLAUSE = r"(?:(?!\b(?:and|but)\s+(?:i|i'm|i am|my)\b)[^.!?;\n])"
_BENEFIT_REQUEST = re.compile(
    # "landlords that accept DSS", "open to tenants on Universal Credit", "takes housing benefit"
    rf"\b(?:accept\w*|welcom\w*|allow\w*|open\s+to|happy\s+(?:with|to\s+(?:take|accept))|"
    rf"work\s+with)\b{_NO_NEW_CLAUSE}{{0,40}}?\b{_BENEFIT_TERM}\b|"
    rf"\btak(?:e|es|ing)\s+(?:(?:tenants?|people|renters)\s+(?:on|claiming|receiving)\s+)?{_BENEFIT_TERM}\b|"
    # "DSS welcome", "housing benefit accepted", "is Universal Credit OK?"
    rf"\b{_BENEFIT_TERM}\b{_NO_NEW_CLAUSE}{{0,25}}?\b(?:accept\w*|welcome[sd]?|considered|"
    r"friendly|allowed|ok(?:ay)?|fine)\b|"
    r"(?:接受|可以用|能用|允许|支持|收)\s*(?:dss|住房补贴|住房福利|统一福利|福利|救济金|低保)|"
    r"(?:dss|住房补贴|住房福利|统一福利|救济金)\s*(?:可以|也行|行吗|接受|没问题)", re.I)
_BENEFIT_NEGATED = re.compile(
    r"\b(?:don.t|do\s+not|doesn.t|does\s+not|no\s+need|not|never)\b[^.!?;\n]{0,30}$|不(?:需要|用|必)\s*$",
    re.I)
_BENEFIT_FEATURE = re.compile(rf"\b{_BENEFIT_TERM}\b|住房补贴|住房福利|统一福利|救济金|低保", re.I)

_BENEFIT_NOTICE_EN = (
    "I can't filter listings by whether the landlord or letting agent accepts housing benefit "
    "or Universal Credit (often listed as \"DSS\"): the listing data doesn't record it. "
    "Please confirm this with the letting agent or landlord before you arrange a viewing.")
_BENEFIT_NOTICE_ZH = (
    "说明：我无法按房东或中介是否接受住房补贴或 Universal Credit（常写作“DSS”）来筛选房源——"
    "房源数据中没有这项信息。请在预约看房前向中介或房东确认。")


def benefit_acceptance_requested(message: str) -> bool:
    """True when ``message`` asks for listings that accept housing benefit / UC ("DSS")."""
    text = str(message or "")[:4000]
    for match in _BENEFIT_REQUEST.finditer(text):
        if not _BENEFIT_NEGATED.search(text[max(0, match.start() - 40):match.start()]):
            return True
    return False


def benefit_acceptance_feature(value) -> bool:
    """True for a property feature that only names benefit acceptance ("DSS accepted")."""
    return isinstance(value, str) and bool(_BENEFIT_FEATURE.search(value))


def render_unsupported_requirement_notice(message: str, *, language: str = "en") -> str:
    """The plain statement that the search cannot check a requirement this message asks for
    (today: benefit acceptance), or ''. It never repeats the user's own circumstances."""
    if not benefit_acceptance_requested(message):
        return ""
    return _BENEFIT_NOTICE_ZH if str(language or "").lower().startswith("zh") else _BENEFIT_NOTICE_EN
