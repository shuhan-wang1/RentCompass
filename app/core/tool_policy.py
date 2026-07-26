"""Deterministic pre-dispatch policy for READ tools in the fc loop (design §2.3).

WHY THIS FILE EXISTS
--------------------
``execute_tools_node`` had a **write-only** dispatch policy. A ``remember`` passes through
``memory_gate`` / ``guardrails.tool_allowed`` before it is allowed to run; a *read* was
dispatched unconditionally, because the only other pre-dispatch refusals are the
duplicate-digest guard and the time budget. There was no place where the executor could
say "no" to a read, so it never did.

The 2026-07-25 ``fc_loop`` sweep recorded three forbidden-tool executions, and all three
are that gap:

    B8   web_search        "UK student accommodation deposit standard amount ..."
    B12  search_properties max_budget=380/week, room_type=studio, NO area
    B14  web_search        "UK maximum tenancy deposit limit England 5 weeks rent 2025"

The eval's ``forbidden_tools`` list is marking metadata and the product neither sees it nor
should. But the class of error underneath it is real and is decidable here, before any
work happens: **all three turns quote a rent the user typed and ask what it costs or what
the law allows, and name no place to search.** Nothing can be retrieved for such a turn
that the product does not already have. B14 proves the harm is not merely wasted latency —
the retrieved snippet ("a tenancy deposit cannot be more than 5 weeks' rent") omitted the
£50,000 threshold and the model led with the wrong number.

WHAT THIS IS NOT
----------------
It is not a re-implementation of routing, and it is not a general "should the model have
called this?" judge. It is one narrow precondition, expressed as an assertion at the point
of dispatch rather than as another sentence in a system prompt, because this repo has been
bitten repeatedly by trusting instructions where it could have trusted a check.

The predicate was validated against all 98 benchmark cases before being wired in: it fires
on 8 (B3, B4, B7, B8, B10, B12, B14, B15) and on nothing else. Every one of those eight is
a ``B_money`` case with **empty** ``expected_tools`` that names ``search_properties``
and/or ``web_search`` in ``forbidden_tools`` — i.e. the gate's own notion of "no retrieval
is possible here" and the contract's agree case-for-case, independently derived. Five of
the eight already called no tools, so the gate is a no-op for them.

A denial is NOT a dead end. The refusal hands the model the authoritative
``tenancy_reference`` figures for the rent it was about to search for, so the turn gets a
better answer than the tool would have produced, at zero latency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Retrieval tools: they reach outside the turn for facts. Everything else in the
# catalogue (calculators, safety/POI lookups keyed on a place, memory) is untouched —
# a policy that denied those would be doing routing's job, not enforcement's.
RETRIEVAL_TOOLS = frozenset({"search_properties", "web_search"})


@dataclass(frozen=True)
class ReadDenial:
    """A refusal to dispatch a read.

    ``reason``    — short, recorded verbatim on the denied artifact (security audit).
    ``guidance``  — model-facing: what to do INSTEAD. Never scolding, always an action.
    ``reference`` — authoritative payload handed over in place of the tool result, so the
                    model is better off than if the call had run. May be None.
    """
    reason: str
    guidance: str
    reference: Optional[dict] = None


# ─── stated-rent detection ───────────────────────────────────────────
# A bare "£1600" is not enough: the 5-vs-6-week cap turns on the ANNUAL rent, so a figure
# whose period we are guessing at is a figure we must not compute a cap from. Both the
# amount and its period must be present in the user's own words.
_AMOUNT_RE = re.compile(r"£\s?([0-9][0-9,]*(?:\.[0-9]+)?)")
_PER_MONTH_RE = re.compile(
    r"(?:pcm|p\.c\.m|per\s+month|a\s+month|/\s*month|monthly|每月|一个月|月租)", re.I)
_PER_WEEK_RE = re.compile(
    r"(?:pw|p\.w|per\s+week|a\s+week|/\s*week|weekly|每周|一周|周租)", re.I)

# The question has to be about what it COSTS or what is LEGALLY allowed. "Make it £1500 a
# month" quotes a rent with a period and is emphatically not this.
_COST_QUESTION_RE = re.compile(
    r"(deposit"
    r"|move[\s-]?in\s+cost"
    r"|upfront"
    r"|all[\s-]?in"
    r"|total\s+cost"
    r"|what'?ll\s+it\s+cost"
    r"|what\s+would\s+it\s+cost"
    r"|how\s+much\s+.{0,40}?(?:cost|pay|deposit)"
    r"|cost\s+me"
    r"|押金|总共要|一共要|总花费)", re.I)

# A turn that asks us to go and FIND something is a retrieval turn by construction, even
# if it also quotes a budget. This veto is what keeps "find me a place under £1500 a
# month" out of the gate.
_FIND_INTENT_RE = re.compile(
    r"\b(?:find|search|look\s+for|show\s+me|listings?|available|recommend|browse)\b"
    r"|找房|搜索|帮我找|推荐", re.I)


# How far past the figure to look for the unit it is quoted in. Long enough for
# "£1,000 a week" / "£4,500 per month" and short enough that the NEXT clause cannot
# supply it.
_PERIOD_WINDOW_CHARS = 18


def _period_in(text: str) -> Optional[str]:
    """"week" / "month" when ``text`` names exactly one of them, else None."""
    weekly = bool(_PER_WEEK_RE.search(text))
    monthly = bool(_PER_MONTH_RE.search(text))
    if weekly == monthly:  # neither, or both
        return None
    return "week" if weekly else "month"


def _amount_with_period(message: str) -> Optional[tuple[float, str]]:
    """(amount, "week"|"month") when the message states a rent AND the period it is quoted
    in, else None.

    The period is read from a short window immediately AFTER the figure, not from the whole
    message. B12 is exactly why: *"I'm looking at a £380/week studio. What'll it cost me
    all-in per month"* names both units, and only one of them is the rent's — a
    whole-message reading calls that ambiguous and lets a doomed search through, while
    picking either at random risks pricing a weekly rent as a monthly one. Whole-message is
    kept only as the fallback for a figure whose unit trails further behind ("the rent, at
    £1,000, is due weekly"), and stays strict about ambiguity there.
    """
    m = _AMOUNT_RE.search(message or "")
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    window = message[m.end():m.end() + _PERIOD_WINDOW_CHARS]
    period = _period_in(window) or _period_in(message)
    if period is None:
        return None
    return amount, period


def _names_a_place(message: str) -> bool:
    """True when the message names a UK place we could actually search in.

    Reuses ``search_properties._extract_area`` — the repo's single source of truth for
    deterministic area recognition — so the gate can never disagree with the executor's
    own area injection about whether a location is present. Imported lazily: this module
    is consulted on every batch and must not drag the scraper stack into import time.
    Any failure means "a place may be present", i.e. do not gate. Fail OPEN: the cost of
    a wrong deny is a refused legitimate search, which is worse than a wasted call.
    """
    try:
        from core.tools.search_properties import _extract_area
    except Exception:
        return True
    try:
        return _extract_area(message or "") is not None
    except Exception:
        return True


def self_contained_money_question(message: str) -> Optional[tuple[float, str]]:
    """The stated ``(amount, period)`` when this turn is a money question answerable from
    the user's own figures plus statute, else None.

    All four must hold:
      1. the message states a rent amount **with** its period;
      2. it asks what that costs, or what deposit is allowed;
      3. it does NOT ask us to find or search for anything;
      4. it names no place we could search in.
    """
    text = (message or "").strip()
    if not text:
        return None
    if _FIND_INTENT_RE.search(text):
        return None
    if not _COST_QUESTION_RE.search(text):
        return None
    rent = _amount_with_period(text)
    if rent is None:
        return None
    if _names_a_place(text):
        return None
    return rent


def read_tool_denial(tool: str, args: dict, *, current_message: str) -> Optional[ReadDenial]:
    """The pre-dispatch verdict for one read call. ``None`` means dispatch it.

    ``args`` are the FINAL arguments (post strict-null-stripping and post
    ``_inject_search_params``), so what is judged here is what would actually have run.
    """
    if tool not in RETRIEVAL_TOOLS:
        return None
    rent = self_contained_money_question(current_message)
    if rent is None:
        return None
    amount, period = rent
    try:
        from core.tenancy_reference import deposit_cap
        reference = deposit_cap(**{f"{period}ly_rent": amount})
    except Exception:
        reference = None

    if tool == "search_properties":
        guidance = (
            "search_properties not dispatched: this turn asks what a rent the user already "
            "stated will cost, and names no area to search in — the listings database "
            "cannot answer it. Answer from the reference figures below and the user's own "
            "numbers. If they DO want listings, ask which area first."
        )
        reason = "self-contained money question, no searchable area"
    else:
        guidance = (
            "web_search not dispatched: the statutory rent and deposit rules are held in "
            "the product and are supplied below, already applied to the rent the user "
            "stated. Web summaries of the deposit cap routinely omit the annual-rent "
            "threshold and are the known cause of quoting the wrong cap. Answer from the "
            "reference figures; state each component."
        )
        reason = "statutory money rule owned in-product"

    if reference is None:  # reference unavailable: still refuse, but say why plainly
        guidance += " (Reference figures unavailable; compute from the stated rent.)"
    return ReadDenial(reason=reason, guidance=guidance, reference=reference)
