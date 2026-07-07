from __future__ import annotations

import re


def parse_price(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or "poa" in value.lower():
        return None
    match = re.search(r"[\d,]+(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def extract_postcode(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", value, re.I)
    if not match:
        return None
    compact = re.sub(r"\s+", "", match.group(1).upper())
    return f"{compact[:-3]} {compact[-3:]}"


def filter_by_budget(properties: list[dict], max_price: float) -> list[dict]:
    return [
        item for item in properties
        if item.get("parsed_price") is not None and item["parsed_price"] <= max_price
    ]
