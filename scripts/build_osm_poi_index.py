#!/usr/bin/env python3
"""Turn an ``osmium export -f geojsonseq`` of POIs into the app's local index (SQLite + R*Tree).

Stdlib only; run by ``deploy/build_osm_poi_index.sh``. What the index holds and how the app
reads it are defined in ``app/core/osm_poi_index.py``, loaded here by file path so this
script never imports the app package.

    build_osm_poi_index.py --print-osmium-filters
    build_osm_poi_index.py --geojsonseq poi.geojsonseq --poly region.poly --out index.sqlite3 \
        --source-url URL --source-md5 MD5 --source-timestamp 2026-11-02T21:21:02Z --region NAME
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "core" / "osm_poi_index.py"


def _load_index_module():
    spec = importlib.util.spec_from_file_location("_osm_poi_index_defs", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coords(geometry: dict):
    """Every [lon, lat] pair in a GeoJSON geometry."""
    gtype = geometry.get("type")
    c = geometry.get("coordinates")
    if gtype == "Point":
        yield c
    elif gtype in ("LineString", "MultiPoint"):
        yield from c
    elif gtype in ("Polygon", "MultiLineString"):
        for ring in c:
            yield from ring
    elif gtype == "MultiPolygon":
        for poly in c:
            for ring in poly:
                yield from ring


def representative_point(geometry: dict):
    """(lat, lon) of a node, or the centre of the element's bounding box — the point
    Overpass reports as ``center`` for ways and relations."""
    if geometry.get("type") == "Point":
        lon, lat = geometry["coordinates"][:2]
        return float(lat), float(lon)
    lons, lats = [], []
    for pair in _coords(geometry):
        lons.append(float(pair[0]))
        lats.append(float(pair[1]))
    if not lats:
        return None
    return (min(lats) + max(lats)) / 2.0, (min(lons) + max(lons)) / 2.0


def osm_identity(feature_id: str):
    """('n'|'w'|'r', id) from osmium's ``--add-unique-id=type_id``: n123, w456, and a<area>
    for areas, where an area from way W is 2W and from relation R is 2R+1."""
    kind, number = feature_id[0], int(feature_id[1:])
    if kind in ("n", "w", "r"):
        return kind, number
    if kind == "a":
        return ("w", number // 2) if number % 2 == 0 else ("r", (number - 1) // 2)
    raise ValueError(f"unexpected feature id {feature_id!r}")


def build(geojsonseq: Path, poly: Path, out: Path, *, source_url: str, source_md5: str,
          source_timestamp: str, region: str) -> dict:
    defs = _load_index_module()
    coverage = defs.parse_poly(poly.read_text(encoding="utf-8"))
    started = time.time()
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(out)
    conn.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE poi (
            id INTEGER PRIMARY KEY, osm_type TEXT NOT NULL, osm_id INTEGER NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL, tags TEXT NOT NULL);
        CREATE VIRTUAL TABLE poi_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);
        """
    )
    seen = set()
    counts = {"n": 0, "w": 0, "r": 0}
    skipped = 0
    rows, boxes = [], []
    next_id = 1

    def flush():
        conn.executemany("INSERT INTO poi VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.executemany("INSERT INTO poi_rtree VALUES (?, ?, ?, ?, ?)", boxes)
        rows.clear()
        boxes.clear()

    with geojsonseq.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feature = json.loads(line)
            tags = feature.get("properties") or {}
            if not defs.tags_are_indexed(tags):
                skipped += 1
                continue
            try:
                osm_type, osm_id = osm_identity(str(feature.get("id") or ""))
            except (ValueError, IndexError):
                skipped += 1
                continue
            if (osm_type, osm_id) in seen:
                continue
            point = representative_point(feature.get("geometry") or {})
            if point is None:
                skipped += 1
                continue
            seen.add((osm_type, osm_id))
            lat, lon = point
            rows.append((next_id, osm_type, osm_id, lat, lon, defs.tags_json(tags)))
            boxes.append((next_id, lat, lat, lon, lon))
            counts[osm_type] += 1
            next_id += 1
            if len(rows) >= 20000:
                flush()
    flush()
    try:
        source_epoch = datetime.fromisoformat(source_timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        source_epoch = started
    all_points = [pt for ring in coverage["outer"] for pt in ring]
    meta = {
        "schema_version": defs.SCHEMA_VERSION,
        "region": region,
        "source_url": source_url,
        "source_md5": source_md5,
        "source_timestamp": source_timestamp,
        "source_epoch": f"{source_epoch:.0f}",
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": json.dumps(coverage, separators=(",", ":")),
        "bbox": json.dumps([min(p[1] for p in all_points), min(p[0] for p in all_points),
                            max(p[1] for p in all_points), max(p[0] for p in all_points)]),
        "count_nodes": str(counts["n"]),
        "count_ways": str(counts["w"]),
        "count_relations": str(counts["r"]),
        "filters": " ".join(defs.osmium_filter_expressions()),
    }
    conn.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return {"rows": sum(counts.values()), "nodes": counts["n"], "ways": counts["w"],
            "relations": counts["r"], "skipped": skipped,
            "seconds": round(time.time() - started, 1), "bytes": out.stat().st_size}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print-osmium-filters", action="store_true")
    parser.add_argument("--geojsonseq", type=Path)
    parser.add_argument("--poly", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--source-md5", default="")
    parser.add_argument("--source-timestamp", default="")
    parser.add_argument("--region", default="")
    args = parser.parse_args(argv)
    if args.print_osmium_filters:
        print(" ".join(_load_index_module().osmium_filter_expressions()))
        return 0
    if not (args.geojsonseq and args.poly and args.out):
        parser.error("--geojsonseq, --poly and --out are required")
    stats = build(args.geojsonseq, args.poly, args.out, source_url=args.source_url,
                  source_md5=args.source_md5, source_timestamp=args.source_timestamp,
                  region=args.region)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
