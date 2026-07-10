"""
Scrape-on-demand + persistent cache for the *customer-facing* search path.

The demo used to serve a hardcoded London/Colchester CSV for every query, so a
"Manchester, 1 bed" request came back with London flats. This module replaces
that with real, city-correct OnTheMarket listings fetched on demand and cached:

    resolve (location, beds, price band)
        -> look up a persistent SQLite store (TTL, default 12h)
        -> on hit & fresh:  serve cached rows            (warm, ~ms)
        -> on miss/stale:   scrape OnTheMarket live       (cold, a few seconds),
                            persist, serve
        -> stale-if-error:  if a live scrape fails but an older cached set exists
                            for the query, serve it flagged possibly-outdated
        -> nothing at all:  return an honest empty result (NEVER demo rows)

City-correctness is structural: the location resolves to a specific OnTheMarket
area/city slug, so a Manchester query hits the Manchester area page. A light
cross-contamination guard additionally drops any row whose address names a
*different* major UK city than the one requested.

The bundled fake CSV is only reachable behind SEARCH_ALLOW_DEMO_FALLBACK
(default OFF) for fully offline development.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

from . import onthemarket

# --------------------------------------------------------------------------
# Tunables (all env-overridable)
# --------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parents[3]

# 12h TTL: rental listings turn over slowly enough that a half-day-old snapshot
# is still accurate, while keeping the cache warm across a demo session.
TTL_HOURS = float(os.getenv("SEARCH_CACHE_TTL_HOURS", "12"))
# Wall-clock budget for a single live scrape before we give up and fall back to
# a stale cache / honest-empty. The scraper is normally ~2-3s.
SCRAPE_BUDGET_S = float(os.getenv("SEARCH_SCRAPE_BUDGET_S", "60"))
# Offline-dev escape hatch. OFF by default: the customer path must never serve
# fake rows.
ALLOW_DEMO_FALLBACK = os.getenv("SEARCH_ALLOW_DEMO_FALLBACK", "").strip().lower() in {
    "1", "true", "yes", "on",
}
CACHE_PATH = Path(
    os.getenv("SEARCH_LISTING_CACHE_PATH", str(REPO_ROOT / ".runtime" / "listing_cache.sqlite3"))
)

# --------------------------------------------------------------------------
# Location -> OnTheMarket slug resolution
# --------------------------------------------------------------------------
# Major UK cities: OnTheMarket's plain area slug works for these (verified for
# london / manchester / leeds / edinburgh; the rest follow the same pattern).
CITY_SLUGS = {
    "london": "london", "manchester": "manchester", "birmingham": "birmingham",
    "leeds": "leeds", "liverpool": "liverpool", "bristol": "bristol",
    "sheffield": "sheffield", "nottingham": "nottingham", "leicester": "leicester",
    "coventry": "coventry", "newcastle": "newcastle-upon-tyne", "glasgow": "glasgow",
    "edinburgh": "edinburgh", "cardiff": "cardiff", "oxford": "oxford",
    "cambridge": "cambridge", "york": "york", "brighton": "brighton",
    "reading": "reading", "southampton": "southampton", "portsmouth": "portsmouth",
    "bath": "bath", "durham": "durham", "exeter": "exeter", "norwich": "norwich",
    "hull": "hull", "preston": "preston", "plymouth": "plymouth", "swansea": "swansea",
    "aberdeen": "aberdeen", "dundee": "dundee", "belfast": "belfast", "derby": "derby",
    "stoke": "stoke-on-trent", "wolverhampton": "wolverhampton", "sunderland": "sunderland",
    "salford": "salford", "loughborough": "loughborough", "lancaster": "lancaster",
}

# Landmarks / universities -> (slug, canonical_city). Checked before the plain
# city table so "oxford street" maps to London, not the city of Oxford.
LANDMARK_SLUGS = {
    "ucl": ("bloomsbury", "london"),
    "university college london": ("bloomsbury", "london"),
    "soas": ("bloomsbury", "london"),
    "birkbeck": ("bloomsbury", "london"),
    "kcl": ("holborn", "london"),
    "king's college": ("holborn", "london"),
    "kings college": ("holborn", "london"),
    "king's college london": ("holborn", "london"),
    "lse": ("holborn", "london"),
    "london school of economics": ("holborn", "london"),
    "imperial college": ("south-kensington", "london"),
    "imperial college london": ("south-kensington", "london"),
    "queen mary": ("mile-end", "london"),
    "qmul": ("mile-end", "london"),
    "city university": ("islington", "london"),
    "uel": ("stratford-london", "london"),
    "university of east london": ("stratford-london", "london"),
    "university of greenwich": ("greenwich", "london"),
    "canary wharf": ("canary-wharf", "london"),
    "london bridge": ("london-bridge", "london"),
    "kings cross": ("kings-cross", "london"),
    "king's cross": ("kings-cross", "london"),
    "st pancras": ("kings-cross", "london"),
    "camden": ("camden", "london"),
    "shoreditch": ("shoreditch", "london"),
    "elephant and castle": ("elephant-and-castle", "london"),
    "central london": ("london", "london"),
    "oxford street": ("london", "london"),
    "oxford circus": ("london", "london"),
    "soho": ("soho", "london"),
    "stratford": ("stratford-london", "london"),
    "wembley": ("wembley-park", "london"),
    "mile end": ("mile-end", "london"),
    "bloomsbury": ("bloomsbury", "london"),
}

# Cross-contamination guard: if the requested city is one of these, drop any row
# whose address names a *different* one of these.
_MAJOR_CITIES = set(CITY_SLUGS) | {"newcastle"}

# LANDMARK_SLUGS keys that are universities. classify_place() uses this to tell a
# university (whose legacy `location="UCL"` call should keep its commute annotation)
# from a plain area landmark (Camden, Shoreditch, ...). Every entry here also exists
# in LANDMARK_SLUGS above.
UNIVERSITY_KEYS = frozenset({
    "ucl", "university college london", "soas", "birkbeck",
    "kcl", "king's college", "kings college", "king's college london",
    "lse", "london school of economics",
    "imperial college", "imperial college london",
    "queen mary", "qmul", "city university",
    "uel", "university of east london", "university of greenwich",
})


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9'\s-]", " ", (text or "").lower()).strip()


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", (text or "").lower())
    s = re.sub(r"\s+", "-", s.strip())
    return re.sub(r"-+", "-", s).strip("-")


def _match_location(location: str) -> tuple[str, str | None, str | None, str | None]:
    """Shared resolution core for resolve_location + classify_place.

    Returns (slug, city, matched_key, source) where source is
    'landmark' | 'city' | None and matched_key is the LANDMARK/CITY key that
    matched (None when nothing matched -> slug is the slugified input). Exposing
    the matched key lets classify_place distinguish universities from plain areas
    without duplicating the (order-sensitive) match logic."""
    n = _norm(location)
    if not n:
        return "", None, None, None

    # 1) exact landmark, 2) exact city
    if n in LANDMARK_SLUGS:
        slug, city = LANDMARK_SLUGS[n]
        return slug, city, n, "landmark"
    if n in CITY_SLUGS:
        return CITY_SLUGS[n], n, n, "city"

    # 3) landmark substring (longest key first so specifics win)
    for key in sorted(LANDMARK_SLUGS, key=len, reverse=True):
        if key in n:
            slug, city = LANDMARK_SLUGS[key]
            return slug, city, key, "landmark"

    # 4) city substring, word-boundary (e.g. "University of Manchester" -> manchester)
    for key in sorted(CITY_SLUGS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", n):
            return CITY_SLUGS[key], key, key, "city"

    # 5) LAST-RESORT plain-substring fallback for glued typos ("axomanchester" ->
    #    manchester, "axocamden" -> camden). Ordered strictly AFTER the exact +
    #    word-boundary steps so normal inputs are unchanged; only keys >= 5 chars are
    #    eligible (shorter keys would false-positive), longest key first.
    _fallback_keys = sorted(
        (k for k in set(LANDMARK_SLUGS) | set(CITY_SLUGS) if len(k) >= 5),
        key=len, reverse=True,
    )
    for key in _fallback_keys:
        if key in n:
            if key in LANDMARK_SLUGS:
                slug, city = LANDMARK_SLUGS[key]
                return slug, city, key, "landmark"
            return CITY_SLUGS[key], key, key, "city"

    # 6) unknown -> slugify and let the scraper decide
    return _slugify(location), None, None, None


def resolve_location(location: str) -> tuple[str, str | None]:
    """Map a user destination (city, university, landmark) to an OnTheMarket area
    slug and, when known, its canonical city (for the contamination guard).

    Unknown locations are slugified and tried as-is; an unrecognised slug simply
    404s -> zero rows -> honest empty result (never wrong-city data)."""
    slug, city, _key, _source = _match_location(location)
    return slug, city


def classify_place(name: str) -> dict:
    """Classify a place name for the search layer.

    Returns {"kind": "university"|"area"|"unknown", "slug": str, "city": str|None}.
    - "university": UCL, KCL, LSE, Imperial, Queen Mary, City University, UEL,
      Greenwich, SOAS, Birkbeck, ... — a commute destination the caller should keep
      annotating (legacy `location="UCL"` semantics).
    - "area": any other known landmark (Camden, Shoreditch, ...) or city (Manchester).
    - "unknown": nothing matched -> slug = slugified input, city = None.
    """
    slug, city, matched_key, source = _match_location(name)
    if matched_key is None:
        return {"kind": "unknown", "slug": slug, "city": city}
    kind = "university" if (source == "landmark" and matched_key in UNIVERSITY_KEYS) else "area"
    return {"kind": kind, "slug": slug, "city": city}


def _wrong_city(address: str, requested_city: str | None) -> bool:
    """True if `address` clearly belongs to a major UK city other than the one
    requested. Only fires when both sides are recognised major cities, so local
    suburbs (e.g. Feltham for a London search) are never dropped."""
    if not requested_city or requested_city not in _MAJOR_CITIES:
        return False
    addr = (address or "").lower()
    for city in _MAJOR_CITIES:
        if city == requested_city:
            continue
        if re.search(rf"\b{re.escape(city)}\b", addr):
            # ...unless the requested city is also named (shared area names).
            if not re.search(rf"\b{re.escape(requested_city)}\b", addr):
                return True
    return False


# --------------------------------------------------------------------------
# Persistent SQLite store (write-time timestamp -> real TTL + stale-if-error)
# --------------------------------------------------------------------------
class ListingCache:
    """Tiny per-query listing store. Rows are kept even once stale so a failed
    live scrape can still serve a stale-but-honest snapshot."""

    def __init__(self, path: Path = CACHE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS listings ("
                "key TEXT PRIMARY KEY, rows TEXT NOT NULL, fetched REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key: str) -> tuple[list[dict], float] | None:
        """Return (rows, fetched_epoch) or None if the key was never stored."""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT rows, fetched FROM listings WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0]), float(row[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def set(self, key: str, rows: list[dict]) -> None:
        payload = json.dumps(rows, ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO listings(key, rows, fetched) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET rows=excluded.rows, fetched=excluded.fetched",
                (key, payload, time.time()),
            )


_CACHE: ListingCache | None = None


def _cache() -> ListingCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = ListingCache()
    return _CACHE


def _query_key(slug: str, min_beds: int, max_beds: int, min_price: int, max_price: int) -> str:
    # Bucket the price band to 100s so near-identical budgets share a cache entry.
    lo = (int(min_price) // 100) * 100
    hi = ((int(max_price) + 99) // 100) * 100
    return f"otm|{slug}|b{min_beds}-{max_beds}|p{lo}-{hi}"


def _scrape_live(slug, min_beds, max_beds, min_price, max_price, limit, budget_s):
    """Run the OnTheMarket scrape under a wall-clock budget. Returns the row list
    on success, or None if it errored / timed out (-> caller does stale-if-error)."""
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(
        onthemarket.find_rich_onthemarket,
        slug, 1.0, int(min_price), int(max_price),
        limit, int(min_beds), int(max_beds),
    )
    try:
        rows = fut.result(timeout=budget_s)
        ex.shutdown(wait=False)
        return rows
    except FuturesTimeout:
        print(f"  [on_demand] scrape budget ({budget_s}s) exceeded for '{slug}'")
        ex.shutdown(wait=False)
        return None
    except Exception as e:  # network/parse failure -> stale-if-error
        print(f"  [on_demand] scrape error for '{slug}': {e}")
        ex.shutdown(wait=False)
        return None


def _clean(rows: list[dict], requested_city: str | None) -> list[dict]:
    """Drop placeholder/advert rows (no address/url/price) and wrong-city rows."""
    out = []
    for r in rows or []:
        if not (r.get("Address") and r.get("URL") and r.get("Price")):
            continue
        if _wrong_city(r.get("Address", ""), requested_city):
            continue
        out.append(r)
    return out


def get_listings(
    location: str,
    min_bedrooms: int = 0,
    max_bedrooms: int = 2,
    min_price: int = 100,
    max_price: int = 5000,
    limit: int = 15,
    *,
    force_refresh: bool = False,
    budget_s: float | None = None,
) -> dict:
    """Resolve a query to real, city-correct OnTheMarket listings via the
    scrape-on-demand + persistent-cache pipeline.

    Returns a dict::

        {
          "rows": [ <rich-schema dict>, ... ],   # possibly empty, never fake
          "meta": {
            "slug", "requested_location", "requested_city",
            "source":  hit|scraped|stale-cache|demo|none,
            "stale":   bool,          # rows may be outdated
            "count":   int,
            "elapsed_s": float,
            "message": str,           # honest text when rows is empty
          }
        }
    """
    t0 = time.time()
    slug, city = resolve_location(location)
    meta = {
        "slug": slug,
        "requested_location": location,
        "requested_city": city,
        "source": "none",
        "stale": False,
        "count": 0,
        "elapsed_s": 0.0,
        "message": "",
    }
    if not slug:
        meta["message"] = "No search location was provided."
        meta["elapsed_s"] = round(time.time() - t0, 2)
        return {"rows": [], "meta": meta}

    key = _query_key(slug, min_bedrooms, max_bedrooms, min_price, max_price)
    cached = _cache().get(key)
    budget_s = SCRAPE_BUDGET_S if budget_s is None else budget_s

    # 1) Fresh cache hit.
    if cached and not force_refresh:
        rows, fetched = cached
        age_h = (time.time() - fetched) / 3600.0
        if age_h < TTL_HOURS and rows:
            meta.update(source="hit", count=len(rows),
                        elapsed_s=round(time.time() - t0, 2))
            return {"rows": rows, "meta": meta}

    # 2) Cache miss / stale -> scrape live under budget.
    scraped = _scrape_live(slug, min_bedrooms, max_bedrooms,
                           min_price, max_price, limit, budget_s)
    if scraped is not None:
        rows = _clean(scraped, city)
        if rows:
            _cache().set(key, rows)
            meta.update(source="scraped", count=len(rows),
                        elapsed_s=round(time.time() - t0, 2))
            return {"rows": rows, "meta": meta}

    # 3) Stale-if-error: a live scrape failed/empty but we have an older set.
    if cached:
        rows, _fetched = cached
        rows = _clean(rows, city)
        if rows:
            meta.update(source="stale-cache", stale=True, count=len(rows),
                        elapsed_s=round(time.time() - t0, 2),
                        message="Showing the most recent cached listings; a fresh "
                                "search could not be completed just now.")
            return {"rows": rows, "meta": meta}

    # 4) Offline-dev only: bundled fake CSV (default OFF).
    if ALLOW_DEMO_FALLBACK:
        demo = _demo_rows(city)
        if demo:
            meta.update(source="demo", stale=True, count=len(demo),
                        elapsed_s=round(time.time() - t0, 2),
                        message="Offline development mode: showing bundled demo data.")
            return {"rows": demo, "meta": meta}

    # 5) Nothing available -> honest empty result.
    meta.update(source="none", elapsed_s=round(time.time() - t0, 2),
                message=f"No live listings could be retrieved for '{location}' right now.")
    return {"rows": [], "meta": meta}


def _demo_rows(requested_city: str | None) -> list[dict]:
    """Offline-dev fallback only. Loads the bundled fake CSV and, when a city is
    known, keeps rows for that city so even the escape hatch stays city-plausible."""
    try:
        from .config import FAKE_CSV
        from .normalize import read_csv
        rows = read_csv(FAKE_CSV)
    except Exception as e:
        print(f"  [on_demand] demo fallback unavailable: {e}")
        return []
    if requested_city:
        matched = [r for r in rows if requested_city in str(r.get("Address", "")).lower()]
        rows = matched or []
    return rows
