"""Deterministic multi-objective ranking for live rental listings.

This module deliberately sits after retrieval and hard-constraint filtering.  It
does not decide whether a listing is eligible; it makes the trade-offs among
eligible listings explicit, handles unavailable evidence by re-normalising the
available objectives, and diversifies the result page.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


# Weights are intentionally small in number and documented.  Missing components
# are removed and the remaining weights are normalised, rather than treating
# unknown information as a perfect match.
DEFAULT_WEIGHTS = {
    "price": 0.30,
    "commute": 0.28,
    "semantic": 0.15,
    "features": 0.10,
    "availability": 0.10,
    "freshness": 0.07,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_utility(price: Any, budget: float | None) -> float | None:
    """A continuous score for affordability, including the permitted soft band.

    Reaching the budget cap is still useful (0.55), but cheaper listings gain a
    modest advantage.  The score decays to zero at 15% over budget, matching the
    search tool's soft-budget policy.
    """
    p = _as_float(price)
    if p is None or p < 0 or budget is None or budget <= 0:
        return None
    ratio = p / budget
    if ratio <= 1:
        return 0.55 + 0.45 * (1 - ratio)
    return _clamp(0.55 * (1 - (ratio - 1) / 0.15))


def _commute_utility(minutes: Any, max_commute: float | None) -> float | None:
    """Reward shorter commutes while retaining a small value at the cap."""
    t = _as_float(minutes)
    if t is None or t < 0 or max_commute is None or max_commute <= 0:
        return None
    return _clamp(0.15 + 0.85 * (1 - t / max_commute))


def _semantic_utility(value: Any) -> float | None:
    score = _as_float(value)
    if score is None:
        return None
    # FAISS cosine scores are normally [-1, 1].  Some injected/test stores use
    # [0, 1], so preserve that common representation rather than remapping it.
    if score < 0:
        score = (score + 1) / 2
    return _clamp(score)


def _feature_utility(property_: dict, requested_features: Iterable[str] | None) -> float | None:
    wanted = [str(x).strip().lower() for x in (requested_features or []) if str(x).strip()]
    if not wanted:
        return None
    haystack = " ".join(
        str(property_.get(key, ""))
        for key in ("Room_Type_Category", "Description", "Detailed_Amenities", "Type")
    ).lower()
    return sum(feature in haystack for feature in wanted) / len(wanted)


def _availability_utility(property_: dict, move_in_date: str | None) -> float | None:
    if not move_in_date:
        return None
    available = str(
        property_.get("_resolved_available_from")
        or property_.get("_available_from")
        or property_.get("Available From")
        or ""
    ).strip()
    if not available:
        return 0.50  # explicitly uncertain, never a perfect match
    if available.lower() == "available now" or available <= move_in_date:
        return 1.0
    return 0.20


def _freshness_utility(property_: dict) -> float:
    return 0.45 if property_.get("possibly_outdated") else 1.0


def _normalised_score(components: dict[str, float | None]) -> tuple[float, dict[str, float]]:
    usable = {name: value for name, value in components.items() if value is not None}
    total_weight = sum(DEFAULT_WEIGHTS[name] for name in usable)
    if not usable or total_weight <= 0:
        return 0.0, {}
    contributions = {
        name: round(100 * DEFAULT_WEIGHTS[name] / total_weight * value, 2)
        for name, value in usable.items()
    }
    return round(sum(contributions.values()), 2), contributions


def _token_set(property_: dict) -> set[str]:
    """Tokens used only for page-level diversity, never as an eligibility rule."""
    text = " ".join(
        str(property_.get(key, ""))
        for key in ("Address", "address", "_search_area", "Room_Type_Category", "Type", "Description")
    ).lower()
    return {token for token in re.findall(r"[a-z0-9]{3,}", text) if token not in {"flat", "room", "rent"}}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def rank_and_diversify(
    properties: Iterable[dict],
    *,
    max_budget: float | None = None,
    max_commute: float | None = None,
    requested_features: Iterable[str] | None = None,
    move_in_date: str | None = None,
    diversity_lambda: float = 0.88,
) -> list[dict]:
    """Return copied listings with a score breakdown and MMR-style page rerank.

    ``diversity_lambda`` near 1 favours individual utility; the default applies a
    light novelty penalty so a useful, distinct alternative can appear in the
    first page without displacing the best listing.
    """
    prepared: list[dict] = []
    for raw in properties:
        prop = dict(raw)
        components = {
            "price": _price_utility(prop.get("price"), max_budget),
            "commute": _commute_utility(prop.get("travel_time"), max_commute),
            "semantic": _semantic_utility(prop.get("similarity_score")),
            "features": _feature_utility(prop, requested_features),
            "availability": _availability_utility(prop, move_in_date),
            "freshness": _freshness_utility(prop),
        }
        score, contributions = _normalised_score(components)
        prop["recommendation_score"] = score
        prop["score_breakdown"] = contributions
        prop["_diversity_tokens"] = _token_set(prop)
        prepared.append(prop)

    chosen: list[dict] = []
    remaining = prepared
    lam = _clamp(diversity_lambda)
    while remaining:
        def mmr(candidate: dict) -> tuple[float, float, str]:
            similarity = max(
                (_jaccard(candidate["_diversity_tokens"], picked["_diversity_tokens"]) for picked in chosen),
                default=0.0,
            )
            value = lam * candidate["recommendation_score"] - (1 - lam) * 100 * similarity
            # Deterministic tie-breakers make responses and tests reproducible.
            return value, candidate["recommendation_score"], str(candidate.get("Address", candidate.get("address", "")))

        best = max(remaining, key=mmr)
        best["diversified_score"] = round(mmr(best)[0], 2)
        chosen.append(best)
        remaining.remove(best)
    for listing in chosen:
        listing.pop('_diversity_tokens', None)
    return chosen
