"""
Zoopla source (best-effort).

Zoopla sits behind Cloudflare, so the legacy scraper drives it through a local
FlareSolverr Docker container (http://localhost:8191). If that container isn't
running the legacy function returns [] and we simply contribute nothing — the
provider falls back to Rightmove-only results.

The legacy scraper returns Price/Address/Description/URL/Available From. We
geocode the address for geo_location and infer room type / compose the enhanced
description in normalize_property(). Deeper structured fields (amenities, letting
terms) would require parsing the Zoopla detail __NEXT_DATA__ blob, which only
works when FlareSolverr is up — see _enrich_from_description() for the light
heuristic we apply in the meantime.

To enable Zoopla:
    docker run -p 8191:8191 -e LOG_LEVEL=info --rm flaresolverr/flaresolverr
"""

import re

from .config import load_legacy, DEFAULT_MIN_BEDROOMS, DEFAULT_MAX_BEDROOMS
from .normalize import normalize_property

_AMENITY_HINTS = [
    "gym", "concierge", "parking", "balcony", "garden", "lift", "furnished",
    "wifi", "dishwasher", "wheelchair", "pets", "terrace", "porter",
]


def _enrich_from_description(prop: dict) -> None:
    """Cheap amenity extraction from the listing description text (Zoopla doesn't
    expose structured key-features without a detail-page parse)."""
    text = (prop.get("Description") or "").lower()
    if not text:
        return
    found = [a.capitalize() for a in _AMENITY_HINTS if re.search(rf"\b{a}\b", text)]
    if found:
        prop["Detailed_Amenities"] = ", ".join(dict.fromkeys(found))


def find_rich_zoopla(
    location_slug: str,
    radius: float,
    min_price: int,
    max_price: int,
    limit: int | None = None,
    min_bedrooms: int = DEFAULT_MIN_BEDROOMS,
    max_bedrooms: int = DEFAULT_MAX_BEDROOMS,
) -> list[dict]:
    """Scrape Zoopla via FlareSolverr and normalise to the rich schema. Returns
    [] when FlareSolverr is unavailable (handled inside the legacy scraper)."""
    try:
        legacy = load_legacy("scrape_zoopla_listings")
    except ImportError as e:
        print(f"  [zoopla] scraper unavailable: {e}")
        return []

    base = legacy.find_properties_zoopla(
        location_slug,
        radius,
        min_price,
        max_price,
        min_bedrooms,
        max_bedrooms,
    )
    if not base:
        return []

    if limit:
        base = base[:limit]

    results = []
    for prop in base:
        prop = dict(prop)
        prop["Platform"] = "Zoopla"
        _enrich_from_description(prop)
        results.append(normalize_property(prop))

    print(f"  [zoopla] done: {len(results)} properties.")
    return results
