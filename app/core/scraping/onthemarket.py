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
import os
import html
import json
import time
import random
import sqlite3
import threading
import requests
from pathlib import Path
from datetime import date

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


# ==========================================================================
# Availability-date parsing (stdlib only — no dateutil).
# --------------------------------------------------------------------------
# Empirically (verified live 2026-07) OTM carries the tenancy start date on the
# detail page under initialReduxState.property.lettingDetails.items as an
# "Availability date: 1 Aug 2026" bullet; the summary prose is a secondary source
# ("Available from August 2026"). These parsers normalise whatever text we find to
# a canonical value the search layer can compare and rank against:
#   ""             -> unknown (shown honestly as "Contact agent" downstream)
#   "Available now"-> immediate let
#   "YYYY-MM-DD"   -> concrete start date
# UK dates are day-first; a month with no year resolves to that month's NEXT
# occurrence relative to `now` (today), so a stale "August" never resolves to a
# date already in the past.
# ==========================================================================

# Month name / abbreviation -> month number. Longest-first regex alternation is
# built from the keys so "september" wins over "sep".
_MONTH_NUM = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_RE = "|".join(sorted(_MONTH_NUM, key=len, reverse=True))
_AVAIL_NOW_RE = re.compile(r"\b(now|immediately|asap|today|straight\s*away)\b", re.I)


def _safe_iso(year: int, month: int, day: int) -> str | None:
    """date -> 'YYYY-MM-DD', or None if the (y, m, d) triple is not a real date."""
    try:
        return date(year, month, day).isoformat()
    except (ValueError, TypeError):
        return None


def _resolve_ymd(year, month: int, day: int, today: date) -> str | None:
    """Resolve a (maybe-yearless) date. With a year, that exact date; without one,
    the given month/day's NEXT occurrence relative to `today` (this year if it is
    still upcoming, else next year)."""
    if year:
        return _safe_iso(int(year), month, day)
    cand = _safe_iso(today.year, month, day)
    if cand is None:
        return None
    if cand < today.isoformat():
        return _safe_iso(today.year + 1, month, day)
    return cand


def parse_availability_date(text, *, now: date | None = None) -> str:
    """Fuzzy availability text -> 'YYYY-MM-DD' | 'Available now' | '' (unknown).

    Handles "1 Aug 2026", "15th August", "August 2026", "Aug 2026", "01/09/2026"
    (UK day-first), an explicit ISO date, and immediate-let phrasings ("now",
    "immediately", "asap"). Never raises; anything unrecognised -> ''."""
    if not text:
        return ""
    t = str(text).strip()
    if not t:
        return ""
    today = now or date.today()
    low = t.lower()

    # 1) explicit ISO 2026-09-01
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        iso = _safe_iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            return iso
    # 2) numeric dd/mm/yyyy — UK day-first
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", t)
    if m:
        iso = _safe_iso(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if iso:
            return iso
    # 3) day month [year]  e.g. "1 Aug 2026", "15th August"
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\b\.?\s*(\d{{4}})?", low)
    if m:
        iso = _resolve_ymd(m.group(3), _MONTH_NUM[m.group(2)], int(m.group(1)), today)
        if iso:
            return iso
    # 4) month day [year]  e.g. "August 15th", "Aug 1 2026" (1-2 digit day only)
    m = re.search(rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s*,?\s*(\d{{4}}))?", low)
    if m:
        iso = _resolve_ymd(m.group(3), _MONTH_NUM[m.group(1)], int(m.group(2)), today)
        if iso:
            return iso
    # 5) month year  e.g. "August 2026"
    m = re.search(rf"\b({_MONTH_RE})\s+(\d{{4}})\b", low)
    if m:
        iso = _resolve_ymd(int(m.group(2)), _MONTH_NUM[m.group(1)], 1, today)
        if iso:
            return iso
    # 6) bare month, ONLY with an availability/from context word (avoid firing on a
    #    stray month mention) -> first of that month's next occurrence.
    if re.search(r"availab|\bavail\b|\bfrom\b", low):
        m = re.search(rf"\b({_MONTH_RE})\b", low)
        if m:
            iso = _resolve_ymd(None, _MONTH_NUM[m.group(1)], 1, today)
            if iso:
                return iso
    # 7) immediate let
    if _AVAIL_NOW_RE.search(low):
        return "Available now"
    return ""


def _extract_available_from(data: dict, description_text: str = "", *,
                            now: date | None = None) -> str:
    """Availability of a listing from its detail-page __NEXT_DATA__ (+ enriched
    description): 'YYYY-MM-DD' | 'Available now' | ''.

    Primary source (JSON key, defensive over several candidate shapes):
    initialReduxState.property.lettingDetails.items carries an
    "Availability date: <date>" bullet. Fallbacks: the property summary/description
    prose and the enriched description text ("available from <date>")."""
    candidates: list[str] = []
    prop = None
    try:
        prop = data["props"]["initialReduxState"]["property"]
    except (KeyError, TypeError):
        prop = None
    if isinstance(prop, dict):
        # lettingDetails.items (primary), plus a couple of same-intent candidate keys
        # so a schema tweak still yields the date.
        for cand_key in ("lettingDetails", "letting-details", "lettings"):
            ld = prop.get(cand_key)
            if isinstance(ld, dict):
                for item in ld.get("items") or []:
                    if isinstance(item, str) and re.search(r"availab", item, re.I):
                        candidates.append(item)
            elif isinstance(ld, str) and re.search(r"availab", ld, re.I):
                candidates.append(ld)
        for direct_key in ("available-from", "availableFrom", "availability",
                            "let-available-date", "letAvailableDate"):
            val = prop.get(direct_key)
            if isinstance(val, str) and val.strip():
                candidates.append(val)
        for prose_key in ("summary", "description"):
            val = prop.get(prose_key)
            if isinstance(val, str) and val:
                candidates.append(val)
    if description_text:
        candidates.append(description_text)

    for c in candidates:
        # Focus on the availability clause so a later unrelated date in prose can't win.
        mm = re.search(r"availab\w*[^.\n]{0,40}", c, re.I)
        frag = mm.group(0) if mm else c
        iso = parse_availability_date(frag, now=now)
        if iso:
            return iso
    return ""


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

    # Cheap win: the search JSON usually omits availability, but occasionally a
    # feature bullet (or the title) carries it ("Available from 1st September").
    # Parse it to a canonical value; leave "" otherwise so normalize sets
    # 'Contact agent'. Only bullets that actually mention availability are tried so
    # an unrelated date in a bullet can never masquerade as the start date.
    avail_from = ""
    for bullet in features + [title]:
        if bullet and re.search(r"availab", bullet, re.I):
            avail_from = parse_availability_date(bullet)
            if avail_from:
                break

    return {
        "Price": _pcm_price(listing.get("price")),
        "Address": address or title,
        "Description": description or title,
        "URL": url,
        "Available From": avail_from,  # parsed if the search JSON carried it; else "" -> 'Contact agent'
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


# ==========================================================================
# Detail-page description enrichment
# --------------------------------------------------------------------------
# The search-results __NEXT_DATA__ carries only key-feature bullets, not the
# agent's free-text prose. That prose lives on the individual detail page
# (/details/<id>/) under initialReduxState.property.description. Fetching it is
# one extra GET per property, so it is opt-in (DESC_ENRICH_ENABLED) and every
# result is cached per-URL in SQLite for a week to keep re-fetching rare and
# polite. Mirrors the ListingCache idiom used by on_demand.py.
# ==========================================================================

# Callers may check this before deciding to enrich; the function itself is not
# gated on it (so an explicit call always works).
DESC_ENRICH_ENABLED = os.getenv("DESC_ENRICH_ENABLED", "1") != "0"

# Descriptions change far less often than price/availability; a week-old copy of
# the prose is fine and keeps detail-page hits rare.
DESC_CACHE_TTL_HOURS = 168
# Guard against a runaway page: agent prose is a few hundred to ~1.5k chars.
DESC_MAX_CHARS = 4000

_DESC_REPO_ROOT = Path(__file__).resolve().parents[3]
DESC_CACHE_PATH = Path(
    os.getenv("OTM_DESC_CACHE_PATH", str(_DESC_REPO_ROOT / ".runtime" / "otm_desc_cache.sqlite3"))
)

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.I)
_BLOCK_CLOSE_RE = re.compile(r"</\s*(?:p|div|li|ul|ol|h[1-6]|tr)\s*>", re.I)
_WS_RE = re.compile(r"\s+")

# Detail pages carry several long "description" strings; only the property's own
# one is wanted. These path fragments flag the lettings-fee / tenancy boilerplate
# that must never be mistaken for the listing prose.
_DESC_FEE_HINTS = ("fee", "tenancy")


class _DescCache:
    """Per-URL detail store (description + availability). Mirrors
    on_demand.ListingCache: one write-time timestamp for a real TTL, and an empty
    string is a valid value meaning "known: this page has no description / no
    availability" so we don't keep re-fetching it.

    Design note on the availability column: description and availability are always
    parsed from the SAME single page fetch, so ONE per-row `fetched` timestamp is
    authoritative for both fields (no per-column freshness is needed). The column is
    added in-place via ALTER TABLE guarded by a PRAGMA check, so cache DBs written by
    the pre-availability schema keep working — their pre-existing rows simply report
    available_from='' until their normal TTL lapses and the row is re-fetched with
    both fields populated."""

    def __init__(self, path: Path = DESC_CACHE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS descriptions ("
                "url TEXT PRIMARY KEY, description TEXT NOT NULL, fetched REAL NOT NULL)"
            )
            # In-place migration: add available_from to any pre-existing schema.
            cols = {r[1] for r in db.execute("PRAGMA table_info(descriptions)").fetchall()}
            if "available_from" not in cols:
                db.execute(
                    "ALTER TABLE descriptions ADD COLUMN available_from TEXT NOT NULL DEFAULT ''"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, url: str) -> tuple[str, str, float] | None:
        """Return (description, available_from, fetched_epoch) or None if the URL was
        never stored. Stored empty strings are real hits (known no-value)."""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT description, available_from, fetched FROM descriptions WHERE url = ?",
                (url,),
            ).fetchone()
        if not row:
            return None
        try:
            return (row[0] or ""), (row[1] or ""), float(row[2])
        except (TypeError, ValueError):
            return None

    def set(self, url: str, description: str, available_from: str = "") -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO descriptions(url, description, available_from, fetched) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET description=excluded.description, "
                "available_from=excluded.available_from, fetched=excluded.fetched",
                (url, description or "", available_from or "", time.time()),
            )


_DESC_CACHE: _DescCache | None = None
_DESC_SESSION: requests.Session | None = None


def _desc_cache() -> _DescCache:
    global _DESC_CACHE
    if _DESC_CACHE is None:
        _DESC_CACHE = _DescCache()
    return _DESC_CACHE


def _desc_session() -> requests.Session:
    """Reuse one keep-alive session (same headers/User-Agent as the search
    scraper) across enrichment calls so a batch looks like one polite client."""
    global _DESC_SESSION
    if _DESC_SESSION is None:
        _DESC_SESSION = _new_session()
    return _DESC_SESSION


def _strip_html(raw: str | None) -> str:
    """HTML prose -> clean plain text: block/line breaks become spaces, tags are
    removed, entities decoded, whitespace collapsed."""
    if not raw:
        return ""
    txt = _BR_RE.sub(" ", raw)
    txt = _BLOCK_CLOSE_RE.sub(" ", txt)
    txt = _TAG_RE.sub("", txt)
    txt = html.unescape(txt)
    return _WS_RE.sub(" ", txt).strip()


def _find_description(data: dict) -> str | None:
    """Locate the listing's free-text description in a detail page's parsed
    __NEXT_DATA__. Tries the known path
    (initialReduxState.property.description, then .summary) and falls back to a
    defensive scan for a description-ish string that is NOT fee/tenancy
    boilerplate, so a schema change still yields the real prose."""
    try:
        redux = data["props"]["initialReduxState"]
    except (KeyError, TypeError):
        return None
    if not isinstance(redux, dict):
        return None

    prop = redux.get("property")
    if isinstance(prop, dict):
        for key in ("description", "summary"):
            val = prop.get(key)
            if isinstance(val, str) and val.strip():
                return val

    # Fallback: walk the tree for the longest 'description'/'summary' string,
    # skipping any path that names a fee/tenancy block.
    best: str | None = None
    stack: list[tuple[str, object]] = [("$", data)]
    while stack:
        path, obj = stack.pop()
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                npath = f"{path}.{kl}"
                if (
                    isinstance(v, str)
                    and v.strip()
                    and ("description" in kl or kl == "summary")
                    and not any(h in npath for h in _DESC_FEE_HINTS)
                ):
                    if best is None or len(v) > len(best):
                        best = v
                stack.append((npath, v))
        elif isinstance(obj, list):
            for item in obj:
                stack.append((path, item))
    return best


def fetch_listing_description(
    url: str, *, budget_s: float | None = None, force_refresh: bool = False
) -> str | None:
    """Fetch ONE OnTheMarket detail page and return its full description as plain
    text (HTML stripped, whitespace-collapsed), or None on any failure.

    This is the single network + parse + cache path: it also parses the listing's
    availability date from the SAME page and stores both in the per-URL cache (so
    fetch_listing_details can read availability without a second round-trip). Its
    own return value stays the description string for every existing caller.
    Cache-first; never raises."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None

    cache = _desc_cache()
    if not force_refresh:
        try:
            hit = cache.get(url)
        except Exception as e:  # a broken cache must never fail a fetch
            print(f"  [OTM_DESC] cache read failed: {e}")
            hit = None
        if hit is not None:
            text, _avail, fetched = hit
            if (time.time() - float(fetched)) < DESC_CACHE_TTL_HOURS * 3600:
                # Fresh hit; an empty string is a real "known no-description".
                print(f"  [OTM_DESC] cache hit ({len(text)} chars): {url}")
                return text

    # --- live fetch (cache miss / stale / forced) -----------------------------
    try:
        session = _desc_session()
        # Honour the robots Crawl-delay between live detail fetches, exactly like
        # find_rich_onthemarket does between search pages.
        time.sleep(random.uniform(*_CRAWL_DELAY))
        timeout = budget_s if (budget_s and budget_s > 0) else 25
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [OTM_DESC] fetch failed: {url}: {e}")
        return None
    except Exception as e:
        print(f"  [OTM_DESC] fetch error: {url}: {e}")
        return None

    try:
        m = _NEXT_DATA_RE.search(resp.text)
        if not m:
            print(f"  [OTM_DESC] no __NEXT_DATA__ on page: {url}")
            return None
        data = json.loads(m.group(1))
        raw = _find_description(data)
        text = _strip_html(raw)[:DESC_MAX_CHARS]
        available_from = _extract_available_from(data, text)
        # Cache the outcome either way: "" records a real page with no prose /
        # unknown availability so we don't keep re-fetching it for a week.
        cache.set(url, text, available_from)
        if text:
            print(f"  [OTM_DESC] fetched {len(text)} chars"
                  f"{f', avail={available_from}' if available_from else ''}: {url}")
        else:
            print(f"  [OTM_DESC] no description on page (cached empty): {url}")
        return text
    except Exception as e:
        print(f"  [OTM_DESC] parse error: {url}: {e}")
        return None


def fetch_listing_details(
    url: str, *, budget_s: float | None = None, force_refresh: bool = False
) -> dict | None:
    """Fetch ONE OnTheMarket detail page and return BOTH its description and its
    parsed availability::

        {"description": <str>, "available_from": <str>}

    where ``available_from`` is "" (unknown), "Available now" (immediate let), or an
    ISO "YYYY-MM-DD" start date. Returns None only for an unusable URL. Never raises.

    Design: the description is obtained via the module-level
    ``fetch_listing_description`` (the single fetch + cache-of-both path), then the
    availability is read from that same per-URL cache. Routing description extraction
    through ``fetch_listing_description`` keeps every existing caller — and the
    enrichment layer's monkeypatch of that name — working, while adding availability
    at zero extra network cost."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    description = fetch_listing_description(
        url, budget_s=budget_s, force_refresh=force_refresh
    )
    available_from = ""
    try:
        hit = _desc_cache().get(url)
        if hit is not None:
            available_from = hit[1] or ""
    except Exception as e:  # a broken cache must never fail the details call
        print(f"  [OTM_DESC] availability cache read failed: {e}")
    return {"description": description or "", "available_from": available_from}
