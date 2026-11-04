"""A local OpenStreetMap POI index that answers the app's Overpass queries offline.

The public Overpass servers are a shared, best-effort service: on 2026-11-03 the reference
server answered 502 to everyone, two mirrors timed out and the only mirror that answered held
Swiss data alone, so every "what is near this flat" question degraded to "amenity data is
temporarily unavailable". The places those questions ask about (supermarkets, cafés, parks,
stations, bus stops, gyms) change slowly, so a periodically rebuilt extract answers them as
well as a live query does.

``deploy/build_osm_poi_index.sh`` builds the index from a Geofabrik extract (osmium-tool runs
in a throwaway container; ``scripts/build_osm_poi_index.py`` turns its export into SQLite with
an R*Tree). ``core.maps_service.overpass_request`` calls :func:`answer_overpass_query` first:

* the query is parsed with a small, strict reader of the Overpass QL the app itself writes
  (``node/way/relation/nwr`` + tag filters + ``around`` or a bbox, ``out [center] [body]``);
  anything else returns ``None`` and goes to the network as before;
* every filter set must name a key the index holds completely, and every spatial filter must
  lie wholly inside the extract's boundary polygon, otherwise ``None`` (the network decides);
* a covered query that matches nothing returns an EMPTY result: nothing of that type near the
  point is a fact the complete extract can state, not an outage.

A missing, unreadable or stale index is logged once and the network path is used. Stdlib only.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
GENERATOR = "rentcompass-local-osm-index"

# What the index holds. A key in FULL_KEYS is kept for every value; a key in PARTIAL_KEYS
# only for the listed values (railway tracks and ordinary roads would dwarf the index).
# The build filters on exactly these, and a query is answered locally only when one of its
# filters needs an element the index is certain to contain.
FULL_KEYS = ("amenity", "shop", "leisure", "sport", "station", "brand")
PARTIAL_KEYS = {
    "railway": ("station", "halt", "tram_stop", "subway_entrance"),
    "highway": ("bus_stop",),
}

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = str(_REPO_ROOT / ".runtime" / "osm" / "poi.sqlite3")
_RECHECK_S = 60.0
_EARTH_R = 6371000.0


def index_path() -> str:
    return os.getenv("OSM_POI_INDEX_PATH", DEFAULT_PATH)


def max_age_days() -> float:
    try:
        value = float(os.getenv("OSM_POI_INDEX_MAX_AGE_DAYS", "45"))
    except ValueError:
        return 45.0
    return value if math.isfinite(value) and value > 0 else 45.0


def osmium_filter_expressions() -> list:
    """The ``osmium tags-filter`` expressions that select exactly what the index holds."""
    out = [f"nwr/{key}" for key in FULL_KEYS]
    out += [f"nwr/{key}={','.join(values)}" for key, values in PARTIAL_KEYS.items()]
    return out


def tags_are_indexed(tags: dict) -> bool:
    """True when an element with ``tags`` belongs in the index (the build keeps only these:
    osmium also exports referenced nodes that carry unrelated tags)."""
    if not isinstance(tags, dict):
        return False
    if any(key in tags for key in FULL_KEYS):
        return True
    return any(tags.get(key) in values for key, values in PARTIAL_KEYS.items())


# ── geometry ──────────────────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def _point_in_ring(lat: float, lon: float, ring: list) -> bool:
    """Ray casting; ``ring`` is a list of [lon, lat]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = xi + (lat - yi) * (xj - xi) / ((yj - yi) or 1e-300)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _distance_to_ring_m(lat: float, lon: float, ring: list) -> float:
    """Shortest distance from the point to any edge of ``ring`` (local flat projection)."""
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    best = math.inf
    n = len(ring)
    for i in range(n):
        ax, ay = (ring[i][0] - lon) * kx, (ring[i][1] - lat) * ky
        bx, by = (ring[(i + 1) % n][0] - lon) * kx, (ring[(i + 1) % n][1] - lat) * ky
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
        px, py = ax + t * dx, ay + t * dy
        best = min(best, math.hypot(px, py))
    return best


def parse_poly(text: str) -> dict:
    """Parse an Osmosis ``.poly`` boundary into ``{"outer": [ring], "holes": [ring]}``
    with rings as [[lon, lat], ...]."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    outer, holes = [], []
    i = 1  # first line is the polygon name
    while i < len(lines):
        head = lines[i]
        i += 1
        if not head or head == "END":
            continue
        ring = []
        while i < len(lines) and lines[i] != "END":
            parts = lines[i].split()
            i += 1
            if len(parts) >= 2:
                ring.append([float(parts[0]), float(parts[1])])
        i += 1  # the ring's END
        if len(ring) >= 3:
            (holes if head.startswith("!") else outer).append(ring)
    if not outer:
        raise ValueError("no outer ring in the boundary file")
    return {"outer": outer, "holes": holes}


def circle_inside_coverage(lat: float, lon: float, radius_m: float, coverage: dict) -> bool:
    """True when the whole disc lies inside the boundary (inside an outer ring, outside every
    hole, and at least ``radius_m`` from every ring edge)."""
    rings = coverage.get("outer") or []
    holes = coverage.get("holes") or []
    if not any(_point_in_ring(lat, lon, ring) for ring in rings):
        return False
    if any(_point_in_ring(lat, lon, ring) for ring in holes):
        return False
    return all(_distance_to_ring_m(lat, lon, ring) >= radius_m for ring in rings + holes)


# ── the Overpass QL the app writes ─────────────────────────────────────────────

_TYPES = {"node": ("n",), "way": ("w",), "relation": ("r",), "rel": ("r",),
          "nwr": ("n", "w", "r"), "nw": ("n", "w"), "wr": ("w", "r"), "nr": ("n", "r")}
_SETTINGS_RE = re.compile(r"^\s*(?:\[[a-z]+:[^\]]*\]\s*)+;", re.I)
_STMT_RE = re.compile(
    r"\s*(node|way|relation|rel|nwr|nw|wr|nr)\s*((?:\[[^\]]*\]\s*)+)\(\s*([^)]*)\)\s*;", re.I)
_FILTER_RE = re.compile(
    r'\[\s*(!?)\s*"([^"]+)"\s*(?:(=|!=|~|!~)\s*"((?:[^"\\]|\\.)*)"\s*(,\s*i\s*)?)?\]')
_OUT_RE = re.compile(r"\s*out\b([^;]*);\s*$", re.I)
_NUM = r"-?\d+(?:\.\d+)?"
_AROUND_RE = re.compile(rf"^around\s*:\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})$", re.I)
_BBOX_RE = re.compile(rf"^({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})$")


class _Filter:
    __slots__ = ("key", "op", "value", "regex")

    def __init__(self, key, op, value=None, flags=0):
        self.key, self.op, self.value = key, op, value
        self.regex = re.compile(value, flags) if op in ("~", "!~") else None

    def matches(self, tags: dict) -> bool:
        have = tags.get(self.key)
        if self.op == "exists":
            return have is not None
        if self.op == "absent":
            return have is None
        if self.op == "=":
            return have == self.value
        if self.op == "!=":
            return have != self.value
        if self.op == "~":
            return have is not None and self.regex.search(have) is not None
        return have is None or self.regex.search(have) is None  # "!~"

    def guarantees_indexed(self) -> bool:
        """True when every element passing this filter is certain to be in the index."""
        if self.op in ("=",):
            if self.key in FULL_KEYS:
                return True
            return self.value in PARTIAL_KEYS.get(self.key, ())
        if self.op in ("~", "exists"):
            return self.key in FULL_KEYS
        return False


def parse_overpass_query(query: str) -> Optional[dict]:
    """Read the subset of Overpass QL the app writes; ``None`` for anything else.

    Returns ``{"statements": [{"types", "filters", "circle": (lat, lon, r)} | {"bbox"...}],
    "center": bool, "tags": bool, "limit": int | None}``."""
    text = str(query or "").strip()
    m = _SETTINGS_RE.match(text)
    if m:
        settings = m.group(0)
        if "out:json" not in settings.replace(" ", "").lower():
            return None
        text = text[m.end():]
    else:
        return None  # the app always asks for JSON; anything else is not ours
    out = _OUT_RE.search(text)
    if not out:
        return None
    body = text[:out.start()].strip()
    modifiers = out.group(1).split()
    center, tags, limit = False, True, None
    for word in modifiers:
        lw = word.lower()
        if lw == "center":
            center = True
        elif lw in ("body", "qt", "asc", "meta", "tags"):
            continue
        elif lw.isdigit():
            limit = int(lw)
        else:
            return None  # geom, count, ids, skel, bb, ...: not answered locally
    if body.startswith("(") and body.endswith(");"):
        body = body[1:-2]
    elif body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    statements = []
    pos = 0
    while pos < len(body):
        if not body[pos:].strip():
            break
        sm = _STMT_RE.match(body, pos)
        if not sm:
            return None
        pos = sm.end()
        types = _TYPES.get(sm.group(1).lower())
        raw_filters = sm.group(2)
        filters = []
        consumed = 0
        for fm in _FILTER_RE.finditer(raw_filters):
            if raw_filters[consumed:fm.start()].strip():
                return None
            consumed = fm.end()
            negate, key, op, value, icase = fm.groups()
            if op is None:
                filters.append(_Filter(key, "absent" if negate else "exists"))
            else:
                if negate:
                    return None
                value = value.replace('\\"', '"')
                try:
                    filters.append(_Filter(key, op, value, re.I if icase else 0))
                except re.error:
                    return None
        if raw_filters[consumed:].strip() or not filters:
            return None
        spatial = sm.group(3).strip()
        am = _AROUND_RE.match(spatial)
        bm = _BBOX_RE.match(spatial) if not am else None
        if am:
            radius, lat, lon = (float(x) for x in am.groups())
            stmt = {"types": types, "filters": filters, "circle": (lat, lon, radius)}
        elif bm:
            south, west, north, east = (float(x) for x in bm.groups())
            if south > north or west > east:
                return None
            stmt = {"types": types, "filters": filters, "bbox": (south, west, north, east)}
        else:
            return None
        if not any(f.guarantees_indexed() for f in filters):
            return None
        statements.append(stmt)
    if not statements:
        return None
    return {"statements": statements, "center": center, "tags": tags, "limit": limit}


def tags_json(tags: dict) -> str:
    """How the build stores an element's tags; ``_needles`` relies on this exact form."""
    return json.dumps(tags, ensure_ascii=False, separators=(",", ":"))


def _needles(filters: list) -> tuple:
    """Substrings every matching row's stored JSON must contain (equality filters only)."""
    return tuple(tags_json({f.key: f.value})[1:-1] for f in filters if f.op == "=")


def _spatial_circle(stmt: dict) -> tuple:
    """The disc a statement's spatial filter needs covered: the around disc, or the disc
    that circumscribes a bbox."""
    if "circle" in stmt:
        return stmt["circle"]
    south, west, north, east = stmt["bbox"]
    lat, lon = (south + north) / 2.0, (west + east) / 2.0
    return lat, lon, haversine_m(lat, lon, north, east)


# ── the index ─────────────────────────────────────────────────────────────────

class LocalPoiIndex:
    """Read-only access to one index file; per-thread connections, re-opened when the file
    on disk is replaced (the build renames a new file into place)."""

    def __init__(self, path: str, max_age: float):
        self.path = path
        self.max_age_days = max_age
        self.meta: dict = {}
        self.coverage: dict = {}
        self.generation = 0
        self._stamp = None
        self._checked_at = 0.0
        self._lock = threading.Lock()
        self._local = threading.local()
        self.status = "unchecked"

    def _file_stamp(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{Path(self.path).as_posix()}?mode=ro"
        return sqlite3.connect(uri, uri=True, check_same_thread=False)

    def refresh(self) -> str:
        """Re-read metadata when the file changed; return the status ("ok", "missing",
        "unreadable", "stale")."""
        now = time.monotonic()
        with self._lock:
            if self._checked_at and now - self._checked_at < _RECHECK_S:
                return self.status
            self._checked_at = now
            stamp = self._file_stamp()
            if stamp is None:
                self.status, self._stamp = "missing", None
                return self.status
            if stamp == self._stamp and self.status != "unchecked":
                return self._age_status()
            try:
                conn = self._connect()
                try:
                    rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
                    conn.execute("SELECT 1 FROM poi_rtree LIMIT 1").fetchall()
                finally:
                    conn.close()
                if rows.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError("schema version")
                self.coverage = json.loads(rows["coverage"])
                self.meta = rows
            except Exception as exc:  # noqa: BLE001 — any defect means "do not use it"
                self.status, self._stamp = "unreadable", stamp
                self.meta, self.coverage = {}, {}
                logger.warning("osm_poi_index.unreadable error_type=%s", type(exc).__name__)
                return self.status
            self._stamp = stamp
            self.generation += 1
            return self._age_status()

    def _age_status(self) -> str:
        try:
            age_s = time.time() - float(self.meta.get("source_epoch") or 0)
        except ValueError:
            age_s = math.inf
        self.status = "stale" if age_s > self.max_age_days * 86400 else "ok"
        return self.status

    def _conn(self) -> sqlite3.Connection:
        cached = getattr(self._local, "conn", None)
        if cached is not None and getattr(self._local, "generation", None) == self.generation:
            return cached
        if cached is not None:
            try:
                cached.close()
            except Exception:  # noqa: BLE001
                pass
        conn = self._connect()
        self._local.conn, self._local.generation = conn, self.generation
        return conn

    def covers(self, lat: float, lon: float, radius_m: float) -> bool:
        return bool(self.coverage) and circle_inside_coverage(lat, lon, radius_m, self.coverage)

    def answer(self, parsed: dict) -> Optional[dict]:
        """An Overpass-shaped payload for a parsed query, or None when any statement reaches
        outside the extract."""
        for stmt in parsed["statements"]:
            if not self.covers(*_spatial_circle(stmt)):
                return None
        conn = self._conn()
        found: dict = {}
        # One R*Tree scan per distinct spatial filter (the map's union has eight selectors
        # around the same point), and a cheap substring test on the stored JSON before any
        # row is parsed: the build writes tags with fixed separators, so an equality filter
        # appears verbatim as "key":"value".
        groups: dict = {}
        for stmt in parsed["statements"]:
            spatial = ("circle", stmt["circle"]) if "circle" in stmt else ("bbox", stmt["bbox"])
            groups.setdefault(spatial, []).append((stmt, _needles(stmt["filters"])))
        for (kind, spatial), members in groups.items():
            if kind == "circle":
                lat, lon, radius = spatial
                dlat = radius / 110540.0
                dlon = radius / (111320.0 * max(0.01, math.cos(math.radians(lat))))
                box = (lat - dlat, lat + dlat, lon - dlon, lon + dlon)
            else:
                south, west, north, east = spatial
                box = (south, north, west, east)
            rows = conn.execute(
                "SELECT p.osm_type, p.osm_id, p.lat, p.lon, p.tags FROM poi_rtree r "
                "JOIN poi p ON p.id = r.id WHERE r.max_lat >= ? AND r.min_lat <= ? "
                "AND r.max_lon >= ? AND r.min_lon <= ?",
                (box[0], box[1], box[2], box[3])).fetchall()
            for osm_type, osm_id, plat, plon, raw_tags in rows:
                if (osm_type, osm_id) in found:
                    continue
                tags = None
                inside = None
                for stmt, needles in members:
                    if osm_type not in stmt["types"]:
                        continue
                    if needles and not all(n in raw_tags for n in needles):
                        continue
                    if inside is None:
                        inside = (kind != "circle"
                                  or haversine_m(lat, lon, plat, plon) <= radius)
                    if not inside:
                        break
                    if tags is None:
                        tags = json.loads(raw_tags)
                    if all(f.matches(tags) for f in stmt["filters"]):
                        found[(osm_type, osm_id)] = (plat, plon, tags)
                        break
        order = {"n": 0, "w": 1, "r": 2}
        names = {"n": "node", "w": "way", "r": "relation"}
        elements = []
        for (osm_type, osm_id), (plat, plon, tags) in sorted(
                found.items(), key=lambda kv: (order[kv[0][0]], kv[0][1])):
            element = {"type": names[osm_type], "id": osm_id}
            if osm_type == "n":
                element["lat"], element["lon"] = plat, plon
            elif parsed["center"]:
                element["center"] = {"lat": plat, "lon": plon}
            element["tags"] = tags
            elements.append(element)
        if parsed["limit"] is not None:
            elements = elements[:parsed["limit"]]
        return {
            "version": 0.6,
            "generator": GENERATOR,
            "osm3s": {
                "timestamp_osm_base": self.meta.get("source_timestamp", ""),
                "copyright": "The data included in this document is from www.openstreetmap.org. "
                             "The data is made available under ODbL.",
            },
            "elements": elements,
        }


_INDEX: Optional[LocalPoiIndex] = None
_INDEX_LOCK = threading.Lock()
_REPORTED: set = set()


def _index() -> LocalPoiIndex:
    global _INDEX
    path, age = index_path(), max_age_days()
    with _INDEX_LOCK:
        if _INDEX is None or _INDEX.path != path or _INDEX.max_age_days != age:
            _INDEX = LocalPoiIndex(path, age)
        return _INDEX


def reset_for_tests() -> None:
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None
    _REPORTED.clear()


def _report_once(status: str) -> None:
    if status in _REPORTED:
        return
    _REPORTED.add(status)
    if status == "ok":
        logger.info("osm_poi_index.in_use")
    else:
        logger.info("osm_poi_index.not_used reason=%s; amenity queries use Overpass", status)


def answer_overpass_query(query: str) -> Optional[dict]:
    """The local answer to an Overpass query, or None when the network should answer it
    (index missing/stale/unreadable, query not in the supported subset, or any part of it
    outside the extract). Never raises."""
    try:
        index = _index()
        status = index.refresh()
        _report_once(status)
        if status != "ok":
            return None
        parsed = parse_overpass_query(query)
        if parsed is None:
            return None
        return index.answer(parsed)
    except Exception as exc:  # noqa: BLE001 — a local defect must never cost the network path
        logger.warning("osm_poi_index.query_failed error_type=%s", type(exc).__name__)
        return None
