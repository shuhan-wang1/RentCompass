"""
Rightmove rich source.

Discovery uses the legacy ``scrape_rightmove_api`` (the working, undocumented
/api/_search endpoint) to find listings for a location/price band. Each listing
detail page embeds a big ``window.PAGE_MODEL`` JSON blob that contains the
structured data we need for full schema alignment:

    propertyData.location.{latitude,longitude}   -> geo_location  (no geocoding!)
    propertyData.keyFeatures                     -> Detailed_Amenities
    propertyData.bedrooms / propertySubType      -> Room_Type_Category
    propertyData.text.description                -> Enhanced_Description source
    propertyData.lettings.*                      -> Available From / Payment_Rules

We parse that JSON (robust to nested braces via json.raw_decode) rather than
scraping fragile HTML.
"""

import re
import json
import time
import random
import requests

from .config import load_legacy, DEFAULT_MIN_BEDROOMS, DEFAULT_MAX_BEDROOMS
from .normalize import normalize_property

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _extract_page_model(html: str) -> dict | None:
    """Pull the ``window.PAGE_MODEL = {...}`` object out of a detail page."""
    if not html:
        return None
    marker = "window.PAGE_MODEL"
    idx = html.find(marker)
    if idx == -1:
        return None
    brace = html.find("{", idx)
    if brace == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[brace:])
        return obj
    except (json.JSONDecodeError, ValueError):
        return None


def _room_type(property_data: dict) -> str:
    beds = property_data.get("bedrooms")
    subtype = (
        property_data.get("propertySubType")
        or property_data.get("propertyType")
        or ""
    ).strip()
    if beds == 0:
        return (f"Studio {subtype}").strip()
    if isinstance(beds, int) and beds > 0:
        return (f"{beds} bed {subtype}").strip()
    return subtype


def _payment_rules(lettings: dict, property_data: dict) -> str:
    parts = []
    deposit = lettings.get("deposit")
    if deposit:
        parts.append(f"Deposit £{deposit}")
    furnish = lettings.get("furnishType")
    if furnish:
        parts.append(str(furnish))
    let_type = lettings.get("letType")
    if let_type:
        parts.append(f"Let type: {let_type}")
    term = lettings.get("minimumTermInMonths")
    if term:
        parts.append(f"Minimum term {term} months")
    band = property_data.get("councilTaxBand")
    if band:
        parts.append(f"Council tax band {band}")
    return ". ".join(parts) + ("." if parts else "")


def _rich_from_page_model(page_model: dict) -> dict:
    """Map PAGE_MODEL.propertyData onto our rich fields (only non-empty values)."""
    pdata = (page_model or {}).get("propertyData") or {}
    rich: dict = {}

    loc = pdata.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if lat is not None and lng is not None:
        rich["geo_location"] = f"{lat}, {lng}"

    key_features = pdata.get("keyFeatures") or []
    if key_features:
        rich["Detailed_Amenities"] = ", ".join(str(k) for k in key_features)

    room = _room_type(pdata)
    if room:
        rich["Room_Type_Category"] = room

    text = pdata.get("text") or {}
    desc = text.get("description") or text.get("propertyPhrase") or ""
    if desc:
        rich["_raw_description"] = desc

    lettings = pdata.get("lettings") or {}
    avail = lettings.get("letAvailableDate")
    if avail:
        rich["Available From"] = str(avail)
    pay = _payment_rules(lettings, pdata)
    if pay:
        rich["Payment_Rules"] = pay

    # Prefer the higher-quality detail-page images if the list view had none.
    imgs = pdata.get("images") or []
    urls = [i.get("url") for i in imgs if isinstance(i, dict) and i.get("url")]
    if urls:
        rich["_detail_images"] = urls

    return rich


def _enrich_detail(session: requests.Session, url: str) -> dict:
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"  [rightmove] detail {resp.status_code} for {url}")
            return {}
        pm = _extract_page_model(resp.text)
        if not pm:
            print(f"  [rightmove] PAGE_MODEL not found for {url}")
            return {}
        return _rich_from_page_model(pm)
    except requests.RequestException as e:
        print(f"  [rightmove] detail request failed for {url}: {e}")
        return {}


def find_rich_rightmove(
    location_identifier: str,
    radius: float,
    min_price: int,
    max_price: int,
    limit: int | None = None,
    min_bedrooms: int = DEFAULT_MIN_BEDROOMS,
    max_bedrooms: int = DEFAULT_MAX_BEDROOMS,
) -> list[dict]:
    """Discover listings then enrich each to the rich schema. Returns normalised
    property dicts (exactly RICH_COLUMNS)."""
    legacy = load_legacy("rightmove_scraper")
    session = _new_session()

    base = legacy.scrape_rightmove_api(
        session,
        location_identifier,
        radius,
        min_price,
        max_price,
        min_bedrooms,
        max_bedrooms,
        limit=limit,
    )
    if not base:
        return []

    total = len(base)
    print(f"  [rightmove] enriching {total} listings via detail pages...")
    results = []
    for i, prop in enumerate(base):
        url = prop.get("URL", "")
        rich = _enrich_detail(session, url) if url else {}

        # detail images win only when the list view had none
        detail_images = rich.pop("_detail_images", None)
        if detail_images and not prop.get("Images"):
            prop["Images"] = detail_images

        merged = dict(prop)
        merged.update({k: v for k, v in rich.items() if v})
        merged["Platform"] = "Rightmove"
        results.append(normalize_property(merged))

        if i < total - 1:
            time.sleep(random.uniform(1.2, 2.5))  # be polite to Rightmove

    print(f"  [rightmove] done: {len(results)} properties.")
    return results
