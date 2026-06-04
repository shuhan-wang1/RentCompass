"""
Property provider — the single public entry the rest of the app talks to.

Hybrid cache with TTL:
  - If the scraped-cache CSV exists and is fresh (< TTL), serve it (fast).
  - Otherwise scrape (Rightmove + optional Zoopla), normalise, write the cache,
    and serve the fresh results.
  - On any scrape failure / empty result, fall back to a stale cache if present,
    else to the bundled fake CSV — so the app always has data.
"""

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
# NOTE: source modules (openrent/rightmove/zoopla) import bs4/requests and are
# imported lazily inside _run_source, so that simply SERVING the cached CSV
# (the common startup path) only needs pandas — never bs4.


def get_active_property_csv():
    """Path of the CSV currently backing the system (scraped cache if built,
    else the bundled fake data). Used by get_property_details so the details
    tool stays consistent with what the list/search served."""
    return CACHE_CSV if CACHE_CSV.exists() else FAKE_CSV


def _is_fresh(path, ttl_hours: float) -> bool:
    if not path.exists():
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    return age_h < ttl_hours


def _run_source(source: str, task: dict, radius, min_price, max_price,
                limit_per_task) -> list[dict]:
    """Dispatch one (source, task) pair to the matching scraper."""
    if source == "openrent":
        term = task.get("openrent_term")
        if not term:
            return []
        from . import openrent
        return openrent.find_rich_openrent(
            term, radius, min_price, max_price, limit=limit_per_task
        )
    if source == "zoopla":
        slug = task.get("zoopla_slug")
        if not slug:
            return []
        from . import zoopla
        return zoopla.find_rich_zoopla(
            slug, radius, min_price, max_price, limit=limit_per_task
        )
    if source == "rightmove":
        rm_id = task.get("rightmove_id")
        if not rm_id:
            return []
        from . import rightmove
        return rightmove.find_rich_rightmove(
            rm_id, radius, min_price, max_price, limit=limit_per_task
        )
    print(f"  [provider] unknown source '{source}', skipping")
    return []


def scrape_all(
    tasks: list[dict] | None = None,
    limit_per_task: int | None = None,
    sources: list[str] | None = None,
    rightmove_only: bool = False,  # back-compat; equivalent to sources=['rightmove']
) -> list[dict]:
    """Run every search task across the enabled sources, returning de-duplicated
    rich-schema property dicts. Per-source failures are logged, not fatal."""
    tasks = tasks if tasks is not None else DEFAULT_SEARCH_TASKS
    if limit_per_task is None:
        limit_per_task = DEFAULT_LIMIT_PER_TASK
    if rightmove_only:
        sources = ["rightmove"]
    sources = sources if sources is not None else DEFAULT_SOURCES

    print(f"[provider] sources: {sources}")
    collected: list[dict] = []
    for task in tasks:
        name = task.get("name", "?")
        radius = task.get("radius", 1.5)
        min_price = task.get("min_price", DEFAULT_MIN_PRICE)
        max_price = task.get("max_price", DEFAULT_MAX_PRICE)
        print(f"\n=== Scraping task: {name} (radius {radius}mi, "
              f"£{min_price}-{max_price}) ===")

        for source in sources:
            try:
                got = _run_source(source, task, radius, min_price,
                                  max_price, limit_per_task)
                collected.extend(got)
            except Exception as e:
                print(f"  [provider] {source} task '{name}' failed: {e}")

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
) -> list[dict]:
    """Return rich-schema properties, honouring the hybrid cache.

    Args:
        force_refresh: ignore cache freshness and re-scrape.
        allow_scrape: if False, never hit the network — serve cache/fake only
                      (used for fast app startup).
    """
    if not force_refresh and _is_fresh(CACHE_CSV, TTL_HOURS):
        props = read_csv(CACHE_CSV)
        if props:
            print(f"[provider] cache HIT: {len(props)} properties from "
                  f"{CACHE_CSV.name} (< {TTL_HOURS}h old)")
            return props

    if allow_scrape:
        print("[provider] cache miss/stale -> scraping live data...")
        try:
            props = scrape_all(
                limit_per_task=limit_per_task, rightmove_only=rightmove_only
            )
        except Exception as e:
            print(f"[provider] scrape failed entirely: {e}")
            props = []
        if props:
            try:
                write_csv(props, CACHE_CSV)
                print(f"[provider] wrote cache -> {CACHE_CSV}")
            except Exception as e:
                print(f"[provider] could not write cache: {e}")
            return props
        print("[provider] scrape returned nothing; falling back.")

    # Fallbacks: stale cache, then fake data.
    if CACHE_CSV.exists():
        props = read_csv(CACHE_CSV)
        if props:
            print(f"[provider] serving STALE cache: {len(props)} properties")
            return props
    print(f"[provider] falling back to bundled fake data: {FAKE_CSV.name}")
    return read_csv(FAKE_CSV)
