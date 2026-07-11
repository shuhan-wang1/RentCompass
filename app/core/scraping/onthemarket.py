"""
OnTheMarket rich source.

Added 2026-07 after OpenRent put its listing pages behind an AWS WAF
"Human Verification" challenge (GET /properties-to-rent/* now returns HTTP 405
with a bot-challenge interstitial), which killed the plain-requests OpenRent
path. OnTheMarket is the replacement primary source:

  * robots.txt permits the /to-rent/property/<area>/ listing pages (it even
    names ClaudeBot explicitly with Crawl-delay: 1). Only property-type facets
    (*/flat/, */apartment/, *-bed-*) and agent/image paths are disallowed — we
    deliberately use only the general "property" area path.
  * The search page is a Next.js app whose server-rendered <script
    id="__NEXT_DATA__"> JSON already carries a fully structured list of ~30
    listings per page: price, address, title, property type, beds/baths, exact
    lat/lon, key features and image URLs. No per-detail request or geocoding is
    needed, so one polite GET per area yields a complete batch.

Everything is projected onto the canonical 14-field schema via normalize_property.
We honour the robots Crawl-delay (>=1s) between page requests.
"""

import re
import json
import time
import random
import requests

from .normalize import normalize_property

BASE = "https://www.onthemarket.com"
SEARCH_URL = BASE + "/to-rent/property/{slug}/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# robots.txt asks for Crawl-delay: 1 — stay above that.
_CRAWL_DELAY = (1.2, 1.8)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

_AMENITY_HINTS = [
    "gym", "concierge", "porter", "lift", "parking", "balcony", "garden",
    "terrace", "dishwasher", "washing machine", "bills included", "furnished",
    "unfurnished", "underfloor heating", "wifi", "broadband", "roof terrace",
    "swimming pool", "en-suite", "ensuite",
]


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _extract_listings(html: str) -> list[dict]:
    """Pull the server-rendered listing list out of the __NEXT_DATA__ blob."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        return data["props"]["initialReduxState"]["results"]["list"] or []
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def _pcm_price(raw) -> str:
    """'£2,650 pcm (£612 pw)' -> '£2,650 pcm'."""
    if not raw:
        return ""
    m = re.search(r"£[\d,]+\s*pcm", str(raw), re.I)
    return m.group(0) if m else str(raw).strip()


def _room_type(beds, prop_type: str, title: str) -> str:
    blob = f"{title} {prop_type}".lower()
    if "studio" in blob:
        return "Studio"
    ptype = (prop_type or "").strip()
    if beds:
        return f"{beds} bed {ptype}".strip()
    m = re.search(r"(\d+)\s*bed", title or "", re.I)
    if m:
        return f"{m.group(1)} bed {ptype}".strip()
    return ptype


def _images(listing: dict) -> list[str]:
    out = []
    for img in listing.get("images") or []:
        if isinstance(img, dict) and img.get("default"):
            out.append(img["default"])
    if not out:
        cov = listing.get("cover-image")
        if isinstance(cov, dict) and cov.get("default"):
            out.append(cov["default"])
    return out[:6]


def _map_listing(listing: dict) -> dict:
    url_path = listing.get("details-url") or ""
    url = BASE + url_path if url_path.startswith("/") else url_path

    address = (listing.get("address") or "").strip()
    title = (listing.get("property-title") or "").strip()
    prop_type = (listing.get("humanised-property-type") or "").strip()
    beds = listing.get("bedrooms")

    features = [f for f in (listing.get("features") or []) if isinstance(f, str)]
    # Build a human description from the title + address + key features.
    desc_bits = []
    if title:
        desc_bits.append(title.capitalize())
    if address:
        desc_bits.append(f"in {address}")
    description = " ".join(desc_bits).strip()
    features_text = ". ".join(features)

    # Amenities: prefer recognised amenity keywords from the feature bullets,
    # else fall back to the raw feature list.
    low = features_text.lower()
    hits = [h.title() for h in _AMENITY_HINTS if h in low]
    amenities = ", ".join(dict.fromkeys(hits)) or ", ".join(features[:8])

    loc = listing.get("location") or {}
    geo = ""
    if isinstance(loc, dict) and loc.get("lat") is not None and loc.get("lon") is not None:
        geo = f"{loc['lat']}, {loc['lon']}"

    return {
        "Price": _pcm_price(listing.get("price")),
        "Address": address or title,
        "Description": description or title,
        "URL": url,
        "Available From": "",  # not in search JSON -> normalize sets 'Contact agent'
        "Platform": "OnTheMarket",
        "Images": _images(listing),
        "geo_location": geo,
        "Room_Type_Category": _room_type(beds, prop_type, title),
        "Detailed_Amenities": amenities,
        # richer embedding text: the actual key-feature bullets
        "_raw_description": features_text,
    }


def find_rich_onthemarket(
    slug: str,
    radius: float,
    min_price: int,
    max_price: int,
    limit: int | None = None,
    min_bedrooms: int = 0,
    max_bedrooms: int = 2,
) -> list[dict]:
    """Search OnTheMarket for an area `slug`, paging as needed, and return
    normalised rich-schema dicts. `radius` is accepted for signature parity with
    the other sources but OnTheMarket area pages already cover a local radius."""
    session = _new_session()
    params = {
        "min-price": min_price,
        "max-price": max_price,
        "min-bedrooms": min_bedrooms,
        "max-bedrooms": max_bedrooms,
        "view": "grid",
    }

    results: list[dict] = []
    seen_urls: set[str] = set()
    want = limit if limit else 30
    page = 1
    max_pages = 4  # politeness cap
    while len(results) < want and page <= max_pages:
        page_params = dict(params)
        if page > 1:
            page_params["page-number"] = page
        try:
            resp = session.get(SEARCH_URL.format(slug=slug), params=page_params,
                               timeout=25)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [onthemarket] search failed for '{slug}' p{page}: {e}")
            break

        listings = _extract_listings(resp.text)
        if not listings:
            if page == 1:
                print(f"  [onthemarket] no listings for '{slug}'")
            break

        new_this_page = 0
        for listing in listings:
            mapped = _map_listing(listing)
            url = mapped.get("URL", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            results.append(normalize_property(mapped))
            new_this_page += 1
            if len(results) >= want:
                break

        if new_this_page == 0:
            break
        page += 1
        if len(results) < want and page <= max_pages:
            time.sleep(random.uniform(*_CRAWL_DELAY))

    print(f"  [onthemarket] '{slug}': done, {len(results)} properties.")
    return results
