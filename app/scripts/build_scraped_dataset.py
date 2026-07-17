#!/usr/bin/env python
"""
Build / refresh the scraped property cache.

Runs the OnTheMarket (+ optional Zoopla) scrapers, normalises every listing
to the rich schema used by the RAG/agent pipeline, and writes
``data/scraped_property_listings.csv`` — the cache the app serves when
PROPERTY_SOURCE=scraper (or auto, once built).

Examples:
    python build_scraped_dataset.py                 # default tasks, capped per task
    python build_scraped_dataset.py --limit 25      # up to 25 listings per task
    python build_scraped_dataset.py --sources onthemarket,zoopla
    python build_scraped_dataset.py --min-price 800 --max-price 2200

Zoopla needs a local FlareSolverr container:
    docker run -p 8191:8191 -e LOG_LEVEL=info --rm flaresolverr/flaresolverr
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import argparse

from core.scraping.config import (
    CACHE_CSV,
    DEFAULT_SEARCH_TASKS,
    DEFAULT_MIN_PRICE,
    DEFAULT_MAX_PRICE,
)
from core.scraping.provider import scrape_all
from core.scraping.normalize import write_csv


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the scraped property cache.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Max listings per task (default: SCRAPER_LIMIT_PER_TASK env / 15).")
    ap.add_argument("--sources", type=str, default=None,
                    help="Comma list of sources to run (e.g. 'onthemarket' or "
                         "'onthemarket,zoopla'). Default: SCRAPER_SOURCES env / onthemarket.")
    ap.add_argument("--rightmove-only", action="store_true",
                    help="(legacy no-op) Rightmove source was removed; produces no data.")
    ap.add_argument("--min-price", type=int, default=None,
                    help=f"Override min monthly price (default {DEFAULT_MIN_PRICE}).")
    ap.add_argument("--max-price", type=int, default=None,
                    help=f"Override max monthly price (default {DEFAULT_MAX_PRICE}).")
    args = ap.parse_args()
    sources = (
        [s.strip().lower() for s in args.sources.split(",") if s.strip()]
        if args.sources else None
    )

    tasks = [dict(t) for t in DEFAULT_SEARCH_TASKS]
    if args.min_price is not None:
        for t in tasks:
            t["min_price"] = args.min_price
    if args.max_price is not None:
        for t in tasks:
            t["max_price"] = args.max_price

    print(f"Building scraped dataset -> {CACHE_CSV}")
    print(f"Tasks: {[t['name'] for t in tasks]}  sources={sources or 'default'}"
          f"  rightmove_only={args.rightmove_only}")

    props = scrape_all(
        tasks=tasks,
        limit_per_task=args.limit,
        sources=sources,
        rightmove_only=args.rightmove_only,
    )

    if not props:
        print("\n/!\\ No properties scraped. Cache NOT written. /!\\")
        print("    - Check network / OnTheMarket availability.")
        print("    - For Zoopla, ensure FlareSolverr is running on :8191.")
        return 1

    write_csv(props, CACHE_CSV)
    print(f"\n[OK] Wrote {len(props)} properties to {CACHE_CSV}")
    platforms = {}
    for p in props:
        platforms[p.get("Platform", "?")] = platforms.get(p.get("Platform", "?"), 0) + 1
    print(f"     Breakdown by platform: {platforms}")
    geocoded = sum(1 for p in props if p.get("geo_location"))
    print(f"     With coordinates: {geocoded}/{len(props)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
