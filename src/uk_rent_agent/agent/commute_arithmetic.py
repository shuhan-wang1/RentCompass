"""Arithmetic on this turn's measured journey times: ONE definition of "derived".

A figure is derived from the measured routes when it is

* the difference of two measured routes ("12 minutes each way", "8 minutes slower");
* that difference x 2 (there and back), x the commuting days per week, or both ("24 minutes a
  day", "roughly 2 hours a week"); days per week are 5, the user's own office days, or an
  N-day week the answer itself states;
* the stated result of an explicit product ("12 minutes x 2 journeys x 5 days = 120 minutes")
  whose every operand is a measured figure, a measured difference or a standard multiplier,
  and whose result is right.

Exact, or within 10% (at least 2 minutes) when the figure is hedged ("roughly", "about").

Used by the critic's number grounding (``critic.unsupported_external_numbers``) and by the
answer's commute redactor (``core.candidate_validation``), so the two can never disagree about
whether "40 minutes a week" follows from Holloway 25 and Kentish Town 21 (r13 2264a2d5).
"""
from __future__ import annotations

import re
from itertools import combinations
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
HOURS_AND_MINUTES = re.compile(
    r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*(?:hours?|hrs?|h)\b(?:\s*(?:and\s+)?(\d{1,2})\s*(?:minutes?|mins?)\b)?",
    re.I)
MINUTES = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)[\s\-–]*(?:minutes?|mins?\b|分钟)", re.I)
HEDGE = re.compile(r"\b(?:roughly|about|around|approximately|approx\.?|nearly|almost|"
                   r"just\s+(?:over|under)|some|close\s+to)\b|~|≈", re.I)
DAY_PERIOD = re.compile(r"\b(?:a|per|each|every)\s+day\b|\bdaily\b|\breturn\s+trips?\b|\bround\s+trips?\b|"
                        r"\bthere\s+and\s+back\b|\btwo\s+journeys\b|\b2\s+journeys\b", re.I)
WEEK_PERIOD = re.compile(r"\b(?:a|per|each|every)\s+week\b|\bweekly\b|\bweek\b", re.I)
# A product: "12 minutes × 2 journeys × 5 days = **120 minutes (2 hours) a week**".
_UNITS = r"minutes?|mins?|journeys?|trips?|ways?|days?|times|legs?"
_OPERAND = r"(\d+(?:\.\d+)?)\s*(" + _UNITS + r")?\b"
_OPERAND_NC = r"\d+(?:\.\d+)?\s*(?:" + _UNITS + r")?\b"
PRODUCT = re.compile(
    r"(?<![\d.])" + _OPERAND + r"((?:\s*[×x*]\s*" + _OPERAND_NC + r"){1,3})"
    r"\s*=\s*[*_]*\s*(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?)", re.I)
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
    days = set()
    pattern = (r"\b(" + _NUMBER_WORD + r")[\s-]*(?:office\s+)?days?"
               r"(?:\s+(?:a|per)\s+week|[\s-]+(?:office\s+)?(?:week|commute)|\s+in\s+the\s+office)"
               r"|\b(" + _NUMBER_WORD + r")\s+office\s+days?\b")
    for match in re.finditer(pattern, str(text or ""), re.I):
        word = (match.group(1) or match.group(2)).lower()
        days.add(float(WORD_NUMBERS.get(word) or int(word)))
    return days


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


def close(value: float, target: float, hedged: bool) -> bool:
    return abs(value - target) <= (max(2.0, 0.1 * target) if hedged else 0.5)


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
        result = float(match.group(4)) * (60.0 if match.group(5).lower().startswith("h") else 1.0)
        found.append({"operands": operands, "result": result, "span": match.span()})
    return found


# "25 − 22 = 3 minutes each way": a subtraction step of two measured routes.
SUBTRACTION = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:minutes?|mins?)?\s*(?:[−–-]|minus)\s*(\d+(?:\.\d+)?)\s*(?:minutes?|mins?)?"
    r"\s*=\s*[*_]*\s*(\d+(?:\.\d+)?)\s*(?:minutes?|mins?)", re.I)


def subtractions(text: Any) -> list[dict]:
    return [{"a": float(m.group(1)), "b": float(m.group(2)), "result": float(m.group(3)), "span": m.span()}
            for m in SUBTRACTION.finditer(str(text or ""))]


def subtraction_is_correct(step: dict, measured: Iterable[float]) -> bool:
    measured = list(measured)
    return (any(abs(step["a"] - m) < 1e-9 for m in measured)
            and any(abs(step["b"] - m) < 1e-9 for m in measured)
            and abs(step["a"] - step["b"] - step["result"]) <= 0.5)


def _derived_exactly(value: float, diffs: Iterable[float], days: Iterable[float]) -> bool:
    factors = multipliers(days)
    return any(abs(value - d * f) <= 0.5 for d in diffs for f in factors)


def product_is_correct(product: dict, measured: Iterable[float], days: Iterable[float],
                       involving: float | None = None, diffs: Iterable[float] | None = None) -> bool:
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
        elif unit.startswith(_COUNT_UNITS):
            if value not in (1.0, 2.0):
                return False
        elif not unit:
            if value not in (1.0, 2.0) and not any(abs(value - d) < 1e-9 for d in days):
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


_SENTENCE = re.compile(r"[^.!?\n]*(?:[.!?](?=\s|$)|\n|$)")
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
    lowered = re.sub(r"(?<=[a-z])['’]s\b", "", str(text or "").lower())
    return " ".join(re.sub(r"[^a-z0-9一-鿿]+", " ", lowered.replace("’", "").replace("'", "")).split())


def _mentions(text: Any, label: Any) -> int:
    """Position of ``label`` in ``text`` as whole words (on the normalised text), or -1."""
    key = _key(label)
    return f" {_key(text)} ".find(f" {key} ") if key else -1


def named_routes(evidence: Any, *, _depth: int = 0) -> list[dict]:
    """``[{labels, minutes}]`` for each named whole-trip measurement in tool evidence."""
    found: list[dict] = []
    if _depth > 8 or evidence is None:
        return found
    if isinstance(evidence, dict):
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
            labels = [str(evidence[k]) for k in ("name", "area", "address", "from_address", "label")
                      if isinstance(evidence.get(k), str) and evidence[k].strip()]
            labels += [label.split(",", 1)[0].strip() for label in labels if "," in label]
            found.append({"labels": labels, "minutes": minutes})
        for key, value in evidence.items():
            if key not in ("route_legs", "legs", "steps") and isinstance(value, (dict, list, tuple)):
                found.extend(named_routes(value, _depth=_depth + 1))
    elif isinstance(evidence, (list, tuple)):
        for item in evidence[:200]:
            found.extend(named_routes(item, _depth=_depth + 1))
    return found


def _bases(text: str, measured: list, days: set) -> tuple[set, bool]:
    """(the differences ``text`` quotes as its base, whether one of its bases is WRONG).

    A base is a quoted difference, a subtraction step's result, or a product's minutes
    operand; a product operand that is itself a total of a quoted base ("6 x 5 = 30" after
    "3 x 2 = 6") is a step, not a wrong base."""
    quoted = [float(m.group(1) or m.group(2)) for m in BASE_FIGURE.finditer(text)]
    quoted += [step["result"] for step in subtractions(text)]
    valid = {v for v in quoted if is_difference(v, measured)}
    wrong = any(not is_difference(v, measured) for v in quoted)
    for product in products(text):
        first = product["operands"][0][0]
        if is_difference(first, measured):
            valid.add(first)
        elif not (any(abs(first - m) < 1e-9 for m in measured) or _derived_exactly(first, valid, days)):
            wrong = True
    return valid, wrong


def _route_after(text: str, start: int, routes: list) -> dict | None:
    segment = text[start:]
    stop = re.search(r"\d", segment)
    segment = segment[:stop.start()] if stop else segment
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
    best, best_at = None, -1
    head = f" {_key(text[:end])} "
    for route in routes:
        for label in route["labels"]:
            key = _key(label)
            at = head.rfind(f" {key} ") if key else -1
            if at > best_at:
                best, best_at = route, at
    return best


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
    out = []
    for route in routes:
        if any(re.search(r"\b" + _COMPARED_WORD + r"\s+(?:the\s+)?" + re.escape(str(label)) + r"\b", sentence, re.I)
               for label in route["labels"] if str(label).strip()):
            out.append(route)
    return out


def pair_differences(*, text: str, at: int, sentence: str, line: str, label: str, routes: list,
                     measured: list, days: set, subject: dict | None = None, start: int | None = None) -> set:
    """The differences a figure at ``at`` in ``text`` may be derived from (r14 loophole).

    The base difference quoted in the same sentence wins, then one quoted on the same line;
    then the pair the text names (the route compared WITH — "over X", "versus X", the
    bullet's label — and the clause's subject); then any difference involving that route;
    only when nothing identifies a pair, any difference of the turn."""
    sentence_bases, _ = _bases(sentence, measured, days)
    if sentence_bases:
        return sentence_bases
    line_bases, _ = _bases(line, measured, days)
    if line_bases:
        return line_bases
    target = ((_route_just_before(text, start, routes) if start is not None else None)
              or _route_after(text, at, routes) or (_route_after(label, 0, routes) if label else None))
    if target is None:
        return set(differences(measured))
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
        return {round(abs(target["minutes"] - other["minutes"]), 2)} - {0.0}
    return {d for d, pairs in differences(measured).items() if any(target["minutes"] in p for p in pairs)}


_BOUND_BEFORE = re.compile(
    r"\b(?:under|below|within|inside|less\s+than|fewer\s+than|over|above|more\s+than|at\s+least|"
    r"at\s+most|up\s+to|no\s+more\s+than|beyond)\s+(?:just\s+|about\s+|roughly\s+|around\s+|a\s+)?$", re.I)


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


def check_figures(text: str, *, sentence: str, line: str = "", label: str = "", context: str = "",
                  routes: list, days: Iterable[float], subject: dict | None = None,
                  known: Iterable[float] = (), skip_bounds: bool = False) -> tuple[bool, bool]:
    """``(every figure of text is evidenced, text does arithmetic)`` — THE derived-figure rule.

    A figure is evidenced when it is a correct product or subtraction step (or restates the
    result of one in ``context``), a total of the pair's difference (x2 / x days / x2 x days,
    within 10% only when hedged), the pair's difference itself where the sentence compares,
    or the measured time of the route named right before it. A wrong step, a wrong base on
    the line, or any other figure fails it."""
    routes = [r for r in routes if r.get("minutes") is not None]
    measured = sorted({float(r["minutes"]) for r in routes})
    if len(measured) < 2:
        return False, False
    days = {float(d) for d in days if d} | {STANDARD_DAYS}
    scope = f"{label} {sentence} {context}"
    _, wrong_base = _bases(line or sentence, measured, days)
    if wrong_base:
        return False, True
    covered, results, seen = [], [], False
    for step in subtractions(text):
        if not subtraction_is_correct(step, measured):
            return False, True
        covered.append(step["span"])
        results.append(step["result"])
        seen = True
    for product in products(text):
        diffs = pair_differences(text=text, at=product["span"][1], sentence=sentence, line=line,
                                 label=label, routes=routes, measured=measured, days=days, subject=subject)
        if not product_is_correct(product, measured, days, diffs=diffs):
            return False, True
        covered.append(product["span"])
        results.append(product["result"])
        seen = True
    results += [p["result"] for p in products(context) if product_is_correct(p, measured, days)]
    rest = text
    for start, end in sorted(covered, reverse=True):
        rest = rest[:start] + " " * (end - start) + rest[end:]
    period = bool(DAY_PERIOD.search(scope) or WEEK_PERIOD.search(scope))
    comparison = bool(COMPARISON_CUE.search(scope))
    hedged = bool(HEDGE.search(text))
    known = [float(k) for k in known]
    for start, end, value in figure_spans(rest):
        if any(abs(value - r) < 0.5 for r in results):
            seen = True
            continue
        if any(abs(value - k) <= 0.01 for k in known):
            continue          # the evidence states it outright (a leg, a fare-free figure, …)
        if skip_bounds and _BOUND_BEFORE.search(rest[max(0, start - 40):start]):
            continue          # "still under half an hour" is a verdict, not arithmetic
        diffs = pair_differences(text=rest, at=end, sentence=sentence, line=line, label=label,
                                 routes=routes, measured=measured, days=days, subject=subject,
                                 start=start)
        if period and any(close(value, d * f, hedged) for d in diffs for f in multipliers(days)):
            seen = True
            continue
        if comparison and any(abs(value - d) < 1e-9 for d in diffs):
            seen = True
            continue
        owner = _route_before(rest, start, routes) or subject
        if owner is not None and owner.get("minutes") is not None and abs(owner["minutes"] - value) < 1e-9:
            continue
        return False, True if (seen or period or comparison) else False
    return True, seen


def sentences(text: str) -> list:
    return [m.group() for m in _SENTENCE.finditer(str(text or "")) if m.group().strip()]


def unsupported_arithmetic(text: str, routes: list, days: Iterable[float],
                           known: Iterable[float] = (), limits: Iterable[float] = ()) -> list:
    """For the critic: the arithmetic sentences of ``text`` whose figures do not derive.

    Only a sentence that does arithmetic (a product, a subtraction step, a day/week total, or
    a comparison with a figure) is judged; every figure of it must pass ``check_figures``.
    Hypothetical "would save you …" prose is judged too: a saving is arithmetic either way."""
    days = set(days) | days_stated(text)
    # The user's own limit is a reference point: "about 5 minutes to spare" under a 25-minute
    # cap with a measured 20 is a difference too.
    routes = list(routes) + [{"labels": [], "minutes": float(v)} for v in limits]
    bad = []
    for sentence in sentences(text):
        if not figure_spans(sentence) and not products(sentence):
            continue
        if not (_ARITHMETIC_CUE.search(sentence) or DAY_PERIOD.search(sentence) or WEEK_PERIOD.search(sentence)):
            continue
        ok, seen = check_figures(sentence, sentence=sentence, line=sentence, routes=routes, days=days,
                                 known=known, skip_bounds=True)
        if seen and not ok:
            bad.append(sentence)
    return bad


def supported_in_text(value: float, text: str, routes_or_measured: Iterable, days: Iterable[float],
                      known: Iterable[float] = ()) -> bool:
    """For the critic: ``value`` (a figure the evidence lacks) sits in an arithmetic sentence
    of ``text`` whose figures all derive under the pair rule."""
    routes = [r if isinstance(r, dict) else {"labels": [], "minutes": float(r)} for r in routes_or_measured]
    days = set(days) | days_stated(text)
    token = re.compile(r"(?<![\d.])" + re.escape(f"{value:g}") + r"(?![\d.])")
    for sentence in sentences(text):
        if not token.search(sentence):
            continue
        ok, seen = check_figures(sentence, sentence=sentence, line=sentence, routes=routes, days=days,
                                 known=known)
        if ok and seen:
            return True
    return False
