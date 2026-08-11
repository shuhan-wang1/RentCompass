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
