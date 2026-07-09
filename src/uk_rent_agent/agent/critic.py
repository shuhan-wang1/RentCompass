"""Grounding critic for the response generator.

The critic is the last line of defence against *fabricated listing figures* (a
model quoting a rent, deposit, or total that never appeared in the data). Its
guiding rule is asymmetric:

    * It must catch invented prices.
    * It must NEVER destroy a legitimate answer.

The previous implementation violated the second half badly: it compared only the
``£``/``GBP``-prefixed numbers in the reply against a JSON dump of ``tool_raw_data``
and, on any mismatch, hard-replaced the entire user-facing answer with a canned
fallback string. Pure formatting ("2678 pcm" vs "£2,678"), any legitimate
arithmetic (rent × 12, weekly↔monthly, deposit = N weeks), and anything the model
was shown through the *context* rather than the raw tool payload all produced
false positives — and the "fix" deleted the answer (and its recommendations).

This module replaces that with three principled pieces:

1. NUMERIC NORMALIZATION (``_money_mentions`` / ``unsupported_reply_prices``).
   Prices are parsed out of both sides regardless of currency formatting — the
   ``£``/``GBP`` may be a prefix or a suffix, thousands separators are dropped, and
   ``pcm``/``pw``/``per month``/``per week`` annotations are read to recover the
   billing period. A reply price is *supported* when it (a) matches an evidence
   number within ~1 %, or (b) is a standard derivation of an evidence price:
   weekly↔monthly conversion, an annual / N-month total (× 1‑36), or a deposit of
   N weeks' rent (× 1‑6). Only prices carrying a currency/period marker are gated
   in the reply, so plain integers ("12 months", "3 beds") are never flagged.

2. EVIDENCE SURFACE. The critic node (in ``langgraph_agent``) gathers *everything
   the generator was shown* — ``tool_raw_data``, the observation, the assembled
   context (previous results, comparison data, current property) and the user's
   own budget — and passes it here as ``evidence``. Quoting any of those is
   therefore grounded, which fixes the "unsupported because it came from context"
   class of false positives.

3. ENFORCEMENT (``enforce_grounding``). User-facing text is never hard-replaced.
   A not-grounded verdict triggers exactly one regeneration pass with an explicit
   corrective instruction (supplied by the caller via the ``regenerate``
   callback). If the regenerated answer still fails, it is delivered anyway with a
   single appended caveat sentence — never the bare fallback, and the caller never
   drops recommendations. Every verdict is surfaced through the optional
   ``on_verdict`` hook so misfires stay measurable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from uk_rent_agent.agent.contracts import CriticVerdict


# ── numeric parsing ────────────────────────────────────────────────────────
# A bare number with optional thousands separators / decimals.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Currency immediately *before* the number ("£2,678", "GBP 450").
_CURRENCY_BEFORE = re.compile(r"(?:£|GBP)\s*\Z", re.IGNORECASE)
# Period markers immediately *after* the number, optionally after a "GBP" suffix.
_GBP = r"(?:GBP\s*)?"
_MONTHLY_AFTER = re.compile(
    r"\A\s*" + _GBP + r"(?:pcm|pm\b|/\s*(?:month|mo)\b|per\s+(?:calendar\s+)?month\b"
    r"|a\s+month\b|monthly\b|/month\b)",
    re.IGNORECASE,
)
_WEEKLY_AFTER = re.compile(
    r"\A\s*" + _GBP + r"(?:pw\b|/\s*(?:week|wk|w)\b|per\s+week\b|a\s+week\b|weekly\b|/week\b)",
    re.IGNORECASE,
)
# A "GBP" suffix on its own still marks the number as money (period unknown).
_CURRENCY_AFTER = re.compile(r"\A\s*GBP\b", re.IGNORECASE)

# ~1 % relative tolerance, with a small absolute floor to absorb rounding.
_REL_TOL = 0.01
_ABS_TOL = 1.0

# Integer-multiple range covering annual/N-month totals and deposit multiples.
_MAX_MONTHS = 36
_MAX_DEPOSIT_WEEKS = 6
# Weeks-per-year / months-per-year: the standard UK pcm ↔ pw conversion.
_WEEKS_PER_YEAR = 52
_MONTHS_PER_YEAR = 12

# Delivered (appended, never substituted) when a regenerated answer still fails.
CAVEAT = "Please double-check the exact prices against the source listing."

# Legacy hard-replacement strings. Retained only so callers/tests can assert the
# new pipeline never emits them; the enforcement path no longer uses them.
LEGACY_RETRIEVAL_MISS_FALLBACK = (
    "I couldn't verify this against current listing data. "
    "Please check a live property portal before deciding."
)
LEGACY_INCONSISTENCY_FALLBACK = (
    "I found a possible inconsistency in the available listing data, so I won't "
    "quote unverified details. Please check the source listing."
)


def _serialize(evidence: Any) -> str:
    """Flatten any evidence structure to a single searchable string."""
    if evidence is None:
        return ""
    if isinstance(evidence, str):
        return evidence
    return json.dumps(evidence, ensure_ascii=False, default=str)


def _to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", "").rstrip("."))
    except ValueError:
        return None


def _all_numbers(text: str) -> set[float]:
    """Every numeric token in ``text`` (currency-agnostic); the direct-match pool."""
    out: set[float] = set()
    for match in _NUMBER.finditer(text):
        value = _to_float(match.group())
        if value is not None:
            out.add(value)
    return out


def _money_mentions(text: str) -> list[tuple[float, str]]:
    """Numbers that read as *money* plus their billing period.

    Returns ``(value, unit)`` where ``unit`` is ``"monthly"``, ``"weekly"`` or
    ``"unknown"``. A number qualifies when it carries a currency symbol (prefix or
    ``GBP`` suffix) or a rent-period annotation — plain integers are ignored so we
    never gate "12 months" or "3 bedrooms".
    """
    mentions: list[tuple[float, str]] = []
    if not text:
        return mentions
    for match in _NUMBER.finditer(text):
        value = _to_float(match.group())
        if value is None:
            continue
        before = text[max(0, match.start() - 8):match.start()]
        after = text[match.end():match.end() + 24]

        has_currency_before = bool(_CURRENCY_BEFORE.search(before))
        monthly = bool(_MONTHLY_AFTER.match(after))
        weekly = bool(_WEEKLY_AFTER.match(after))
        has_currency_after = bool(_CURRENCY_AFTER.match(after))

        if not (has_currency_before or monthly or weekly or has_currency_after):
            continue
        unit = "monthly" if monthly else "weekly" if weekly else "unknown"
        mentions.append((value, unit))
    return mentions


def _derivations(value: float, unit: str) -> set[float]:
    """Standard rent-derived figures from a single evidence price.

    * integer multiples (annual / N-month totals, and weekly-rent deposit
      multiples, both covered by ``value × 1‑36``),
    * weekly → monthly conversion,
    * monthly → weekly conversion and deposits of N weeks derived from it.
    """
    out: set[float] = {value}
    for n in range(2, _MAX_MONTHS + 1):
        out.add(value * n)
    if unit in ("weekly", "unknown"):
        out.add(value * _WEEKS_PER_YEAR / _MONTHS_PER_YEAR)
    if unit in ("monthly", "unknown"):
        weekly = value * _MONTHS_PER_YEAR / _WEEKS_PER_YEAR
        out.add(weekly)
        for n in range(1, _MAX_DEPOSIT_WEEKS + 1):
            out.add(weekly * n)
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(_ABS_TOL, _REL_TOL * max(abs(a), abs(b)))


def _close_to_any(value: float, pool: set[float]) -> bool:
    return any(_close(value, candidate) for candidate in pool)


def unsupported_reply_prices(response: str, evidence: Any) -> list[float]:
    """Reply prices that are neither present in nor derivable from the evidence."""
    evidence_text = _serialize(evidence)
    evidence_numbers = _all_numbers(evidence_text)
    evidence_rents = _money_mentions(evidence_text)

    # Derive from annotated rents (with their real unit) *and* every bare evidence
    # number (unit unknown) — the latter catches numeric JSON price fields.
    supported: set[float] = set()
    for value, unit in evidence_rents:
        supported |= _derivations(value, unit)
    for number in evidence_numbers:
        supported |= _derivations(number, "unknown")

    unsupported: list[float] = []
    for value, _unit in _money_mentions(response or ""):
        if not _close_to_any(value, supported):
            unsupported.append(value)
    return sorted(set(unsupported))


def evaluate_grounding(
    response: str,
    evidence: Any,
    *,
    retrieval_expected: bool = True,
    tool_errored: bool = False,
) -> CriticVerdict:
    """Deterministic grounding rubric shared by online guardrails and evals.

    Only *prices* are gated, and only when retrieval was expected. When it was not
    (direct answers / chat), price gating is skipped entirely so conversational
    replies that echo the user's own numbers are never penalised.
    """
    answer = (response or "").strip()
    answered = bool(answer)
    issues: list[str] = []
    if not answered:
        issues.append("empty_answer")

    if not retrieval_expected:
        return CriticVerdict(
            grounded=True,
            answered=answered,
            retrieval_hit=True,
            issues=issues,
            needs_replan=False,
        )

    unsupported = unsupported_reply_prices(answer, evidence)
    asserts_facts = bool(_money_mentions(answer))
    # A retrieval_miss only matters when the tool actually errored *and* the reply
    # asserts specific figures. A legitimately-empty result is left alone — the
    # generator already narrates "no results" honestly.
    retrieval_miss = tool_errored and asserts_facts

    if unsupported:
        issues.append("unsupported_prices:" + ",".join(f"{v:g}" for v in unsupported))
    if retrieval_miss:
        issues.append("retrieval_miss")

    grounded = answered and not unsupported and not retrieval_miss
    return CriticVerdict(
        grounded=grounded,
        answered=answered,
        retrieval_hit=not tool_errored,
        issues=issues,
        needs_replan=not grounded,
    )


def _format_price(value: float) -> str:
    if value == int(value):
        return f"£{int(value):,}"
    return f"£{value:,.2f}"


def build_correction_instruction(unsupported: list[float]) -> str:
    """Corrective instruction appended to the generation prompt on regeneration."""
    if unsupported:
        figures = ", ".join(_format_price(v) for v in unsupported)
        cited = f"cited price figure(s) that are NOT present in the data above: {figures}."
    else:
        cited = "cited price figures that are not supported by the data above."
    return (
        "=== IMPORTANT CORRECTION ===\n"
        f"Your previous draft {cited} "
        "Rewrite the answer so that EVERY monetary figure you mention is either copied "
        "verbatim from the data above or is an explicitly-labelled calculation of those "
        "figures (a weekly-to-monthly conversion, an annual/N-month total, or a deposit "
        "of N weeks' rent). Do NOT invent, guess, round, or approximate any price; if a "
        "figure is not in the data, omit it or say it is unavailable. Keep the rest of "
        "your answer, its structure, and its language unchanged.\n"
        "Corrected response:"
    )


def append_caveat(text: str) -> str:
    """Append the double-check caveat once, without discarding the answer."""
    body = (text or "").rstrip()
    if CAVEAT in body:
        return body
    return f"{body}\n\n{CAVEAT}" if body else CAVEAT


@dataclass
class GroundingOutcome:
    """Result of the enforcement pass handed back to the critic node."""

    response: str
    verdict: CriticVerdict
    attempts: int
    regenerated: bool


async def enforce_grounding(
    response: str,
    evidence: Any,
    *,
    regenerate: Callable[[str], Awaitable[str]],
    retrieval_expected: bool = True,
    tool_errored: bool = False,
    on_verdict: Optional[Callable[..., None]] = None,
) -> GroundingOutcome:
    """Grade ``response`` and, if it fails, run one corrective regeneration pass.

    ``regenerate(correction_instruction)`` must return a fresh answer string (it
    closes over the original generation prompt in the caller). The user-facing text
    is never hard-replaced with a canned fallback: a persistently-failing answer is
    delivered with a single appended caveat instead.
    """

    def _emit(verdict: CriticVerdict, stage: str) -> None:
        if on_verdict is not None:
            on_verdict(verdict, stage=stage)

    verdict = evaluate_grounding(
        response, evidence, retrieval_expected=retrieval_expected, tool_errored=tool_errored
    )
    _emit(verdict, "initial")
    if verdict.grounded:
        return GroundingOutcome(response=response, verdict=verdict, attempts=1, regenerated=False)

    correction = build_correction_instruction(unsupported_reply_prices(response, evidence))
    try:
        new_text = await regenerate(correction)
    except Exception:  # regeneration must never crash the turn
        new_text = ""

    if not new_text or not new_text.strip():
        # No usable regeneration — keep the original answer with a caveat.
        return GroundingOutcome(
            response=append_caveat(response), verdict=verdict, attempts=2, regenerated=True
        )

    verdict2 = evaluate_grounding(
        new_text, evidence, retrieval_expected=retrieval_expected, tool_errored=tool_errored
    )
    _emit(verdict2, "regenerated")
    if verdict2.grounded:
        return GroundingOutcome(response=new_text, verdict=verdict2, attempts=2, regenerated=True)
    return GroundingOutcome(
        response=append_caveat(new_text), verdict=verdict2, attempts=2, regenerated=True
    )


def safe_fallback(verdict: CriticVerdict) -> str:
    """Deprecated. Retained for backward compatibility only.

    The enforcement pipeline (:func:`enforce_grounding`) no longer hard-replaces
    user-facing text, so this is unused by the live graph. Kept importable so any
    external caller does not break.
    """
    if "retrieval_miss" in verdict.issues:
        return LEGACY_RETRIEVAL_MISS_FALLBACK
    return LEGACY_INCONSISTENCY_FALLBACK
