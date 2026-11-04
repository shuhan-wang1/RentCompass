"""rent_price_summary — what rent is typical for a room type in one area, from our own listings.

"Could you first help me establish a typical range for an en-suite and a studio near UCL?"
was answered from UCL hall prices found by web search, and the reply said it could not give a
studio range, although the listing data the search uses held Bloomsbury studios from £1,127
(2026-11-03 replay). A full listing search was the wrong tool for that question: the user had
asked for a range BEFORE seeing listings. This tool gives the range without listings.

It reads the SAME listings the search reads (``on_demand.get_listings``: the listing cache
first, a bounded scrape only when the cache holds nothing and the time allows), classifies
each row with the search's own room-type rules, normalises every price to a month, and
returns per room type: how many listings, and the min / quartiles / median / max monthly
asking rent. Nothing is invented: a room type with no listing comes back with a count of 0
and no figures, and a thin sample is marked as such.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from typing import Optional

from core.tool_system import Tool

# The time a summary may spend: reading the cache is instant; a scrape is only attempted
# when the cache holds nothing for the area and at least this much time remains.
RENT_SUMMARY_BUDGET_S = float(os.getenv("RENT_SUMMARY_BUDGET_S", "14"))
_MIN_SCRAPE_S = 4.0
_LOW_SAMPLE = 4
_ALL_TYPES = ("studio", "ensuite", "shared", "whole")

_LABELS = {
    "studio": ("Studios", "Studio"),
    "ensuite": ("En-suite rooms", "独立卫浴房间"),
    "shared": ("Rooms in a shared flat or house", "合租房间"),
    "whole": ("Whole flats and houses", "整套房屋"),
}


def _L(zh: bool, en: str, chinese: str) -> str:
    return chinese if zh else en


def _area_label(slug: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in str(slug or "").replace("-", " ").split())


def _resolve_location(area: Optional[str], destination: Optional[str]) -> Optional[str]:
    """The area whose listings to read. A named neighbourhood wins; a whole city with a
    destination falls to the destination's own area (UCL -> bloomsbury), like the search."""
    from core.scraping.on_demand import (classify_place, curated_destination_area,
                                         is_unsearchable_city_area)
    area = str(area or "").strip() or None
    destination = str(destination or "").strip() or None
    if area and not (destination and is_unsearchable_city_area(area)):
        return area
    if destination:
        curated = curated_destination_area(destination)
        if curated:
            return curated
        try:
            place = classify_place(destination) or {}
        except Exception:
            place = {}
        slug = str(place.get("slug") or "").strip()
        if slug and not is_unsearchable_city_area(slug):
            return slug
    return area


def _monthly(row: dict) -> Optional[int]:
    from core.area_stats import monthly_price
    for key in ("Price", "price"):
        value = row.get(key)
        if value in (None, ""):
            continue
        amount = monthly_price(value)
        if amount and amount > 0:
            return int(round(amount))
    return None


def _stats(prices: list) -> dict:
    prices = sorted(prices)
    out = {"min": prices[0], "median": int(round(statistics.median(prices))), "max": prices[-1]}
    if len(prices) >= _LOW_SAMPLE:
        q1, _q2, q3 = statistics.quantiles(prices, n=4, method="inclusive")
        out["p25"], out["p75"] = int(round(q1)), int(round(q3))
    return out


def summarise_rows(rows: list, room_types) -> list:
    """Per room type: {room_type, listing_count, monthly_rent{min,p25,median,p75,max}}.
    Pure, so it is tested without the network. Rows are counted once (by URL) and a row
    whose price cannot be read is left out of the figures."""
    from core.tools.search_properties import _matches_one_room_type, room_type_parts
    parts = room_type_parts(room_types) or _ALL_TYPES
    seen: set = set()
    unique = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("URL") or row.get("url") or "").split("?", 1)[0].rstrip("/").lower()
        key = key or f"{row.get('Address')}|{row.get('Price')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    results = []
    for part in parts:
        prices = [price for price in (_monthly(row) for row in unique
                                      if _matches_one_room_type(row, part)) if price]
        entry = {"room_type": part, "listing_count": len(prices),
                 "monthly_rent": _stats(prices) if prices else None,
                 "low_sample": 0 < len(prices) < _LOW_SAMPLE}
        if part == "ensuite":
            entry["matched_on"] = "listing text (title, features, description)"
        results.append(entry)
    return results


def _summary_text(results: list, area: str, zh: bool) -> str:
    lines = []
    for entry in results:
        en_label, zh_label = _LABELS.get(entry["room_type"], (entry["room_type"], entry["room_type"]))
        count, rent = entry["listing_count"], entry["monthly_rent"]
        if not rent:
            read = entry.get("listings_read")
            among = f" among the {read} listings read" if read else ""
            among_zh = f"在读到的 {read} 套房源里" if read else ""
            how = (" (en-suite is taken from the listing text, and only a few room listings are read "
                   "in full, so some may be missed)" if entry["room_type"] == "ensuite" else "")
            how_zh = "（独立卫浴是根据房源文字判断的，只完整读了少数几套合租房源，可能有遗漏）" \
                if entry["room_type"] == "ensuite" else ""
            lines.append(_L(zh, f"{en_label} in {area}: none{among}{how}.",
                            f"{area} 的{zh_label}：{among_zh}没有找到{how_zh}。"))
            continue
        if "p25" in rent:
            lines.append(_L(
                zh,
                f"{en_label} in {area}: {count} listings advertised now; the middle half ask "
                f"£{rent['p25']:,}–£{rent['p75']:,} a month (median £{rent['median']:,}), "
                f"from £{rent['min']:,} to £{rent['max']:,}.",
                f"{area} 的{zh_label}：目前有 {count} 套在租，中间一半的月租在 £{rent['p25']:,}–£{rent['p75']:,}"
                f"（中位数 £{rent['median']:,}），最低 £{rent['min']:,}，最高 £{rent['max']:,}。"))
        else:
            lines.append(_L(
                zh,
                f"{en_label} in {area}: only {count} listing{'s' if count != 1 else ''} advertised "
                f"now, from £{rent['min']:,} to £{rent['max']:,} a month; too few for a typical range.",
                f"{area} 的{zh_label}：目前只有 {count} 套在租，月租 £{rent['min']:,} 到 £{rent['max']:,}，"
                f"样本太少，算不出典型区间。"))
    return "\n".join(lines)


async def rent_price_summary_impl(area: Optional[str] = None, destination: Optional[str] = None,
                                  room_types: Optional[str] = None,
                                  reply_language: Optional[str] = None,
                                  _deadline_monotonic: Optional[float] = None, **_ignored) -> dict:
    zh = str(reply_language or "").lower().startswith("zh")
    location = await asyncio.to_thread(_resolve_location, area, destination)
    if not location:
        return {"status": "need_area", "results": [],
                "message": _L(zh, "Which area, or which university or workplace, should the rent "
                                  "range be for?", "请告诉我要查哪个区域的租金，或者您要通勤去的学校或公司。")}
    from core.scraping import on_demand
    from core.tools.search_properties import join_room_types, room_type_parts
    joined = join_room_types(room_type_parts(room_types) or _ALL_TYPES)
    started = time.monotonic()
    deadline = started + RENT_SUMMARY_BUDGET_S
    if isinstance(_deadline_monotonic, (int, float)) and not isinstance(_deadline_monotonic, bool):
        deadline = min(deadline, float(_deadline_monotonic))

    def _read(**kw):
        return on_demand.get_listings(
            location, on_demand.CANONICAL_MIN_BEDS, on_demand.CANONICAL_MAX_BEDS,
            on_demand.CANONICAL_MIN_PRICE, on_demand.CANONICAL_MAX_PRICE,
            limit=on_demand.CANONICAL_SCRAPE_LIMIT, room_type=joined, **kw)

    freshness = "cached"
    try:
        result = await asyncio.to_thread(_read, cache_only=True)
    except Exception:
        result = {}
    left = deadline - time.monotonic()
    if not (result or {}).get("rows") and left >= _MIN_SCRAPE_S:
        freshness = "live"
        try:
            # Deeper pages only when there is room for them after the first page of each channel.
            result = await asyncio.wait_for(
                asyncio.to_thread(_read, budget_s=max(1.0, left - 2.0),
                                  deep_budget_s=max(0.0, min(6.0, left - 8.0))),
                timeout=max(0.5, left - 0.5))
        except Exception:
            result = {}
    rows = [dict(row) for row in (result or {}).get("rows") or [] if isinstance(row, dict)]
    if rows and "ensuite" in room_type_parts(joined):
        # As the search does: a room listing says "en-suite" on its details page more often
        # than in its title, so a few room listings are read in full, time-boxed.
        from core.tools.search_properties import _read_room_descriptions
        read_s = min(float(os.getenv("SEARCH_ROOM_READ_BUDGET_S", "5")),
                     deadline - time.monotonic() - 0.5)
        if read_s > 1.0:
            try:
                await _read_room_descriptions(rows, read_s)
            except Exception:
                pass
    label = _area_label(location)
    if not rows:
        return {"status": "no_data", "area": label, "location_searched": location, "results": [],
                "message": _L(zh, f"No listing data for {label} could be read in time, so no "
                                  "range can be given from listings.",
                              f"没能及时读到 {label} 的房源数据，暂时给不出基于房源的租金区间。")}
    results = await asyncio.to_thread(summarise_rows, rows, joined)
    for entry in results:
        entry["listings_read"] = len(rows)
    return {
        "status": "ok",
        "area": label,
        "location_searched": location,
        "destination": str(destination or "").strip() or None,
        "source": "OnTheMarket listings advertised now (asking rents, not agreed rents)",
        "data_freshness": freshness,
        "results": results,
        "summary": _summary_text(results, label, zh),
        "note": _L(zh, "Asking rents of the listings currently advertised in this area, converted "
                       "to a month; a shared-house or student price may be per person.",
                   "以上是该区域目前在租房源的挂牌租金，已统一换算成月租；合租或学生房的价格可能是每人价格。"),
    }


rent_price_summary_tool = Tool(
    name="rent_price_summary",
    description=(
        "Typical monthly RENT RANGE per room type (studio / en-suite / shared room / whole "
        "flat) in one area or near a destination, from the listings currently advertised: "
        "count, median and middle-half range. Use it when the user asks what rent is typical "
        "or wants a price range BEFORE choosing a budget; it shows no listings. Not for "
        "finding properties — that is search_properties. 查询某区域或某学校/公司附近各房型的"
        "典型月租区间（基于当前在租房源，不列出房源）；用户先问价格区间时使用。"
    ),
    func=rent_price_summary_impl,
    parameters={
        "type": "object",
        "properties": {
            "area": {
                "type": "string",
                "description": "A neighbourhood or town (e.g. Bloomsbury, Camden). Give this or "
                               "destination.",
            },
            "destination": {
                "type": "string",
                "description": "A university or workplace the user will commute to (e.g. UCL); "
                               "its nearby area is used when no neighbourhood is named.",
            },
            "room_types": {
                "type": "string",
                "description": "Room types to summarise, joined by '+': studio, ensuite, shared, "
                               "whole (e.g. 'studio+ensuite'). Omit for all four.",
            },
            "reply_language": {
                "type": "string",
                "enum": ["zh", "en"],
                "description": "Language for user-facing strings ('zh' or 'en').",
            },
        },
        "required": [],
    },
    side_effect="none",
    max_retries=1,
)
