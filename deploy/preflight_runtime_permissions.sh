#!/usr/bin/env bash
# Read-only ownership gate for bind mounts used by the non-root app image.
set -euo pipefail

REPO="${RUNTIME_PREFLIGHT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uid=1000
gid=1000

bad=0
for relative in .runtime chroma_db chroma_db_area app/chroma_db_agent_memory app/data; do
  path="$REPO/$relative"
  if [ ! -d "$path" ]; then
    echo "missing writable bind directory: $relative" >&2
    bad=1
    continue
  fi
  owner="$(stat -c '%u:%g' "$path")"
  mode="$(stat -c '%A' "$path")"
  if [ "$owner" != "$uid:$gid" ] || [ "${mode:1:1}" != "r" ] || [ "${mode:2:1}" != "w" ] || [ "${mode:3:1}" != "x" ]; then
    echo "non-root bind is not owned/writable by $uid:$gid: $relative ($owner $mode)" >&2
    bad=1
  fi
done

if [ "$bad" -ne 0 ]; then
  echo "One-time operator repair (review paths first):" >&2
  echo "  sudo chown -R $uid:$gid .runtime chroma_db chroma_db_area app/chroma_db_agent_memory app/data" >&2
  exit 1
fi
echo "runtime bind ownership is compatible with non-root uid:gid $uid:$gid"
