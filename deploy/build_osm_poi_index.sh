#!/usr/bin/env bash
# Build the local OpenStreetMap POI index the app reads before asking public Overpass.
#
#   bash deploy/build_osm_poi_index.sh .runtime/osm/poi.sqlite3
#
# Downloads a Geofabrik extract (verified against its published .md5) and its boundary
# polygon, selects the POI tags the app asks about with osmium-tool inside a throwaway
# container (no host packages, no network inside the container), converts the export with
# scripts/build_osm_poi_index.py (stdlib Python) and renames the result into place, so the
# running app switches to the new file on its next check (within a minute) without a restart.
#
# The pools read /app/.runtime/osm/poi.sqlite3, i.e. .runtime/osm/poi.sqlite3 in the checkout.
# Rebuild weekly; the app stops using an index whose data is older than
# OSM_POI_INDEX_MAX_AGE_DAYS (default 45) and goes back to Overpass.
#
# Environment:
#   OSM_POI_REGION   Geofabrik path without suffix
#                    (default europe/united-kingdom/england/greater-london)
#   OSM_POI_MIRROR   download base URL (default https://download.geofabrik.de)
#   OSM_POI_WORKDIR  scratch directory for the download (default: $TMPDIR or /tmp)
set -euo pipefail

die() { printf '!! %s\n' "$*" >&2; exit 1; }

OUT="${1:-${OSM_POI_INDEX_OUT:-}}"
[ -n "$OUT" ] || die "usage: $0 <output.sqlite3>"
REGION="${OSM_POI_REGION:-europe/united-kingdom/england/greater-london}"
MIRROR="${OSM_POI_MIRROR:-https://download.geofabrik.de}"
# debian:bookworm-slim, pinned by digest; osmium-tool comes from its own archive.
BASE_IMAGE="debian@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3"
TOOL_IMAGE="rentcompass-osmium:bookworm-99c0b25c46d0"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$(dirname "$OUT")"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
WORK="$(mktemp -d "${OSM_POI_WORKDIR:-${TMPDIR:-/tmp}}/osm-poi.XXXXXX")"
TMP_OUT="$OUT.building.$$"
trap 'rm -rf "$WORK"; rm -f "$TMP_OUT"' EXIT

command -v docker >/dev/null || die "docker is required"
command -v python3 >/dev/null || die "python3 is required"

printf '==> Downloading %s\n' "$REGION"
curl -fsSL --retry 3 -o "$WORK/region.osm.pbf.md5" "$MIRROR/$REGION-latest.osm.pbf.md5"
curl -fSL --retry 3 --progress-bar -o "$WORK/region.osm.pbf" "$MIRROR/$REGION-latest.osm.pbf"
curl -fsSL --retry 3 -o "$WORK/region.poly" "$MIRROR/$REGION.poly"
want="$(awk '{print $1}' "$WORK/region.osm.pbf.md5")"
got="$(md5sum "$WORK/region.osm.pbf" | awk '{print $1}')"
[ -n "$want" ] && [ "$want" = "$got" ] || die "md5 mismatch: published $want, downloaded $got"
printf '    md5 verified: %s\n' "$got"

if ! docker image inspect "$TOOL_IMAGE" >/dev/null 2>&1; then
  printf '==> Building the osmium tool image (once)\n'
  docker build -q -t "$TOOL_IMAGE" - >/dev/null <<EOF
FROM $BASE_IMAGE
RUN apt-get update \\
 && apt-get install -y --no-install-recommends osmium-tool \\
 && rm -rf /var/lib/apt/lists/*
EOF
fi

FILTERS="$(python3 "$REPO/scripts/build_osm_poi_index.py" --print-osmium-filters)"
printf '==> Selecting POIs: %s\n' "$FILTERS"
# shellcheck disable=SC2086  # FILTERS is a list of osmium expressions
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$WORK:/data" "$TOOL_IMAGE" \
  sh -ec "osmium tags-filter -O -o /data/poi.osm.pbf /data/region.osm.pbf $FILTERS
          osmium export -O -f geojsonseq --add-unique-id=type_id \
            -x print_record_separator=false -o /data/poi.geojsonseq /data/poi.osm.pbf
          osmium fileinfo -g header.option.osmosis_replication_timestamp /data/region.osm.pbf \
            > /data/timestamp.txt"
STAMP="$(tr -d '\r\n' < "$WORK/timestamp.txt")"

printf '==> Building the index (data as of %s)\n' "${STAMP:-unknown}"
python3 "$REPO/scripts/build_osm_poi_index.py" \
  --geojsonseq "$WORK/poi.geojsonseq" --poly "$WORK/region.poly" --out "$TMP_OUT" \
  --source-url "$MIRROR/$REGION-latest.osm.pbf" --source-md5 "$got" \
  --source-timestamp "$STAMP" --region "$REGION"
chmod 0644 "$TMP_OUT"
mv -f "$TMP_OUT" "$OUT"
printf '==> Index ready: %s (%s)\n' "$OUT" "$(du -h "$OUT" | cut -f1)"
