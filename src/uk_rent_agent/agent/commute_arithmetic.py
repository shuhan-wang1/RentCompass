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


def durations(text: Any) -> list[float]:
    """Every duration ``text`` states, in minutes: "2 hours 20 minutes" is one 140."""
    text = str(text or "")
    found = [float(m.group(1)) * 60 + float(m.group(2) or 0) for m in HOURS_AND_MINUTES.finditer(text)]
    rest = HOURS_AND_MINUTES.sub(" ", text)
    found.extend(float(m.group(1)) for m in MINUTES.finditer(rest))
    words = " ".join(rest.lower().split())
    for phrase, value in DURATION_WORDS:
        if phrase in words:
            found.append(value)
            words = words.replace(phrase, " ")
    return found


def days_stated(text: Any) -> set[float]:
    """Commuting days per week a text states: "a five-day week", "3 days a week"."""
    days = set()
    for match in re.finditer(r"\b(" + _NUMBER_WORD + r")[\s-]*days?(?:\s+(?:a|per)\s+week|[\s-]+(?:week|commute))",
                             str(text or ""), re.I):
        word = match.group(1).lower()
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


def product_is_correct(product: dict, measured: Iterable[float], days: Iterable[float],
                       involving: float | None = None) -> bool:
    """Every operand is evidenced and the stated result is the product."""
    measured = list(measured)
    days = {float(d) for d in days if d} | {STANDARD_DAYS}
    total = 1.0
    minute_operands = 0
    for value, unit in product["operands"]:
        total *= value
        if unit.startswith("min") or not unit and minute_operands == 0:
            minute_operands += 1
            if not (any(abs(value - m) < 1e-9 for m in measured)
                    or is_difference(value, measured, involving)):
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
    r"\b(?:each|one)\s+way\b|\bper\s+(?:trip|journey)\b|\bsaves?\b|\bsaving\b|\bdifference\b|"
    r"\b(?:quicker|faster|shorter|longer|slower|less|more)\b|\bversus\b|\bvs\.?|\bover\b|\bthan\b|"
    r"[×*]|\bx\s*\d", re.I)


def supported_in_text(value: float, text: str, measured: Iterable[float], days: Iterable[float]) -> bool:
    """For the critic: ``value`` appears in a sentence of ``text`` that does arithmetic on the
    measured routes, and it is a difference, a derived total or a correct product there."""
    measured = list(measured)
    if len(measured) < 2:
        return False
    days = set(days) | days_stated(text)
    token = re.compile(r"(?<![\d.])" + re.escape(f"{value:g}") + r"(?![\d.])")
    for match in _SENTENCE.finditer(str(text or "")):
        sentence = match.group()
        if not token.search(sentence) or not _ARITHMETIC_CUE.search(sentence):
            continue
        for product in products(sentence):
            if product_is_correct(product, measured, days) and any(
                    abs(value - v) < 1e-9 for v, _u in product["operands"] + [(product["result"], "")]):
                return True
        hedged = bool(HEDGE.search(sentence))
        if is_difference(value, measured) or (
                (DAY_PERIOD.search(sentence) or WEEK_PERIOD.search(sentence))
                and is_derived_total(value, measured, days, hedged=hedged)):
            return True
    return False
