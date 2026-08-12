#!/usr/bin/env bash
# One-time post-release retirement of the duplicate Chroma memory files.
#
# Preconditions are deliberately stronger than normal readiness:
#   * BOTH rollback pools answer /ready from the same pinned release;
#   * neither image contains the chromadb package;
#   * the migration tool verifies every legacy row and seals count + SHA-256;
#   * the operator confirms the exact destructive boundary.
#
# Run only after:
#   bash deploy/release.sh -- --both
# Then:
#   bash deploy/retire_legacy_agent_memory.sh
set -euo pipefail

REPO_DIR="${RETIRE_MEMORY_REPO_DIR:-/home/shuhan/uk_rent_recommendation}"
PIN_ENV="${DEPLOY_PIN_ENV:-/etc/rentcompass/deploy.env}"
PYTHON_CMD="${RETIRE_MEMORY_PYTHON:-/usr/bin/python3}"
DOCKER_CMD="${RETIRE_MEMORY_DOCKER:-docker}"
CURL_CMD="${RETIRE_MEMORY_CURL:-curl}"
DB_PATH="${RETIRE_MEMORY_DB_PATH:-$REPO_DIR/app/chroma_db_agent_memory}"

die() { printf '\033[31m!!  %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }

ASSUME_YES=0
case "${1:-}" in
  --yes|-y) ASSUME_YES=1 ;;
  "") ;;
  *) die "usage: $0 [--yes]" ;;
esac

cd "$REPO_DIR"
if [ "${RENTCOMPASS_DEPLOY_LOCK_HELD:-0}" != "1" ]; then
  DEPLOY_LOCK_FILE="${RENTCOMPASS_DEPLOY_LOCK_FILE:-}"
  if [ -z "$DEPLOY_LOCK_FILE" ]; then
    GIT_COMMON_DIR="$(git rev-parse --path-format=absolute --git-common-dir)" \
      || die "cannot resolve the shared git metadata directory"
    DEPLOY_LOCK_FILE="$GIT_COMMON_DIR/rentcompass-deploy.lock"
  fi
  mkdir -p "$(dirname "$DEPLOY_LOCK_FILE")"
  exec 9>"$DEPLOY_LOCK_FILE" || die "cannot open deploy lock: $DEPLOY_LOCK_FILE"
  flock -n 9 || die "another release/update/switch/retirement operation is running"
  export RENTCOMPASS_DEPLOY_LOCK_HELD=1
fi
[ -z "$(git status --porcelain --untracked-files=all)" ] \
  || die "working tree is dirty; refusing to run an unpinned migration script"
[ -r "$PIN_ENV" ] || die "pin file is not readable: $PIN_ENV"
# shellcheck source=/dev/null
DEPLOY_PINNED_SHA=""; . "$PIN_ENV"
[[ "${DEPLOY_PINNED_SHA:-}" =~ ^[0-9a-f]{40}$ ]] \
  || die "DEPLOY_PINNED_SHA is not a full commit"

head_sha=$(git rev-parse HEAD)
[ "$head_sha" = "$DEPLOY_PINNED_SHA" ] \
  || die "HEAD $head_sha does not equal pinned release $DEPLOY_PINNED_SHA"

declare -A PORT=( [legacy]=5001 [fc]=5002 )
declare -A CONTAINER=( [legacy]=uk-rent-app [fc]=uk-rent-app-fc )
declare -A ARCH=( [legacy]=legacy [fc]=fc_loop )
for pool in legacy fc; do
  headers=$($CURL_CMD -fsS -D- -o /dev/null \
    "http://127.0.0.1:${PORT[$pool]}/ready") \
    || die "$pool pool is not ready"
  sha=$(awk 'tolower($1) == "x-agent-version:" {gsub("\r", "", $2); print $2}' \
    <<<"$headers")
  arch=$(awk 'tolower($1) == "x-agent-arch:" {gsub("\r", "", $2); print $2}' \
    <<<"$headers")
  [ "$sha" = "$DEPLOY_PINNED_SHA" ] \
    || die "$pool pool serves ${sha:-<unknown>}, expected $DEPLOY_PINNED_SHA"
  [ "$arch" = "${ARCH[$pool]}" ] \
    || die "$pool endpoint reports ${arch:-<unknown>}, expected ${ARCH[$pool]}"
  $DOCKER_CMD exec "${CONTAINER[$pool]}" python -c \
    'import importlib.util; assert importlib.util.find_spec("chromadb") is None' \
    || die "$pool image still contains chromadb"
  say "$pool is on the pinned Chroma-free release"
done

inspection=$(mktemp /tmp/rentcompass-memory-inspection.XXXXXX.json)
trap 'rm -f -- "$inspection"' EXIT
$PYTHON_CMD scripts/migrate_agent_memory.py --db-path "$DB_PATH" > "$inspection" \
  || die "migration inspection failed"
read -r source_count source_digest < <(
  $PYTHON_CMD -c \
    'import json,sys; d=json.load(open(sys.argv[1], encoding="utf-8")); print(d["source_count"], d["source_digest"])' \
    "$inspection"
)
[[ "$source_count" =~ ^[0-9]+$ && "$source_digest" =~ ^[0-9a-f]{64}$ ]] \
  || die "inspection did not produce a sealed count/digest"

say "Verified $source_count legacy records"
printf '    source digest  %s\n' "$source_digest"
printf '    delete target  %s/chroma.sqlite3 and UUID index directories only\n' "$DB_PATH"
printf '    preserve       %s/agent_memory.sqlite3\n' "$DB_PATH"
if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Retire the verified duplicate legacy files? [y/N] '
  read -r reply
  case "$reply" in y|Y|yes|YES) ;; *) die "aborted — nothing was retired" ;; esac
fi

$PYTHON_CMD scripts/migrate_agent_memory.py \
  --db-path "$DB_PATH" \
  --retire-legacy \
  --expected-source-count "$source_count" \
  --expected-source-digest "$source_digest" \
  --confirm-no-legacy-processes

for pool in legacy fc; do
  $CURL_CMD -fsS "http://127.0.0.1:${PORT[$pool]}/ready" >/dev/null \
    || die "$pool did not return to ready after retirement"
done
say "Legacy AgentMemory duplicate retired; both pools remain ready"
