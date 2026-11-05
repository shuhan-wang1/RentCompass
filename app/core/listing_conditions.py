"""What a listing's own text says about the conditions a tenant asked for.

A search card carries a structured room type ("Studio") and a short line; the listing's own
detail page often says far more. A "Studio" in Guilford Street was put first as the best
matching studio while its own page said it was let only to nurses, NHS staff and medical
workers, that its kitchen and bathroom were shared with another room, and that electricity
was not included (2026-11-05 browser test). The card's "Details" text said all of that; the
validator never read it.

Each check here reads only the provider's text, never model prose, and returns one row per
condition:

    {"condition": "eligibility" | "private_bathroom" | "own_kitchen" | "bills" | "move_in",
     "status": "met" | "not_met" | "unknown",
     "reason": <stable machine reason>, "quote": <the listing's own words, or "">}

``not_met`` is a confirmed conflict and excludes the listing; ``unknown`` keeps it out of the
eligible set without claiming it fails. A condition the listing text says nothing about
produces no row: the structured checks in ``candidate_validation`` still decide it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# ── reasons (stable machine strings; candidate_validation localises them) ──────────────────
REASON_ELIGIBILITY_NOT_MET = "listing is let only to {group}"
REASON_ELIGIBILITY_UNKNOWN = "listing is let only to {group}; eligibility is not confirmed"
REASON_SHARED_BATHROOM = "listing says the bathroom is shared"
REASON_SHARED_KITCHEN = "listing says the kitchen is shared"
REASON_BILLS_NOT_MET = "listing says not all bills are included"
REASON_BILLS_UNKNOWN = "bills included is not confirmed"
REASON_MOVE_IN_UNKNOWN = "listed as available from {available}; not confirmed for {requested}"
REASON_MOVE_IN_NOW_UNKNOWN = "listed as available now; not confirmed for {requested}"

TENANT_GROUP_LABELS = {
    "healthcare": "nurses, NHS and medical staff",
    "professionals": "working professionals (no students)",
    "students": "students",
    "postgraduates": "postgraduate students",
    "female": "female tenants",
    "male": "male tenants",
}
TENANT_GROUP_LABELS_ZH = {
    "healthcare": "护士、NHS 及医护人员",
    "professionals": "在职人士（不接受学生）",
    "students": "学生",
    "postgraduates": "研究生",
    "female": "女性租客",
    "male": "男性租客",
}

_HEALTH = (r"(?:nurses?|nursing\s+staff|n\.?h\.?s\.?(?:\s+(?:staff|workers?|employees?|members?|"
           r"trust\s+staff))?|medical\s+(?:workers?|staff|professionals?|students?)|"
           r"health\s*care\s+(?:workers?|staff|professionals?)|hospital\s+(?:staff|workers?|employees?)|"
           r"(?:junior\s+)?doctors?|key\s+workers?)")
_HEALTH_LIST = _HEALTH + r"(?:\s*(?:,|/|&|\band\b|\bor\b)\s*" + _HEALTH + r")*"
_ONLY = r"(?:exclusively|only|strictly|solely)"
_TENANT_PATTERNS = (
    ("healthcare", re.compile(
        r"\b(?:available\s+|let\s+|offered\s+|reserved\s+|restricted\s+)?" + _ONLY +
        r"\s+(?:available\s+)?(?:to|for)\s+(?:single\s+)?(?:tenants?\s+(?:who\s+are\s+)?"
        r"(?:employed\s+(?:directly\s+)?(?:by|with)\s+(?:the\s+)?)?)?" + _HEALTH_LIST, re.I)),
    ("healthcare", re.compile(
        r"\b(?:licen[cs]ed|available|reserved|let|suitable)\s+(?:to|for)\s+" + _HEALTH_LIST +
        r"\s+only\b", re.I)),
    ("healthcare", re.compile(r"\b" + _HEALTH_LIST + r"\s+only\b", re.I)),
    ("professionals", re.compile(
        r"\b(?:working\s+)?professionals?\s+only\b|\bonly\s+(?:suitable\s+)?(?:for\s+)?"
        r"(?:working\s+)?professionals?\b|\bno\s+students\b|\bstudents?\s+(?:are\s+)?not\s+"
        r"(?:accepted|allowed|permitted|considered|eligible)\b|\bnot\s+suitable\s+for\s+students\b",
        re.I)),
    ("postgraduates", re.compile(
        r"\b(?:post[- ]?grad(?:uate)?s?|phd)\s+(?:students?\s+)?only\b|\bonly\s+(?:to|for)\s+"
        r"post[- ]?grad(?:uate)?s?\b", re.I)),
    ("students", re.compile(
        r"\b(?:full[- ]time\s+)?students?\s+only\b|\b" + _ONLY + r"\s+(?:available\s+)?(?:to|for)\s+"
        r"(?:full[- ]time\s+)?students\b", re.I)),
    ("female", re.compile(r"\b(?:female|ladies|women)\s+only\b|\bonly\s+(?:for\s+)?(?:female|women)\b",
                          re.I)),
    ("male", re.compile(r"\b(?<!fe)(?:male|men)\s+only\b|\bonly\s+(?:for\s+)?(?<!fe)(?:male|men)\b",
                        re.I)),
)
# What each restriction means for each thing the user said about themselves
# (constraint_updates.tenant_status_update). A pair not listed is "unknown": "I'm not a
# student" says nothing about NHS employment, and "I'm a student" does not say undergraduate.
_GROUP_DECISION = {
    "healthcare": {"healthcare": "met", "student": "not_met", "postgraduate": "not_met"},
    "professionals": {"healthcare": "met", "student": "not_met", "postgraduate": "not_met"},
    "students": {"student": "met", "postgraduate": "met", "other": "not_met"},
    "postgraduates": {"postgraduate": "met", "other": "not_met"},
}
_NEGATED_BEFORE = re.compile(
    r"(?:\b(?:no|not|never|without|nor|don[’']?t|doesn[’']?t|isn[’']?t|aren[’']?t)\b|no\s+need\s+to)"
    r"[\w\s,-]{0,18}$", re.I)

_BATH = r"(?:bath(?:room)?s?|shower(?:\s*rooms?)?|toilets?|w\.?c\.?|washrooms?)"
_KITCHEN_LEAD = r"(?:kitchen(?:\s+area)?\s*(?:,|/|&|\band\b)\s*)"
_SHARED_BATHROOM = re.compile(
    r"\bshared\s+" + _KITCHEN_LEAD + r"?" + _BATH + r"\b"
    r"|\bshar(?:e|es|ing)\s+(?:a\s+|the\s+|one\s+)?" + _KITCHEN_LEAD + r"?" + _BATH + r"\b"
    r"|\b" + _BATH + r"\s+(?:is\s+|are\s+)?(?:shared|communal)\b"
    r"|\bcommunal\s+" + _BATH + r"\b", re.I)
_SHARED_KITCHEN = re.compile(
    r"\bshared\s+kitchen\b|\bshar(?:e|es|ing)\s+(?:a\s+|the\s+)?kitchen\b"
    r"|\bkitchen(?:\s+area)?\s+(?:is\s+)?(?:shared|communal)\b|\bcommunal\s+kitchen\b", re.I)
_PRIVATE_BATHROOM = re.compile(
    r"\ben[- ]?suite\b|\bprivate\s+" + _BATH + r"|\bown\s+(?:private\s+)?" + _BATH +
    r"|\bseparate\s+shower\s*room\b|\bself[- ]contained\b", re.I)

_BILLS_EXCLUDED = (
    re.compile(r"\bbills?\s+(?:are\s+)?(?:not|n[o’']t)\s+(?:included|inclusive)\b", re.I),
    re.compile(r"\b(?:excluding|exclusive\s+of|excl\.?|plus|\+)\s+(?:all\s+)?(?:utility\s+)?bills\b", re.I),
    re.compile(r"\bbills?\s+(?:are\s+)?(?:extra|additional|excluded|on\s+top|payable\s+separately|"
               r"charged\s+separately|to\s+be\s+paid\s+separately)\b", re.I),
    re.compile(r"\b(?:all\s+)?(?:bills|utilities)\s+(?:are\s+)?(?:included|inclusive)\s+(?:apart\s+from|"
               r"except(?:\s+for)?|excluding|but\s+not|other\s+than|save\s+for|bar)\s+[\w\s]{2,30}", re.I),
    re.compile(r"\b(?:electric(?:ity)?|gas|energy|water|internet|broadband|wi-?fi|utilities)"
               r"(?:\s+(?:bills?|for\s+(?:your|the)\s+room))?\s+(?:is|are)\s+(?:not\s+included|extra|"
               r"additional|payable\s+separately|charged\s+separately|on\s+top|metered|"
               r"paid\s+(?:separately|by\s+the\s+tenant))\b", re.I),
    re.compile(r"\b(?:tenants?|you)\s+(?:will\s+)?(?:pay|be\s+(?:liable|responsible))\s+(?:for\s+)?"
               r"(?:all\s+|your\s+own\s+|the\s+)?(?:electric(?:ity)?|gas|energy|bills|utilities)\b", re.I),
)
_BILLS_INCLUDED = re.compile(
    r"\b(?:all\s+)?bills\s+(?:are\s+)?(?:included|inclusive)\b|\binclusive\s+of\s+(?:all\s+)?bills\b"
    r"|\bincluding\s+(?:all\s+)?bills\b|\ball[- ]inclusive\b|\bbills[- ]inclusive\b", re.I)

# How long a listing's stated start date can come before the requested move-in and still be
# read as "free then": lettings rarely wait months for a tenant.
AVAILABILITY_HOLD_DAYS = 31


def listing_text(candidate: dict) -> str:
    """The provider's own words for a listing: detail-page prose first, then card text."""
    if not isinstance(candidate, dict):
        return ""
    parts: list[str] = []
    for key in ("description", "_full_description", "Enhanced_Description", "Detailed_Amenities",
                "detailed_amenities", "Description", "explanation"):
        value = candidate.get(key)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value if item)
        text = " ".join(str(value or "").split())
        if text and not any(text in seen for seen in parts):
            parts.append(text)
    return " \n ".join(parts)[:12000]


def _quote(text: str, start: int, end: int, *, limit: int = 220) -> str:
    """The sentence holding ``text[start:end]``, clipped around the match when long."""
    left = max(text.rfind(mark, 0, start) for mark in (". ", "! ", "? ", "\n", "; "))
    left = 0 if left < 0 else left + 1
    rights = [text.find(mark, end) for mark in (". ", "! ", "? ", "\n", "; ")]
    right = min([r for r in rights if r >= 0] or [len(text)]) + 1
    matched = text[start:end]
    if matched.isupper():
        # A capitalised headline runs into the next sentence with no full stop
        # ("…MEDICAL WORKERS A well-presented bedsit"): end where the capitals end, and
        # leave the capitalised first word of that next sentence out.
        run = re.match(r"[A-Z0-9 ,/&'’()-]*", text[end:]).group(0)
        if end + len(run) < len(text) and text[end + len(run)].islower():
            cut = run.rstrip().rfind(" ")
            run = run[:cut] if cut >= 0 else ""
        right = min(right, end + len(run.rstrip(" ,(")))
    sentence = text[left:right].strip(" .;\n")
    if len(sentence) <= limit:
        return sentence
    a = max(left, start - 80)
    b = min(right, end + 80)
    clipped = text[a:b].strip()
    if a > left:
        clipped = "…" + clipped.split(" ", 1)[-1]
    if b < right:
        clipped = clipped.rsplit(" ", 1)[0] + "…"
    return clipped


def _positive_matches(pattern: re.Pattern, text: str):
    for match in pattern.finditer(text):
        before = text[max(0, match.start() - 30):match.start()]
        if _NEGATED_BEFORE.search(before):
            continue
        yield match


def _first(pattern: re.Pattern, text: str):
    return next(_positive_matches(pattern, text), None)


def tenant_restrictions(text: str) -> list[tuple[str, str]]:
    """``[(group, quote)]`` for each tenant group the listing text limits lettings to."""
    found: dict[str, str] = {}
    for group, pattern in _TENANT_PATTERNS:
        if group in found:
            continue
        match = _first(pattern, text)
        if match is None:
            continue
        if group == "students" and ("postgraduates" in found or re.search(
                r"post[- ]?grad|phd", text[max(0, match.start() - 25):match.end()], re.I)):
            continue
        found[group] = _quote(text, match.start(), match.end())
    return list(found.items())


def bills_evidence(text: str) -> tuple[str, str]:
    """('not_met' | 'met' | 'unknown', quote) for "bills included", from listing text."""
    for pattern in _BILLS_EXCLUDED:
        match = _first(pattern, text)
        if match is not None:
            return "not_met", _quote(text, match.start(), match.end())
    match = _first(_BILLS_INCLUDED, text)
    if match is not None:
        return "met", _quote(text, match.start(), match.end())
    return "unknown", ""


def _requested_room_types(constraints: dict) -> set[str]:
    raw = str(constraints.get("room_type") or "").lower()
    parts = {part.strip() for part in re.split(r"[+,/|]", raw) if part.strip()}
    out = set()
    for part in parts:
        if "studio" in part:
            out.add("studio")
        elif "suite" in part:
            out.add("ensuite")
    features = {str(f).lower() for f in constraints.get("property_features") or []}
    if "studio" in features:
        out.add("studio")
    if {"en-suite", "ensuite"} & features:
        out.add("ensuite")
    return out


def _as_date(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def move_in_condition(available: Any, requested: Any, *, today: date | None = None) -> dict | None:
    """An "unknown" row when the listing's start date is well before the requested move-in.

    "Available from 2027-01-07" for a September 2027 start, or "available now" for a start
    ten months away, says the flat is free NOW; whether it is still free on the requested day
    is not known. ``None`` when there is nothing to add to the structured date check."""
    wanted = _as_date(requested)
    if wanted is None:
        return None
    text = str(available or "").strip()
    if not text:
        return None
    now = today or date.today()
    if text.lower() in {"available now", "available_now", "now", "immediately"}:
        start, shown = now, "now"
    else:
        start = _as_date(text)
        shown = text[:10]
    if start is None or (wanted - start).days <= AVAILABILITY_HOLD_DAYS:
        return None
    if (wanted - now).days <= AVAILABILITY_HOLD_DAYS:
        return None
    reason = (REASON_MOVE_IN_NOW_UNKNOWN.format(requested=wanted.isoformat()) if shown == "now"
              else REASON_MOVE_IN_UNKNOWN.format(available=shown, requested=wanted.isoformat()))
    return {"condition": "move_in", "status": "unknown", "reason": reason, "quote": ""}


def check_listing_conditions(candidate: dict, constraints: dict) -> list[dict]:
    """Condition rows for one listing; see the module docstring."""
    text = listing_text(candidate)
    rows: list[dict] = []
    if text:
        tenant = str(constraints.get("tenant_status") or "").lower() or None
        for group, quote in tenant_restrictions(text):
            label = TENANT_GROUP_LABELS[group]
            status = _GROUP_DECISION.get(group, {}).get(tenant or "", "unknown")
            if status == "met":
                rows.append({"condition": "eligibility", "status": "met", "reason": "", "quote": quote,
                             "group": group})
                continue
            template = REASON_ELIGIBILITY_NOT_MET if status == "not_met" else REASON_ELIGIBILITY_UNKNOWN
            rows.append({"condition": "eligibility", "status": status, "group": group,
                         "reason": template.format(group=label), "quote": quote})

        wanted = _requested_room_types(constraints)
        if wanted:
            bath = _first(_SHARED_BATHROOM, text)
            kitchen = _first(_SHARED_KITCHEN, text)
            fits = set()
            if "ensuite" in wanted and bath is None:
                fits.add("ensuite")
            if "studio" in wanted and bath is None and kitchen is None:
                fits.add("studio")
            if not fits:
                if bath is not None:
                    rows.append({"condition": "private_bathroom", "status": "not_met",
                                 "reason": REASON_SHARED_BATHROOM,
                                 "quote": _quote(text, bath.start(), bath.end())})
                if kitchen is not None and "studio" in wanted and (bath is None or kitchen.start() != bath.start()):
                    rows.append({"condition": "own_kitchen", "status": "not_met",
                                 "reason": REASON_SHARED_KITCHEN,
                                 "quote": _quote(text, kitchen.start(), kitchen.end())})
            else:
                private = _first(_PRIVATE_BATHROOM, text)
                if private is not None:
                    rows.append({"condition": "private_bathroom", "status": "met", "reason": "",
                                 "quote": _quote(text, private.start(), private.end())})

        if constraints.get("bills_included"):
            status, quote = bills_evidence(text)
            rows.append({"condition": "bills", "status": status,
                         "reason": {"not_met": REASON_BILLS_NOT_MET, "unknown": REASON_BILLS_UNKNOWN}
                         .get(status, ""), "quote": quote})
    elif constraints.get("bills_included"):
        rows.append({"condition": "bills", "status": "unknown", "reason": REASON_BILLS_UNKNOWN,
                     "quote": ""})
    if constraints.get("move_in_date"):
        available = candidate.get("available_from") or candidate.get("_available_from")
        row = move_in_condition(available, constraints["move_in_date"])
        if row is not None:
            rows.append(row)
    return rows


def condition_reason_zh(reason: str) -> str | None:
    """Chinese presentation of a reason produced here, or None if it is not one of ours."""
    text = str(reason or "")
    for group, label in TENANT_GROUP_LABELS.items():
        if text == REASON_ELIGIBILITY_NOT_MET.format(group=label):
            return f"房源只租给{TENANT_GROUP_LABELS_ZH[group]}，您不符合"
        if text == REASON_ELIGIBILITY_UNKNOWN.format(group=label):
            return f"房源只租给{TENANT_GROUP_LABELS_ZH[group]}，尚未确认您是否符合"
    fixed = {
        REASON_SHARED_BATHROOM: "房源原文说明卫生间是共用的",
        REASON_SHARED_KITCHEN: "房源原文说明厨房是共用的",
        REASON_BILLS_NOT_MET: "房源原文说明账单并未全包",
        REASON_BILLS_UNKNOWN: "尚未确认是否包 bills",
    }
    if text in fixed:
        return fixed[text]
    match = re.fullmatch(r"listed as available from (.+); not confirmed for (.+)", text)
    if match:
        return f"房源标注 {match.group(1)} 起可入住，未确认 {match.group(2)} 是否仍可入住"
    match = re.fullmatch(r"listed as available now; not confirmed for (.+)", text)
    if match:
        return f"房源标注现在即可入住，未确认 {match.group(1)} 是否仍可入住"
    return None


def is_condition_reason(reason: str) -> bool:
    return condition_reason_zh(reason) is not None


def conflict_quotes(conditions: list[dict] | None) -> list[str]:
    """The listing's own words behind each confirmed conflict, in order, without repeats."""
    out: list[str] = []
    for row in conditions or []:
        quote = str((row or {}).get("quote") or "").strip()
        if (row or {}).get("status") == "not_met" and quote and quote not in out:
            out.append(quote)
    return out
