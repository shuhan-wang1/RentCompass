"""Arithmetic on this turn's measured journey times: ONE definition of "derived".

A figure is derived from the measured routes when it is

* the difference of two measured routes ("12 minutes each way", "8 minutes slower");
* that difference x 2 (there and back), x the commuting days per week, or both ("24 minutes a
  day", "roughly 2 hours a week"); days per week are 5, the user's own office days, or an
  N-day week the answer itself states;
* the stated result of an explicit product ("12 minutes x 2 journeys x 5 days = 120 minutes")
  whose every operand is a measured figure, a measured difference or a standard multiplier,
  and whose result is right;
* a monthly total = that weekly total x 52/12 (written 52 ÷ 12, 4.3, 4.33 or 4.345), or x 4
  where the answer frames a month as four weeks ("using 4 weeks as a month", "48 x 4") or
  hedges the figure; a yearly total = the weekly total x 52 or the monthly one x 12; an
  explicit conversion step ("48 min × 52 weeks ÷ 12", "48 × 4.33 = about 208 minutes") whose
  weekly operand derives and whose result is right (r17 8c35fd5b). The same total in hours or
  minutes ("192 minutes, i.e. 3 hours 12 minutes") is the same figure.

Exact (to the precision written: "3.5 hours" is 3.45-3.55 hours), or within 10% (at least 2
minutes) when the figure is hedged ("roughly", "about").

Used by the critic's number grounding (``critic.unsupported_external_numbers``) and by the
answer's commute redactor (``core.candidate_validation``), so the two can never disagree about
whether "40 minutes a week" follows from Holloway 25 and Kentish Town 21 (r13 2264a2d5).
"""
from __future__ import annotations

import bisect
import functools
import re
from itertools import combinations, product as _cartesian
from typing import Any, Iterable

STANDARD_DAYS = 5.0
WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_NUMBER_WORD = r"(?:[1-7]|one|two|three|four|five|six|seven)"

DURATION_WORDS = (
    ("an hour and a half", 90.0), ("one and a half hours", 90.0), ("an hour and a quarter", 75.0),
    ("three quarters of an hour", 45.0), ("half an hour", 30.0), ("half-an-hour", 30.0),
    ("a half hour", 30.0), ("a quarter of an hour", 15.0), ("quarter of an hour", 15.0),
    ("two hours", 120.0), ("an hour", 60.0), ("one hour", 60.0),
)
# "about 3.5 extra hours a month" (r20 f510d8f3) is a figure too: it escaped every check.
_MORE = r"(?:(?:extra|more|additional|fewer|less)[ \t]+)?"
HOURS_AND_MINUTES = re.compile(
    r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*" + _MORE + r"(?:hours?|hrs?|h)\b(?:\s*(?:and\s+)?(\d{1,2})\s*(?:minutes?|mins?)\b)?",
    re.I)
MINUTES = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)[\s\-–]*" + _MORE + r"(?:minutes?|mins?\b|分钟)", re.I)
HEDGE = re.compile(r"\b(?:roughly|about|around|approximately|approx\.?|nearly|almost|"
                   r"just\s+(?:over|under)|some|close\s+to)\b|~|≈", re.I)
# "Per office day: 11 minutes each way" (r18 eb65e918) is a day too, as ``_UNIT_PHRASES`` reads it.
DAY_PERIOD = re.compile(r"\b(?:a|per|each|every)\s+(?:working\s+|office\s+|commuting\s+)?day\b|\bdaily\b|\breturn\s+trips?\b|\bround\s+trips?\b|"
                        r"\bthere\s+and\s+back\b|\btwo\s+journeys\b|\b2\s+journeys\b", re.I)
WEEK_PERIOD = re.compile(r"\b(?:a|per|each|every)\s+week\b|\bweekly\b|\bweek\b", re.I)
# A product: "12 minutes × 2 journeys × 5 days = **120 minutes (2 hours) a week**".
_UNITS = r"minutes?|mins?|journeys?|trips?|ways?|days?|times|legs?|weeks?|months?"
_OPERAND = r"(\d+(?:\.\d+)?)\s*(" + _UNITS + r")?\b"
_OPERAND_NC = r"\d+(?:\.\d+)?\s*(?:" + _UNITS + r")?\b"
# A bare result is minutes when the period follows it: "38 x 3 = 114 a week" (r19 W3b) was
# no product at all, so a wrong step on a removed figure shipped unjudged. Money is no operand.
PRODUCT = re.compile(
    r"(?<![\d.,£$€])" + _OPERAND + r"((?:\s*[×x*]\s*" + _OPERAND_NC + r"){1,3})"
    r"\s*=\s*[*_]*\s*(\d+(?:\.\d+)?)[ \t]{0,3}(?:(minutes?|mins?|hours?|hrs?)"
    r"|(?=(?:a|per|each|every)[ \t]{1,3}(?:day|week|month|year)\b))", re.I)
_PRODUCT_TERM = re.compile(r"[×x*]\s*" + _OPERAND, re.I)
_COUNT_UNITS = ("journey", "trip", "way", "time", "leg")


_WORD_HOURS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:and\s+an?\s+(half|quarter)\s+)?hours?\b", re.I)
_HOUR_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
               "nine": 9, "ten": 10}


def _word_hours(match) -> float:
    extra = {"half": 30.0, "quarter": 15.0}.get((match.group(2) or "").lower(), 0.0)
    return _HOUR_WORDS[match.group(1).lower()] * 60.0 + extra


def durations(text: Any) -> list[float]:
    """Every duration ``text`` states, in minutes: "2 hours 20 minutes" is one 140."""
    return [value for _start, _end, value in figure_spans(str(text or ""))]


def days_stated(text: Any) -> set[float]:
    """Commuting days per week a text states: "a five-day week", "3 days a week", "three
    office days", "4 days in the office" (r14 95f29d0b)."""
    return set(_days_stated(str(text or "")))


# Cached: figure_days asks for the sentence's days once per figure (quadratic on a long one).
@functools.lru_cache(maxsize=256)
def _days_stated(text: str) -> frozenset:
    days = set()
    pattern = (r"\b(" + _NUMBER_WORD + r")[\s-]*(?:office\s+)?days?"
               r"(?:\s+(?:a|per)\s+week|[\s-]+(?:office\s+)?(?:week|commute)|\s+in\s+the\s+office)"
               r"|\b(" + _NUMBER_WORD + r")\s+office\s+days?\b"
               r"|\bon\s+(" + _NUMBER_WORD + r")\s+days\b")
    for match in re.finditer(pattern, text, re.I):
        word = (match.group(1) or match.group(2) or match.group(3)).lower()
        days.add(float(WORD_NUMBERS.get(word) or int(word)))
    return frozenset(days)


def differences(measured: Iterable[float]) -> dict[float, list[tuple[float, float]]]:
    """Each positive difference of two measured figures, with the pairs it comes from."""
    values = sorted({float(v) for v in measured if isinstance(v, (int, float)) and v >= 0})[:40]
    out: dict[float, list[tuple[float, float]]] = {}
    for a, b in combinations(values, 2):
        out.setdefault(round(b - a, 2), []).append((a, b))
    return out


def multipliers(days: Iterable[float]) -> set[float]:
    """There and back, and per commuting week: x2, x days, x 2 x days."""
    days = {float(d) for d in days if d} | {STANDARD_DAYS}
    return {2.0} | days | {2.0 * d for d in days}


def close(value: float, target: float, hedged: bool, rounding: float = 0.5) -> bool:
    """``rounding``: half a unit of the figure's last written digit, in minutes."""
    return abs(value - target) <= max(rounding, max(2.0, 0.1 * target) if hedged else 0.5)


# A month is 52/12 weeks however the answer writes the factor; four weeks only when it says so.
WEEKS_PER_MONTH = (52.0 / 12.0, 4.3, 4.33, 4.345)
FOUR_WEEK_MONTH = re.compile(r"\b(?:4|four)[\s-]*weeks?\b|[×x*]\s*4(?![\d.])", re.I)
LONG_PERIOD = re.compile(r"\b(?:a|per|each|every|over\s+(?:a|the)(?:\s+typical)?|in\s+a)\s+(?:month|year)\b|"
                         r"\bmonthly\b|\byearly\b|\bannual(?:ly)?\b", re.I)
# The rent's period is not the commute's: "£2,350 a month", "2,350 pcm".
# Every gap is bounded, so a long blank run costs O(1) per figure (the B-01 class).
_MONEY_PERIOD = re.compile(
    r"(?:[£$€][ \t]{0,3}\d[\d,.]*k?|(?<![\d.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\d.,])\d{3,}(?:\.\d+)?)"
    r"(?:[ \t]{0,3}(?:pcm|pw|p/?m)\b|[ \t]{0,3}/[ \t]{0,3}(?:month|week|mo|year|yr)\b"
    r"|[ \t]{1,3}(?:per|a|each)[ \t]{1,3}(?:month|week|year)\b|[ \t]{1,3}(?:monthly|weekly)\b)"
    r"|[£$€][ \t]{0,3}\d[\d,.]*k?", re.I)


def without_money(text: str) -> str:
    """``text`` with each money figure and the period it is quoted for blanked, in place."""
    return _without_money(str(text or ""))


@functools.lru_cache(maxsize=256)
def _without_money(text: str) -> str:
    # Cached: figure_unit asks for the whole text and sentence once per FIGURE, which made a
    # long answer quadratic (25 KB of "about 3 hours a month, …" took 11 s in the critic).
    return _MONEY_PERIOD.sub(lambda m: " " * len(m.group()), text)


_DECIMAL_HOURS = re.compile(r"\s*\d+\.(\d+)\s*(?:hours?|hrs?|h)\b", re.I)


_WHOLE_HOURS = re.compile(r"\s*(\d{1,2})\s*" + _MORE + r"(?:hours?|hrs?)\b(?!\s*(?:and\s+)?\d)", re.I)


def written_rounding(figure: str) -> float:
    """Half a unit of the last digit ``figure`` is written to, in minutes: "3.5 hours" is any
    time from 3.45 to 3.55 hours (3 minutes either way); "208 minutes" is 207.5 to 208.5; "3
    hours" (two or more whole hours, no minutes) is written to the hour — "the 3 hours a month"
    for 182 minutes (r21 8f4f0a00)."""
    match = _DECIMAL_HOURS.match(str(figure or ""))
    if match:
        return max(0.5, 30.0 * 10 ** -len(match.group(1)))
    whole = _WHOLE_HOURS.match(str(figure or ""))
    if whole and int(whole.group(1)) >= 2:
        return 29.0       # to the nearest hour; an exact half ("2 hours" for 90 minutes) is not
    return 0.5


def is_difference(value: float, measured: Iterable[float], involving: float | None = None) -> bool:
    for diff, pairs in differences(measured).items():
        if abs(value - diff) < 1e-9 and (involving is None or any(involving in pair for pair in pairs)):
            return True
    return False


def is_derived_total(value: float, measured: Iterable[float], days: Iterable[float], *,
                     hedged: bool = False, involving: float | None = None) -> bool:
    """``value`` = a measured difference x a commute multiplier (x2, x days, x2 x days)."""
    factors = multipliers(days)
    for diff, pairs in differences(measured).items():
        if diff <= 0 or (involving is not None and not any(involving in pair for pair in pairs)):
            continue
        if any(close(value, diff * factor, hedged) for factor in factors):
            return True
    return False


def products(text: Any) -> list[dict]:
    """Every explicit product in ``text``: operands with units, stated result in minutes."""
    found = []
    for match in PRODUCT.finditer(str(text or "")):
        operands = [(float(match.group(1)), (match.group(2) or "").lower())]
        operands += [(float(term.group(1)), (term.group(2) or "").lower())
                     for term in _PRODUCT_TERM.finditer(match.group(3))]
        result = float(match.group(4)) * (60.0 if (match.group(5) or "").lower().startswith("h") else 1.0)
        found.append({"operands": operands, "result": result, "span": match.span()})
    return found


# "25 − 22 = 3 minutes each way": a subtraction step of two measured routes.
# Bounded gaps: two unbounded blank runs around an optional unit made a long blank run after a
# number quadratic (the B-01 class); r6 put more sentences through this check.
SUBTRACTION = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)(?:[ \t]{0,3}(?:minutes?|mins?))?[ \t]{0,3}(?:[−–-]|minus)[ \t]{0,3}"
    r"(\d+(?:\.\d+)?)(?:[ \t]{0,3}(?:minutes?|mins?))?"
    r"[ \t]{0,3}=[ \t]{0,3}[*_]{0,3}[ \t]{0,3}(\d+(?:\.\d+)?)[ \t]{0,3}(?:minutes?|mins?)", re.I)


def subtractions(text: Any) -> list[dict]:
    return [{"a": float(m.group(1)), "b": float(m.group(2)), "result": float(m.group(3)), "span": m.span()}
            for m in SUBTRACTION.finditer(str(text or ""))]


def subtraction_is_correct(step: dict, measured: Iterable[float], caps: Iterable[float] = ()) -> bool:
    """Both operands are measured routes, or one is the user's limit ("25 − 19 = 6 minutes
    under", "29 − 25 = 4 minutes over", r16 0a6c11d6), and the result is right."""
    points = list(measured) + [float(c) for c in caps if c]
    return (any(abs(step["a"] - m) < 1e-9 for m in points)
            and any(abs(step["b"] - m) < 1e-9 for m in points)
            and (any(abs(step["a"] - m) < 1e-9 for m in measured)
                 or any(abs(step["b"] - m) < 1e-9 for m in measured))
            and abs(step["a"] - step["b"] - step["result"]) <= 0.5)


def _derived_exactly(value: float, diffs: Iterable[float], days: Iterable[float]) -> bool:
    factors = multipliers(days)
    return any(abs(value - d * f) <= 0.5 for d in diffs for f in factors)


def _period_factor(value: float, period: str | None) -> bool:
    """A month or year multiplier for a total over that period: x 4.33 (x 4) weeks a month,
    x 52 weeks or x 12 months a year."""
    if period == "month":
        return value == 4.0 or any(abs(value - m) < 0.005 for m in WEEKS_PER_MONTH)
    if period == "year":
        return value in (12.0, 52.0)
    return False


def product_is_correct(product: dict, measured: Iterable[float], days: Iterable[float],
                       involving: float | None = None, diffs: Iterable[float] | None = None,
                       period: str | None = None) -> bool:
    """Every operand is evidenced and the stated result is the product.

    The minutes operand is a measured figure, a difference of the pair (``diffs``, or any
    difference involving ``involving``), or a total already derived from that difference:
    the chained step "3 x 2 = 6 minutes per day, and 6 x 5 = 30 minutes per week" (r14
    95f29d0b) multiplies an earlier correct step."""
    measured = list(measured)
    days = {float(d) for d in days if d} | {STANDARD_DAYS}
    pair = set(diffs) if diffs is not None else {
        d for d, pairs in differences(measured).items()
        if involving is None or any(involving in p for p in pairs)}
    total = 1.0
    minute_operands = 0
    for value, unit in product["operands"]:
        total *= value
        if unit.startswith("min") or not unit and minute_operands == 0:
            minute_operands += 1
            if not (any(abs(value - m) < 1e-9 for m in measured)
                    or any(abs(value - d) < 1e-9 for d in pair)
                    or _derived_exactly(value, pair, days)):
                return False
        elif unit.startswith("day"):
            if not any(abs(value - d) < 1e-9 for d in days):
                return False
        elif unit.startswith(("week", "month")):
            # "x 4.33 weeks" for a monthly total, "x 52 weeks" / "x 12 months" for a yearly one.
            if not _period_factor(value, period):
                return False
        elif unit.startswith(_COUNT_UNITS):
            # 2 a day, or the week's journeys: "x 10 journeys" for 5 days, "6 journeys" for 3.
            if value not in (1.0, 2.0) and not any(abs(value - 2 * d) < 1e-9 for d in days):
                return False
        elif not unit:
            if value not in (1.0, 2.0) and not any(abs(value - d) < 1e-9 or abs(value - 2 * d) < 1e-9
                                                   for d in days) and not _period_factor(value, period):
                return False
        else:
            return False
    return minute_operands == 1 and abs(total - product["result"]) <= 0.5


def route_minutes(evidence: Any, *, _depth: int = 0, _key: str = "") -> set[float]:
    """Whole-trip measured minutes in structured tool evidence (never a single leg)."""
    found: set[float] = set()
    if _depth > 8 or evidence is None:
        return found
    if isinstance(evidence, dict):
        for key, value in evidence.items():
            if key in ("route_legs", "legs", "steps"):
                continue
            if key in ("commute_minutes", "duration_minutes", "verified_commute_minutes") \
                    and isinstance(value, (int, float)) and not isinstance(value, bool):
                found.add(float(value))
            elif key == "travel_time" and isinstance(value, str):
                match = re.match(r"\s*~?\s*(\d{1,3})\s*min", value)
                if match:
                    found.add(float(match.group(1)))
            elif isinstance(value, (dict, list, tuple)):
                found |= route_minutes(value, _depth=_depth + 1, _key=str(key))
    elif isinstance(evidence, (list, tuple)):
        for item in evidence[:200]:
            found |= route_minutes(item, _depth=_depth + 1, _key=_key)
    return found


# A decimal point ("around 2.5 hours") and an abbreviation dot do not end a sentence.
_SENTENCE = re.compile(r"(?:[^.!?\n]|\.(?=\d)|(?<=\b(?:St|Dr|Mr|Ms|vs|No))\.|(?<=\b[ie])\.(?=[eg]\.)|"
                       r"(?<=\b(?:i\.e|e\.g))\.)*(?:[.!?](?=\s|$)|\n|$)")       # "i.e." / "e.g." go on
_ARITHMETIC_CUE = re.compile(
    r"\b(?:each|one)\s+way\b|\bper\s+(?:trip|journey)\b|\bsaves?\b|\bsaving\b|\bdifference\b|\bgap\b|"
    r"\b(?:quicker|faster|shorter|longer|slower|less|more|extra)\b|\bversus\b|\bvs\.?|\bover\b|\bthan\b|"
    r"\bagainst\b|[×*−]|\bx\s*\d|\bminus\b", re.I)
COMPARISON_CUE = re.compile(
    r"\b(?:versus|vs\.?|over|than|compared|against|saves?|saving|difference|gap|quicker|faster|"
    r"shorter|longer|slower|less|more|extra|instead\s+of|minus)\b|[−]", re.I)
# A difference the text quotes as its BASE: "12 minutes each way", "4 minutes slower",
# "a gap of roughly 14 minutes", "a difference of 9 minutes".
BASE_FIGURE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:extra\s+)?(?:minutes?|mins?)\s+(?:each\s+way|one\s+way|per\s+(?:trip|journey)|"
    r"(?:slower|faster|quicker|shorter|longer|less|more|extra)\b)|"
    r"\b(?:gap|difference|saving)\s+of\s+(?:only\s+|just\s+|roughly\s+|about\s+|around\s+)?(\d+(?:\.\d+)?)\s*"
    r"(?:minutes?|mins?)", re.I)


def _key(text: Any) -> str:
    return _key_of(str(text or ""))


# Cached: every figure asks for its sentence's key once per route, which made a long answer
# quadratic (17 KB of "Stratford 20 minutes versus Bow 27 minutes, …" took 6 s).
@functools.lru_cache(maxsize=1024)
def _key_of(text: str) -> str:
    lowered = re.sub(r"(?<=[a-z])['’]s\b", "", text.lower())
    return " ".join(re.sub(r"[^a-z0-9一-鿿]+", " ", lowered.replace("’", "").replace("'", "")).split())


@functools.lru_cache(maxsize=256)
def _padded_key(text: str) -> str:
    return f" {_key_of(text)} "


def _mentions(text: Any, label: Any) -> int:
    """Position of ``label`` in ``text`` as whole words (on the normalised text), or -1."""
    key = _key(label)
    return _padded_key(str(text or "")).find(f" {key} ") if key else -1


# A journey by bike or by car is a DIFFERENT journey from the public-transport one the turn
# measured (the redactor's standing rule for bounds: "a walking/driving/cycling time is a
# different journey, never this bound"). A figure stated for that mode is evidenced only by a
# route measured in that mode; arithmetic on transit routes never licenses it (r6b: "8
# minutes each way by bike" was kept as the transit difference).
_CYCLING = re.compile(r"\b(?:by\s+(?:bike|bicycle|cycle)|on\s+(?:a|your|the)\s+bike|cycl(?:e|es|ed|ing)|"
                      r"bik(?:e|es|ing)|bicycl\w*)\b", re.I)
_DRIVING = re.compile(r"\b(?:by\s+(?:car|taxi|cab)|driv(?:e|es|ing)|car\s+journey)\b", re.I)


def route_mode(value: Any) -> str:
    """'bicycling' / 'driving' / 'walking' for a measured route's own mode, else 'transit'."""
    text = str(value or "").lower()
    if text.startswith(("bicycl", "cycl", "bike")):
        return "bicycling"
    if text.startswith(("driv", "car")):
        return "driving"
    if text.startswith("walk"):
        return "walking"
    return "transit"


def figure_mode(clause: str) -> str | None:
    """The non-transit mode a clause states its figure for: 'bicycling', 'driving' or None."""
    if _CYCLING.search(clause or ""):
        return "bicycling"
    if _DRIVING.search(clause or ""):
        return "driving"
    return None


# "Stratford is 8 minutes each way" / "Stratford takes 19 minutes": the route's name is the
# whole subject of the clause, so the figure is that route's OWN time (r6b: "Per day: Stratford
# is 8 minutes each way" with Stratford 19 was kept as the 8-minute difference).
_NOT_OWN_TIME_AFTER = re.compile(
    r"\s*(?:(?:each|one)\s+way\s+|per\s+(?:trip|journey)\s+)?(?:slower|faster|quicker|shorter|longer|less|more|"
    r"extra|saved|saving|over|under|above|below|within|inside|to\s+spare|than|apart|behind|ahead)\b", re.I)


def route_named_as_subject(text: str, start: int, end: int, routes: list) -> dict | None:
    """The route whose own time the figure at [start, end) states, via "<route> is / takes"."""
    if _NOT_OWN_TIME_AFTER.match(text[end:end + 40]):
        return None
    lead = ("\n" if start <= 80 else "") + text[max(0, start - 80):start]
    best, best_len = None, 0
    for route in routes:
        for label in route.get("labels") or []:
            if not str(label).strip() or len(str(label)) <= best_len:
                continue
            if re.search(r"(?:[\n:;,(|]|\*\*|\s[-–—]\s)\s*(?:[-•*]\s+)?(?:\*\*)?\s*" + re.escape(str(label)) +
                         r"(?:['’]s(?:\s+(?:commute|journey|trip|route))?)?\s+(?:is|takes)\s+"
                         r"(?:about\s+|roughly\s+|around\s+|approximately\s+|just\s+|only\s+|~\s*)?$",
                         lead, re.I):
                best, best_len = route, len(str(label))
    return best


def named_routes(evidence: Any, *, _depth: int = 0, _inherited: tuple = ()) -> list[dict]:
    """``[{labels, minutes}]`` for each named whole-trip measurement in tool evidence.

    A measurement nested under its request keeps the request's place: calculate_commute_cost
    returns ``{"from_address": "Stratford, London", "commute": {"duration_minutes": 20}}``, and
    the nameless 20 left the critic unable to bind "Stratford … 20 minutes" (r20 f510d8f3), while
    the redactor's bindings read the same nesting."""
    found: list[dict] = []
    if _depth > 8 or evidence is None:
        return found
    if isinstance(evidence, dict):
        own_labels = [str(evidence[k]) for k in ("name", "area", "address", "from_address", "label")
                      if isinstance(evidence.get(k), str) and evidence[k].strip()]
        own_labels += [label.split(",", 1)[0].strip() for label in own_labels if "," in label]
        inherited = tuple(own_labels) or _inherited
        minutes = None
        for key in ("commute_minutes", "duration_minutes", "verified_commute_minutes"):
            value = evidence.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minutes = float(value)
                break
        if minutes is None and isinstance(evidence.get("travel_time"), str):
            match = re.match(r"\s*(\d{1,3})\s*min", evidence["travel_time"])
            minutes = float(match.group(1)) if match else None
        if minutes is not None:
            found.append({"labels": list(inherited), "minutes": minutes, "mode": route_mode(evidence.get("mode"))})
        for key, value in evidence.items():
            if key not in ("route_legs", "legs", "steps") and isinstance(value, (dict, list, tuple)):
                found.extend(named_routes(value, _depth=_depth + 1,
                                          _inherited=inherited if isinstance(value, dict) else ()))
    elif isinstance(evidence, (list, tuple)):
        for item in evidence[:200]:
            found.extend(named_routes(item, _depth=_depth + 1))
    return found


def _bases(text: str, measured: Iterable[float], days: Iterable[float]) -> tuple[set, bool]:
    """Cached per (line, routes, days): every sentence of a line asks for the line's bases, and
    recomputing them per sentence was quadratic in a long line (the B-01 class)."""
    valid, wrong = _bases_of(str(text or ""), tuple(sorted(float(m) for m in measured)),
                             tuple(sorted({float(d) for d in days})))
    return set(valid), wrong


@functools.lru_cache(maxsize=512)
def _bases_of(text: str, measured_key: tuple, days_key: tuple) -> tuple[frozenset, bool]:
    measured, days = list(measured_key), set(days_key)
    valid, wrong = _bases_uncached(text, measured, days)
    return frozenset(valid), wrong


def _bases_uncached(text: str, measured: list, days: set) -> tuple[set, bool]:
    """(the differences ``text`` quotes as its base, whether one of its bases is WRONG).

    A base is a quoted difference, a subtraction step's result, or a product's minutes
    operand; a product operand that is itself a total of a quoted base ("6 x 5 = 30" after
    "3 x 2 = 6") is a step, not a wrong base."""
    quoted = [float(m.group(1) or m.group(2)) for m in BASE_FIGURE.finditer(text)
              if not _own_time_each_way(text, m, measured)]
    # A subtraction of two measured routes is a base; one against the user's limit ("25 − 19 =
    # 6 minutes under") is a margin, judged as one.
    quoted += [step["result"] for step in subtractions(text)
               if any(abs(step["a"] - m) < 1e-9 for m in measured)
               and any(abs(step["b"] - m) < 1e-9 for m in measured)]
    valid = {v for v in quoted if is_difference(v, measured)}
    wrong = any(not is_difference(v, measured) for v in quoted)
    # A conversion step's operand is a weekly total, judged by ``period_step_holds``: "48 min
    # × 4 = 192 minutes" a month has no base of its own.
    conversions = [step["span"] for step in period_steps(text, text)]
    for product in products(text):
        if any(s0 < product["span"][1] and product["span"][0] < s1 for s0, s1 in conversions):
            continue
        first = product["operands"][0][0]
        if is_difference(first, measured):
            valid.add(first)
        elif not (any(abs(first - m) < 1e-9 for m in measured) or _derived_exactly(first, valid, days)
                  # "- Per week: 14 × 3 = 42" chains the "7 × 2 = 14" of the line before (r21)
                  or (not valid and _derived_exactly(first, set(differences(measured)), days))):
            wrong = True
    return valid, wrong


_SIDE_BY_SIDE = re.compile(r"\b(?:versus|vs\.?|compared\s+(?:with|to)|against)\b", re.I)
_EACH_WAY_AFTER = re.compile(r"\s*(?:each|one)[\s-]way\b|\s*per\s+(?:trip|journey|direction)\b", re.I)
_EACH_WAY_ONLY = re.compile(r"(?:minutes?|mins?)\s+(?:each\s+way|one\s+way|per\s+(?:trip|journey))$", re.I)


def _own_time_each_way(text: str, match, measured: Iterable[float]) -> bool:
    """"**Stratford — 19 minutes each way**" states a route's own measured time, not a
    difference: no comparison leads into it within its clause and none follows it. "Stratford
    saves 19 minutes each way" and "19 minutes each way faster" are still bases (r17 8c35fd5b:
    the critic graded both route headers as wrong bases and footed the answer "Unverified")."""
    if not (match.group(1) and _EACH_WAY_ONLY.search(match.group())):
        return False
    value = float(match.group(1))
    if not any(abs(value - m) < 1e-9 for m in measured):
        return False
    before = text[max(0, match.start() - 120):match.start()]
    lead = before[max([m.end() for m in _CLAUSE_BREAK.finditer(before)], default=0):]
    if re.match(r"\s*(?:slower|faster|quicker|shorter|longer|less|more|extra|saved|saving)\b",
                text[match.end():], re.I):
        return False
    # "20 minutes versus 28 minutes each way" sets two routes' own times side by side; only a
    # cue other than versus/vs/compared/against ("saves", "faster by") makes it a difference
    # (r20 f510d8f3: the critic read the 28 as a wrong base and footed a correct answer).
    return not COMPARISON_CUE.search(_SIDE_BY_SIDE.sub(" ", lead))


_ROUTE_AFTER_STOP = re.compile(r"\d|[,;()]|\s(?:and|but|while)\s|[—–]")


def _route_after(text: str, start: int, routes: list) -> dict | None:
    """The route compared WITH right after a figure, inside the figure's own clause: "18
    minutes vs Holloway, 96 minutes vs Camden" (r16) gives each figure its own target, while
    "(about half an hour) and … against Camden (…)" gives "half an hour" none."""
    stop = _ROUTE_AFTER_STOP.search(text, start)       # from ``start``: no copy of the rest
    segment = text[start:stop.start()] if stop else text[start:]
    match = re.search(r"\b(?:over|versus|vs\.?|than|compared\s+(?:with|to)|against)\s+(.{1,60})", segment, re.I)
    if not match:
        return None
    named = [r for r in routes if any(_mentions(match.group(1), label) >= 0 for label in r["labels"])]
    return named[0] if len({id(r) for r in named}) == 1 else None


def _first_route(text: str, routes: list, *, other_than: dict | None = None) -> dict | None:
    best, best_at = None, None
    for route in routes:
        if route is other_than or (other_than is not None and route["minutes"] == other_than["minutes"]
                                   and set(route["labels"]) == set(other_than["labels"])):
            continue
        hits = [at for at in (_mentions(text, label) for label in route["labels"]) if at >= 0]
        if hits and (best_at is None or min(hits) < best_at):
            best, best_at = route, min(hits)
    return best


def _route_before(text: str, end: int, routes: list) -> dict | None:
    """The route named last before ``end``: the rightmost whole-word mention of one of its
    labels in ``_key(text[:end])``. Answered from the text's token index (built once per text)
    instead of the key of the whole prefix per figure, which made a long answer quadratic."""
    text = str(text or "")
    index = _token_index(text)
    starts, ends, before, tokens = index
    chunk = bisect.bisect_right(starts, end - 1) - 1
    tail: list = []
    if chunk < 0:
        complete = 0
    elif ends[chunk] <= end:
        complete = before[chunk + 1]
    else:
        complete = before[chunk]              # the chunk ``end`` cuts: its own prefix's tokens
        tail = _key(text[starts[chunk]:end]).split()

    def token(i: int) -> str:
        return tokens[i] if i < complete else tail[i - complete]

    best, best_at = None, -1
    for route in routes:
        for label in route["labels"]:
            key = _key(label)
            if not key:
                continue
            words = key.split()
            n = len(words)
            at = -1
            for s in range(complete + len(tail) - n, max(0, complete - n + 1) - 1, -1):
                if all(token(s + k) == words[k] for k in range(n)):
                    at = s                     # a mention reaching into the cut chunk
                    break
            if at < 0:
                found = _key_occurrences(text, key)
                j = bisect.bisect_right(found, complete - n) - 1
                at = found[j] if j >= 0 else -1
            if at > best_at:
                best, best_at = route, at
    return best


# Characters that end a token in every step of ``_key`` (not a word character, not an
# apostrophe, lower-cased to themselves): the key of a text is the keys of the runs between
# them, joined, so a prefix's key is the complete runs' tokens plus the cut run's own.
_TOKEN_BOUNDARY = re.compile(r"[\s!-&(-/:-@\[-^`{-~，。；：！？、（）「」『』【】《》“”‘—–…·]")


@functools.lru_cache(maxsize=64)
def _token_index(text: str) -> tuple:
    starts, ends, before, tokens = [], [], [0], []
    position = 0
    for match in _TOKEN_BOUNDARY.finditer(text + " "):
        if match.start() > position:
            starts.append(position)
            ends.append(match.start())
            tokens.extend(_key(text[position:match.start()]).split())
            before.append(len(tokens))
        position = match.end()
    return starts, ends, before, tokens


@functools.lru_cache(maxsize=256)
def _key_occurrences(text: str, key: str) -> list:
    """Token offsets of every whole-word mention of ``key`` in the text's token index."""
    tokens = _token_index(text)[3]
    words = key.split()
    n = len(words)
    return [i for i in range(len(tokens) - n + 1) if tokens[i] == words[0] and tokens[i:i + n] == words]


_COMPARED_WORD = r"(?:over|versus|vs\.?|than|compared\s+(?:with|to)|against)"


def _route_just_before(text: str, start: int, routes: list) -> dict | None:
    """The route compared with right before a figure: "against Holloway (about half an hour)"."""
    lead = text[max(0, start - 80):start]
    best, best_at = None, -1
    for route in routes:
        for label in route["labels"]:
            if not str(label).strip():
                continue
            for match in re.finditer(r"\b" + _COMPARED_WORD + r"\s+" + re.escape(str(label))
                                     + r"(?:['’]s)?[\s(:,—–-]*(?:(?:at|of|only|just|about|roughly|around|"
                                       r"some|nearly|almost)\s+)*$", lead, re.I):
                if match.start() > best_at:
                    best, best_at = route, match.start()
    return best


def _compared_routes(sentence: str, routes: list) -> list:
    """Routes the sentence names right after a comparison word ("over X", "against X")."""
    chosen = _compared_route_indices(str(sentence or ""), tuple(tuple(str(label) for label in route["labels"])
                                                              for route in routes))
    return [routes[index] for index in chosen]


@functools.lru_cache(maxsize=256)
def _compared_route_indices(sentence: str, labels: tuple) -> tuple:
    # Cached: asked once per figure of the sentence, each a scan of the whole sentence
    # (quadratic on a long one: 35 KB of "Stratford 20 minutes versus Bow 27 minutes, …").
    return tuple(index for index, route_labels in enumerate(labels)
                 if any(re.search(r"\b" + _COMPARED_WORD + r"\s+(?:the\s+)?" + re.escape(label) + r"\b", sentence, re.I)
                        for label in route_labels if label.strip()))


def pair_differences(*, text: str, at: int, sentence: str, line: str, label: str, routes: list,
                     measured: list, days: set, subject: dict | None = None, start: int | None = None) -> set:
    return pair_reference(text=text, at=at, sentence=sentence, line=line, label=label, routes=routes,
                          measured=measured, days=days, subject=subject, start=start)[0]


def pair_reference(*, text: str, at: int, sentence: str, line: str, label: str, routes: list,
                   measured: list, days: set, subject: dict | None = None,
                   start: int | None = None) -> tuple[set, bool]:
    """``(differences, fixed)``: ``fixed`` is False only for the any-pair fallback."""
    """The differences a figure at ``at`` in ``text`` may be derived from (r14 loophole).

    The base difference quoted in the same sentence wins, then one quoted on the same line;
    then the pair the text names (the route compared WITH — "over X", "versus X", the
    bullet's label — and the clause's subject); then any difference involving that route;
    only when nothing identifies a pair, any difference of the turn."""
    sentence_bases, _ = _bases(sentence, measured, days)
    if sentence_bases:
        return sentence_bases, True
    line_bases, _ = _bases(line, measured, days)
    if line_bases:
        return line_bases, True
    target = (_route_after(text, at, routes)
              or (_route_just_before(text, start, routes) if start is not None else None)
              or (_route_after(label, 0, routes) if label else None))
    if target is None:
        # Nothing names the pair; with two measured routes there is only one pair to name (r17
        # 8c35fd5b: "Per month: roughly 3.2 hours" under Stratford 19 / Bow 27).
        every = set(differences(measured))
        return every, len(every) == 1
    # The other end: a route the sentence names that is not itself compared WITH ("… against
    # Holloway (…) and … against Camden (…)" names two targets and no subject).
    compared = _compared_routes(sentence, routes)
    candidates = [r for r in routes if all(r is not c for c in compared) and r is not target]
    other = _first_route(sentence, candidates) if candidates else None
    if other is None and subject is not None and subject.get("minutes") is not None \
            and subject.get("minutes") != target["minutes"] \
            and not any(c["minutes"] == subject["minutes"] for c in compared):
        other = subject
    if other is not None:
        return {round(abs(target["minutes"] - other["minutes"]), 2)} - {0.0}, True
    return {d for d, pairs in differences(measured).items() if any(target["minutes"] in p for p in pairs)}, True


# A margin against the user's LIMIT: its reference is the cap, never another route.
# "with about 5 minutes to spare", "5 minutes inside your cap", "under your limit by 5
# minutes", "a margin of 5 minutes"; and the other way, "roughly 4 minutes over (your limit)".
_LIMIT_NOUN = r"(?:cap|limit|max(?:imum)?|ceiling|threshold|target)"
MARGIN_UNDER = re.compile(
    r"\bto\s+spare\b|\bspare\b|\bmargin\b|\bheadroom\b|\bleeway\b|\bbuffer\b|"
    r"\b(?:inside|within|under|below)\s+(?:your|the)\s+(?:\d+[\s-]*minute\s+)?" + _LIMIT_NOUN + r"\b", re.I)
MARGIN_OVER = re.compile(
    r"\b(?:over|above|beyond|past)\s+(?:your|the)\s+(?:\d+[\s-]*minute\s+)?" + _LIMIT_NOUN + r"\b|"
    r"\b(?:minutes?|mins?)\s+(?:too\s+long|over)\b(?!\s+(?:the\s+)?[A-Z])", re.I)
CAP_NAMED = re.compile(
    r"\b(?:your|the)\s+(?:\d+[\s-]*minute\s+)?" + _LIMIT_NOUN + r"\b|\b\d+[\s-]*minute\s+" + _LIMIT_NOUN + r"\b",
    re.I)


_CAP_PHRASE = r"(?:your|the)\s+(?:\d+[\s-]*minute\s+)?" + _LIMIT_NOUN
_HEDGE_WORDS = r"(?:about\s+|roughly\s+|around\s+|just\s+|only\s+|nearly\s+|almost\s+)?"
_AFTER_UNDER = re.compile(
    r"^\s*(?:to\s+spare|spare|of\s+(?:headroom|leeway|margin|buffer|room)|"
    r"(?:inside|within|under|below)\s+" + _CAP_PHRASE + r")\b", re.I)
_BEFORE_UNDER = re.compile(
    r"(?:(?:inside|within|under|below)\s+" + _CAP_PHRASE + r"[,\s]+by|(?:a\s+)?(?:margin|buffer|headroom|leeway)\s+of)"
    r"\s+" + _HEDGE_WORDS + r"$", re.I)
_AFTER_OVER_CAP = re.compile(r"^\s*(?:over|above|beyond|past|outside)\s+" + _CAP_PHRASE + r"\b|^\s*too\s+long\b", re.I)
_AFTER_OVER_BARE = re.compile(r"^\s*over\b(?!\s+(?:the\s+|a\s+)?[A-Za-z])", re.I)
_BEFORE_OVER = re.compile(r"(?:over|above|beyond|past|outside|exceeds?|misses)\s+" + _CAP_PHRASE + r"[,\s]+by\s+"
                          + _HEDGE_WORDS + r"$", re.I)


@functools.lru_cache(maxsize=256)
def _cap_named(sentence: str) -> bool:
    return bool(CAP_NAMED.search(sentence))


def margin_side(text: str, start: int, end: int, sentence: str, caps_known: bool = False,
                lead: str = "") -> str | None:
    """'under' / 'over' when the figure at [start, end) is a margin against the user's limit:
    "5 minutes to spare", "5 minutes inside your cap", "under your limit by 5 minutes", "a
    margin of 5 minutes"; "4 minutes over your limit", "over your cap by 4 minutes", and a
    bare "4 minutes over." when a limit is named in the sentence or known for this turn."""
    # ``lead`` is the clause before this one: "**Bow — outside your limit, by 4 minutes**"
    # splits at its comma, and the cap the "by 4 minutes" refers to is in the clause before.
    after, before = text[end:end + 40], (f"{lead} " + text[:start])[-60:]
    if _AFTER_UNDER.search(after) or _BEFORE_UNDER.search(before):
        return "under"
    if _AFTER_OVER_CAP.search(after) or _BEFORE_OVER.search(before):
        return "over"
    if _AFTER_OVER_BARE.search(after) and (caps_known or _cap_named(str(sentence or ""))):
        return "over"
    return None


_BOUND_BEFORE = re.compile(
    r"\b(?:under|below|within|inside|less\s+than|fewer\s+than|over|above|more\s+than|at\s+least|"
    r"at\s+most|up\s+to|no\s+more\s+than|beyond)\s+(?:just\s+|about\s+|roughly\s+|around\s+|a\s+)?$", re.I)


# The period a total is FOR, read next to the figure (r15 efaa330f: "about 18 minutes a week
# saved — around 54 minutes over a typical month" with 9 x 2 x 3 = 54 a week passed because
# every multiplier was allowed whatever the unit).
_UNIT_PHRASES = (
    ("month", re.compile(r"\b(?:a|per|each|every|the|over\s+(?:a|the)(?:\s+typical)?|in\s+a)\s+month\b|"
                         r"\bmonthly\b|\bmonths?\b", re.I)),
    ("year", re.compile(r"\b(?:a|per|each|every|over\s+(?:a|the)|in\s+a)\s+year\b|\byearly\b|"
                        r"\bannual(?:ly)?\b", re.I)),
    ("week", re.compile(r"\b(?:a|per|each|every|the|over\s+(?:a|the)|in\s+a|across\s+the)\s+"
                        r"(?:[\w-]+\s+){0,2}?week\b|\bweekly\b|\bweek\b", re.I)),
    ("day", re.compile(r"\b(?:a|per|each|every)\s+(?:working\s+|office\s+)?day\b|\bdaily\b", re.I)),
)
_CLAUSE_BREAK = re.compile(r"[,;:—–()]|\s-\s|(?<=[.!?])\s")
_ONE_WAY = re.compile(r"\b(?:one[\s-]way|each\s+way|in\s+each\s+direction|per\s+direction)\b", re.I)


def _unit_in(segment: str, *, last: bool = False) -> str | None:
    found = []
    for unit, pattern in _UNIT_PHRASES:
        for match in pattern.finditer(segment):
            found.append((match.start(), unit))
    if not found:
        return None
    found.sort()
    return found[-1][1] if last else found[0][1]


def figure_unit(text: str, start: int, end: int, sentence: str = "") -> str | None:
    """'day' / 'week' / 'month' / 'year' for the total at [start, end): the period phrase right
    after it in its clause, else the nearest one before it in the clause, else in the sentence.
    A rent's period ("£2,350 a month") is not the total's."""
    text, sentence = without_money(text), without_money(sentence)
    after = text[end:end + 60]
    stop = re.search(r"\d|[,;:—–]|(?<=[.!?])\s|$", after)
    unit = _unit_in(after[:stop.start()] if stop else after)
    if unit:
        return unit
    before = text[max(0, start - 80):start]
    cut = max([m.end() for m in _CLAUSE_BREAK.finditer(before)], default=0)
    unit = _unit_in(before[cut:], last=True)
    if unit:
        return unit
    if sentence:
        probe = text[max(0, start - 20):start]
        at = sentence.find(probe) + len(probe) if probe and probe in sentence else -1
        if at >= 0:
            return _unit_in(sentence[:at], last=True) or _unit_in(sentence[at:])
        return _unit_in(sentence)
    return None


def _clause_of(text: str, start: int, end: int) -> str:
    before = text[max(0, start - 120):start]
    cut = max([m.end() for m in _CLAUSE_BREAK.finditer(before)], default=0)
    after = text[end:end + 120]
    stop = _CLAUSE_BREAK.search(after)
    return before[cut:] + text[start:end] + (after[:stop.start()] if stop else after)


def figure_days(clause: str, sentence: str, user_days: Iterable[float], answer_days: Iterable[float]) -> set:
    """Commuting days for a weekly total: an N-day week the answer states FOR this figure
    (its clause, then its sentence), else the user's own office days, else the standard 5 or
    an N-day week the answer states elsewhere. The user's days are never overridden by a 5-day
    default; without them, an example elsewhere ("e.g. three days a week") does not displace
    the standard week, so a text judged after a removal reads its 5-day totals the same way
    (r17: the critic flagged the correct leftover once the "five-day week" lead-in went)."""
    local = days_stated(clause) or days_stated(sentence)
    if local:
        return local
    user = {float(d) for d in user_days if d}
    if user:
        return user
    return {float(d) for d in answer_days if d} | {STANDARD_DAYS}


def period_factors(unit: str | None, days: Iterable[float], *, one_way: bool = False,
                   four_weeks: bool = False) -> set:
    """The multipliers on a one-way difference for a total over ``unit``: a day is x 2 (x 1
    only "one way"); a week x 2 x days (x days only "one way"); a month that week x 52/12 (x 4
    only where the answer frames a month as four weeks, or hedges); a year that week x 52 or
    that month x 12. No period, no total."""
    if unit == "day":
        return {2.0} | ({1.0} if one_way else set())
    if unit not in ("week", "month", "year"):
        return set()
    days = [float(d) for d in days if d]
    weekly = {2.0 * d for d in days} | ({d for d in days} if one_way else set())
    if unit == "week":
        return weekly
    monthly = {w * m for w in weekly for m in set(WEEKS_PER_MONTH) | ({4.0} if four_weeks else set())}
    return monthly if unit == "month" else {w * 52.0 for w in weekly} | {m * 12.0 for m in monthly}


def total_matches(value: float, diffs: Iterable[float], unit: str | None, days: Iterable[float], *,
                  hedged: bool, one_way: bool = False, four_weeks: bool = False,
                  rounding: float = 0.5) -> bool:
    """``value`` is a total of one of ``diffs`` for the period ``unit`` names
    (``period_factors``), exact to the written precision or, hedged, within 10%."""
    factors = period_factors(unit, days, one_way=one_way, four_weeks=four_weeks or hedged)
    return any(close(value, d * f, hedged, rounding) for d in diffs for f in factors)


# A conversion of a weekly total into a month or a year, written out: "48 min × 52 weeks ÷ 12",
# "48 × 4.33 = about 208 minutes", "48 min × 4 = 192 minutes", "208 × 12 = 2496 minutes".
# Every gap is bounded (the B-01 class).
_GAP = r"[ \t]{0,3}"
PERIOD_STEP = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)(?:" + _GAP + r"(?:minutes?|mins?))?" + _GAP + r"[×x*]" + _GAP +
    r"(\d+(?:\.\d+)?)(?:" + _GAP + r"(weeks?|months?))?"
    r"(?:" + _GAP + r"(?:÷|/)" + _GAP + r"(\d+(?:\.\d+)?)(?:" + _GAP + r"months?)?)?"
    r"(?![\d.]|" + _GAP + r"[×x*]" + _GAP + r"\d)"
    r"(?:" + _GAP + r"=" + _GAP + r"[*_]{0,3}" + _GAP +
    r"((?:about|roughly|around|approximately|approx\.?|nearly|almost|~|≈)" + _GAP + r")?"
    r"(\d[\d,]*(?:\.\d+)?)" + _GAP + r"(minutes?|mins?|hours?|hrs?))?", re.I)


def period_steps(text: str, sentence: str = "") -> list:
    """``[{weekly, factor, period, result, hedged, span, from_month}]`` for each conversion step
    of ``text`` into a month or a year. "x 4.33", "x 52 ÷ 12" and "x 52 weeks" name their
    period; a bare "x 4" or "x 12" is one only where the step's own period is a month or a
    year (or its factor is written in weeks / months)."""
    found = []
    for match in PERIOD_STEP.finditer(str(text or "")):
        first, factor = float(match.group(1)), float(match.group(2))
        if match.group(4):
            divisor = float(match.group(4))
            if not divisor:
                continue
            factor /= divisor
        unit = (match.group(3) or "").lower()
        if abs(factor - 4.0) > 1e-9 and any(abs(factor - m) < 0.005 for m in WEEKS_PER_MONTH):
            period = "month"
        elif factor == 52.0 and not unit.startswith("month"):
            period = "year"
        elif factor in (4.0, 12.0):
            stated = figure_unit(text, match.end() - 1, match.end(), sentence)
            period = stated if stated in ("month", "year") and _period_factor(factor, stated) else None
            if period is None and unit.startswith("week") and factor == 4.0:
                period = "month"
            if period is None and unit.startswith("month") and factor == 12.0:
                period = "year"
        else:
            period = None
        if period is None:
            continue
        result = None
        if match.group(6):
            result = float(match.group(6).replace(",", "")) * (
                60.0 if match.group(7).lower().startswith("h") else 1.0)
        found.append({"weekly": first, "factor": factor, "period": period, "result": result,
                      "hedged": bool(match.group(5)), "span": match.span(),
                      "from_month": period == "year" and factor == 12.0})
    return found


def period_step_holds(step: dict, diffs: Iterable[float], days: Iterable[float], *,
                      results: Iterable[float] = (), one_way: bool = False) -> bool:
    """The step's operand is a weekly total of the pair (a monthly one for "x 12 months"), or
    the result of an earlier correct step; its result, if stated, is the product."""
    diffs = list(diffs)
    operand = step["weekly"]
    if not (any(abs(operand - r) < 0.5 for r in results)
            or total_matches(operand, diffs, "month" if step["from_month"] else "week", days,
                             hedged=False, one_way=one_way, four_weeks=True)):
        return False
    if step["result"] is None:
        return True
    return close(step["result"], operand * step["factor"], step["hedged"])


# The SPREAD of the routes: "all three are within 4 minutes of each other" (Camden 25, Holloway
# 24, Kentish Town 21; r15 700d77a8), "they are 4 minutes apart".
_N = r"(\d{1,3}(?:\.\d+)?)\s*(?:minutes?|mins?)"
SPREAD_BOUND = re.compile(
    r"\b(?:within|under|less\s+than|no\s+more\s+than|at\s+most)\s+(?:about\s+|roughly\s+|around\s+|just\s+)?"
    + _N + r"\s+of\s+(?:each\s+other|one\s+another)\b", re.I)
SPREAD_EXACT = re.compile(
    r"\b" + _N + r"\s+apart\b|\b(?:spread|range)\s+of\s+(?:only\s+|just\s+|about\s+|roughly\s+)?" + _N +
    r"|\b(?:separated|differ)\s+by\s+(?:only\s+|just\s+|about\s+|roughly\s+)?" + _N, re.I)


def spreads(text: str, sentence: str, routes: list) -> list:
    """``[(span, holds)]`` for each spread claim in ``text``: a bound must be >= max - min of
    the routes the sentence names (all measured routes when it names fewer than two); an
    exact spread must equal it (hedged: within the tolerance)."""
    named = [r for r in routes if any(_mentions(sentence, label) >= 0 for label in r["labels"] if label)]
    pool = named if len(named) >= 2 else routes
    values = [float(r["minutes"]) for r in pool if r.get("minutes") is not None]
    if len(values) < 2:
        return []
    spread = max(values) - min(values)
    found = []
    for match in SPREAD_BOUND.finditer(text):
        found.append((match.span(), float(match.group(1)) >= spread - 1e-9))
    for match in SPREAD_EXACT.finditer(text):
        value = float(next(g for g in match.groups() if g))
        found.append((match.span(), close(value, spread, bool(HEDGE.search(match.group())))))
    return found


# A stated journey COUNT: "(10 journeys)", "6 journeys", "10 trips a week", "5 return trips",
# "two journeys a day", "5 one-way trips". A week has 2 x days journeys (days one-way trips only
# when one-way is explicit, days return/round trips); a day has 2 (1 one-way).
_COUNT_WORD = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fourteen)"
_COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14}
JOURNEY_COUNT = re.compile(
    r"\b" + _COUNT_WORD + r"\s+(one[\s-]way\s+|single\s+|return\s+|round[\s-]|commuting\s+)?"
    r"(journeys|trips|commutes)\b(\s+(?:a|per|each)\s+(?:day|week))?", re.I)


def journey_counts(text: str) -> list:
    """``[(span, count, kind, period)]``: kind 'leg' (one-way journeys), 'one_way' (explicit
    one-way wording), 'round' (return/round trips); period 'day', 'week' or None."""
    found = []
    for match in JOURNEY_COUNT.finditer(str(text or "")):
        word = match.group(1).lower()
        count = float(_COUNT_WORDS.get(word) or int(word))
        qualifier = (match.group(2) or "").lower()
        kind = ("one_way" if qualifier.startswith(("one", "single")) else
                "round" if qualifier.startswith(("return", "round")) else "leg")
        period = None
        if match.group(4):
            period = "day" if "day" in match.group(4).lower() else "week"
        found.append((match.span(), count, kind, period))
    return found


def journey_count_holds(count: float, kind: str, period: str | None, days: Iterable[float]) -> bool:
    if period == "day":
        return count == (1.0 if kind in ("one_way", "round") else 2.0)
    days = [float(d) for d in days if d]
    if kind == "round":
        return any(count == d for d in days)
    if kind == "one_way":
        return any(count in (d, 2 * d) for d in days)
    return any(count == 2 * d for d in days) or (period is None and count in (1.0, 2.0))


def journey_counts_ok(text: str, *, context: str = "", sentence: str = "",
                      user_days: Iterable[float] = (), answer_days: Iterable[float] = ()) -> bool:
    """Every journey count ``text`` states matches the week (or day) it is for."""
    for (start, end), count, kind, period in journey_counts(text):
        scope = f"{context} {_clause_of(text, start, end)}"
        if not journey_count_holds(count, kind, period, figure_days(scope, sentence, user_days, answer_days)):
            return False
    return True


def figure_spans(text: str) -> list:
    """``[(start, end, minutes)]`` for every duration in ``text`` ("2 hours 20 minutes" = 140)."""
    spans = [(m.start(), m.end(), float(m.group(1)) * 60 + float(m.group(2) or 0))
             for m in HOURS_AND_MINUTES.finditer(text)]
    masked = HOURS_AND_MINUTES.sub(lambda m: " " * len(m.group()), text)
    spans += [(m.start(), m.end(), _word_hours(m)) for m in _WORD_HOURS.finditer(masked)]
    masked = _WORD_HOURS.sub(lambda m: " " * len(m.group()), masked)
    spans += [(m.start(), m.end(), float(m.group(1))) for m in MINUTES.finditer(masked)]
    words = masked.lower()
    for phrase, value in DURATION_WORDS:
        at = words.find(phrase)
        if at >= 0:
            spans.append((at, at + len(phrase), value))
            words = words[:at] + " " * len(phrase) + words[at + len(phrase):]
    return sorted(spans)


def _period_owned_elsewhere(sentence: str, unit: str, value: float) -> bool:
    """Every phrase of ``unit`` in ``sentence`` follows ANOTHER figure in its own clause ("about
    3.5 hours a month", "3.5 hours less commuting per month"), so none is this figure's."""
    phrased, unowned, owners = _period_owners(str(sentence or ""), unit)
    return phrased and not unowned and not any(abs(v - value) < 1e-9 for v in owners)


@functools.lru_cache(maxsize=256)
def _period_owners(sentence: str, unit: str) -> tuple[bool, bool, frozenset]:
    """``(any phrase of unit, a phrase with no figure of its own, every owning figure)`` — once
    per sentence and unit, not once per figure x phrase x figure (cubic on a long sentence)."""
    pattern = dict(_UNIT_PHRASES).get(unit)
    phrases = list(pattern.finditer(sentence)) if pattern else []
    if not phrases:
        return False, False, frozenset()
    spans = sorted(figure_spans(sentence), key=lambda span: span[1])
    ends = [e for _s, e, _v in spans]
    unowned, owners = False, set()
    for phrase in phrases:
        at = phrase.start()
        found = [v for _s, e, v in spans[bisect.bisect_left(ends, at - 30):bisect.bisect_right(ends, at)]
                 if not re.search(r"[\d,;:—–()]", sentence[e:at])]
        if not found:
            unowned = True
        owners.update(found)
    return True, unowned, frozenset(owners)


# ONE PLACE, SEVERAL MEASUREMENTS (r21 8f4f0a00): calculate_commute timed "Bow, London" at 27,
# the area ranking timed "Bow" at 28. A route may carry ``alternatives`` — the same place's
# other measured times — and a claim holds when it holds under ONE consistent choice per place
# (all of a sentence on 27, or all of it on 28): "27 each way" with "28 x 2 x 3" in one chain
# holds under neither. The more specific measurement (the longer place name) is the first choice.
_MAX_VARIANTS = 16


def merge_same_place(routes: list) -> list:
    """Routes of one place and mode as ONE route: the more specific measurement first, the
    others' different times as its ``alternatives``."""
    merged: list = []
    for route in routes:
        if route.get("minutes") is None:
            merged.append(route)
            continue
        head = _place_head(route)
        mode = route.get("mode") or "transit"
        # The same PLACE, by its most specific name ("Bow, London" and "Bow"); a listing row
        # that merely carries "area": "Islington" is not the Islington area route (r07 d8b4e4a1).
        home = next((m for m in merged if m.get("minutes") is not None and head
                     and (m.get("mode") or "transit") == mode and _place_head(m) == head), None)
        if home is None:
            merged.append({**route, "alternatives": list(route.get("alternatives") or [])})
            continue
        specific = max(len(str(l)) for l in route.get("labels") or [""]) > \
            max(len(str(l)) for l in home.get("labels") or [""])
        labels = list(dict.fromkeys(list(home.get("labels") or []) + list(route.get("labels") or [])))
        if route["minutes"] != home["minutes"]:
            if specific:
                alternatives = [home["minutes"]] + list(home.get("alternatives") or [])
                home.update({**route, "labels": labels})
                home["alternatives"] = alternatives
            elif route["minutes"] not in home.get("alternatives", []):
                home.setdefault("alternatives", []).append(route["minutes"])
        home["labels"] = labels
    return merged


def _place_head(route: dict) -> str:
    """The key of a route's most specific place name, before its first comma."""
    labels = [str(label) for label in route.get("labels") or [] if str(label).strip()]
    if not labels:
        return ""
    return _key(max(labels, key=len).split(",", 1)[0])


def route_variants(routes: list) -> list:
    """Every consistent choice of one time per place, the first choice first (bounded)."""
    options = [[{k: v for k, v in r.items() if k != "alternatives"}]
               + [{**{k: v for k, v in r.items() if k != "alternatives"}, "minutes": float(a)}
                  for a in r.get("alternatives") or [] if a != r.get("minutes")]
               for r in routes]
    out = []
    for combo in _cartesian(*options):
        out.append(list(combo))
        if len(out) >= _MAX_VARIANTS:
            break
    return out


def check_figures(text: str, *, sentence: str, line: str = "", label: str = "", context: str = "",
                  routes: list, days: Iterable[float], subject: dict | None = None,
                  known: Iterable[float] = (), skip_bounds: bool = False,
                  caps: Iterable[float] = (), user_days: Iterable[float] = (),
                  lead: str = "") -> tuple[bool, bool]:
    """THE derived-figure rule, under each consistent choice of measurement per place."""
    known = _sorted_figures(known)       # once for every variant (and safe for an iterator)
    caps = _distinct_caps(caps)
    kwargs = dict(sentence=sentence, line=line, label=label, context=context, days=days, known=known,
                  skip_bounds=skip_bounds, caps=caps, user_days=user_days, lead=lead)
    if not any(r.get("alternatives") for r in routes):
        return _check_figures(text, routes=[{k: v for k, v in r.items() if k != "alternatives"} for r in routes],
                              subject=subject, **kwargs)
    results = []
    for variant in route_variants(routes):
        chosen = subject
        if subject is not None:
            keys = {_key(label) for label in subject.get("labels") or [] if _key(label)}
            twin = next((r for r in variant if keys & {_key(l) for l in r.get("labels") or []}), None)
            if twin is not None:
                chosen = {**subject, "minutes": twin["minutes"]}
        results.append(_check_figures(text, routes=variant, subject=chosen, **kwargs))
        if results[-1] == (True, True):
            return True, True
    if any(ok for ok, _seen in results):
        return True, False
    return False, any(seen for _ok, seen in results)


@functools.lru_cache(maxsize=256)
def _scope_flags(scope: str) -> tuple[bool, bool, bool]:
    """``(names a period, compares, a four-week month)`` for a check's scope."""
    return (bool(DAY_PERIOD.search(scope) or WEEK_PERIOD.search(scope) or LONG_PERIOD.search(without_money(scope))),
            bool(COMPARISON_CUE.search(scope)), bool(FOUR_WEEK_MONTH.search(scope)))


@functools.lru_cache(maxsize=256)
def _correct_results(text: str, measured: tuple, days: tuple) -> tuple:
    """The results of the correct product steps of ``text``."""
    return tuple(p["result"] for p in products(text) if product_is_correct(p, list(measured), set(days)))


class SortedFigures(tuple):
    """Figures sorted once (a caller checking many sentences against one evidence pool builds
    it once); :func:`_near` reads it by bisection."""


def _sorted_figures(figures: Iterable[float]) -> SortedFigures:
    if isinstance(figures, SortedFigures):
        return figures
    return SortedFigures(sorted(float(f) for f in figures))


def _near(value: float, figures: SortedFigures, tolerance: float = 0.01) -> bool:
    """``any(abs(value - f) <= tolerance for f in figures)``, by bisection."""
    low = bisect.bisect_left(figures, value - tolerance - 1e-9)
    high = bisect.bisect_right(figures, value + tolerance + 1e-9)
    return any(abs(value - f) <= tolerance for f in figures[low:high])


class SortedCaps(tuple):
    """The user's limits, distinct, non-zero and sorted once per text (see SortedFigures)."""


def _distinct_caps(caps: Iterable[float]) -> SortedCaps:
    if isinstance(caps, SortedCaps):
        return caps
    return SortedCaps(sorted({float(c) for c in caps if c}))


def _is_cap(value: float, caps: list) -> bool:
    """``any(abs(value - cap) < 1e-9 for cap in caps)`` on the sorted caps, by bisection."""
    low = bisect.bisect_left(caps, value - 1e-6)
    return any(abs(value - cap) < 1e-9 for cap in caps[low:bisect.bisect_right(caps, value + 1e-6)])


def _blanked(text: str, spans: list) -> str:
    """``text`` with each span blanked, in one pass (span by span re-copied the whole text)."""
    pieces, at = [], 0
    for start, end in sorted(spans):
        start = max(start, at)
        if end <= start:
            continue
        pieces += [text[at:start], " " * (end - start)]
        at = end
    pieces.append(text[at:])
    return "".join(pieces)


def _check_figures(text: str, *, sentence: str, line: str = "", label: str = "", context: str = "",
                   routes: list, days: Iterable[float], subject: dict | None = None,
                   known: Iterable[float] = (), skip_bounds: bool = False,
                   caps: Iterable[float] = (), user_days: Iterable[float] = (),
                   lead: str = "") -> tuple[bool, bool]:
    """``(every figure of text is evidenced, text does arithmetic)`` — THE derived-figure rule.

    A figure is evidenced when it is a correct product or subtraction step (or restates the
    result of one in ``context``), a total of the pair's difference (x2 / x days / x2 x days,
    within 10% only when hedged), the pair's difference itself where the sentence compares,
    or the measured time of the route named right before it. A wrong step, a wrong base on
    the line, or any other figure fails it."""
    routes = [r for r in routes if r.get("minutes") is not None]
    measured = sorted({float(r["minutes"]) for r in routes})
    # One measured route still has a margin against the user's own limit ("roughly 12 minutes
    # of headroom against your 30-minute cap", r18 1787af1e); nothing else derives from it.
    if not measured or (len(measured) < 2 and not any(c for c in caps)):
        return False, False
    answer_days = {float(d) for d in days if d}
    user_days = {float(d) for d in user_days if d}
    days = answer_days | user_days | {STANDARD_DAYS}       # every week length anyone stated
    scope = f"{label} {sentence} {context}"
    # The sentence-wide reads are cached by content: the redactor checks every clause of a line
    # against the same line, which made a long one-line answer quadratic (25 KB took 7 s).
    period, comparison, four_weeks = _scope_flags(scope)
    _, wrong_base = _bases(line or sentence, measured, days)
    if wrong_base:
        return False, True
    caps = _distinct_caps(caps)
    covered, results, seen = [], [], False
    periods: dict = {}          # the period a step's result is FOR, where the step names one
    for step in subtractions(text):
        if not subtraction_is_correct(step, measured, caps):
            return False, True
        covered.append(step["span"])
        results.append(step["result"])
        seen = True
    # Earlier correct steps of the sentence (or of the clause a parenthetical belongs to) are
    # operands a conversion may build on: "8 x 2 x 3 = 48 …; 48 x 4.33 = about 208".
    earlier = list(_correct_results(f"{sentence} {context}", tuple(measured), tuple(sorted(days))))
    steps = period_steps(text, sentence)
    for step in steps:
        start_, end_ = step["span"]
        diffs = pair_differences(text=text, at=end_, sentence=sentence, line=line,
                                 label=label, routes=routes, measured=measured, days=days, subject=subject)
        clause = _clause_of(text, start_, end_)
        stated = figure_unit(text, end_ - 1, end_, "") if step["result"] is not None else None
        if (stated not in (None, step["period"])
                or not period_step_holds(step, diffs, figure_days(clause, sentence, user_days, answer_days),
                                         results=results + earlier, one_way=bool(_ONE_WAY.search(clause)))):
            return False, True
        covered.append(step["span"])
        if step["result"] is not None:
            results.append(step["result"])
            periods[len(results) - 1] = step["period"]
        results.append(step["weekly"])
        periods[len(results) - 1] = "month" if step["from_month"] else "week"
        seen = True
    for product in products(text):
        start_, end_ = product["span"]
        if any(s0 < end_ and start_ < s1 for s0, s1 in (step["span"] for step in steps)):
            continue          # judged as a conversion step above
        diffs = pair_differences(text=text, at=end_, sentence=sentence, line=line,
                                 label=label, routes=routes, measured=measured, days=days, subject=subject)
        clause = _clause_of(text, start_, end_)
        local = days_stated(clause)
        # ... and the result is a total for the period it names: "3 x 2 = 6 minutes per day",
        # never "4 x 2 = 8 minutes a week"; "8 x 2 x 3 x 4.33 = 208 minutes a month".
        unit = figure_unit(text, end_ - 1, end_, "")
        if not product_is_correct(product, measured, local or days, diffs=diffs,
                                  period=unit if unit in ("month", "year") else None):
            return False, True
        stated_days = {v for v, u in product["operands"] if u.startswith("day")}
        if unit in ("day", "week", "month", "year") and diffs and not total_matches(
                product["result"], diffs, unit,
                stated_days or figure_days(clause, sentence, user_days, answer_days),
                hedged=False, one_way=bool(_ONE_WAY.search(clause)),
                four_weeks=any(v == 4.0 for v, _u in product["operands"][1:])):
            return False, True
        covered.append(product["span"])
        results.append(product["result"])
        if unit:
            periods[len(results) - 1] = unit
        seen = True
    for span, holds in spreads(text, sentence, routes):
        if not holds:
            return False, True
        covered.append(span)
        seen = True
    # A stated journey count must match the week it is for: "(8 journeys)" for "a typical
    # five-day week with two journeys a day" is wrong (5 x 2 = 10; r17).
    if journey_counts(text):
        if not journey_counts_ok(text, context=context, sentence=sentence, user_days=user_days,
                                 answer_days=answer_days):
            return False, True
        seen = True
    results += [p["result"] for p in products(context) if product_is_correct(p, measured, days)]
    rest = _blanked(text, covered)
    hedged = bool(HEDGE.search(text))
    known = _sorted_figures(known)
    caps = _distinct_caps(caps)
    for start, end, value in figure_spans(rest):
        # The same total restated ("192 minutes, i.e. 3 hours 12 minutes a month"), but never for
        # another period: "= 54 minutes a week — roughly 54 minutes a month" is not a restatement.
        restated_for = figure_unit(rest, start, end, "") if period else None   # its own clause only
        if any(abs(value - r) < 0.5 and (restated_for is None or periods.get(i) in (None, restated_for))
               for i, r in enumerate(results)):
            seen = True
            continue
        if caps and _is_cap(value, caps) and CAP_NAMED.search(
                rest[max(0, start - 12):end + 20]):
            continue          # the user's limit itself: "your 25-minute limit"
        side = margin_side(rest, start, end, sentence, caps_known=bool(caps), lead=lead)
        if side is not None:
            # A margin against the user's limit: cap - the subject's minutes (or the reverse
            # when over), exact or hedged, and NEVER another pair or an evidence coincidence:
            # "about 9 minutes to spare" (Bow - Stratford) under a 25 cap with Stratford at 20.
            seen = True
            owner = (_route_before(rest, start, routes) or subject or _first_route(sentence, routes)
                     or (routes[0] if len(measured) == 1 else None))   # the turn's only route
            if owner is None or not caps:
                return False, True
            margins = [cap - owner["minutes"] if side == "under" else owner["minutes"] - cap for cap in caps]
            if not any(margin > 0 and close(value, margin, hedged) for margin in margins):
                return False, True
            continue
        mode = figure_mode(_clause_of(rest, start, end))
        if mode is not None:
            # A bike / car figure: only a route measured in that mode evidences it.
            same = [r for r in routes if r.get("mode", "transit") == mode]
            owner = route_named_as_subject(rest, start, end, same) or (_route_before(rest, start, same) if same else None)
            if not any(abs(value - r["minutes"]) < 1e-9 for r in ([owner] if owner else same)):
                return False, True
            continue
        own = route_named_as_subject(rest, start, end, routes)
        if own is not None:
            if abs(own["minutes"] - value) < 1e-9:
                continue
            return False, True        # "Stratford is 8 minutes each way" with Stratford 19
        diffs, fixed = pair_reference(text=rest, at=end, sentence=sentence, line=line, label=label,
                                      routes=routes, measured=measured, days=days, subject=subject,
                                      start=start)
        if known and not fixed and _near(value, known):
            continue          # the evidence states it outright and nothing fixes a reference
        if skip_bounds and _BOUND_BEFORE.search(rest[max(0, start - 40):start]):
            continue          # "still under half an hour" is a verdict, not arithmetic
        # A total is checked for the period it names, over the right number of days; the any-
        # pair fallback takes a hedged figure only EXACTLY.
        unit = figure_unit(rest, start, end, sentence) if period else None
        if unit is not None and (
                _EACH_WAY_AFTER.match(rest[end:end + 24])
                or re.match(r"\s*" + _SIDE_BY_SIDE.pattern, rest[end:end + 24], re.I)
                or re.search(_SIDE_BY_SIDE.pattern + r"\s*$", rest[max(0, start - 24):start], re.I)):
            # "8 minutes each way, about 3.5 hours a month" / "20 minutes versus 28 minutes each
            # way, … 3.5 hours … per month": a one-way figure or a route time set side by side
            # takes no period from LATER in its sentence; the month belongs to the later figure
            # (r20 f510d8f3). A period no other figure owns ("- Per day: 8 minutes each way")
            # still holds.
            if figure_unit(rest, start, end, "") is None and _period_owned_elsewhere(sentence or rest, unit, value):
                unit = None
        if unit in ("month", "year"):
            # A monthly total is the weekly one x 52/12 (x 4 framed as four weeks); a yearly one
            # x 52 or the month x 12, to the precision written ("about 3.5 hours", r17 8c35fd5b).
            # Anything else goes: "around 54 minutes over a typical month" for 9 x 2 x 3 = 54 a
            # WEEK (r15 efaa330f).
            clause = _clause_of(rest, start, end)
            if total_matches(value, diffs, unit, figure_days(clause, sentence, user_days, answer_days),
                             hedged=hedged and fixed, one_way=bool(_ONE_WAY.search(rest[end:end + 40])),
                             four_weeks=four_weeks,
                             rounding=written_rounding(rest[start:end])):
                seen = True
                continue
            return False, True
        if unit in ("day", "week"):
            clause = _clause_of(rest, start, end)
            if total_matches(value, diffs, unit, figure_days(clause, sentence, user_days, answer_days),
                             hedged=hedged and fixed, one_way=bool(_ONE_WAY.search(rest[end:end + 40])),
                             rounding=written_rounding(rest[start:end])):
                seen = True
                continue
            if not (comparison and any(abs(value - d) < 1e-9 for d in diffs)):
                owner = _route_before(rest, start, routes) or subject
                if not (owner is not None and owner.get("minutes") is not None
                        and abs(owner["minutes"] - value) < 1e-9):
                    return False, True
        if comparison and any(abs(value - d) < 1e-9 for d in diffs):
            seen = True
            continue
        owner = _route_before(rest, start, routes) or subject
        if owner is not None and owner.get("minutes") is not None and abs(owner["minutes"] - value) < 1e-9:
            continue
        if _near(value, known):
            continue          # an evidence figure (a leg, …) quoted beside the comparison
        return False, True if (seen or period or comparison) else False
    return True, seen


# A PERIOD TABLE: rows are periods, columns are routes or the saving between them (r19
# de1fba15):
#
#   |                          | Stratford  | Bow         | Saving             |
#   | Per one-way trip         | 20 min     | 28 min      | 8 min              |
#   | Per office day (return)  | 40 min     | 56 min      | 16 min             |
#   | Per week (3 office days) | 2 h 00 min | 2 h 48 min  | **48 min**         |
#   | Per month (13 days)      | 8 h 40 min | 12 h 08 min | about 3 h 28 min   |
#
# A cell is that column's route time (or the pair's difference) x the row's period: x 1 a trip,
# x 2 a day, x 2 x days a week, x 2 x the stated office days (or x 2 x days x 52/12) a month.
# Judged cell by cell, never as an unattributed sentence: both the critic and the redactor
# rejected every true cell of this table.
_TABLE_RULE_LINE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_SAVING_COLUMN = re.compile(r"\b(?:saving|saved|saves?|difference|gap)\b", re.I)
_ONE_WAY_ROW = re.compile(r"\b(?:one[\s-]way|each\s+way|single|per\s+(?:trip|journey))\b", re.I)
_RETURN_ROW = re.compile(r"\b(?:return|round[\s-]trip|there\s+and\s+back)\b", re.I)
_DAYS_IN_PERIOD = re.compile(r"\b(\d{1,2})\s+(?:office\s+|working\s+|commuting\s+)?days\b", re.I)


def _table_cells(line: str) -> list:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _row_factors(label: str, user_days: Iterable[float], answer_days: Iterable[float],
                 hedged: bool) -> set:
    """The multipliers a row's period applies to a one-way time."""
    unit = _unit_in(without_money(label))
    if unit is None and _RETURN_ROW.search(label):
        unit = "day"
    if unit is None:
        return {1.0} if _ONE_WAY_ROW.search(label) else set()
    days = figure_days(label, "", user_days, answer_days)
    factors = set(period_factors(unit, days, four_weeks=hedged or bool(FOUR_WEEK_MONTH.search(label))))
    if unit == "month":
        factors |= {2.0 * float(m.group(1)) for m in _DAYS_IN_PERIOD.finditer(label)}
    return factors


def period_table_rows(text: str, routes: list, *, user_days: Iterable[float] = (),
                      answer_days: Iterable[float] = ()) -> dict:
    """``{line index: {"table": header index, "ok": bool}}`` for every data row of a period
    table in ``text`` that carries a judged figure; ``ok`` is every judged cell holding under
    ONE consistent choice of measurement per place for the whole table (a table is one chain:
    a Bow column on 27 a day and on 28 a week holds under neither)."""
    routes = merge_same_place([r for r in routes if r.get("minutes") is not None])
    verdicts: list = []
    for variant in route_variants(routes):
        verdicts.append(_period_table_rows(text, variant, user_days=user_days, answer_days=answer_days))
        if all(v["ok"] for v in verdicts[-1].values()):
            break          # every table holds under this choice: the others cannot do better
    out: dict = {}
    for table in sorted({v["table"] for verdict in verdicts for v in verdict.values()}):
        rows = [{row: v for row, v in verdict.items() if v["table"] == table} for verdict in verdicts]
        # the first choice under which every row holds, else the one with the most rows holding
        out.update(max(rows, key=lambda r: (all(v["ok"] for v in r.values()),
                                            sum(v["ok"] for v in r.values()), -rows.index(r))))
    return out


def _period_table_rows(text: str, routes: list, *, user_days: Iterable[float] = (),
                       answer_days: Iterable[float] = ()) -> dict:
    routes = [r for r in routes if r.get("minutes") is not None and (r.get("mode") or "transit") == "transit"]
    lines = str(text or "").split("\n")
    out: dict = {}
    index = 0
    while index + 1 < len(lines):
        header, rule = lines[index], lines[index + 1]
        if not (header.strip().startswith("|") and _TABLE_RULE_LINE.match(rule)):
            index += 1
            continue
        columns = []
        for cell in _table_cells(header):
            named = [r for r in routes if any(_mentions(cell, label) >= 0 for label in r["labels"] if label)]
            unique = {id(r): r for r in named}
            if len(unique) == 1:
                columns.append(("route", next(iter(unique.values()))))
            elif _SAVING_COLUMN.search(cell):
                columns.append(("saving", None))
            else:
                columns.append((None, None))
        route_columns = [r for kind, r in columns if kind == "route"]
        row = index + 2
        if route_columns:
            pair = {round(abs(a["minutes"] - b["minutes"]), 2) for a, b in combinations(route_columns, 2)} - {0.0}
            diffs = pair or set(differences(sorted({r["minutes"] for r in routes})))
            while row < len(lines) and lines[row].strip().startswith("|"):
                cells = _table_cells(lines[row])
                label = cells[0] if cells else ""
                verdicts = []
                for position, cell in enumerate(cells[1:], start=1):
                    kind, route = columns[position] if position < len(columns) else (None, None)
                    spans = figure_spans(cell)
                    if kind is None or not spans:
                        continue
                    hedged = bool(HEDGE.search(cell))
                    factors = _row_factors(label, user_days, answer_days, hedged)
                    if not factors:
                        continue
                    bases = [route["minutes"]] if kind == "route" else list(diffs)
                    verdicts.append(all(any(close(value, base * factor, hedged, written_rounding(cell[start:])
                                                  if not hedged else 0.5)
                                            for base in bases for factor in factors)
                                        for start, _end, value in spans))
                if verdicts:
                    out[row] = {"table": index, "ok": all(verdicts)}
                row += 1
        index = max(row, index + 1)
    return out


def without_lines(text: str, indices: Iterable[int]) -> str:
    """``text`` with the given lines blanked (line count kept)."""
    drop = set(indices)
    return "\n".join("" if i in drop else line for i, line in enumerate(str(text or "").split("\n")))


def sentences(text: str) -> list:
    return [m.group() for m in _SENTENCE.finditer(str(text or "")) if m.group().strip()]


def _sentences_with_lines(text: str) -> list:
    """``[(sentence, its line)]``: a base quoted earlier in the paragraph fixes the pair of a
    later sentence ("… a gap of roughly 14 minutes each way … at three office days it's about
    1 hour 25 minutes"), exactly as the redactor reads it."""
    return [(sentence, line) for line in str(text or "").split("\n") for sentence in sentences(line)]


_LIST_ITEM = re.compile(r"^\s*(?:[-•]|\*(?!\*)|\d+[.)])\s+")
_HEADER = re.compile(r"^\s*(?:\*\*|#{1,4}\s)|[:：]\s*$")


def _with_header_subjects(text: str, routes: list) -> list:
    """``[(sentence, line, subject)]``: a list item under a header that names ONE route is
    about that route ("**Bow — no, it's over**" then "- 29 minutes door to door, so 4 minutes
    over your limit", r16 0a6c11d6), as the redactor reads it."""
    out, subject = [], None
    for line in str(text or "").split("\n"):
        if line.strip() and not _LIST_ITEM.match(line):
            named = [r for r in routes if any(_mentions(line, label) >= 0 for label in r["labels"] if label)]
            subject = named[0] if _HEADER.search(line) and len(named) == 1 else None
        for sentence in sentences(line):
            out.append((sentence, line, subject if _LIST_ITEM.match(line) else None))
    return out


def unsupported_arithmetic(text: str, routes: list, days: Iterable[float],
                           known: Iterable[float] = (), limits: Iterable[float] = (),
                           user_days: Iterable[float] = ()) -> list:
    """For the critic: the arithmetic sentences of ``text`` whose figures do not derive.

    Only a sentence that does arithmetic (a product, a subtraction step, a day/week total, or
    a comparison with a figure) is judged; every figure of it must pass ``check_figures``.
    Hypothetical "would save you …" prose is judged too: a saving is arithmetic either way."""
    days = set(days) | days_stated(text)
    known, limits = _sorted_figures(known), _distinct_caps(limits)
    bad = []
    for sentence, line, subject in _with_header_subjects(text, routes):
        if not figure_spans(sentence) and not products(sentence) and not JOURNEY_COUNT.search(sentence):
            continue
        if not (_ARITHMETIC_CUE.search(sentence) or DAY_PERIOD.search(sentence) or WEEK_PERIOD.search(sentence)
                or LONG_PERIOD.search(without_money(sentence)) or period_steps(sentence, sentence)
                or MARGIN_UNDER.search(sentence) or MARGIN_OVER.search(sentence)
                or SPREAD_BOUND.search(sentence) or SPREAD_EXACT.search(sentence)
                or JOURNEY_COUNT.search(sentence)):
            continue
        # The user's own limit is the reference of a margin ("about 5 minutes to spare").
        ok, seen = check_figures(sentence, sentence=sentence, line=line, routes=routes, days=days,
                                 known=known, skip_bounds=True, caps=limits, user_days=user_days,
                                 subject=subject)
        if seen and not ok:
            bad.append(sentence)
    return bad


def supported_in_text(value: float, text: str, routes_or_measured: Iterable, days: Iterable[float],
                      known: Iterable[float] = (), caps: Iterable[float] = (),
                      user_days: Iterable[float] = ()) -> bool:
    """For the critic: ``value`` (a figure the evidence lacks) sits in an arithmetic sentence
    of ``text`` whose figures all derive under the pair rule."""
    return value in supported_values_in_text([value], text, routes_or_measured, days, known=known, caps=caps,
                                             user_days=user_days)


def supported_values_in_text(values: Iterable[float], text: str, routes_or_measured: Iterable,
                             days: Iterable[float], known: Iterable[float] = (), caps: Iterable[float] = (),
                             user_days: Iterable[float] = ()) -> set:
    """:func:`supported_in_text` for several values at once: a sentence's verdict does not
    depend on the value, so each sentence is checked once, not once per value (a long answer
    with many distinct figures was quadratic)."""
    routes = [r if isinstance(r, dict) else {"labels": [], "minutes": float(r)} for r in routes_or_measured]
    days = set(days) | days_stated(text)
    known, caps = _sorted_figures(known), _distinct_caps(caps)
    sentences = list(_with_header_subjects(text, routes))
    # "(?<![\d.])20(?![\d.])" finds 20 exactly where a maximal run of digits and points is "20":
    # each sentence's runs are read once, not searched once per value.
    runs = [set(re.findall(r"[\d.]+", sentence)) for sentence, _line, _subject in sentences]
    verdicts: dict = {}
    supported = set()
    for value in values:
        written = f"{value:g}"
        plain = bool(re.fullmatch(r"[\d.]+", written))
        token = None if plain else re.compile(r"(?<![\d.])" + re.escape(written) + r"(?![\d.])")
        for index, (sentence, line, subject) in enumerate(sentences):
            if not (written in runs[index] if plain else token.search(sentence)):
                continue
            if index not in verdicts:
                verdicts[index] = check_figures(sentence, sentence=sentence, line=line, routes=routes, days=days,
                                                known=known, caps=caps, user_days=user_days, subject=subject)
            ok, seen = verdicts[index]
            if ok and seen:
                supported.add(value)
                break
    return supported
