"""
OpenRent rich source.

OpenRent is markedly more scraping-tolerant than Rightmove: its robots.txt
permits the /properties-to-rent and individual listing paths (only account /
affiliate / admin paths are disallowed) and it publishes a public sitemap.

Pipeline:
  1. Search  GET /properties-to-rent/london?term=<area>&within=<mi>&prices_*&bedrooms_*
     The server renders ~20 property cards + parallel JS arrays
     (PROPERTYIDS / PROPERTYLISTLATITUDES / PROPERTYLISTLONGITUDES) giving exact
     coordinates per listing. The cards already carry price, title (area +
     outcode), beds/baths, furnish state, availability and the main image.
  2. Detail GET <card href> for the full description, key features, deposit & bills
     (capped to `limit` listings).

Everything is projected onto the canonical 14-field schema via normalize_property.
"""

import re
import time
import random
import requests
from bs4 import BeautifulSoup

from .normalize import normalize_property

BASE = "https://www.openrent.co.uk"
SEARCH_URL = BASE + "/properties-to-rent/london"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

_TYPE_RE = re.compile(
    r"\b(Studio|Flat|Apartment|House|Maisonette|Bungalow|Room|Penthouse)\b", re.I
)

# Common amenity keywords to recover from free-text descriptions when there's no
# structured "Key Features" list.
_AMENITY_HINTS = [
    "gym", "concierge", "porter", "lift", "parking", "balcony", "garden",
    "terrace", "dishwasher", "washing machine", "bills included", "furnished",
    "underfloor heating", "wifi", "broadband", "roof terrace", "swimming pool",
]


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _js_array(html: str, name: str) -> list[str]:
    """Extract a `var NAME = [ ... ];` numeric/string array from the page."""
    m = re.search(rf"var\s+{name}\s*=\s*\[([^\]]*)\]", html)
    if not m:
        return []
    return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]


def _coord_map(html: str) -> dict[str, tuple[str, str]]:
    ids = _js_array(html, "PROPERTYIDS")
    lats = _js_array(html, "PROPERTYLISTLATITUDES")
    lngs = _js_array(html, "PROPERTYLISTLONGITUDES")
    out: dict[str, tuple[str, str]] = {}
    for i, pid in enumerate(ids):
        if i < len(lats) and i < len(lngs):
            out[pid] = (lats[i], lngs[i])
    return out


def _room_type(title: str) -> str:
    t = title or ""
    if re.search(r"\bstudio\b", t, re.I):
        return "Studio"
    beds = re.search(r"(\d+)\s*bed", t, re.I)
    typ = _TYPE_RE.search(t)
    typ_word = typ.group(1).capitalize() if typ else ""
    if beds:
        return f"{beds.group(1)} bed {typ_word}".strip()
    return typ_word


def _parse_card(card, coords: dict[str, tuple[str, str]]) -> dict:
    href = card.get("href", "")
    url = BASE + href if href.startswith("/") else href
    pid = href.rstrip("/").split("/")[-1]

    img = card.select_one("img.propertyPic")
    title = (img.get("alt") if img else "") or ""
    image = ""
    if img and img.get("src"):
        src = img["src"]
        image = ("https:" + src) if src.startswith("//") else src

    text = card.get_text(" ", strip=True)

    price = ""
    m = re.search(r"£([\d,]+)\s*per month", text)
    if m:
        price = f"£{m.group(1)} pcm"

    avail = ""
    m = re.search(r"Available\s+([^|]+?)(?:\s*\||\s*EPC|$)", text)
    if m:
        avail = m.group(1).strip()

    furnish = ""
    m = re.search(r"\b(Furnished|Unfurnished|Part-furnished)\b", text)
    if m:
        furnish = m.group(1)

    geo = ""
    if pid in coords:
        geo = f"{coords[pid][0]}, {coords[pid][1]}"

    return {
        "Price": price,
        "Address": title,
        "Description": title,
        "URL": url,
        "Available From": avail,
        "Platform": "OpenRent",
        "Images": [image] if image else [],
        "geo_location": geo,
        "Room_Type_Category": _room_type(title),
        "Detailed_Amenities": furnish,
        "_pid": pid,
    }


def _extract_features(description: str) -> str:
    """Pull a 'Key Features' bullet list out of the detail description, if any."""
    m = re.search(
        r"Key Features\s*[:\-]?\s*(.+?)(?:Compliance|Location|Price\s*&|EPC Rating|$)",
        description,
        re.I | re.S,
    )
    if not m:
        return ""
    chunk = m.group(1)
    bullets = [b.strip(" -•\t") for b in re.split(r"\s*-\s+|•", chunk) if b.strip(" -•\t")]
    return ", ".join(bullets[:12])


def _enrich_detail(session: requests.Session, url: str) -> dict:
    rich: dict = {}
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            print(f"  [openrent] detail {resp.status_code} for {url}")
            return rich
        soup = BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        print(f"  [openrent] detail request failed for {url}: {e}")
        return rich

    desc = ""
    desc_div = soup.select_one("#descriptionText")
    if desc_div:
        desc = re.sub(r"\s+", " ", desc_div.get_text(" ", strip=True))
        desc = re.sub(r"\s*Read more\s*$", "", desc).strip()
        if desc:
            rich["_raw_description"] = desc
            # Amenities: prefer a structured "Key Features" list, else keyword scan.
            feats = _extract_features(desc)
            if not feats:
                low = desc.lower()
                hits = [h.title() for h in _AMENITY_HINTS if h in low]
                feats = ", ".join(dict.fromkeys(hits))
            if feats:
                rich["Detailed_Amenities"] = feats
            # Tenancy restrictions -> Excluded_Features
            restr = re.findall(
                r"((?:strictly\s+)?no\s+(?:subletting|corporate lets?|pets|smokers|sharers|dss|students)[^.]*)",
                desc, re.I,
            )
            if restr:
                rich["Excluded_Features"] = ". ".join(s.strip().capitalize() for s in restr[:4])

    full = soup.get_text(" ", strip=True)

    # Price: the card sometimes omits the "per month" text; the detail page's
    # "Rent PCM" (or the meta description "£X p/m") is authoritative.
    m = re.search(r"Rent PCM\s*£([\d,]+)", full)
    if not m:
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            m = re.search(r"£([\d,]+)(?:\.\d+)?\s*p/m", md["content"])
    if m:
        rich["Price"] = f"£{m.group(1)} pcm"

    # Availability: card often omits it; the detail page is reliable.
    m = re.search(r"Date Available[:\s]*([0-9][^<\n)]{2,30})", full, re.I)
    if not m:
        m = re.search(r"Available\s+from\s+([0-9][^<\n|)]{2,30})", desc or full, re.I)
    if m:
        rich["Available From"] = m.group(1).strip().rstrip(".")

    pay = []
    m = re.search(r"Deposit\s*£([\d,\.]+)", full)
    if m:
        pay.append(f"Deposit £{m.group(1)}")
    m = re.search(r"Minimum Tenancy[^\d]*(\d+)\s*Month", full, re.I)
    if m:
        pay.append(f"Minimum tenancy {m.group(1)} months")
    m = re.search(r"Council Tax Band[:\s]*([A-H])", full, re.I)
    if m:
        pay.append(f"Council tax band {m.group(1)}")
    if pay:
        rich["Payment_Rules"] = ". ".join(pay) + "."

    return rich


def find_rich_openrent(
    term: str,
    radius: float,
    min_price: int,
    max_price: int,
    limit: int | None = None,
    min_bedrooms: int = 0,
    max_bedrooms: int = 2,
) -> list[dict]:
    """Search OpenRent for `term`, enrich each card via its detail page, and
    return normalised rich-schema dicts."""
    session = _new_session()
    params = {
        "term": term,
        "within": radius,
        "prices_min": min_price,
        "prices_max": max_price,
        "bedrooms_min": min_bedrooms,
        "bedrooms_max": max_bedrooms,
    }
    try:
        resp = session.get(SEARCH_URL, params=params, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [openrent] search failed for '{term}': {e}")
        return []

    html = resp.text
    coords = _coord_map(html)
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("a.search-property-card")
    if not cards:
        print(f"  [openrent] no cards for '{term}'")
        return []

    if limit:
        cards = cards[:limit]
    print(f"  [openrent] '{term}': enriching {len(cards)} listings...")

    results = []
    for i, card in enumerate(cards):
        base = _parse_card(card, coords)
        base.pop("_pid", None)
        if base.get("URL"):
            rich = _enrich_detail(session, base["URL"])
            # merge non-empty rich fields (detail amenities override the bare furnish tag)
            for k, v in rich.items():
                if v:
                    base[k] = v
        results.append(normalize_property(base))
        if i < len(cards) - 1:
            time.sleep(random.uniform(0.5, 1.2))

    print(f"  [openrent] '{term}': done, {len(results)} properties.")
    return results
