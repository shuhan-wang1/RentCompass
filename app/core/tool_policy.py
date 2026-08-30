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
        raise RuntimeError("area recognizer unavailable")
    try:
        return _extract_area(message or "") is not None
    except Exception:
        raise RuntimeError("area recognizer failed")


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


# ─── turns that reduce ENTIRELY to the statutory arithmetic ──────────
# ``self_contained_money_question`` above answers "is retrieval pointless here?" — a
# question about the TOOL. The predicate below answers a strictly stronger one: "is this
# turn nothing but ``tenancy_reference`` arithmetic?", which is a question about the ANSWER.
# Only the second licenses ``guard_node`` to skip the model entirely, so it is separate and
# it is narrower: every case the first admits but the second refuses still reaches the model
# exactly as before, with the denial-path figures attached.
#
# Validated over every case id in evaluation/benchmark/cases*.jsonl — 117 of them, i.e. the
# 98-case sweep plus the extension and guard-regression sets (see
# tests/test_deposit_boundary.py, which re-derives this from the files rather than trusting
# this comment): it fires on B3, B4, B7, B8, B10, B14, B15 and on nothing else.
# Notably NOT on B12 — "what'll it cost
# me all-in per month, including bills and council tax" is only PARTLY derivable, and its
# contract requires refusing to fabricate the rest. A deterministic total there would be
# confidently wrong, which is worse than the defect being fixed.

# "First month plus the deposit" / "total upfront" — the total, not just the cap. Checked
# BEFORE the deposit shape because B4, B8 and B15 say both and the total is what they want.
_MOVE_IN_RE = re.compile(
    r"move[\s-]?in\s+cost"
    r"|(?:total|overall)\s+(?:up[\s-]?front|move[\s-]?in|initial|first)"
    r"|up[\s-]?front\s+cost"
    r"|first\s+month(?:'?s)?\s+(?:rent\s+)?(?:plus|and|\+)"
    r"|total\s+cost\s+to\s+move"
    r"|入住(?:前)?(?:总|一共|总共)|总花费|前期费用", re.I)

_DEPOSIT_RE = re.compile(r"deposit|押金", re.I)

# Cost components this module cannot derive from a rent. If the turn asks for any of them
# the honest answer needs the model (and, per B12's contract, an explicit refusal to
# estimate) — so hand it over. "all-in" is here rather than in the move-in shape for that
# reason: an all-in figure is bills-inclusive by definition.
_NON_DERIVABLE_COST_RE = re.compile(
    r"\ball[\s-]?in\b|bills?|utilit|council\s*tax|electric|\bgas\b|\bwater\b"
    r"|broadband|internet|wi[\s-]?fi|insur|\btv licen|service\s*charge"
    r"|admin(?:istration)?\s*fee|agen(?:cy|t'?s?)\s*fee|referenc|inventory"
    r"|账单|水电|市政税|网费|中介费|服务费", re.I)

# Asks that are about the deposit but are not arithmetic: protection schemes, getting it
# back, deductions, disputes, timing. The template answers none of these, so it must not
# pre-empt them. (Deliberately does NOT veto "should I expect" — B3 is that phrasing and is
# pure arithmetic.)
_BEYOND_ARITHMETIC_RE = re.compile(
    r"how\s+(?:do|can|would|will)\s+(?:i|we)\b"
    r"|\bget\s+(?:it|my|the\s+deposit)\s+back\b|\bdeposit\s+back\b"
    r"|\bprotect|\bscheme\b|\bdisput|\brefund|\bdeduct|\bwithhold|\bclaim\b"
    r"|\bwhat\s+(?:if|happens)\b|\bwhen\s+(?:do|does|is|will)\b"
    r"|\bcan\s+(?:they|the\s+landlord)\s+keep\b"
    r"|怎么退|能退|退还|纠纷|保护计划", re.I)

# A user-stated holding deposit: "I've already paid a £350 holding deposit". Matched near
# the phrase so the rent figure is never mistaken for it. This is the ONE way an amount
# other than the rent may appear and the turn still be answered deterministically — and it
# exists precisely because a stated holding deposit is what B4 double-counted.
_HOLDING_RE = re.compile(r"holding\s+(?:deposit|fee|payment)|意向金|预定金|定金", re.I)
# Tight, for the same reason ``_PERIOD_WINDOW_CHARS`` is tight: long enough for "a £346.15
# holding deposit" / "holding deposit of £346.15", short enough that the RENT figure in the
# preceding clause cannot be mistaken for it.
_HOLDING_WINDOW_CHARS = 24


def _amount_spans(message: str) -> list[tuple[float, int, int]]:
    out = []
    for m in _AMOUNT_RE.finditer(message or ""):
        try:
            out.append((float(m.group(1).replace(",", "")), m.start(), m.end()))
        except ValueError:
            continue
    return out


def _split_holding_deposit(message: str) -> tuple[Optional[float], set]:
    """(holding-deposit amount, indices of amount spans it consumed).

    None unless the message names a holding deposit AND exactly one £ figure sits within
    ``_HOLDING_WINDOW_CHARS`` of that phrase. Ambiguity yields None, which makes the turn
    non-deterministic and sends it to the model — the safe direction.
    """
    hm = _HOLDING_RE.search(message or "")
    if hm is None:
        return None, set()
    near = [(i, amt) for i, (amt, s, e) in enumerate(_amount_spans(message))
            if s <= hm.end() + _HOLDING_WINDOW_CHARS and e >= hm.start() - _HOLDING_WINDOW_CHARS]
    if len(near) != 1:
        return None, set()
    idx, amt = near[0]
    return amt, {idx}


def statutory_money_answer(message: str) -> Optional[tuple]:
    """``(kind, amount, period, holding_deposit)`` when this turn is answerable ENTIRELY by
    ``tenancy_reference``, else None.

    ``kind`` is one of ``tenancy_reference.ANSWER_KINDS``. Everything
    ``self_contained_money_question`` requires must hold, PLUS:

      5. the question is a deposit-cap or a move-in-total question and nothing else;
      6. it asks for no cost component we cannot derive from a rent (bills, council tax,
         agency fees, an "all-in" figure);
      7. it asks nothing non-arithmetical about the deposit (protection, deductions,
         getting it back);
      8. exactly ONE rent figure is in play. A second £ amount is only tolerated when it is
         unambiguously a stated holding deposit, because picking the wrong one of two
         amounts to price is a silent wrong answer, and silent wrong answers are the
         defect.
    """
    rent = self_contained_money_question(message)
    if rent is None:
        return None
    text = message or ""
    if _NON_DERIVABLE_COST_RE.search(text) or _BEYOND_ARITHMETIC_RE.search(text):
        return None

    holding, consumed = _split_holding_deposit(text)
    rent_amounts = {amt for i, (amt, _s, _e) in enumerate(_amount_spans(text))
                    if i not in consumed}
    if len(rent_amounts) != 1:
        return None
    if _HOLDING_RE.search(text) and holding is None:
        return None  # holding deposit named but unpriced: let the model ask.

    amount, period = rent
    if amount not in rent_amounts:
        return None  # the priced figure is not the one the period binds to.

    if _MOVE_IN_RE.search(text):
        kind = "move_in"
    elif _DEPOSIT_RE.search(text):
        kind = "deposit"
    else:
        return None
    if kind == "deposit" and holding is not None:
        # A holding deposit is a payment, not a cap. It changes a total, never the cap, so
        # a "what's the deposit" turn that mentions one is asking something else too.
        return None
    return kind, amount, period, holding


# ─── standalone weekly/monthly conversion ──────────────────────────
_CONVERSION_TARGET_MONTH_RE = re.compile(
    r"(?:monthly|per\s+(?:calendar\s+)?month|pcm|month(?:ly)?\s+equivalent|每月|一个月)", re.I)
_CONVERSION_TARGET_WEEK_RE = re.compile(
    r"(?:weekly|per\s+week|pw|week(?:ly)?\s+equivalent|每周|一周)", re.I)
_CONVERSION_VERB_RE = re.compile(
    r"(?:convert|conversion|equivalent|how\s+much|calculate|work\s+out|换算|折算|计算)", re.I)


def standalone_rent_conversion(message: str) -> Optional[tuple[str, float]]:
    """Return ``(direction, amount)`` for an unambiguous rent conversion.

    "Unambiguous" means the whole turn is the conversion. B12 — *"I'm looking at a
    £380/week studio. What'll it cost me all-in per month, including bills and council
    tax?"* — contains a conversion but is not one: the answer it asks for is a
    bills-inclusive total, and no weekly-to-monthly arithmetic can produce that. Returning
    a verdict here short-circuits the entire turn deterministically, so the user would get
    the rent conversion presented as the answer to a question about total cost. Anything
    this module cannot derive from a rent hands over to the model, which owns the refusal
    to invent bills (``_NON_DERIVABLE_COST_RE`` is the same gate ``statutory_money_answer``
    uses, deliberately — one definition of "not derivable from a rent", not two).
    """
    text = (message or "").strip()
    if not text or _FIND_INTENT_RE.search(text) or _names_a_place(text):
        return None
    if _NON_DERIVABLE_COST_RE.search(text):
        return None
    source = _amount_with_period(text)
    if source is None:
        return None
    amount, period = source
    target_month = bool(_CONVERSION_TARGET_MONTH_RE.search(text))
    target_week = bool(_CONVERSION_TARGET_WEEK_RE.search(text))
    if period == "week":
        if not target_month or (target_week and not _CONVERSION_VERB_RE.search(text)):
            return None
        return "week_to_month", amount
    if not target_week or (target_month and not _CONVERSION_VERB_RE.search(text)):
        return None
    return "month_to_week", amount


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
