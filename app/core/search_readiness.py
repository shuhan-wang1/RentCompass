from __future__ import annotations

import re
from dataclasses import dataclass


PROCEED_PHRASES_ZH = (
    "继续搜索", "继续搜", "继续找", "继续", "就这样吧", "就这样", "直接搜索", "直接搜",
    "可以了", "没事", "不用了继续", "都行", "先搜", "随便搜",
)
PROCEED_PATTERNS_EN = (
    r"\bcontinue\b", r"\bgo ahead\b", r"\bsearch anyway\b", r"\bjust search\b",
    r"\bsearch now\b", r"\bproceed\b", r"\bthat'?s fine\b", r"\bthat is fine\b",
    r"\bgo on\b", r"\bkeep going\b", r"\bit'?s fine\b",
)


def is_proceed_intent(text: str) -> bool:
    if not text:
        return False
    if any(phrase in text for phrase in PROCEED_PHRASES_ZH):
        return True
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in PROCEED_PATTERNS_EN)


@dataclass(frozen=True)
class SearchReadiness:
    status: str
    missing_hard: tuple[str, ...]
    missing_soft: tuple[str, ...]
    missing_optional: tuple[str, ...]
    proceed_intent: bool

    @property
    def should_search(self) -> bool:
        return self.status == "ready"


def assess_search_readiness(
    *,
    resolved_area: str | None,
    max_budget,
    room_type: str | None,
    no_commute: bool,
    commute_destination: str | None,
    criteria_gate_shown: bool,
    confirmed: bool,
    user_message: str,
    move_in_date: str | None = None,
) -> SearchReadiness:
    """The single deterministic decision for listing-search admission.

    ``resolved_area`` is deliberately post-resolution: a commute destination may be
    converted to a searchable residential slug by the tool before this function runs.
    """
    missing_hard = () if str(resolved_area or "").strip() else ("area",)
    missing_soft = []
    try:
        has_budget = max_budget is not None and int(max_budget) > 0
    except (TypeError, ValueError):
        has_budget = False
    if not has_budget:
        missing_soft.append("budget")
    if not str(room_type or "").strip():
        missing_soft.append("room_type")
    if not (bool(no_commute) or bool(str(commute_destination or "").strip())):
        missing_soft.append("commute")
    proceed = bool(confirmed) or is_proceed_intent(user_message)
    if missing_hard:
        status = "missing_hard"
    elif missing_soft and not criteria_gate_shown and not proceed:
        status = "ask_soft_once"
    else:
        status = "ready"
    return SearchReadiness(
        status=status,
        missing_hard=missing_hard,
        missing_soft=tuple(missing_soft),
        missing_optional=(() if move_in_date else ("move_in",)),
        proceed_intent=proceed,
    )


SEARCH_READINESS_SYSTEM_RULE = (
    "SEARCH READINESS CONTRACT v1 — confirmed=true; CRITERIA COMPLETE, ACT FIRST: "
    "search_properties is the sole authority for readiness. For a listing-search intent, "
    "call it instead of inventing a separate clarification policy. It requires one resolved "
    "residential area (a commute destination may be resolved by the tool). Budget, room type, "
    "and commute are recommended, not hard requirements: the tool may show exactly one soft "
    "criteria gate. After that gate, after any answer to it, or when the user says continue, "
    "call the tool with accumulated criteria and confirmed=true; never show the gate twice. "
    "When area plus budget, room type, and commute/no_commute are present, call the tool "
    "directly and do not pre-emptively clarify about 单间 sub-types or which campus; state a "
    "reasonable interpretation and allow refinement. A clarification outside the tool is only "
    "for genuinely missing hard criteria or contradictory input; the criteria gate owns soft "
    "missing fields. Explicit research-only/no-search instructions remain higher priority."
)


SEARCH_TOOL_DESCRIPTION = (
    "Search the UK database for specific rental listings. Use for listing-search intent, "
    "not general market/area/cost research. Readiness is deterministic inside this tool: a "
    "resolved residential area is the only hard requirement (commute_destination may resolve "
    "one); budget, room type and commute/no_commute are recommended. If recommended criteria "
    "are missing, it can return one soft_criteria clarification at most. Pass confirmed=true "
    "after the user answers that gate or asks to proceed. Missing optional criteria then mean "
    "no corresponding filter, not another clarification. area is where the user lives; "
    "commute_destination is where they travel to."
)


# ── Relative budget edits ─────────────────────────────────────────────────────
# "Raise the budget by £200" was applied as an ABSOLUTE budget of £200 on both
# architectures (live: the model reported `max_budget: 200` and the search returned 0
# results). The deterministic budget extractor sees a currency amount and cannot tell a
# DELTA from a CEILING, so the delta replaced the ceiling. This is the single place that
# tells the two apart; every caller that folds a stated budget into the accumulated
# criteria must consult it BEFORE treating the number as a ceiling.
#
# The rule is deliberately narrow: a relative marker ("by", "more", "less", "another",
# 「提高」「多」「少」…) must be present AND the amount must not be introduced by an
# absolute preposition ("to £2400" / 「提高到 2400」), which is a new ceiling, not a delta.

_AMOUNT = r"(\d[\d,]*(?:\.\d+)?)"

# An absolute restatement dressed in raise/lower language: "raise the budget TO £2400",
# 「把预算提高到2400」. Checked first; when it matches, this is NOT a relative edit.
# A CEILING marker, not a delta. Two shapes: a marker introducing the amount
# ("at most £2,200" / 「最多2200英镑」) and the Chinese postfix ("2200镑以内").
#
# The English markers all require a currency token, deliberately: "keep the commute under
# 30 minutes" must not turn a sentence that ALSO says "raise the budget by £200" into an
# absolute restatement. The Chinese prefix markers must be followed by digits, so
# 「最多提高200镑」 (a genuine bounded delta) is untouched — 最多 there is followed by 提高.
_ABSOLUTE_TARGET = re.compile(
    r"(?:to|up\s+to|at\s+most|max(?:imum)?(?:\s+of)?)\s*(?:£|gbp\s*)?" + _AMOUNT
    + r"|(?:到|至)\s*(?:£)?\s*" + _AMOUNT
    # EN ceiling markers — currency required (see above).
    + r"|\b(?:under|below|within|no\s+more\s+than|not\s+more\s+than|at\s+most|"
      r"up\s+to|maximum(?:\s+of)?|max(?:\s+of)?|cap(?:ped)?(?:\s+at)?)\s*"
      r"(?:£|gbp\s+)" + _AMOUNT
    # ZH ceiling markers introducing the amount.
    + r"|(?:最多|至多|顶多|不超过|不高于|不多于|最高|上限|封顶)\s*(?:£)?\s*" + _AMOUNT
    # ZH postfix ceiling ("2200镑以内" / "2200以下").
    + r"|" + _AMOUNT + r"\s*(?:£|镑|英镑|磅|元|块)?\s*(?:以内|以下|之内|以下|封顶)",
    re.IGNORECASE,
)

# (regex, sign, strict). ``strict`` patterns name the edit explicitly ("raise ... by X")
# and stand on their own; the loose ones ("add £200", 「预算多200」) additionally require
# money evidence in the message, so "add 2 bedrooms" can never move a budget. Ordered:
# the most explicit phrasings first.
_RELATIVE_BUDGET_PATTERNS: tuple[tuple, ...] = (
    # EN — explicit verb + "by"
    (re.compile(r"\b(?:raise|increase|bump|push|lift|extend|stretch|up|upped|raising|"
                r"increasing|bumping)\b[^.!?\n]{0,40}?\bby\s*(?:£|gbp\s*)?" + _AMOUNT,
                re.IGNORECASE), +1, True),
    (re.compile(r"\b(?:lower|reduce|decrease|cut|drop|trim|shave|lowering|reducing|"
                r"cutting|dropping)\b[^.!?\n]{0,40}?\bby\s*(?:£|gbp\s*)?" + _AMOUNT,
                re.IGNORECASE), -1, True),
    # EN — "budget up/down by X" with no verb of its own
    (re.compile(r"\bbudget\b[^.!?\n]{0,20}?\bup\s+by\s*(?:£|gbp\s*)?" + _AMOUNT,
                re.IGNORECASE), +1, True),
    (re.compile(r"\bbudget\b[^.!?\n]{0,20}?\bdown\s+by\s*(?:£|gbp\s*)?" + _AMOUNT,
                re.IGNORECASE), -1, True),
    # EN — "£200 more/extra", "£200 less/cheaper"
    (re.compile(r"(?:£|gbp\s*)?" + _AMOUNT + r"\s*(?:pounds?|quid|gbp)?\s*"
                r"(?:a\s+month\s+|per\s+month\s+|pcm\s+|/month\s+)?(?:more|extra|higher)\b",
                re.IGNORECASE), +1, False),
    (re.compile(r"(?:£|gbp\s*)?" + _AMOUNT + r"\s*(?:pounds?|quid|gbp)?\s*"
                r"(?:a\s+month\s+|per\s+month\s+|pcm\s+|/month\s+)?(?:less|lower|cheaper)\b",
                re.IGNORECASE), -1, False),
    # EN — "add £200", "another £200"
    (re.compile(r"\b(?:add|another|plus)\s*(?:£|gbp\s*)?" + _AMOUNT, re.IGNORECASE),
     +1, False),
    # ZH — 提高/上调/增加/加/涨/多 + amount (「到/至」 already excluded above)
    # ``(?<![最至顶])`` is load-bearing: without it the bare 多 matched inside 最多, so
    # 「预算每月最多2200英镑」 — a plain CEILING — was read as a +2200 delta, and (with no
    # prior ceiling to add it to) the caller then dropped the budget filter entirely.
    # 「最多提高200镑」 still resolves as a delta: 提高 is preceded by 多, not by 最.
    (re.compile(r"(?<![最至顶])(?:提高|提升|上调|增加|加大|加|涨|多)\s*(?:£)?\s*" + _AMOUNT),
     +1, False),
    # ZH — 降低/下调/减少/减/少 + amount
    (re.compile(r"(?<![最至顶不])(?:降低|下调|减少|调低|减|少)\s*(?:£)?\s*" + _AMOUNT),
     -1, False),
)

# The token immediately after the amount, when it is a NON-money unit, says the number
# counts something else — bedrooms, minutes, people. Those numbers never move a budget.
_NON_MONEY_TAIL = re.compile(
    r"^\s*(?:%|percent|个|间|套|室|居|人|位|天|晚|岁|分钟|小时|公里|米|站|"
    r"bed(?:room)?s?\b|beds?\b|rooms?\b|people\b|persons?\b|mins?\b|minutes?\b|"
    r"hours?\b|days?\b|weeks?\b|months?\b|years?\b|miles?\b|km\b|m2\b)",
    re.IGNORECASE)

# Money evidence anywhere in the message, for the loose patterns.
_MONEY_EVIDENCE = re.compile(
    r"£|\bgbp\b|\bpounds?\b|\bquid\b|\bpcm\b|\bbudget\b|\brent\b|\bprice\b|"
    r"预算|租金|月租|房租|镑|英镑|价格",
    re.IGNORECASE)

# A weekly delta is normalised to monthly, because the accumulated ceiling is monthly.
_WEEKLY_MARKER = re.compile(r"\b(?:a|per)\s*week\b|\bweekly\b|\bpw\b|/\s*w\b|每周|一周|/周",
                            re.IGNORECASE)


def detect_relative_budget_delta(text: str) -> float | None:
    """Signed MONTHLY delta stated in ``text``, or ``None`` when it states no relative edit.

    ``None`` means "not a relative edit" — including the raise/lower phrasings that name a
    new absolute ceiling ("raise the budget to £2400"), which the caller must keep treating
    as a ceiling.
    """
    message = str(text or "")
    if not message.strip():
        return None
    if _ABSOLUTE_TARGET.search(message):
        return None
    has_money = bool(_MONEY_EVIDENCE.search(message))
    for pattern, sign, strict in _RELATIVE_BUDGET_PATTERNS:
        match = pattern.search(message)
        if not match:
            continue
        if not strict and not has_money:
            continue
        if _NON_MONEY_TAIL.match(message[match.end():]):
            continue
        raw = (match.group(1) or "").replace(",", "")
        try:
            amount = float(raw)
        except ValueError:
            continue
        if amount <= 0:
            continue
        if _WEEKLY_MARKER.search(message):
            try:
                from core.tenancy_reference import monthly_from_weekly
                amount = float(monthly_from_weekly(amount))
            except Exception:
                pass
        return sign * amount
    return None


def resolve_relative_budget(text: str, prior_budget) -> int | None:
    """The new MONTHLY ceiling for a relative edit, or ``None`` when it does not apply.

    ``None`` covers both "this message states no relative edit" and "it does, but there is
    no prior ceiling to apply it to" — the caller distinguishes them with
    :func:`detect_relative_budget_delta` when it needs to (a delta with no base is not a
    ceiling either, so the caller must drop the filter rather than search at the delta).
    The result is floored at 1: "lower it by £3000" from a £2000 ceiling is a nonsense
    edit, not a free search.
    """
    delta = detect_relative_budget_delta(text)
    if delta is None:
        return None
    try:
        base = float(prior_budget)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return max(1, int(round(base + delta)))
