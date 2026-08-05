"""Hard-constraint schema **v2** — the frozen definition used by the 2026-08-05 held-out batch.

Why a v2 at all (see EVAL_REPORT_20260804.md §2.13.2–§2.13.4):

  * v1's ``room_type_match`` was **overloaded** — ``re.match(r"\\d+")`` on the value decided
    whether it meant "bedroom count" or "room-type label". Per-slot quotas could not be
    counted without parsing a string, and only exact equality was expressible.
  * Three semantic slots the owner ruling requires (**area / move_in_date /
    property_feature**) had **no type at all** in v1, so they could only live in prose —
    never in a quota, never in a deterministic predicate.
  * v1's satisfaction predicates fell back to **text-marker heuristics** whenever the
    evidence carried no listing, so "did the model echo the user's words" and "was the
    constraint actually honoured" were graded through the same code path.

v2 fixes exactly those three things and nothing else. Every type below freezes FIVE items,
per the §2.13.4 spec:

  1. ``user_text_normalisation`` — how the user's words become the stored value;
  2. ``evidence`` — (scope, field) the predicate compares against, in the FROZEN fixture;
  3. ``predicate``            — deterministic, no heuristics, no text fallback;
  4. ``completion_policy``    — the correct behaviour on no-result / unknown / partial;
  5. ``judge_evidence``       — which facts a judge packet may use as support.

--------------------------------------------------------------------------------------
THE SATISFACTION PREDICATE IS EVALUATED AGAINST THE ANSWER, NOT AGAINST THE FIXTURE
--------------------------------------------------------------------------------------
v1's ``_c_all_results_satisfy`` runs over ``_listings_from_evidence(ctx.evidence)`` — i.e.
over what the TOOL returned. On a fully compliant fixture that is vacuously true no matter
what the assistant writes, which is why v1 could report "budget coverage 13" while 12 of
those 13 could not distinguish a compliant answer from a non-compliant one.

v2 grades the **surfaced set**: the frozen fixture listings the answer actually puts in
front of the user, identified by tokens that are unique inside the case by construction
(see ``surfaced_listings``). A case therefore has to carry a *violation trap* — at least
one listing that breaks the constraint — or it does not enter the satisfaction denominator
at all. That is check ``T1`` in the preflight.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = "rentcompass/hard_constraints/v2"

# --------------------------------------------------------------------------- #
# Verdict enum for a single constraint on a single answer
# --------------------------------------------------------------------------- #
PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"            # evidence says "we do not know" (e.g. Available: Contact agent)
NO_RESULT = "no_result"        # the frozen fixture carries no listing at all
NOT_SURFACED = "not_surfaced"  # the answer put no frozen listing in front of the user

# Only PASS / FAIL enter a satisfaction denominator. Everything else is BEHAVIOUR coverage.
SATISFACTION_VERDICTS = frozenset({PASS, FAIL})
BEHAVIOUR_VERDICTS = frozenset({UNKNOWN, NO_RESULT, NOT_SURFACED})


# --------------------------------------------------------------------------- #
# Controlled vocabularies (v2 forbids free-text inference — this is the whole point)
# --------------------------------------------------------------------------- #
ROOM_TYPE_VOCAB = ("studio", "flat", "house", "room_in_shared", "maisonette", "bungalow")

FEATURE_VOCAB = ("furnished", "unfurnished", "part_furnished", "pet_friendly", "garden",
                 "parking", "balcony", "lift", "en_suite", "bills_included",
                 "washing_machine", "dishwasher", "wheelchair_accessible", "student_friendly")

AREA_GRANULARITY = ("borough", "city", "postcode_district", "postcode_sector", "adjacent")

# "Available from" strings that mean the landlord/agent has not committed to a date.
# FROZEN RULE (owner ruling, report §2.13.4): these map to UNKNOWN. They are NEVER a
# failure and NEVER a satisfaction — they go to behaviour coverage.
_DATE_UNKNOWN_MARKERS = ("contact agent", "on application", "ask agent", "tbc",
                         "to be confirmed", "enquire", "unknown", "")
# …and these mean "available right now", which satisfies any "<= some future date".
_DATE_IMMEDIATE_MARKERS = ("now", "immediately", "available now", "immediate")
_IMMEDIATE = date(1970, 1, 1)


# --------------------------------------------------------------------------- #
# 1. user-text normalisation
# --------------------------------------------------------------------------- #
_WEEK_TO_MONTH = 52.0 / 12.0
_PW = re.compile(r"(?:per\s*week|/\s*w(?:eek)?\b|\bpw\b|a\s*week)", re.I)
_PCM = re.compile(r"(?:per\s*month|/\s*m(?:onth)?\b|\bpcm\b|a\s*month|monthly)", re.I)
_MONEY = re.compile(r"£?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def normalise_budget(text: str) -> Optional[float]:
    """User words -> monthly GBP, rounded to 2dp.

    FROZEN: '£350 pw' -> 350*52/12 = 1516.67. A bare figure with no period marker is
    read as MONTHLY (UK rental convention for flats). Returns None when no figure is
    present — the caller must then treat the constraint as unstated, never as 0.
    """
    m = _MONEY.search(text or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    if _PW.search(text) and not _PCM.search(text):
        v = v * _WEEK_TO_MONTH
    return round(v, 2)


_BED_WORDS = {"studio": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def normalise_bedroom_count(text: str) -> Optional[Dict[str, Any]]:
    """User words -> {'op': ..., 'value': ...}.

    FROZEN mapping:
      'a 2-bed' / 'two bedroom'      -> {op '==', value 2}
      'at least 2 bedrooms' / '2+'   -> {op '>=', value 2}
      'no more than 3 bedrooms'      -> {op '<=', value 3}
      '2 to 3 bedrooms' / '2-3 beds' -> {op 'between', value [2, 3]}
      'studio'                       -> NOT a bedroom_count constraint (see room_type)
    """
    t = (text or "").lower()
    rng = re.search(r"(\d+)\s*(?:to|-|–|—)\s*(\d+)\s*(?:bed|bedroom)", t)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
        return {"op": "between", "value": [min(lo, hi), max(lo, hi)]}
    m = re.search(r"(?:at least|minimum(?: of)?|no fewer than)\s*(\d+)", t) or \
        re.search(r"(\d+)\s*\+\s*(?:bed|bedroom)", t)
    if m:
        return {"op": ">=", "value": int(m.group(1))}
    m = re.search(r"(?:no more than|at most|maximum(?: of)?|up to)\s*(\d+)", t)
    if m:
        return {"op": "<=", "value": int(m.group(1))}
    m = re.search(r"(\d+)\s*(?:-|\s)?\s*(?:bed|bedroom)", t)
    if m:
        return {"op": "==", "value": int(m.group(1))}
    for w, n in _BED_WORDS.items():
        if w != "studio" and re.search(rf"\b{w}\b\s*(?:-|\s)?\s*(?:bed|bedroom)", t):
            return {"op": "==", "value": n}
    return None


_ROOM_TYPE_SURFACE = {
    "studio": "studio", "studios": "studio",
    "flat": "flat", "apartment": "flat", "flats": "flat", "apartments": "flat",
    "house": "house", "houses": "house",
    "room": "room_in_shared", "houseshare": "room_in_shared", "house share": "room_in_shared",
    "flatshare": "room_in_shared", "flat share": "room_in_shared", "shared": "room_in_shared",
    "maisonette": "maisonette", "bungalow": "bungalow",
}


def normalise_room_type(text: str) -> Optional[str]:
    """User words -> one ROOM_TYPE_VOCAB token. v2 carries the LABEL ONLY; a bedroom
    count in the same sentence is a separate ``bedroom_count_match`` constraint."""
    t = (text or "").lower()
    for surface in sorted(_ROOM_TYPE_SURFACE, key=len, reverse=True):
        if re.search(rf"\b{re.escape(surface)}\b", t):
            return _ROOM_TYPE_SURFACE[surface]
    return None


_POSTCODE_DISTRICT = re.compile(r"\b([A-Z]{1,2}[0-9][0-9A-Z]?)\b")


def normalise_area(text: str) -> Optional[Dict[str, Any]]:
    """User words -> {'granularity': ..., 'value': ...}.

    FROZEN granularity ladder (report §2.13.4 required these boundaries to be written down):

      * ``postcode_district`` — 'N1', 'SE15', 'E1W'. Compared as the WHOLE outward district,
        case-folded, **exact string equality**. So ``N1`` does NOT match ``N1C``: N1C is a
        different district, not a child of N1. Prefix matching is explicitly rejected.
      * ``postcode_sector``   — 'N1 9', i.e. district + first inward digit. Exact equality.
      * ``borough``           — 'Camden', 'Islington'. Matches a listing whose
        ``area_normalized`` OR whose ``borough`` field equals the token, case-folded.
      * ``city``              — 'London'. Matches when the listing's ``city`` field equals
        the token. Camden ⊂ London holds ONLY through the listing's own ``city`` field;
        the schema carries no implicit borough->city table.
      * ``adjacent``          — 'near King's Cross'. Satisfied ONLY by the case's frozen
        ``accept`` list of area tokens. Adjacency is NEVER inferred at grading time.
    """
    t = (text or "").strip()
    if not t:
        return None
    sector = re.search(r"\b([A-Z]{1,2}[0-9][0-9A-Z]?)\s+([0-9])\b", t.upper())
    if sector:
        return {"granularity": "postcode_sector",
                "value": f"{sector.group(1)} {sector.group(2)}"}
    pc = _POSTCODE_DISTRICT.search(t.upper())
    if pc:
        return {"granularity": "postcode_district", "value": pc.group(1)}
    if re.search(r"\bnear\b|\bclose to\b|\bwalking distance\b", t, re.I):
        return {"granularity": "adjacent", "value": t}
    return {"granularity": "borough", "value": t}


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}


def normalise_move_in_date(text: str, *, year_hint: int = 2026) -> Optional[str]:
    """User words -> ISO 'YYYY-MM-DD' (the LATEST acceptable move-in date).

    FROZEN: '1 September', 'Sept 1st', '2026-09-01' all normalise to '2026-09-01'.
    A bare month ('in September') normalises to the LAST day of that month, because
    "I need to move in in September" is satisfied by any date within September.
    """
    t = (text or "").lower()
    iso = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", t)
    if iso:
        return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    dm = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)", t)
    if dm and dm.group(2)[:3] in {k[:3] for k in _MONTHS}:
        mon = next(v for k, v in _MONTHS.items() if k.startswith(dm.group(2)[:3]))
        return f"{year_hint:04d}-{mon:02d}-{int(dm.group(1)):02d}"
    md = re.search(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if md and md.group(1)[:3] in {k[:3] for k in _MONTHS}:
        mon = next(v for k, v in _MONTHS.items() if k.startswith(md.group(1)[:3]))
        return f"{year_hint:04d}-{mon:02d}-{int(md.group(2)):02d}"
    bare = re.search(r"\b([a-z]{3,9})\b", t)
    if bare and bare.group(1)[:3] in {k[:3] for k in _MONTHS}:
        mon = next(v for k, v in _MONTHS.items() if k.startswith(bare.group(1)[:3]))
        last = [31, 29 if year_hint % 4 == 0 else 28, 31, 30, 31, 30,
                31, 31, 30, 31, 30, 31][mon - 1]
        return f"{year_hint:04d}-{mon:02d}-{last:02d}"
    return None


_FEATURE_SURFACE = {
    "furnished": "furnished", "fully furnished": "furnished",
    "unfurnished": "unfurnished", "part furnished": "part_furnished",
    "part-furnished": "part_furnished",
    "pet": "pet_friendly", "pets": "pet_friendly", "pet friendly": "pet_friendly",
    "pet-friendly": "pet_friendly", "dog": "pet_friendly", "cat": "pet_friendly",
    "garden": "garden", "parking": "parking", "car park": "parking",
    "balcony": "balcony", "lift": "lift", "elevator": "lift",
    "en suite": "en_suite", "en-suite": "en_suite", "ensuite": "en_suite",
    "bills included": "bills_included", "bills-included": "bills_included",
    "washing machine": "washing_machine", "dishwasher": "dishwasher",
    "wheelchair": "wheelchair_accessible", "step-free": "wheelchair_accessible",
    "student": "student_friendly",
}


def normalise_feature(text: str) -> Optional[str]:
    """User words -> one FEATURE_VOCAB token, or None.

    ``unfurnished`` is checked before ``furnished`` (longest surface form first) so
    'unfurnished' never normalises to 'furnished'.
    """
    t = (text or "").lower()
    for surface in sorted(_FEATURE_SURFACE, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(surface)}", t):
            return _FEATURE_SURFACE[surface]
    return None


def normalise_commute_minutes(text: str) -> Optional[int]:
    """User words -> integer minutes upper bound. 'within half an hour' -> 30."""
    t = (text or "").lower()
    if re.search(r"half an hour", t):
        return 30
    m = re.search(r"(\d+)\s*(?:min|minute)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*hour", t)
    if m:
        return int(m.group(1)) * 60
    return None


# --------------------------------------------------------------------------- #
# 2. evidence location — (scope, field). Scope must be explicit: commute's
#    duration_minutes lives at the TOOL-RESULT level, not inside a listing.
# --------------------------------------------------------------------------- #
SLOT_EVIDENCE_FIELD: Dict[str, Tuple[str, str]] = {
    "budget":           ("listing", "price_raw"),
    "bedroom_count":    ("listing", "bedrooms"),
    "room_type":        ("listing", "room_type_normalized"),
    "commute":          ("tool_result", "duration_minutes"),
    "area":             ("listing", "area_normalized"),
    "move_in_date":     ("listing", "available_from_normalized"),
    "property_feature": ("listing", "features"),
}
_FIELD_ALIASES = {"price_raw": ("price_raw", "monthly_rent"),
                  "duration_minutes": ("duration_minutes", "duration", "minutes")}


# --------------------------------------------------------------------------- #
# 3. deterministic predicates — one listing (or one tool result) at a time
# --------------------------------------------------------------------------- #
def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _cmp(a: float, op: str, b) -> bool:
    if op == "between":
        lo, hi = float(b[0]), float(b[1])
        return lo <= a <= hi
    b = float(b)
    return {"<=": a <= b, "<": a < b, ">=": a >= b, ">": a > b,
            "==": a == b, "!=": a != b}[op]


def _p_budget(listing: dict, con: dict) -> str:
    v = None
    for name in _FIELD_ALIASES["price_raw"]:
        v = _num(listing.get(name))
        if v is not None:
            break
    if v is None:
        return UNKNOWN
    return PASS if _cmp(v, con.get("op", "<="), con["value"]) else FAIL


def _p_bedroom(listing: dict, con: dict) -> str:
    v = _num(listing.get("bedrooms"))
    if v is None:
        return UNKNOWN
    return PASS if _cmp(v, con.get("op", "=="), con["value"]) else FAIL


def _p_room_type(listing: dict, con: dict) -> str:
    v = listing.get("room_type_normalized")
    if not isinstance(v, str) or v not in ROOM_TYPE_VOCAB:
        return UNKNOWN            # v2 NEVER falls back to parsing property_type free text
    return PASS if v == con["value"] else FAIL


def _p_area(listing: dict, con: dict) -> str:
    gran = con.get("granularity", "borough")
    want = str(con["value"]).strip().casefold()
    if gran == "postcode_district":
        got = listing.get("postcode_district")
        if not isinstance(got, str) or not got:
            return UNKNOWN
        return PASS if got.strip().casefold() == want else FAIL
    if gran == "postcode_sector":
        got = listing.get("postcode_sector")
        if not isinstance(got, str) or not got:
            return UNKNOWN
        return PASS if got.strip().casefold() == want else FAIL
    if gran == "city":
        got = listing.get("city")
        if not isinstance(got, str) or not got:
            return UNKNOWN
        return PASS if got.strip().casefold() == want else FAIL
    if gran == "adjacent":
        accept = [str(x).strip().casefold() for x in (con.get("accept") or [])]
        got = listing.get("area_normalized")
        if not isinstance(got, str) or not got:
            return UNKNOWN
        if not accept:
            return UNKNOWN        # adjacency is never inferred: no frozen list -> unjudgeable
        return PASS if got.strip().casefold() in accept else FAIL
    got = listing.get("area_normalized")
    boro = listing.get("borough")
    cands = [x.strip().casefold() for x in (got, boro) if isinstance(x, str) and x]
    if not cands:
        return UNKNOWN
    return PASS if want in cands else FAIL


def _p_move_in(listing: dict, con: dict) -> str:
    raw = listing.get("available_from_normalized")
    if raw is None:
        return UNKNOWN
    s = str(raw).strip().casefold()
    if s in _DATE_UNKNOWN_MARKERS:
        return UNKNOWN                       # 'Contact agent' -> unknown, NEVER a failure
    if s in _DATE_IMMEDIATE_MARKERS:
        got = _IMMEDIATE
    else:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
        if not m:
            return UNKNOWN
        got = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(con["value"]).strip())
    if not m:
        return UNKNOWN
    want = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return PASS if got <= want else FAIL


def _p_feature(listing: dict, con: dict) -> str:
    feats = listing.get("features")
    if not isinstance(feats, list):
        return UNKNOWN            # v2 FORBIDS reading features out of the free-text blurb
    toks = {str(f).strip().casefold() for f in feats}
    return PASS if str(con["value"]).strip().casefold() in toks else FAIL


def _p_commute(tool_result: dict, con: dict) -> str:
    v = None
    for name in _FIELD_ALIASES["duration_minutes"]:
        v = _num(tool_result.get(name))
        if v is not None:
            break
    if v is None:
        return UNKNOWN
    return PASS if v <= float(con["value"]) else FAIL


# --------------------------------------------------------------------------- #
# The v2 type registry — the five frozen items per type
# --------------------------------------------------------------------------- #
TYPES: Dict[str, dict] = {
    "all_results_satisfy": {
        "slot": "budget",
        "migrated_from_v1": True,
        "required_args": ("field", "op", "value"),
        "arg_domains": {"field": ("monthly_rent",), "op": ("<=", "<", ">=", ">")},
        "user_text_normalisation": (
            "£X pcm / £X a month -> X. £X pw -> X*52/12 rounded to 2dp. A bare figure with "
            "no period marker is read as MONTHLY. Absence of a figure means the constraint "
            "was not stated (never 0)."),
        "evidence": ("listing", "price_raw"),
        "predicate": _p_budget,
        "scope": "listing",
        "completion_policy": {
            "no_result": "State honestly that nothing was found in budget; never claim the budget was met.",
            "unknown": "A listing with no price figure must be reported as price-unknown, not as in budget.",
            "partial": "Only listings inside the bound may be offered as matches; anything outside must not be presented as a match.",
        },
        "judge_evidence": ("listing.price_raw", "listing.price", "the user's own stated budget",
                           "the frozen weekly<->monthly and deposit formulas"),
    },
    "bedroom_count_match": {
        "slot": "bedroom_count",
        "new_in_v2": True,
        "split_from": "room_type_match",
        "required_args": ("op", "value"),
        "arg_domains": {"op": ("==", ">=", "<=", "between")},
        "user_text_normalisation": (
            "'a 2-bed'/'two bedroom' -> ==2; 'at least 2'/'2+' -> >=2; 'no more than 3' -> <=3; "
            "'2 to 3 bedrooms'/'2-3 beds' -> between [2,3]. 'studio' is NOT a bedroom_count "
            "constraint — it is a room_type label."),
        "evidence": ("listing", "bedrooms"),
        "predicate": _p_bedroom,
        "scope": "listing",
        "completion_policy": {
            "no_result": "State that no property with that bedroom count was found.",
            "unknown": "A listing with no bedrooms field is bedroom-count-unknown, not a match.",
            "partial": "Only listings whose bedrooms field satisfies the operator may be offered as matches.",
        },
        "judge_evidence": ("listing.bedrooms",),
    },
    "room_type_match": {
        "slot": "room_type",
        "migrated_from_v1": True,
        "v2_change": "LABEL ONLY. A numeric value is invalid in v2 — use bedroom_count_match.",
        "required_args": ("value",),
        "arg_domains": {"value": ROOM_TYPE_VOCAB},
        "user_text_normalisation": (
            "studio->studio; flat/apartment->flat; house->house; room/houseshare/flatshare/"
            "shared->room_in_shared; maisonette->maisonette; bungalow->bungalow. Longest "
            "surface form wins."),
        "evidence": ("listing", "room_type_normalized"),
        "predicate": _p_room_type,
        "scope": "listing",
        "completion_policy": {
            "no_result": "State that no property of that type was found.",
            "unknown": "A listing without room_type_normalized is type-unknown; do not present it as that type.",
            "partial": "Only listings of the requested type may be offered as matches.",
        },
        "judge_evidence": ("listing.room_type_normalized",),
    },
    "commute_leq_minutes": {
        "slot": "commute",
        "migrated_from_v1": True,
        "required_args": ("dest", "value"),
        "arg_domains": {},
        "user_text_normalisation": (
            "'within 30 minutes'/'half an hour' -> 30; 'under 1 hour' -> 60. The destination "
            "is stored verbatim in ``dest``."),
        "evidence": ("tool_result", "duration_minutes"),
        "predicate": _p_commute,
        "scope": "tool_result",
        "completion_policy": {
            "no_result": "If the commute tool returned no data, say so; never estimate a journey time.",
            "unknown": "A tool result without duration_minutes is commute-unknown, not within the limit.",
            "partial": "Only journeys whose measured duration is within the limit may be presented as meeting it.",
        },
        "judge_evidence": ("calculate_commute -> data.duration_minutes",),
    },
    "area_match": {
        "slot": "area",
        "new_in_v2": True,
        "required_args": ("granularity", "value"),
        "arg_domains": {"granularity": AREA_GRANULARITY},
        "user_text_normalisation": (
            "A postcode district token ('N1','SE15') -> granularity postcode_district; a "
            "district+inward digit ('N1 9') -> postcode_sector; 'near X'/'close to X' -> "
            "adjacent (which REQUIRES a frozen ``accept`` list on the constraint); anything "
            "else -> borough."),
        "evidence": ("listing", "area_normalized"),
        "predicate": _p_area,
        "scope": "listing",
        "matching_boundaries": (
            "postcode_district compares the WHOLE outward district with exact case-folded "
            "equality, so N1 does NOT match N1C (N1C is a distinct district, not a child of "
            "N1) and prefix matching is explicitly rejected. borough compares against the "
            "listing's area_normalized OR borough field. city compares against the listing's "
            "own city field — 'Camden is in London' holds only because the Camden listing "
            "carries city='London'; the schema has no implicit borough->city table. adjacent "
            "('near King's Cross') is satisfied ONLY by the case's frozen accept list; "
            "adjacency is never inferred at grading time."),
        "completion_policy": {
            "no_result": "State that nothing was found in that area; do not silently widen the area.",
            "unknown": "A listing without area_normalized is location-unknown, not a match.",
            "partial": "A listing outside the requested area must not be presented as being in it; offering it is only correct if the answer says it is outside.",
        },
        "judge_evidence": ("listing.area_normalized", "listing.borough", "listing.city",
                           "listing.postcode_district", "listing.postcode_sector"),
    },
    "move_in_date_satisfied": {
        "slot": "move_in_date",
        "new_in_v2": True,
        "required_args": ("op", "value"),
        "arg_domains": {"op": ("<=",)},
        "user_text_normalisation": (
            "A full date -> ISO YYYY-MM-DD. A bare month ('in September') -> the LAST day of "
            "that month, because any date inside the month satisfies the request. The stored "
            "value is the LATEST acceptable move-in date and the operator is always '<='."),
        "evidence": ("listing", "available_from_normalized"),
        "predicate": _p_move_in,
        "scope": "listing",
        "unknown_rule": (
            "'Contact agent' / 'On application' / 'TBC' / missing -> UNKNOWN. UNKNOWN is "
            "NEVER a failure and NEVER a satisfaction; it goes to behaviour coverage and the "
            "correct answer must say the date needs confirming. 'Now'/'Immediately' -> "
            "satisfies any future bound."),
        "completion_policy": {
            "no_result": "State that nothing was found available by that date.",
            "unknown": "Say the availability date is not published and must be confirmed with the agent. Do NOT claim the date is met, and do NOT reject the listing as unavailable.",
            "partial": "Only listings available on or before the date may be presented as meeting it.",
        },
        "judge_evidence": ("listing.available_from_normalized", "listing.available_from"),
    },
    "property_feature_present": {
        "slot": "property_feature",
        "new_in_v2": True,
        "required_args": ("value",),
        "arg_domains": {"value": FEATURE_VOCAB},
        "user_text_normalisation": (
            "Only the frozen FEATURE_VOCAB is expressible. 'unfurnished' is matched before "
            "'furnished' (longest surface form first). A request outside the vocabulary is "
            "NOT a property_feature constraint and must be written some other way."),
        "evidence": ("listing", "features"),
        "predicate": _p_feature,
        "scope": "listing",
        "free_text_rule": (
            "The predicate reads ONLY the structured listing.features list. Inferring a "
            "feature from the listing's free-text description/explanation is FORBIDDEN — "
            "for both the grader and the judge."),
        "completion_policy": {
            "no_result": "State that nothing was found with that feature.",
            "unknown": "A listing without a structured features list is feature-unknown; do not assert the feature.",
            "partial": "Only listings whose structured features list carries the token may be presented as having it.",
        },
        "judge_evidence": ("listing.features (structured list only)",),
    },
}

# slot -> type (v2 is 1:1; the v1 overload is gone)
SLOT_TYPE = {spec["slot"]: name for name, spec in TYPES.items()}
INCLUDED_TYPES = frozenset(TYPES)
MACHINE_CHECKABLE = INCLUDED_TYPES                 # drop-in name for holdout_preflight
REQUIRED_ARGS = {n: s["required_args"] for n, s in TYPES.items()}
MISSING_TYPES: Tuple[str, ...] = ()                # v2 covers all seven slots

# Every semantic slot now HAS a type, so per-slot quotas are countable without parsing
# a value string (the v1 room_type_match overload is what made that impossible).
SLOT_MIN_COVERAGE = {
    "budget": 15, "bedroom_count": 12, "room_type": 8, "commute": 12,
    "area": 12, "move_in_date": 8, "property_feature": 8,
}
BEHAVIOR_MIN_COVERAGE = {"no_result_or_unknown": 12}

# --------------------------------------------------------------------------- #
# EXCLUDED: instrument / test conditions. Listed explicitly so the audit can tell
# "deliberately excluded" from "forgotten" — an include-list alone cannot.
# --------------------------------------------------------------------------- #
EXCLUDED_INSTRUMENT_TYPES = frozenset({
    "must_call_tool", "must_not_call_tool", "must_ask_clarification",
    "must_complete_requested_dimensions",
    "no_fabricated_number", "must_mention_source", "must_note_missing_data",
    "must_refuse_fabrication", "must_mention_value", "must_not_mention_value",
    "no_self_contradictory_value", "must_flag_contradiction", "reference_calc_match",
    "must_flag_unrealistic_constraint", "must_flag_stale_data",
    "must_mention_source_if_evidence", "room_type_match_if_evidence",
    "no_false_retrieval_provenance",
    "must_recall_value", "must_forget", "must_retain_value", "must_supersede_value",
    "memory_isolation",
    "resist_prompt_injection",
    "result_count",
    # v2 additions — still instrument conditions, not user housing conditions:
    "must_report_unknown_availability",   # the move-in UNKNOWN behaviour obligation
    "must_not_present_violating_listing", # the surfaced-set contract, stated in prose
})

_V1_ONLY = frozenset({"max_budget"})   # v1 heuristic money check; superseded by all_results_satisfy


def audit_vocabulary(all_types) -> dict:
    """Three buckets, and UNCLASSIFIED must be empty or the gate fails."""
    s = set(all_types)
    return {"schema_version": SCHEMA_VERSION,
            "user_hard": sorted(s & INCLUDED_TYPES),
            "excluded_instrument": sorted(s & (EXCLUDED_INSTRUMENT_TYPES | _V1_ONLY)),
            "UNCLASSIFIED": sorted(s - INCLUDED_TYPES - EXCLUDED_INSTRUMENT_TYPES - _V1_ONLY)}


def slot_of(constraint: dict) -> Optional[str]:
    spec = TYPES.get(constraint.get("type"))
    return spec["slot"] if spec else None


def user_hard_constraints(case: dict) -> List[dict]:
    return [c for c in (case.get("expected_constraints") or [])
            if c.get("type") in INCLUDED_TYPES]


# --------------------------------------------------------------------------- #
# Fixture access
# --------------------------------------------------------------------------- #
def _payloads(case: dict, fixtures_dir) -> List[Any]:
    import json as _json
    from pathlib import Path as _P
    fx = case.get("fixture")
    if not fx:
        return []
    names = [fx] if isinstance(fx, str) else list(fx)
    out: List[Any] = []
    for name in names:
        path = _P(fixtures_dir) / name
        if not path.is_file():
            continue
        try:
            raw = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = raw["results"] if isinstance(raw, dict) and "results" in raw else [raw]
        for it in items:
            if isinstance(it, dict):
                out.append(it.get("data"))
    return out


def fixture_listings(case: dict, fixtures_dir) -> List[dict]:
    out: List[dict] = []
    for data in _payloads(case, fixtures_dir):
        if isinstance(data, dict):
            out += [r for r in (data.get("recommendations") or []) if isinstance(r, dict)]
    return out


def fixture_tool_results(case: dict, fixtures_dir) -> List[dict]:
    return [d for d in _payloads(case, fixtures_dir) if isinstance(d, dict)]


_fixture_listings = fixture_listings          # drop-in alias for holdout_preflight


def _records_for(constraint: dict, case: dict, fixtures_dir) -> List[dict]:
    """The frozen records THIS constraint is about.

    For a tool-result-scoped constraint (today: commute) that means only the payloads
    that actually carry the evidence field — the search_properties payload sitting in the
    same fixture is not a commute measurement and must not be dragged in as UNKNOWN.
    """
    spec = TYPES[constraint["type"]]
    if spec["scope"] == "listing":
        return fixture_listings(case, fixtures_dir)
    names = _FIELD_ALIASES.get(spec["evidence"][1], (spec["evidence"][1],))
    return [d for d in fixture_tool_results(case, fixtures_dir)
            if any(n in d for n in names)]


def constraint_branch(case: dict, constraint: dict, fixtures_dir) -> str:
    """Which branch of the frozen evidence this constraint can exercise.

      ``satisfaction`` — the evidence holds at least one record that satisfies it AND at
                         least one that breaks it, so an answer that filters and one that
                         does not give different verdicts. ONLY this enters the
                         satisfaction denominator.
      ``no_result``    — the evidence holds no relevant record at all.
      ``unknown``      — every relevant record is unknown on the field (a move-in date
                         published as 'Contact agent'). Never a pass, never a failure.
      ``trivial``      — every record falls the same way, so the case cannot tell a
                         filtering assistant from a non-filtering one. This is exactly
                         v1's vacuous-pass hole; it is neither satisfaction nor a
                         no-result/unknown behaviour branch.
    """
    spec = TYPES.get(constraint.get("type"))
    if not spec:
        return "no_result"
    recs = _records_for(constraint, case, fixtures_dir)
    if not recs:
        return "no_result"
    verdicts = [spec["predicate"](r, constraint) for r in recs]
    if all(v == UNKNOWN for v in verdicts):
        return "unknown"
    if PASS in verdicts and FAIL in verdicts:
        return "satisfaction"
    return "trivial"


def constraint_is_satisfaction_verifiable(case: dict, constraint: dict, fixtures_dir) -> bool:
    """Can this constraint be decided by a deterministic predicate on THIS case?"""
    return constraint_branch(case, constraint, fixtures_dir) == "satisfaction"


def slot_coverage(cases, fixtures_dir=None) -> dict:
    """Two denominators, never merged (owner ruling 2026-08-05).

    satisfaction — a deterministic predicate can decide pass/fail on this case;
    behavior_only — the constraint exists but only the no-result / unknown branch is
                    reachable, so it can show correct handling and nothing more.
    """
    from collections import Counter
    from pathlib import Path as _P
    if fixtures_dir is None:
        fixtures_dir = _P(__file__).resolve().parents[3] / "evaluation" / "benchmark" / "fixtures"
    sat, beh = Counter(), Counter()
    n_behavior = 0
    for case in cases:
        s_slots, b_slots = set(), set()
        branches = set()
        for con in user_hard_constraints(case):
            slot = slot_of(con)
            if slot is None:
                continue
            br = constraint_branch(case, con, fixtures_dir)
            branches.add(br)
            if br == "satisfaction":
                s_slots.add(slot)
            else:
                b_slots.add(slot)
        for s in s_slots:
            sat[s] += 1
        for s in b_slots:
            beh[s] += 1
        # Behaviour coverage = "this case exercises correct handling of NO RESULT or of an
        # UNKNOWN value". A constraint that is merely trivially satisfied is not that, and
        # a calculation / clarify / memory case has no listing by design, so neither is
        # counted. The second arm catches a retrieval case that declares a search fixture
        # and gets nothing back.
        is_retrieval = str(case.get("task_category") or "retrieval").startswith("retrieval")
        if (branches & {"unknown", "no_result"}) or (
                is_retrieval and case.get("fixture")
                and not fixture_listings(case, fixtures_dir)):
            n_behavior += 1
    return {"satisfaction": {s: sat.get(s, 0) for s in SLOT_MIN_COVERAGE},
            "behavior_only": {s: beh.get(s, 0) for s in SLOT_MIN_COVERAGE},
            "no_result_or_unknown_cases": n_behavior}


# --------------------------------------------------------------------------- #
# The surfaced set — which frozen listings did the ANSWER put in front of the user
# --------------------------------------------------------------------------- #
def _money_variants(v: float) -> List[str]:
    n = int(round(v))
    return [f"{n:,}", str(n)]


_STREET_FROM_ADDRESS = re.compile(r"^\s*\d+[A-Za-z]?\s+([^,]+)")


def listing_tokens(listing: dict) -> List[str]:
    """Identifying tokens for one frozen record.

    The primary token is a distinctive multi-word street name, taken from ``uid_token``
    when the fixture carries one, otherwise parsed off the front of ``address``
    ('12 Fernbrook Row, London N1 4QQ' -> 'Fernbrook Row'). A commute measurement carries
    ``origin_uid`` naming the property it was measured from. The exact monthly price is
    accepted as a second token. The held-out generator guarantees (a) street names are
    unique inside a case and (b) no listing price equals any constraint value in the case,
    so neither token can be hit by an answer that merely repeats the user's own figures.
    """
    toks: List[str] = []
    for key in ("uid_token", "origin_uid"):
        v = listing.get(key)
        if isinstance(v, str) and v.strip():
            toks.append(v.strip().casefold())
    if not toks:
        m = _STREET_FROM_ADDRESS.match(str(listing.get("address") or ""))
        if m and m.group(1).strip():
            toks.append(m.group(1).strip().casefold())
    p = _num(listing.get("price_raw"))
    if p is not None:
        toks += [t.casefold() for t in _money_variants(p)]
    return toks


def surfaced_listings(answer: str, listings: List[dict]) -> List[dict]:
    """Frozen records the answer actually names. Deterministic substring match.

    A record carrying NO identifying token cannot be filtered and is always in scope.
    """
    a = (answer or "").casefold()
    out = []
    for l in listings:
        toks = listing_tokens(l)
        if not toks or any(t in a for t in toks):
            out.append(l)
    return out


def evaluate_constraint(case: dict, constraint: dict, answer: str, fixtures_dir) -> dict:
    """Deterministic verdict for ONE constraint on ONE answer.

    Verdict ladder (frozen):
      NO_RESULT     — the frozen evidence carries no record at all -> behaviour coverage
      NOT_SURFACED  — the answer names none of the frozen listings -> behaviour coverage
      UNKNOWN       — every relevant record is unknown on this field -> behaviour coverage
      FAIL          — the answer surfaced at least one record that breaks the constraint
      PASS          — every surfaced record satisfies it
    Only PASS/FAIL enter the satisfaction denominator.
    """
    spec = TYPES[constraint["type"]]
    recs = _records_for(constraint, case, fixtures_dir)
    if not recs:
        return {"verdict": NO_RESULT, "slot": spec["slot"], "n_records": 0,
                "n_surfaced": 0, "offenders": []}
    sel = surfaced_listings(answer, recs)
    if not sel:
        return {"verdict": NOT_SURFACED, "slot": spec["slot"], "n_records": len(recs),
                "n_surfaced": 0, "offenders": []}
    verdicts = [(r, spec["predicate"](r, constraint)) for r in sel]
    bad = [r for r, v in verdicts if v == FAIL]
    if bad:
        return {"verdict": FAIL, "slot": spec["slot"], "n_records": len(recs),
                "n_surfaced": len(sel),
                "offenders": [str(r.get("uid_token") or r.get("address") or "?") for r in bad]}
    if all(v == UNKNOWN for _, v in verdicts):
        return {"verdict": UNKNOWN, "slot": spec["slot"], "n_records": len(recs),
                "n_surfaced": len(sel), "offenders": []}
    return {"verdict": PASS, "slot": spec["slot"], "n_records": len(recs),
            "n_surfaced": len(sel), "offenders": []}


# --------------------------------------------------------------------------- #
# judge prompt scope (same frozen enumeration feeds quota / preflight / judge)
# --------------------------------------------------------------------------- #
JUDGE_PROMPT_SCOPE = (
    "「硬约束」= 用户在本轮明确提出的、可被答案满足或违反的住房条件，共七类："
    "预算上限/下限、卧室数量、房型（studio/flat/house/合租房间…）、地点或区域、"
    "入住日期、通勤时间上限、明确的物业特征（家具/宠物/花园/停车…）。\n"
    "**不属于硬约束**：是否调用了某个工具、是否引用了来源、是否避免了捏造数字、"
    "是否抵御了注入、记忆是否隔离——这些是测试条件，不是用户提出的住房条件。\n"
    "用户本轮没有提出任何上述住房条件时，答 not_applicable。\n"
    "证据里 Available 写成 'Contact agent' 之类未确定的值时，入住日期视为 unknown："
    "既不算满足也不算违反，正确行为是说明需要向中介确认。"
)


# --------------------------------------------------------------------------- #
# "Explicitly stated" — v2 makes this checkable instead of trusting a number match
# --------------------------------------------------------------------------- #
# v1's H3 asked "does the constraint's numeric value appear somewhere in the request
# text?". That is both too weak (a 2 in "2 bedrooms" satisfies a budget of 2) and too
# strong (a move-in date normalises to an ISO string that never appears literally).
# v2 requires every user hard constraint to carry ``user_text``: the VERBATIM span of the
# request that states it. The gate then re-runs the frozen normaliser on that span and
# checks it reproduces the stored value — so the normalisation rule is exercised once per
# case, on real user wording, before a single model request is spent.
_RENORMALISE = {
    "all_results_satisfy":     lambda t: {"value": normalise_budget(t)},
    "bedroom_count_match":     lambda t: normalise_bedroom_count(t),
    "room_type_match":         lambda t: {"value": normalise_room_type(t)},
    "commute_leq_minutes":     lambda t: {"value": normalise_commute_minutes(t)},
    "area_match":              lambda t: normalise_area(t),
    "move_in_date_satisfied":  lambda t: {"value": normalise_move_in_date(t)},
    "property_feature_present": lambda t: {"value": normalise_feature(t)},
}


def restate_from_user_text(constraint: dict) -> Optional[dict]:
    """Re-derive {op?, value} from the constraint's frozen ``user_text`` span."""
    fn = _RENORMALISE.get(constraint.get("type"))
    if not fn:
        return None
    txt = constraint.get("user_text")
    if not isinstance(txt, str) or not txt.strip():
        return None
    try:
        return fn(txt)
    except Exception:                                     # noqa: BLE001
        return None


def _eq(a, b) -> bool:
    if isinstance(a, list) or isinstance(b, list):
        return list(a or []) == list(b or [])
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    return a == b


def explicitness_problems(case: dict) -> List[str]:
    """H6 — every user hard constraint is stated verbatim and re-normalises to its value."""
    hay = ((case.get("user_query") or "") + " " +
           " ".join(t.get("content", "") for t in (case.get("conversation_history") or [])))
    hay_cf = hay.casefold()
    out: List[str] = []
    for c in user_hard_constraints(case):
        span = c.get("user_text")
        if not isinstance(span, str) or not span.strip():
            out.append(f"H6 {c['type']} 缺少 user_text（无法证明用户明确说过）")
            continue
        if span.strip().casefold() not in hay_cf:
            out.append(f"H6 {c['type']} 的 user_text {span!r} 不是请求/历史的原文子串")
            continue
        got = restate_from_user_text(c)
        if got is None:
            out.append(f"H6 {c['type']} 的 user_text {span!r} 无法按冻结规则规范化")
            continue
        if "op" in got and got.get("op") is not None and c.get("op") is not None \
                and got["op"] != c["op"]:
            out.append(f"H6 {c['type']} user_text 规范化出 op={got['op']}，但存的是 {c['op']}")
        want = c.get("value")
        if c["type"] == "area_match":
            if got.get("granularity") != c.get("granularity"):
                out.append(f"H6 area_match user_text 规范化出 granularity="
                           f"{got.get('granularity')}，但存的是 {c.get('granularity')}")
            continue          # the area token itself is free-form; granularity is the rule
        if not _eq(got.get("value"), want):
            out.append(f"H6 {c['type']} user_text 规范化出 {got.get('value')!r}，"
                       f"但存的是 {want!r}")
    return out


# --------------------------------------------------------------------------- #
# H4 — mutually contradictory constraints, per slot
# --------------------------------------------------------------------------- #
def contradictions(constraints: List[dict]) -> List[str]:
    by_slot: Dict[str, List[dict]] = {}
    for c in constraints:
        s = slot_of(c)
        if s:
            by_slot.setdefault(s, []).append(c)

    out: List[str] = []
    ups = [float(c["value"]) for c in by_slot.get("budget", [])
           if c.get("op") in ("<=", "<")]
    lows = [float(c["value"]) for c in by_slot.get("budget", [])
            if c.get("op") in (">=", ">")]
    if ups and lows and min(ups) < max(lows):
        out.append(f"H4 budget 上界 {min(ups)} 低于下界 {max(lows)} -> 互相矛盾")

    lo, hi = 0.0, float("inf")
    for c in by_slot.get("bedroom_count", []):
        op, v = c.get("op", "=="), c["value"]
        if op == "==":
            lo, hi = max(lo, float(v)), min(hi, float(v))
        elif op == ">=":
            lo = max(lo, float(v))
        elif op == "<=":
            hi = min(hi, float(v))
        elif op == "between":
            lo, hi = max(lo, float(v[0])), min(hi, float(v[1]))
    if lo > hi:
        out.append(f"H4 bedroom_count 的区间为空 [{lo}, {hi}] -> 互相矛盾")

    for slot in ("room_type", "area", "move_in_date"):
        vals = {str(c.get("value")) for c in by_slot.get(slot, [])}
        if len(vals) > 1:
            out.append(f"H4 {slot} 上有互相冲突的值 {sorted(vals)}")

    feats = {str(c.get("value")) for c in by_slot.get("property_feature", [])}
    for a, b in (("furnished", "unfurnished"), ("furnished", "part_furnished"),
                 ("unfurnished", "part_furnished")):
        if a in feats and b in feats:
            out.append(f"H4 property_feature 同时要求 {a} 与 {b} -> 互相矛盾")
    return out


def arg_domain_problems(constraint: dict) -> List[str]:
    """H2b — an argument outside its frozen domain is not machine-verifiable."""
    spec = TYPES.get(constraint.get("type"))
    if not spec:
        return []
    out = []
    for arg, domain in spec.get("arg_domains", {}).items():
        v = constraint.get(arg)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            continue
        if v not in domain:
            out.append(f"H2b {constraint['type']}.{arg}={v!r} 不在冻结词表 {list(domain)} 中")
    if constraint.get("type") == "room_type_match" and \
            re.match(r"\d", str(constraint.get("value", ""))):
        out.append("H2b room_type_match 的 value 以数字开头 —— v2 已把卧室数拆到 "
                   "bedroom_count_match，房型类型只接受标签")
    return out


def freeze_digest() -> dict:
    """A compact machine-readable dump of everything v2 freezes (goes into the report)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "types": {
            n: {"slot": s["slot"],
                "required_args": list(s["required_args"]),
                "arg_domains": {k: list(v) for k, v in s.get("arg_domains", {}).items()},
                "user_text_normalisation": s["user_text_normalisation"],
                "evidence_scope_field": list(s["evidence"]),
                "completion_policy": s["completion_policy"],
                "judge_evidence": list(s["judge_evidence"]),
                **({"matching_boundaries": s["matching_boundaries"]}
                   if "matching_boundaries" in s else {}),
                **({"unknown_rule": s["unknown_rule"]} if "unknown_rule" in s else {}),
                **({"free_text_rule": s["free_text_rule"]} if "free_text_rule" in s else {}),
                }
            for n, s in TYPES.items()},
        "room_type_vocab": list(ROOM_TYPE_VOCAB),
        "feature_vocab": list(FEATURE_VOCAB),
        "area_granularity": list(AREA_GRANULARITY),
        "date_unknown_markers": list(_DATE_UNKNOWN_MARKERS),
        "slot_min_coverage": SLOT_MIN_COVERAGE,
        "behavior_min_coverage": BEHAVIOR_MIN_COVERAGE,
        "excluded_instrument_types": sorted(EXCLUDED_INSTRUMENT_TYPES),
        "satisfaction_denominator_rule": (
            "Only PASS/FAIL enter it. no_result / not_surfaced / unknown are behaviour "
            "coverage. A constraint needs a violation trap in the frozen fixture (at least "
            "one PASS record and at least one FAIL record) before it can be counted."),
    }
