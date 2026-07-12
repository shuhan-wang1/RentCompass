"""Web-grounded, LLM-reasoned, then VALIDATED recommender of residential areas
to live near a commute destination (a university/workplace).

Nothing here is hand-written or hallucinated. Each recommendation goes through a
four-stage pipeline whose only *creative* step (the LLM) is boxed in on both
sides by real data:

    web search  ->  LLM extract  ->  per-candidate VALIDATION  ->  persist
    (SearXNG)       (DeepSeek)       (classify + geocode +          (SQLite,
                                      commute, all real network)     long TTL)

  * The web-search snippets are the ONLY source material; the LLM is instructed to
    name areas the snippets actually support and to invent nothing.
  * Every LLM-named area is then independently VALIDATED against real data — it
    must resolve to a real UK place, sit in the destination's city (contamination
    guard), not itself be a commute destination (another uni/office), geocode to a
    real centroid, and route to the destination within the commute cap. Anything
    that fails any check is dropped, so an LLM slip cannot reach the caller.
  * Results (including honest empties) are cached in SQLite so repeats are instant.

Mirrors the idioms of ``core.scraping.on_demand``: a write-time-timestamped
SQLite store with a real TTL, a ``.runtime/`` repo-root path with an env override,
and a graceful "never raise, return an honest empty" contract.

Coordinate dicts use the ``{"lat": .., "lng": ..}`` shape returned by
``maps_service.geocode_address``; centroids are returned as ``[lat, lng]`` lists.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from core.commute import commute_minutes, in_london
from core.llm_interface import _call_deepseek, extract_first_json
from core.maps_service import geocode_address
from core.scraping.on_demand import classify_place, is_destination, resolve_location
from core.web_search import get_search_snippets

# --------------------------------------------------------------------------
# Tunables (all env-overridable)
# --------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parents[2]  # app/core/recommend_areas.py -> repo root

# Master switch. Set AREA_RECOS_ENABLED=0 to make recommend_areas a no-op ([]).
AREA_RECOS_ENABLED = os.getenv("AREA_RECOS_ENABLED", "1") != "0"

# Where good-areas-to-live sit relative to a fixed landmark barely changes, so the
# cache can live for weeks (~45 days). A hit is served instantly with no network.
AREA_RECO_TTL_HOURS = int(os.getenv("AREA_RECO_TTL_HOURS", "1080"))
# A cached EMPTY result (nothing found / providers down) is only reused for a few
# hours, so a transient outage doesn't wedge a destination at "no areas" for weeks
# while still preventing a per-call refetch storm.
AREA_RECO_EMPTY_TTL_HOURS = float(os.getenv("AREA_RECO_EMPTY_TTL_HOURS", "3"))
# Fallback commute cap when the caller passes no max_commute_time.
AREA_RECO_DEFAULT_COMMUTE = int(os.getenv("AREA_RECO_DEFAULT_COMMUTE", "60"))
# How many candidate area names to ask the LLM to extract from the snippets.
AREA_RECO_MAX_CANDIDATES = int(os.getenv("AREA_RECO_MAX_CANDIDATES", "8"))
# DeepSeek reasoning-call timeout (seconds).
AREA_RECO_LLM_TIMEOUT_S = int(os.getenv("AREA_RECO_LLM_TIMEOUT_S", "60"))

CACHE_PATH = Path(
    os.getenv("AREA_RECO_CACHE_PATH", str(REPO_ROOT / ".runtime" / "area_reco_cache.sqlite3"))
)


# --------------------------------------------------------------------------
# Persistent SQLite store (write-time timestamp -> real TTL). Mirrors
# on_demand.ListingCache: one small table, upsert on write, tolerant reads.
# --------------------------------------------------------------------------
class AreaRecoCache:
    """Tiny per-destination area-recommendation store.

    ``areas`` is a JSON list of validated item dicts; ``fetched`` is the write
    epoch used for TTL. An empty list is a legitimate cached value (see the
    shorter empty-TTL in recommend_areas)."""

    def __init__(self, path: Path = CACHE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS area_recos ("
                "dest_key TEXT PRIMARY KEY, areas TEXT NOT NULL, fetched REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, dest_key: str) -> tuple[list[dict], float] | None:
        """Return (areas, fetched_epoch) or None if the key was never stored."""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT areas, fetched FROM area_recos WHERE dest_key = ?", (dest_key,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0]), float(row[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def set(self, dest_key: str, areas: list[dict]) -> None:
        payload = json.dumps(areas, ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO area_recos(dest_key, areas, fetched) VALUES (?, ?, ?) "
                "ON CONFLICT(dest_key) DO UPDATE SET areas=excluded.areas, fetched=excluded.fetched",
                (dest_key, payload, time.time()),
            )


_CACHE: AreaRecoCache | None = None


def _cache() -> AreaRecoCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = AreaRecoCache()
    return _CACHE


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _dest_key(destination: str, city: str | None) -> str:
    """Stable lowercase cache key from destination (+ city)."""
    base = re.sub(r"\s+", " ", (destination or "").strip().lower())
    c = re.sub(r"\s+", " ", (city or "").strip().lower())
    return f"{base}|{c}" if c else base


def _norm_excludes(exclude_slugs) -> set[str]:
    """Normalize the exclude list (slugs and/or names) to a lowercase string set."""
    out: set[str] = set()
    for e in exclude_slugs or []:
        s = re.sub(r"\s+", " ", str(e).strip().lower())
        if s:
            out.add(s)
    return out


def _build_queries(destination: str, city: str | None) -> list[str]:
    """The 1-2 web-search queries whose snippets ground the recommendation."""
    tail = f" {city}" if city and city.lower() not in (destination or "").lower() else ""
    return [
        f"best areas to live commuting to {destination}{tail} rent",
        f"good neighbourhoods near {destination}{tail} to rent",
    ]


def _reason_ok(reason) -> str:
    """Coerce an LLM reason to a short, safe one-liner (<=100 chars)."""
    r = re.sub(r"\s+", " ", str(reason or "").strip())
    return r[:100]


def _llm_system_prompt() -> str:
    return (
        "You extract residential AREAS from UK web-search snippets for a rental "
        "assistant. You must ONLY name areas that the snippets actually support as "
        "good places to LIVE with a reasonable commute to the destination. Never "
        "invent an area that is not grounded in the snippets. Reply with ONLY a JSON "
        "object, no prose."
    )


def _llm_user_prompt(destination: str, city: str | None, snippets: str) -> str:
    where = f" in {city}" if city else ""
    return (
        f'Destination the renter commutes to: "{destination}"{where}.\n\n'
        "Web search snippets (the ONLY source you may use):\n"
        '"""\n'
        f"{snippets}\n"
        '"""\n\n'
        f"From ONLY the information in the snippets above, list up to "
        f"{AREA_RECO_MAX_CANDIDATES} residential areas / neighbourhoods that the "
        "snippets describe as good places to live with a reasonable commute to the "
        "destination. Each must be somewhere a person would LIVE — never the "
        "destination itself, never another university or office/employer.\n\n"
        "Return EXACTLY this JSON, nothing else:\n"
        '{"areas": [{"name": "<area/neighbourhood name>", '
        '"reason": "<one short sentence grounded in the snippets>"}]}\n\n'
        "Rules:\n"
        "- Only include areas actually mentioned or supported by the snippets.\n"
        f'- Do NOT include "{destination}" itself.\n'
        "- Keep each reason under 100 characters.\n"
        '- If the snippets support no area, return {"areas": []}.'
    )


def _looks_empty(snippets: str) -> bool:
    """True when the web layer produced nothing usable (SearXNG down / no hits)."""
    s = (snippets or "").strip().lower()
    return (not s) or ("no search results" in s and len(s) < 120)


def _extract_candidates(snippets: str, destination: str, city: str | None) -> list[dict]:
    """Call DeepSeek to extract snippet-grounded candidate areas. Blocking.

    Returns a list of ``{"name","reason"}`` (possibly empty). Salvages nothing on a
    parse failure rather than inventing. Never raises."""
    try:
        resp = _call_deepseek(
            _llm_user_prompt(destination, city, snippets),
            system_prompt=_llm_system_prompt(),
            timeout=AREA_RECO_LLM_TIMEOUT_S,
            temperature=0.2,
            max_tokens=1200,
        )
        if not resp:
            return []
        data = extract_first_json(resp) or {}
        raw = data.get("areas") or []
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = re.sub(r"\s+", " ", str(item.get("name", "")).strip())
            if not name or len(name) > 80:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "reason": _reason_ok(item.get("reason"))})
            if len(out) >= AREA_RECO_MAX_CANDIDATES:
                break
        return out
    except Exception as e:  # LLM/parse failure -> ground nothing
        print(f"[AREA_RECO] candidate extraction failed: {e}")
        return []


def _validate_one(
    cand: dict,
    destination: str,
    dest_coords: dict,
    dest_city: str | None,
    london: bool,
    max_commute: int,
    excludes: set[str],
) -> dict | None:
    """VALIDATE one LLM candidate against real data (blocking). Returns a finished
    item dict on success, or None if any check fails. Never raises.

    Checks, in order (cheapest first): resolve -> not-excluded -> right-city ->
    not-a-destination -> geocodable -> within commute cap."""
    try:
        name = cand.get("name", "")
        if not name:
            return None

        # 1) Resolve to a real UK place + its canonical city / kind.
        place = classify_place(name)
        slug = place.get("slug") or resolve_location(name)[0]
        cand_city = place.get("city")
        if not slug:
            return None

        # 2) Never recommend an excluded slug/name.
        nlow = name.lower()
        if slug in excludes or nlow in excludes or (cand_city and f"{nlow}|{cand_city}" in excludes):
            print(f"[AREA_RECO] drop '{name}': excluded")
            return None

        # 3) Contamination guard: a KNOWN city that differs from the destination's.
        if cand_city and dest_city and cand_city != dest_city:
            print(f"[AREA_RECO] drop '{name}': wrong city ({cand_city} != {dest_city})")
            return None

        # 4) It must be a place to LIVE, not another commute destination.
        if is_destination(place):
            print(f"[AREA_RECO] drop '{name}': is a destination ({place.get('kind')})")
            return None

        # 5) Geocode to a real centroid (anchored to the destination city).
        anchor = dest_city or cand_city or ""
        geo = geocode_address(f"{name}, {anchor}" if anchor else name)
        if not geo:
            print(f"[AREA_RECO] drop '{name}': not geocodable")
            return None
        centroid = [geo["lat"], geo["lng"]]

        # 6) Real commute to the destination within the cap.
        mins = commute_minutes(
            destination, dest_coords,
            origin_address=name, origin_geo=geo, london=london,
        )
        if mins is None or mins > max_commute:
            print(f"[AREA_RECO] drop '{name}': commute {mins} > {max_commute}")
            return None

        return {
            "name": name,
            "slug": slug,
            "city": cand_city or dest_city,
            "centroid": centroid,
            "commute_minutes": int(mins),
            "reason": _reason_ok(cand.get("reason")),
            "source": "web+validated",
        }
    except Exception as e:  # a single bad candidate must never sink the batch
        print(f"[AREA_RECO] validation error for '{cand.get('name')}': {e}")
        return None


async def _run_blocking(loop, fn, *args):
    return await loop.run_in_executor(None, fn, *args)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
async def recommend_areas(
    destination: str,
    *,
    city: str | None = None,
    dest_coords: dict | None = None,
    max_commute_time: int | None = None,
    exclude_slugs=None,
    limit: int = 4,
    force_refresh: bool = False,
    budget_s: float | None = None,
) -> list[dict]:
    """Recommend good residential areas to live near a commute ``destination``.

    Returns a list of item dicts::

        {"name": str, "slug": str, "city": str | None, "centroid": [lat, lng],
         "commute_minutes": int, "reason": str, "source": "cache" | "web+validated"}

    Cache-first: a fresh cached result is returned instantly. On a miss the areas
    are web-searched, LLM-extracted from the snippets, then every candidate is
    validated against real data (resolve + city guard + not-a-destination + geocode
    + commute). Sorted by commute ascending, capped at ``limit``. Never raises —
    any failure yields ``[]``.

    Parameters mirror the search layer: ``city`` is the canonical destination city
    (contamination guard), ``dest_coords`` an already-known ``{"lat","lng"}`` (else
    geocoded), ``exclude_slugs`` slugs/names never to recommend (e.g. the
    destination's own default area), ``budget_s`` a soft overall time budget.
    """
    if not AREA_RECOS_ENABLED:
        return []
    if not destination or not destination.strip():
        return []

    t0 = time.time()
    max_commute = int(max_commute_time) if max_commute_time else AREA_RECO_DEFAULT_COMMUTE
    excludes = _norm_excludes(exclude_slugs)
    dest_key = _dest_key(destination, city)

    # ---- 1) Cache lookup (fresh hit -> instant) --------------------------
    try:
        cached = _cache().get(dest_key)
    except Exception as e:
        print(f"[AREA_RECO] cache read failed: {e}")
        cached = None
    if cached and not force_refresh:
        areas, fetched = cached
        age_h = (time.time() - fetched) / 3600.0
        if areas and age_h < AREA_RECO_TTL_HOURS:
            return [dict(a, source="cache") for a in areas][:limit]
        if not areas and age_h < AREA_RECO_EMPTY_TTL_HOURS:
            return []  # honest empty, still fresh -> don't hammer providers
        # otherwise: stale -> fall through and regenerate

    # ---- 2) Generate (all blocking work offloaded; never raises) ---------
    try:
        loop = asyncio.get_event_loop()

        # 2a) Destination coords + canonical city.
        if not city or dest_coords is None:
            place = await _run_blocking(loop, classify_place, destination)
            if not city:
                city = place.get("city")
            geo_target = place.get("address") or destination
        else:
            geo_target = destination
        if dest_coords is None:
            dest_coords = await _run_blocking(loop, geocode_address, geo_target)
        if not dest_coords:
            print(f"[AREA_RECO] could not geocode destination '{destination}'")
            _cache().set(dest_key, [])
            return []
        london = in_london(dest_coords)

        if budget_s is not None and (time.time() - t0) > budget_s:
            return []

        # 2b) Web search -> snippet text (the only source material).
        queries = _build_queries(destination, city)
        snippet_parts = await asyncio.gather(
            *[_run_blocking(loop, get_search_snippets, q, 6) for q in queries]
        )
        snippets = "\n\n---\n\n".join(p for p in snippet_parts if p)
        if _looks_empty(snippets):
            print(f"[AREA_RECO] no web snippets for '{destination}' -> [] (grounded)")
            _cache().set(dest_key, [])
            return []

        if budget_s is not None and (time.time() - t0) > budget_s:
            return []

        # 2c) LLM extract snippet-grounded candidates.
        candidates = await _run_blocking(
            loop, functools.partial(_extract_candidates, snippets, destination, city)
        )
        if not candidates:
            print(f"[AREA_RECO] LLM extracted no grounded candidates for '{destination}'")
            _cache().set(dest_key, [])
            return []

        # 2d) Validate every candidate concurrently against real data.
        tasks = [
            loop.run_in_executor(
                None, _validate_one, c, destination, dest_coords,
                city, london, max_commute, excludes,
            )
            for c in candidates
        ]
        if budget_s is not None:
            remaining = max(0.1, budget_s - (time.time() - t0))
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            for p in pending:
                p.cancel()
            results = []
            for d in done:
                try:
                    results.append(d.result())
                except Exception:
                    pass
        else:
            results = await asyncio.gather(*tasks, return_exceptions=True)

        validated = [r for r in results if isinstance(r, dict)]

        # 2e) Dedupe by slug, sort by commute asc, cap at limit.
        deduped: dict[str, dict] = {}
        for item in validated:
            key = item.get("slug") or item["name"].lower()
            prev = deduped.get(key)
            if prev is None or item["commute_minutes"] < prev["commute_minutes"]:
                deduped[key] = item
        final = sorted(deduped.values(), key=lambda x: x["commute_minutes"])[:limit]

        # 2f) Persist (including an honest empty) and return.
        _cache().set(dest_key, final)
        print(f"[AREA_RECO] '{destination}': {len(final)} validated areas "
              f"in {time.time() - t0:.1f}s")
        return final

    except Exception as e:  # never poison the cache with a hard error
        print(f"[AREA_RECO] generate failed for '{destination}': {e}")
        return []
