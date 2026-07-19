"""Per-area rent aggregation over the OnTheMarket listing SQLite cache.

The customer search path (``core.scraping.on_demand``) scrapes real OnTheMarket
listings on demand and persists them in a tiny SQLite store: one row per query
key ``otm|<slug>|b<min>-<max>|p<lo>-<hi>``, the ``rows`` column a JSON blob of
rich-schema listing dicts, and a write-time ``fetched`` epoch used for TTL. This
module reads THAT SAME store and, for a set of area slugs, computes rent
statistics (min / max / median / sample size / freshness / budget-match rate)
OFFLINE — one small ``LIKE 'otm|<slug>|%'`` query per slug, JSON decoded only for
the matched keys, never the whole table.

Honesty is structural: nothing is scraped here and nothing is invented. A slug
with no cached listings comes back with ``sample_size == 0`` and null stats — the
caller (``compare_or_rank_areas``) turns that into an explicit no-data marker
rather than a made-up number. Weekly prices are normalised to monthly EXACTLY the
way the search tool does it: numbers via ``parse_price`` (the search tool's price
converter) and a weekly->monthly factor of ``WEEKS_PER_MONTH`` (the constant the
search tool uses to convert a weekly budget).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
import time
from pathlib import Path

from uk_rent_agent.data.parsing import parse_price
from uk_rent_agent.domain.constants import WEEKS_PER_MONTH

_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parents[2]  # app/core/area_stats.py -> repo root


def _cache_path() -> Path:
    """The listing-cache path, resolved at call time from the SAME env var
    ``on_demand`` uses (``SEARCH_LISTING_CACHE_PATH``), so a test can point both at
    one temp DB. Mirrors ``on_demand.CACHE_PATH``'s default."""
    default = REPO_ROOT / ".runtime" / "listing_cache.sqlite3"
    return Path(os.getenv("SEARCH_LISTING_CACHE_PATH", str(default)))


# Weekly-price markers on the raw ``Price`` string. When present (and no monthly
# marker is), the parsed number is a per-week figure -> convert to monthly.
_WEEKLY_RE = re.compile(r"(?:\bpw\b|p\.?w\.?|per\s*week|/\s*w(?:k|eek)?\b|\ba\s*week\b|weekly)", re.I)
_MONTHLY_RE = re.compile(r"(?:\bpcm\b|\bpm\b|per\s*month|/\s*m(?:o|onth)?\b|\ba\s*month\b|monthly)", re.I)


def monthly_price(raw) -> float | None:
    """Monthly rent (GBP) from a rich-schema ``Price`` string, or None.

    Reuses ``parse_price`` (the search tool's number extractor). A string that is
    explicitly weekly ("£612 pw") and not also monthly is scaled by
    ``WEEKS_PER_MONTH`` — the same factor the search tool applies to a weekly
    budget — so weekly and monthly listings aggregate on one monthly axis."""
    amt = parse_price(raw)
    if amt is None:
        return None
    s = str(raw)
    if _WEEKLY_RE.search(s) and not _MONTHLY_RE.search(s):
        amt = amt * float(WEEKS_PER_MONTH)
    return round(float(amt), 2)


def _empty() -> dict:
    return {
        "min": None, "max": None, "median": None,
        "sample_size": 0, "freshness_days": None, "budget_match_rate": None,
    }


def monthly_budget(budget) -> float | None:
    """``(amount, period)`` -> monthly GBP, or None. ``period`` 'week' is scaled by
    ``WEEKS_PER_MONTH`` (identical to the search tool's budget conversion)."""
    if not budget:
        return None
    try:
        amount, period = budget
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    if str(period or "month").strip().lower().startswith("w"):
        amount = amount * float(WEEKS_PER_MONTH)
    return amount


def aggregate(slugs, budget=None, *, now: float | None = None) -> dict:
    """Rent stats per area slug straight from the listing cache.

    ``slugs``: iterable of OnTheMarket area slugs (the same slugs ``on_demand`` keys
    its cache by). ``budget``: ``(amount, period)`` where period is 'week'|'month',
    or None.

    Returns ``{slug: {min, max, median, sample_size, freshness_days,
    budget_match_rate}}`` — monthly GBP; ``sample_size``/``freshness_days`` from the
    listings de-duplicated by URL (newest ``fetched`` wins); ``budget_match_rate``
    the share priced at or under the monthly-normalised budget (None without a
    budget or with no data). A slug with no cached listings maps to
    ``sample_size == 0`` and null stats.

    Never raises: a missing/locked cache yields all-empty stats."""
    now = time.time() if now is None else now
    slugs = [str(s).strip() for s in (slugs or []) if s and str(s).strip()]
    out = {s: _empty() for s in slugs}
    if not slugs:
        return out
    path = _cache_path()
    if not path.exists():
        return out
    mbudget = monthly_budget(budget)
    try:
        conn = sqlite3.connect(str(path), timeout=10)
    except sqlite3.Error as exc:
        print(f"  [area_stats] cache open failed: {exc}")
        return out
    try:
        for slug in slugs:
            slug_l = slug.lower()
            # A single listing (URL) can sit under several price/bed buckets; keep the
            # copy from the newest key (freshest data) so it is counted once.
            best: dict[str, tuple[float, float]] = {}  # url -> (fetched, monthly_price)
            newest = 0.0
            try:
                fetched_rows = conn.execute(
                    "SELECT rows, fetched FROM listings WHERE key LIKE ?",
                    (f"otm|{slug_l}|%",),
                ).fetchall()
            except sqlite3.Error as exc:
                print(f"  [area_stats] query failed for '{slug_l}': {exc}")
                continue
            for rows_json, fetched in fetched_rows:
                try:
                    fetched = float(fetched)
                except (TypeError, ValueError):
                    fetched = 0.0
                try:
                    rows = json.loads(rows_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                for r in rows or []:
                    if not isinstance(r, dict):
                        continue
                    price = monthly_price(r.get("Price"))
                    if price is None:
                        continue
                    url = (str(r.get("URL") or "").strip()) or f"_n{len(best)}"
                    prev = best.get(url)
                    if prev is None or fetched > prev[0]:
                        best[url] = (fetched, price)
                    if fetched > newest:
                        newest = fetched
            prices = sorted(p for _f, p in best.values())
            if not prices:
                continue  # stays _empty()
            stat = {
                "min": prices[0],
                "max": prices[-1],
                "median": round(statistics.median(prices), 2),
                "sample_size": len(prices),
                "freshness_days": (round((now - newest) / 86400.0) if newest > 0 else None),
                "budget_match_rate": None,
            }
            if mbudget is not None:
                under = sum(1 for p in prices if p <= mbudget)
                stat["budget_match_rate"] = round(under / len(prices), 3)
            out[slug] = stat
    finally:
        conn.close()
    return out
