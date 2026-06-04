"""
Configuration & legacy-scraper loader for the scraping layer.

Everything tunable lives here:
  - the rich property schema (must match the columns of fake_property_listings.csv)
  - default search tasks (which locations / price bands to scrape)
  - cache file location + TTL
  - a helper that makes the standalone scrapers in ``scrapped_data_demo/scrapper``
    importable from inside ``local_data_demo``.
"""

import os
import sys
import importlib
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# .../local_data_demo/core/scraping/config.py
#   parents[0] = scraping
#   parents[1] = core
#   parents[2] = local_data_demo
#   parents[3] = <repo root>
_THIS = Path(__file__).resolve()
LOCAL_DEMO_DIR = _THIS.parents[2]
REPO_ROOT = _THIS.parents[3]

DATA_DIR = LOCAL_DEMO_DIR / "data"
FAKE_CSV = DATA_DIR / "fake_property_listings.csv"
CACHE_CSV = DATA_DIR / "scraped_property_listings.csv"

# The legacy, working scrapers (Rightmove + Zoopla) live here.
SCRAPPER_DIR = REPO_ROOT / "scrapped_data_demo" / "scrapper"

# ---------------------------------------------------------------------------
# Rich schema — MUST stay column-identical to fake_property_listings.csv so the
# loader, FAISS embeddings and get_property_details all keep working unchanged.
# ---------------------------------------------------------------------------
RICH_COLUMNS = [
    "Price",
    "Address",
    "Description",
    "URL",
    "Available From",
    "Platform",
    "Images",
    "geo_location",
    "Room_Type_Category",
    "Detailed_Amenities",
    "Guest_Policy",
    "Payment_Rules",
    "Excluded_Features",
    "Enhanced_Description",
]

# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------
TTL_HOURS = float(os.getenv("SCRAPER_CACHE_TTL_HOURS", "24"))
# Per-task safety cap so an accidental full crawl can't block startup for ages.
DEFAULT_LIMIT_PER_TASK = int(os.getenv("SCRAPER_LIMIT_PER_TASK", "15"))

# ---------------------------------------------------------------------------
# Default search tasks. Each task drives one Rightmove (and optional Zoopla)
# query. rightmove_id values are Rightmove locationIdentifiers; the ones below
# are verified student-relevant central-London locations. Override price/radius
# per task as needed, or pass your own task list to scrape_all().
# ---------------------------------------------------------------------------
DEFAULT_MIN_PRICE = int(os.getenv("SCRAPER_MIN_PRICE", "700"))
DEFAULT_MAX_PRICE = int(os.getenv("SCRAPER_MAX_PRICE", "3500"))
DEFAULT_MIN_BEDROOMS = int(os.getenv("SCRAPER_MIN_BEDROOMS", "0"))
DEFAULT_MAX_BEDROOMS = int(os.getenv("SCRAPER_MAX_BEDROOMS", "2"))

# Which sources to run, in order. Comma-separated env override.
#   openrent  -> works out of the box (scraping-tolerant, no extra setup)
#   zoopla    -> needs a local FlareSolverr Docker container on :8191
#   rightmove -> DEAD: Rightmove decommissioned /api/_search and prohibits
#                scraping; kept only as an opt-in stub. Don't expect data.
DEFAULT_SOURCES = [
    s.strip().lower()
    for s in os.getenv("SCRAPER_SOURCES", "openrent").split(",")
    if s.strip()
]

# Each task carries the per-source location handle it needs. OpenRent resolves a
# free-text term server-side, so openrent_term is just the area/landmark name.
# Focused on student-dense London areas (the app's travel-time/geocoding is
# London-optimised via TfL). Overlapping central areas are de-duplicated by URL.
DEFAULT_SEARCH_TASKS = [
    {
        "name": "Russell Square / UCL",
        "openrent_term": "University College London",
        "rightmove_id": "STATION^7877",          # (legacy; endpoint is dead)
        "zoopla_slug": "station/tube/russell-square",
        "radius": 1.0,
    },
    {
        "name": "King's Cross",
        "openrent_term": "King's Cross",
        "radius": 1.0,
    },
    {
        "name": "Camden Town",
        "openrent_term": "Camden Town",
        "radius": 1.0,
    },
    {
        "name": "Stratford (UEL / QMUL)",
        "openrent_term": "Stratford London",
        "radius": 1.5,
    },
    {
        "name": "Mile End (QMUL)",
        "openrent_term": "Mile End",
        "radius": 1.0,
    },
    {
        "name": "Elephant & Castle (LSE / UAL)",
        "openrent_term": "Elephant and Castle",
        "radius": 1.0,
    },
    {
        "name": "Wembley Park",
        "openrent_term": "Wembley Park",
        "rightmove_id": "STATION^9782",           # (legacy; endpoint is dead)
        "zoopla_slug": "wembley-park",
        "radius": 1.5,
    },
]


# ---------------------------------------------------------------------------
# Legacy scraper loader
# ---------------------------------------------------------------------------
def load_legacy(module_name: str):
    """Import a module from scrapped_data_demo/scrapper by name.

    The standalone scrapers import each other with bare names
    (``from rightmove_scraper import ...``), so we add their directory to
    sys.path before importing. Raises ImportError if the scrapper dir is missing.
    """
    if not SCRAPPER_DIR.exists():
        raise ImportError(f"scrapper directory not found at {SCRAPPER_DIR}")
    sp = str(SCRAPPER_DIR)
    if sp not in sys.path:
        sys.path.insert(0, sp)
    return importlib.import_module(module_name)
