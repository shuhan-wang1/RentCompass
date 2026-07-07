from __future__ import annotations

import re
from typing import Any

from uk_rent_agent.agent.contracts import DropReason, Listing, RankedResult, ScoredListing


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[0-9][0-9,]*(?:\.\d+)?", str(value or ""))
    return float(match.group().replace(",", "")) if match else None


def rank_listings(
    listings: list[Listing | dict],
    *,
    max_budget: float | None = None,
    excluded_areas: list[str] | None = None,
    limit: int = 10,
) -> RankedResult:
    excluded = [area.casefold() for area in (excluded_areas or [])]
    ranked: list[ScoredListing] = []
    dropped: list[DropReason] = []
    for raw in listings:
        listing = raw if isinstance(raw, Listing) else Listing.model_validate(raw)
        price = _number(listing.price)
        address = (listing.address or "").casefold()
        identity = listing.id or listing.url
        if max_budget is not None and price is not None and price > max_budget:
            dropped.append(DropReason(listing_id=identity, reason="over_budget"))
            continue
        if any(area in address for area in excluded):
            dropped.append(DropReason(listing_id=identity, reason="excluded_area"))
            continue
        score = 1.0
        reasons: list[str] = []
        if max_budget and price is not None:
            score += max(0.0, 1 - price / max_budget)
            reasons.append("within_budget")
        if listing.source == "local_faiss":
            score += 0.05
            reasons.append("local_source_available")
        ranked.append(ScoredListing(listing=listing, score=round(score, 6), reasons=reasons))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return RankedResult(ranked=ranked[:limit], dropped=dropped)
