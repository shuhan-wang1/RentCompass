"""
Property provider — the single public entry the rest of the app talks to.

Hybrid cache with TTL:
  - If the scraped-cache CSV exists and is fresh (< TTL), serve it (fast).
  - Otherwise scrape (OnTheMarket + optional Zoopla), normalise, write the cache,
    and serve the fresh results.
  - On any scrape failure / empty result, fall back to a stale cache if present.
  - Bundled fake rows require the explicit SEARCH_ALLOW_DEMO_FALLBACK flag.
"""

import os
import time

from .config import (
    CACHE_CSV,
    FAKE_CSV,
    TTL_HOURS,
    DEFAULT_LIMIT_PER_TASK,
    DEFAULT_SEARCH_TASKS,
    DEFAULT_SOURCES,
    DEFAULT_MIN_PRICE,
    DEFAULT_MAX_PRICE,
)
from .normalize import read_csv, write_csv
# NOTE: source modules (onthemarket/zoopla) import bs4/requests and are
# imported lazily inside _run_source, so that simply SERVING the cached CSV
# (the common startup path) only needs pandas — never bs4.


def get_active_property_csv():
    """Path of the CSV currently backing the system (scraped cache if built,
    else an explicitly enabled bundled demo dataset)."""
    if CACHE_CSV.exists():
        return CACHE_CSV
    if os.getenv("SEARCH_ALLOW_DEMO_FALLBACK", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return FAKE_CSV
    return None


def _is_fresh(path, ttl_hours: float) -> bool:
    if not path.exists():
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    return age_h < ttl_hours


def _run_source(source: str, task: dict, radius, min_price, max_price,
                limit_per_task) -> list[dict]:
    """Dispatch one (source, task) pair to the matching scraper."""
    if source == "onthemarket":
        slug = task.get("onthemarket_slug")
        if not slug:
            return []
        from . import onthemarket
        return onthemarket.find_rich_onthemarket(
            slug, radius, min_price, max_price, limit=limit_per_task
        )
    if source == "zoopla":
        slug = task.get("zoopla_slug")
        if not slug:
            return []
        from . import zoopla
        return zoopla.find_rich_zoopla(
            slug, radius, min_price, max_price, limit=limit_per_task
        )
    print(f"  [provider] unknown source; source_chars={len(str(source))}; skipping")
    return []


def scrape_all(
    tasks: list[dict] | None = None,
    limit_per_task: int | None = None,
    sources: list[str] | None = None,
    rightmove_only: bool = False,  # legacy no-op: Rightmove source removed (dead endpoint)
) -> list[dict]:
    """Run every search task across the enabled sources, returning de-duplicated
    rich-schema property dicts. Per-source failures are logged, not fatal."""
    tasks = tasks if tasks is not None else DEFAULT_SEARCH_TASKS
    if limit_per_task is None:
        limit_per_task = DEFAULT_LIMIT_PER_TASK
    if rightmove_only:
        sources = ["rightmove"]
    sources = sources if sources is not None else DEFAULT_SOURCES

    print(f"[provider] source_count={len(sources)}")
    collected: list[dict] = []
    for task in tasks:
        name = task.get("name", "?")
        radius = task.get("radius", 1.5)
        min_price = task.get("min_price", DEFAULT_MIN_PRICE)
        max_price = task.get("max_price", DEFAULT_MAX_PRICE)
        print(f"\n=== Scraping task: name_chars={len(str(name))} "
              f"source_count={len(sources)} ===")

        for source in sources:
            try:
                got = _run_source(source, task, radius, min_price,
                                  max_price, limit_per_task)
                collected.extend(got)
            except Exception as e:
                print(f"  [provider] task failed; source={source} name_chars={len(str(name))} error_type={type(e).__name__}")

    # De-duplicate by URL (same listing can surface across overlapping tasks).
    seen, unique = set(), []
    for prop in collected:
        url = prop.get("URL", "")
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        unique.append(prop)

    print(f"\n=== Scrape complete: {len(unique)} unique properties "
          f"({len(collected)} before de-dup) ===")
    return unique


def get_properties(
    force_refresh: bool = False,
    allow_scrape: bool = True,
    limit_per_task: int | None = None,
    rightmove_only: bool = False,
    allow_demo: bool | None = None,
) -> list[dict]:
    """Return rich-schema properties, honouring the hybrid cache.

    Args:
        force_refresh: ignore cache freshness and re-scrape.
        allow_scrape: if False, never hit the network — serve cache only
                      (used for fast app startup).
        allow_demo: explicit offline-development opt-in.  When omitted, reads
                    SEARCH_ALLOW_DEMO_FALLBACK (default false).
    """
    if not force_refresh and _is_fresh(CACHE_CSV, TTL_HOURS):
        props = read_csv(CACHE_CSV)
        if props:
            print(f"[provider] cache HIT: property_count={len(props)} "
                  "fresh=True")
            return props

    if allow_scrape:
        print("[provider] cache miss/stale -> scraping live data...")
        try:
            props = scrape_all(
                limit_per_task=limit_per_task, rightmove_only=rightmove_only
            )
        except Exception as e:
            print(f"[provider] scrape failed entirely; error_type={type(e).__name__}")
            props = []
        if props:
            try:
                write_csv(props, CACHE_CSV)
                print(f"[provider] wrote cache; property_count={len(props)}")
            except Exception as e:
                print(f"[provider] could not write cache; error_type={type(e).__name__}")
            return props
        print("[provider] scrape returned nothing; falling back.")

    # Fallback: an explicitly labelled stale real-data cache.
    if CACHE_CSV.exists():
        props = read_csv(CACHE_CSV)
        if props:
            print(f"[provider] serving STALE cache: {len(props)} properties")
            return props
    if allow_demo is None:
        allow_demo = os.getenv("SEARCH_ALLOW_DEMO_FALLBACK", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
    if allow_demo:
        print("[provider] explicit demo mode enabled")
        return read_csv(FAKE_CSV)
    print("[provider] no real listing dataset is available; returning an honest empty result")
    return []
