from __future__ import annotations

import json
import re
from typing import Any

from uk_rent_agent.agent.contracts import CriticVerdict


_PRICE = re.compile(r"(?:£|GBP\s*)\s?([0-9][0-9,]*(?:\.\d{1,2})?)", re.IGNORECASE)


def _prices(value: Any) -> set[float]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return {float(match.replace(",", "")) for match in _PRICE.findall(text)}


def evaluate_grounding(
    response: str,
    evidence: Any,
    *,
    retrieval_expected: bool = True,
) -> CriticVerdict:
    """Deterministic first-pass rubric shared by online guardrails and evals."""
    answer = (response or "").strip()
    evidence_text = json.dumps(evidence, ensure_ascii=False, default=str) if evidence else ""
    answer_prices = _prices(answer)
    evidence_prices = _prices(evidence_text)
    unsupported = sorted(answer_prices - evidence_prices)
    issues: list[str] = []
    if not answer:
        issues.append("empty_answer")
    if unsupported:
        issues.append("unsupported_prices:" + ",".join(f"{price:g}" for price in unsupported))
    retrieval_hit = bool(evidence)
    if retrieval_expected and not retrieval_hit:
        issues.append("retrieval_miss")
    grounded = not unsupported and (retrieval_hit or not retrieval_expected)
    answered = bool(answer)
    return CriticVerdict(
        grounded=grounded,
        answered=answered,
        retrieval_hit=retrieval_hit,
        issues=issues,
        needs_replan=not grounded and retrieval_expected,
    )


def safe_fallback(verdict: CriticVerdict) -> str:
    if "retrieval_miss" in verdict.issues:
        return "I couldn't verify this against current listing data. Please check a live property portal before deciding."
    return "I found a possible inconsistency in the available listing data, so I won't quote unverified details. Please check the source listing."
