#!/usr/bin/env bash
# Audit the complete writable bind-mount trees used by the non-root app image.
#
# Default mode is read-only and is used by update.sh as a final fail-closed gate.
# `--repair` is used by release.sh before it advances the deploy pin: it repairs
# only these five repo-local bind roots, then repeats the full recursive audit.
set -euo pipefail

REPO_INPUT="${RUNTIME_PREFLIGHT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uid="${RUNTIME_PREFLIGHT_UID:-1000}"
gid="${RUNTIME_PREFLIGHT_GID:-1000}"
SUDO_CMD="${RUNTIME_PREFLIGHT_SUDO_CMD:-sudo}"
REPAIR=0

usage() {
  cat <<'EOF'
usage: deploy/preflight_runtime_permissions.sh [--repair]

Without arguments, recursively audits the writable production bind mounts and
changes nothing.  --repair creates missing roots, fixes owner/group and restores
owner write access, then re-runs the same audit.  Repair is deliberately scoped
to the five bind roots declared below and never crosses a filesystem boundary.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repair) REPAIR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$uid" =~ ^[0-9]+$ ]] || { echo "invalid runtime uid: $uid" >&2; exit 2; }
[[ "$gid" =~ ^[0-9]+$ ]] || { echo "invalid runtime gid: $gid" >&2; exit 2; }
REPO="$(cd "$REPO_INPUT" 2>/dev/null && pwd -P)" \
  || { echo "runtime preflight repo is unavailable: $REPO_INPUT" >&2; exit 1; }

relative_roots=(
  .runtime
  chroma_db
  chroma_db_area
  app/chroma_db_agent_memory
  app/data
)
roots=()
missing=()

for relative in "${relative_roots[@]}"; do
  path="$REPO/$relative"
  case "$path" in
    "$REPO"/*) ;;
    *) echo "refusing bind path outside repository: $path" >&2; exit 1 ;;
  esac
  if [ -L "$path" ]; then
    echo "refusing symlinked writable bind root: $relative" >&2
    exit 1
  fi
  if [ ! -d "$path" ]; then
    missing+=("$path")
  fi
  roots+=("$path")
done

if [ "${#missing[@]}" -gt 0 ]; then
  if [ "$REPAIR" -ne 1 ]; then
    for path in "${missing[@]}"; do
      echo "missing writable bind directory: ${path#"$REPO"/}" >&2
    done
    echo "Run the one-click release to repair persistent runtime state:" >&2
    echo "  bash deploy/release.sh" >&2
    exit 1
  fi
  echo "Creating ${#missing[@]} missing writable bind root(s)..."
  "$SUDO_CMD" install -d -o "$uid" -g "$gid" -m 0775 -- "${missing[@]}" \
    || { echo "failed to create writable bind directories" >&2; exit 1; }
fi

audit_file="$(mktemp)"
trap 'rm -f "$audit_file"' EXIT

audit() {
  : > "$audit_file"
  local root
  for root in "${roots[@]}"; do
    # Ownership applies to every inode. Directories require owner rwx; regular
    # files require owner rw. Symlink targets are never followed.
    find -P "$root" -xdev \
      \( ! -uid "$uid" -o ! -gid "$gid" \
         -o \( -type d ! -perm -0700 \) \
         -o \( -type f ! -perm -0600 \) \) \
      -print0 >> "$audit_file" \
      || return 1
  done
}

describe_bad_paths() {
  local shown=0 path owner mode relative
  while IFS= read -r -d '' path; do
    [ "$shown" -lt 20 ] || continue
    owner="$(stat -c '%u:%g' -- "$path" 2>/dev/null || echo '?:?')"
    mode="$(stat -c '%A' -- "$path" 2>/dev/null || echo '?')"
    relative="${path#"$REPO"/}"
    printf '  %s %s %s\n' "$owner" "$mode" "$relative" >&2
    shown=$((shown + 1))
  done < "$audit_file"
}

audit || { echo "could not recursively audit runtime bind mounts" >&2; exit 1; }
mapfile -d '' -t bad_paths < "$audit_file"

if [ "${#bad_paths[@]}" -gt 0 ] && [ "$REPAIR" -ne 1 ]; then
  echo "non-root bind audit found ${#bad_paths[@]} incompatible path(s):" >&2
  describe_bad_paths
  [ "${#bad_paths[@]}" -le 20 ] \
    || echo "  ... $(( ${#bad_paths[@]} - 20 )) more" >&2
  echo "Run the one-click release to repair persistent runtime state:" >&2
  echo "  bash deploy/release.sh" >&2
  exit 1
fi

if [ "${#bad_paths[@]}" -gt 0 ]; then
  echo "Repairing ${#bad_paths[@]} incompatible persistent-runtime path(s) for uid:gid $uid:$gid..."
  # Operate on the audited inode list one path at a time. This preserves -xdev,
  # does not follow symlinks, and cannot broaden into an unreviewed parent tree.
  for path in "${bad_paths[@]}"; do
    "$SUDO_CMD" chown --no-dereference "$uid:$gid" -- "$path" \
      || { echo "failed to repair ownership: ${path#"$REPO"/}" >&2; exit 1; }
    if [ ! -L "$path" ]; then
      "$SUDO_CMD" chmod u+rwX -- "$path" \
        || { echo "failed to repair owner permissions: ${path#"$REPO"/}" >&2; exit 1; }
    fi
  done

  audit || { echo "could not verify repaired runtime bind mounts" >&2; exit 1; }
  mapfile -d '' -t bad_paths < "$audit_file"
  if [ "${#bad_paths[@]}" -gt 0 ]; then
    echo "runtime bind repair did not converge; refusing deployment:" >&2
    describe_bad_paths
    exit 1
  fi
fi

echo "runtime bind trees are owned and owner-writable by non-root uid:gid $uid:$gid"
