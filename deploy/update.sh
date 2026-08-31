#!/usr/bin/env bash
# Redeploy the LIVE pool after a code change. Public-pool deploys drain by
# default and therefore need sudo for the nginx switch.
#
#   cd /home/shuhan/uk_rent_recommendation
#   git fetch && git checkout <the pinned sha>   # pull new code FIRST
#   bash deploy/update.sh --both                 # refreshes both pools with safe drain
#
# nginx / TLS / Xray / searxng / valkey are untouched. User accounts, chat
# history, .env and chroma indexes persist (they are bind-mounted from the host).
#
# ---------------------------------------------------------------------------
# WHY THIS SCRIPT READS THE NGINX UPSTREAM
# ---------------------------------------------------------------------------
# It used to hardcode `docker compose up -d --build app` and health-check :5001.
# That was correct only while the public upstream pointed at the legacy pool. It
# now points at fc (:5002), so the old script rebuilt a pool NOBODY WAS SERVING
# and reported "Healthy ✅ Live at https://rentcompass.co.uk:8443" — a green
# deploy that changed nothing the public could see. Worse, `app` is the standing
# rollback escape hatch, so the one thing the old script DID do was recreate the
# container you fall back to in an emergency, silently, on every deploy.
#
# So the target is derived, never assumed: read the `server 127.0.0.1:PORT;` line
# out of the public upstream block (the same line switch_pool.sh owns) and deploy
# to whichever pool is actually answering the public. `--pool` overrides it,
# `--both` does both.
#
# ---------------------------------------------------------------------------
# HOW EACH POOL SHIPS (they are NOT symmetric — compose says so)
# ---------------------------------------------------------------------------
#   legacy (:5001)  retains `build:` for developer compose use, but this release
#                   path builds uk-rent-agent:latest from an isolated worktree.
#   fc     (:5002)  has NO `build:` on purpose ("the working tree can never
#                   silently become what canary traffic executes"). It runs a
#                   pre-built, immutably tagged image. Both release paths build
#                   from ISOLATED WORKTREES checked out at the pin — never from
#                   the operational checkout — then persist their provenance.
#
# Both paths finish by asking the deployed pool WHICH COMMIT IT IS RUNNING
# (X-Agent-Version) and refusing to report success unless it answers with the
# full 40-char pin. "Container started" is not "the new code is live".
#
# ---------------------------------------------------------------------------
# OPTIONS
# ---------------------------------------------------------------------------
#   --pool auto|fc|legacy   which pool to deploy (default: auto = the live one)
#   --both                  deploy BOTH pools to the pin (keeps the rollback
#                           target from drifting; recommended after a real fix)
#   --drain                 move the public upstream to the healthy standby at
#                           rollout stage `maintenance`, redeploy, then restore the
#                           recorded pre-drain weight/rollout-id/stage — on success
#                           AND on failure (EXIT trap). The safe default.
#   --allow-in-place        explicitly accept a public 502 boot window and the
#                           risk that failed readiness leaves the candidate on
#                           the public port. Never implied by failed drain.
#   --rebuild-image         rebuild the fc image even if its tag already exists
#   --force                 redeploy even when the pool already serves the pin
#   --status                print pin + both pools' identity, change nothing
#
# Every external command is injectable so the whole script can be rehearsed with
# no docker, no root and no network (see deploy/test_update_assertions.sh).
set -euo pipefail

REPO_DIR="${UPDATE_REPO_DIR:-/home/shuhan/uk_rent_recommendation}"
cd "$REPO_DIR"

COMPOSE_CMD="${UPDATE_COMPOSE_CMD:-docker compose}"
DOCKER_CMD="${UPDATE_DOCKER_CMD:-docker}"
GIT_CMD="${UPDATE_GIT_CMD:-git}"
CURL_CMD="${UPDATE_CURL_CMD:-curl}"
SWITCH_CMD="${UPDATE_SWITCH_CMD:-bash deploy/switch_pool.sh}"
ENV_FILE="${UPDATE_ENV_FILE:-$REPO_DIR/.env}"
ENV_BACKUP_DIR="${UPDATE_ENV_BACKUP_DIR:-$(dirname "$REPO_DIR")/.rentcompass-env-backups}"
CONF="${UPDATE_CONF:-/etc/nginx/sites-available/rentcompass.co.uk.conf}"
ROUTE_CONF="${UPDATE_ROUTE_CONF:-/etc/nginx/snippets/rentcompass-canary-routing.conf}"
HEALTH_FMT="${UPDATE_HEALTH_FMT:-http://127.0.0.1:%s/ready}"
HEALTH_RETRIES="${UPDATE_HEALTH_RETRIES:-30}"
HEALTH_DELAY="${UPDATE_HEALTH_DELAY:-3}"
UPSTREAM_BLOCK='upstream rentcompass_app'

say()  { printf '==> %s\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

read_root_env() { # key default; explicit process env wins
  local key="$1" fallback="$2" value=""
  if [ -n "${!key+x}" ]; then value="${!key}"
  elif [ -r "$ENV_FILE" ]; then
    value="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -1 | tr -d '\r')"
    value="${value#\"}"; value="${value%\"}"
    value="${value#\'}"; value="${value%\'}"
  fi
  printf '%s' "${value:-$fallback}"
}

CANDIDATE_ARCH="$(read_root_env CANARY_AGENT_ARCH fc_loop)"
CANDIDATE_SPECIALISTS="$(read_root_env CANARY_MANAGER_V1_SPECIALISTS 0)"
CANDIDATE_MCP="$(read_root_env CANARY_USE_MCP_TOOLS 0)"
case "$CANDIDATE_ARCH:$CANDIDATE_SPECIALISTS:$CANDIDATE_MCP" in
  fc_loop:0:0|manager_v1:1:0) ;;
  manager_v1:*) die "manager_v1 candidate requires CANARY_MANAGER_V1_SPECIALISTS=1 and CANARY_USE_MCP_TOOLS=0" ;;
  fc_loop:*) die "fc_loop candidate requires CANARY_MANAGER_V1_SPECIALISTS=0 and CANARY_USE_MCP_TOOLS=0" ;;
  *) die "CANARY_AGENT_ARCH must be fc_loop or manager_v1" ;;
esac

declare -A PORT=( [legacy]=5001 [fc]=5002 )
declare -A ARCH=( [legacy]=legacy [fc]="$CANDIDATE_ARCH" )
declare -A SPECIALISTS=( [legacy]=0 [fc]="$CANDIDATE_SPECIALISTS" )
declare -A SERVICE=( [legacy]=app [fc]=app-fc )

POOL="auto"; BOTH=0; DRAIN=1; ALLOW_IN_PLACE=0
REBUILD_IMAGE=0; FORCE=0; STATUS_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pool)           POOL="${2:-}"; shift 2 ;;
    --both)           BOTH=1; shift ;;
    --drain)          DRAIN=1; ALLOW_IN_PLACE=0; shift ;;
    --allow-in-place) DRAIN=0; ALLOW_IN_PLACE=1; shift ;;
    --rebuild-image)  REBUILD_IMAGE=1; shift ;;
    --force)          FORCE=1; shift ;;
    --status)         STATUS_ONLY=1; shift ;;
    -h|--help)        sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *)                die "unknown argument: $1  (try --help)" ;;
  esac
done
case "$POOL" in auto|fc|legacy) ;; *) die "--pool must be auto, fc or legacy (got '$POOL')" ;; esac

# ---------------------------------------------------------------------------
# Pool discovery + identity
# ---------------------------------------------------------------------------
upstream_port() {
  [ -r "$CONF" ] || return 1
  awk "/^${UPSTREAM_BLOCK}[[:space:]]*\{/,/^\}/" "$CONF" \
    | sed -n 's/.*server[[:space:]]\+127\.0\.0\.1:\([0-9]\+\);.*/\1/p' | head -1
}

pool_of_port() { case "$1" in 5001) echo legacy ;; 5002) echo fc ;; *) echo "" ;; esac; }

routing_weight() {
  sed -n 's/^# rentcompass-canary-weight: \([0-9][0-9]*\)$/\1/p' "$ROUTE_CONF" 2>/dev/null | head -1
}

# The generated routing include IS the rollout state file: set_canary_weight.sh
# writes the live weight, rollout id and stage into its header comments, and
# every reader (this script, the monitor, canary_report) parses them from there.
# The drain below records and restores those same markers rather than inventing
# a second store that could disagree with the file nginx is actually serving.
route_marker() { # rentcompass-<name>
  sed -n "s/^# rentcompass-$1: //p" "$ROUTE_CONF" 2>/dev/null | head -1
}

other_pool() { case "$1" in legacy) echo fc ;; fc) echo legacy ;; esac; }

# ---------------------------------------------------------------------------
# The active-drain marker
# ---------------------------------------------------------------------------
# `--stage maintenance` is the only way to reach 100% candidate exposure without
# CANARY_ALLOW_FLIP, so it must be provably a DRAIN and not a cutover wearing the
# word. This file is that proof: written here before the drain, deleted after the
# restore, and required (with a matching rollout id) by set_canary_weight.sh and
# switch_pool.sh. It lives beside the deploy lock in the SHARED git metadata dir,
# so the sudo'd controller resolves the same path from any worktree.
maintenance_marker_path() {
  if [ -n "${RENTCOMPASS_MAINTENANCE_MARKER:-}" ]; then
    printf '%s' "$RENTCOMPASS_MAINTENANCE_MARKER"; return 0
  fi
  local common
  common="$($GIT_CMD rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common" ] || return 1
  printf '%s/rentcompass-maintenance-drain' "$common"
}
MAINTENANCE_MARKER="$(maintenance_marker_path || true)"

open_maintenance_window() { # <rollout-id> — authorise ONE drain, by name
  [ -n "$MAINTENANCE_MARKER" ] \
    || die "cannot resolve the maintenance drain marker path; set RENTCOMPASS_MAINTENANCE_MARKER"
  mkdir -p "$(dirname "$MAINTENANCE_MARKER")" \
    || die "cannot create the directory for $MAINTENANCE_MARKER"
  printf '%s' "$1" > "$MAINTENANCE_MARKER" \
    || die "cannot write the maintenance drain marker $MAINTENANCE_MARKER"
}
close_maintenance_window() { [ -n "$MAINTENANCE_MARKER" ] && rm -f "$MAINTENANCE_MARKER"; return 0; }

# "<arch> <sha> <specialists>" for a pool, or "" when readiness/identity fails.
identity_of() {
  local url hdrs
  url=$(printf "$HEALTH_FMT" "${PORT[$1]}")
  hdrs=$($CURL_CMD -sS -D- -o /dev/null --max-time 10 "$url" 2>/dev/null) || return 1
  grep -qi '^HTTP/[0-9.]* 200' <<<"$hdrs" || return 1
  printf '%s %s %s\n' \
    "$(grep -i '^x-agent-arch:'    <<<"$hdrs" | tr -d '\r' | awk '{print $2}')" \
    "$(grep -i '^x-agent-version:' <<<"$hdrs" | tr -d '\r' | awk '{print $2}')" \
    "$(grep -i '^x-agent-specialists:' <<<"$hdrs" | tr -d '\r' | awk '{print $2}')"
}

# Block until the pool answers 200, then hold the caller to arch AND full sha.
verify_pool() {
  local pool="$1" want_sha="$2" i=0 got arch sha specialists
  local url; url=$(printf "$HEALTH_FMT" "${PORT[$pool]}")
  say "Waiting for $pool health (the app reloads RAG/FAISS, ~30-40s)..."
  while [ "$i" -lt "$HEALTH_RETRIES" ]; do
    if got=$(identity_of "$pool"); then break; fi
    i=$((i + 1)); sleep "$HEALTH_DELAY"
  done
  [ -n "${got:-}" ] || die "$pool is not answering 200 at $url. Inspect: $COMPOSE_CMD logs --tail=60 ${SERVICE[$pool]}"
  read -r arch sha specialists <<<"$got"
  [ "$arch" = "${ARCH[$pool]}" ] \
    || die "$pool answered as arch '$arch', expected '${ARCH[$pool]}' — the wrong image is on :${PORT[$pool]}"
  [ "$specialists" = "${SPECIALISTS[$pool]}" ] \
    || die "$pool answered with specialists='${specialists:-<absent>}', expected '${SPECIALISTS[$pool]}' — release identity is incomplete"
  # The whole point of the deploy is that the NEW commit is live. A pool that
  # cannot name its commit, or names the old one, is a failed deploy however
  # healthy it looks.
  [ "$sha" = "$want_sha" ] \
    || die "$pool answered with commit '${sha:-<absent>}', expected the pin $want_sha — the new code is NOT live"
  say "$pool is live and self-identifies as $arch $sha specialists=$specialists ✅"
}

# ---------------------------------------------------------------------------
# Root .env pin rewriting (idempotent; keeps every other line byte-identical)
# ---------------------------------------------------------------------------
# One backup per RUN, taken before the first write. It lives outside the repo in
# a 0700 directory and is itself 0600: gitignore is not a secret-storage boundary.
# A per-write backup would overwrite the pre-run state with a half-rewritten file.
_ENV_BACKED_UP=0
set_env_var() {
  local key="$1" value="$2" tmp backup_file
  [ -f "$ENV_FILE" ] || die "root env file '$ENV_FILE' is missing — compose needs it"
  if [ "$_ENV_BACKED_UP" -eq 0 ]; then
    mkdir -p "$ENV_BACKUP_DIR"
    chmod 700 "$ENV_BACKUP_DIR"
    backup_file="$ENV_BACKUP_DIR/root-env.$(date -u +%Y%m%dT%H%M%SZ).$$.bak"
    cp "$ENV_FILE" "$backup_file"
    chmod 600 "$backup_file"
    _ENV_BACKED_UP=1
    say "Secured pre-run root env backup outside the repo: $backup_file"
  fi
  tmp=$(mktemp "${ENV_FILE}.tmp.XXXXXX")
  if grep -q "^${key}=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" \
      '$0 ~ "^" k "=" { print k "=" v; next } { print }' "$ENV_FILE" > "$tmp"
  else
    cat "$ENV_FILE" > "$tmp"; printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  chmod 600 "$tmp"
  mv -f "$tmp" "$ENV_FILE"
}

# ---------------------------------------------------------------------------
# Pin gate — refuse to deploy unless HEAD is EXACTLY the pinned release
# ---------------------------------------------------------------------------
# The pinned commit is read from an UNTRACKED, server-local env file, so the pin
# lives OUTSIDE version control — committing this gate can never change the pin
# (no self-reference). Production deploys the EXACT pinned commit and nothing
# else: there is deliberately no "deploy a later commit" escape hatch.
#
#   Pin file (default): /etc/rentcompass/deploy.env
#     DEPLOY_PINNED_SHA=<full 40-char sha>
#     DEPLOY_PYTHON_IMAGE=python@sha256:<64 hex>
#   Re-pin procedure  : edit that file AND `git checkout <sha>` to the same commit.
#   (Override the pin-file path for testing with DEPLOY_PIN_ENV=/path.)
# >>> PIN GATE START
PIN_ENV_FILE="${DEPLOY_PIN_ENV:-/etc/rentcompass/deploy.env}"
DEPLOY_PINNED_SHA=""
DEPLOY_PYTHON_IMAGE=""
if [ -r "$PIN_ENV_FILE" ]; then
  # shellcheck source=/dev/null
  . "$PIN_ENV_FILE"
fi

# --status changes nothing and must remain available when deployment metadata is
# incomplete; otherwise the diagnostic command disappears exactly when an operator
# needs it. All mutating paths continue through the strict gate below.
_head_full="$($GIT_CMD rev-parse "HEAD^{commit}")"
ROUTE_WEIGHT="$(routing_weight || true)"
ROUTE_ROLLOUT_ID="$(route_marker rollout-id || true)"
ROUTE_ROLLOUT_STAGE="$(route_marker rollout-stage || true)"
if [ -n "$ROUTE_WEIGHT" ]; then
  case "$ROUTE_WEIGHT" in
    0) LIVE_PORT=5001; LIVE_POOL=legacy ;;
    100) LIVE_PORT=5002; LIVE_POOL=fc ;;
    5|20|50) LIVE_PORT="weighted:${ROUTE_WEIGHT}%"; LIVE_POOL=mixed ;;
    *) LIVE_PORT="invalid:${ROUTE_WEIGHT}"; LIVE_POOL="" ;;
  esac
else
  LIVE_PORT="$(upstream_port || true)"
  LIVE_POOL="$(pool_of_port "${LIVE_PORT:-}")"
fi
if [ "$STATUS_ONLY" -eq 1 ]; then
  _pin_display="${DEPLOY_PINNED_SHA:-<unset>}"
  _pin_relation="   (pin unavailable; a deploy would be REFUSED)"
  if [ -n "${DEPLOY_PINNED_SHA:-}" ] \
     && $GIT_CMD rev-parse --verify -q "${DEPLOY_PINNED_SHA}^{commit}" >/dev/null 2>&1; then
    _pin_display="$($GIT_CMD rev-parse "${DEPLOY_PINNED_SHA}^{commit}")"
    if [ "$_head_full" = "$_pin_display" ]; then
      _pin_relation="   == pin ✅"
    else
      _pin_relation="   != pin (a deploy would be REFUSED)"
    fi
  fi
  printf 'pin           %s  (%s)\n' "$_pin_display" "$PIN_ENV_FILE"
  printf 'python base   %s%s\n' "${DEPLOY_PYTHON_IMAGE:-<unset>}" \
    "$([[ "${DEPLOY_PYTHON_IMAGE:-}" =~ @sha256:[0-9a-f]{64}$ ]] \
       && echo '   immutable ✅' || echo '   invalid/missing (a deploy would be REFUSED)')"
  printf 'HEAD          %s%s\n' "$_head_full" "$_pin_relation"
  printf 'upstream      %s\n' "${LIVE_PORT:-<conf unreadable: $CONF>}"
  printf 'candidate     arch=%s specialists=%s mcp=%s\n' \
    "$CANDIDATE_ARCH" "$CANDIDATE_SPECIALISTS" "$CANDIDATE_MCP"
  for p in legacy fc; do
    printf 'pool %-6s   :%s  %s%s\n' "$p" "${PORT[$p]}" \
      "$(identity_of "$p" || echo '<unreachable>')" \
      "$([ "$p" = "$LIVE_POOL" ] && echo '   <- PUBLIC' || true)"
  done
  exit 0
fi

if [ "${RENTCOMPASS_DEPLOY_LOCK_HELD:-0}" != "1" ]; then
  DEPLOY_LOCK_FILE="${RENTCOMPASS_DEPLOY_LOCK_FILE:-}"
  if [ -z "$DEPLOY_LOCK_FILE" ]; then
    GIT_COMMON_DIR="$($GIT_CMD rev-parse --path-format=absolute --git-common-dir)" \
      || die "cannot resolve the shared git metadata directory"
    DEPLOY_LOCK_FILE="$GIT_COMMON_DIR/rentcompass-deploy.lock"
  fi
  mkdir -p "$(dirname "$DEPLOY_LOCK_FILE")"
  exec 9>"$DEPLOY_LOCK_FILE" || die "cannot open deploy lock: $DEPLOY_LOCK_FILE"
  flock -n 9 || die "another release/update/switch/retirement operation is running"
  export RENTCOMPASS_DEPLOY_LOCK_HELD=1
fi

# The deploy lock is held from here on, so no other drain can be in flight: any
# marker still on disk is the debris of a run that was killed outright (SIGKILL,
# power loss) and must not keep authorising a 100% maintenance exposure.
if [ -n "$MAINTENANCE_MARKER" ] && [ -e "$MAINTENANCE_MARKER" ]; then
  warn "Clearing a stale maintenance drain marker from an interrupted run: $MAINTENANCE_MARKER"
  rm -f "$MAINTENANCE_MARKER"
fi

if [ ! -r "$PIN_ENV_FILE" ]; then
  echo "!! Pin gate: pin file '$PIN_ENV_FILE' is missing or unreadable." >&2
  echo "!! Create it (root) with:  DEPLOY_PINNED_SHA=<full sha>   (see deploy/monitoring/README.md)." >&2
  exit 1
fi
if [ -z "${DEPLOY_PINNED_SHA:-}" ]; then
  echo "!! Pin gate: DEPLOY_PINNED_SHA is not set in $PIN_ENV_FILE." >&2
  exit 1
fi
if ! $GIT_CMD rev-parse --verify -q "${DEPLOY_PINNED_SHA}^{commit}" >/dev/null 2>&1; then
  echo "!! Pin gate: pinned commit '${DEPLOY_PINNED_SHA}' (from $PIN_ENV_FILE) is not in this repo." >&2
  exit 1
fi
PIN_FULL="$($GIT_CMD rev-parse "${DEPLOY_PINNED_SHA}^{commit}")"
PIN_SHORT="$($GIT_CMD rev-parse --short "$PIN_FULL")"
[[ "${DEPLOY_PYTHON_IMAGE:-}" =~ @sha256:[0-9a-f]{64}$ ]] \
  || die "Pin gate: DEPLOY_PYTHON_IMAGE must be an immutable digest reference (python@sha256:<64 hex>) in $PIN_ENV_FILE"

if [ "$_head_full" != "$PIN_FULL" ]; then
  echo "!! Pin gate FAILED: HEAD $($GIT_CMD rev-parse --short HEAD) is not the pinned release." >&2
  echo "!!   HEAD = ${_head_full}" >&2
  echo "!!   PIN  = ${PIN_FULL}   (from $PIN_ENV_FILE)" >&2
  echo "!! Production deploys ONLY the exact pin. Fix:  git checkout ${PIN_FULL}" >&2
  exit 1
fi
_tree_status="$($GIT_CMD status --porcelain --untracked-files=all)"
if [ -n "$_tree_status" ]; then
  echo "!! Pin gate FAILED: working tree is DIRTY (tracked or untracked) — refusing a contaminated build context:" >&2
  printf '%s\n' "$_tree_status" >&2
  echo "!! Commit, ignore, or remove the files above, then redeploy." >&2
  exit 1
fi
say "Pin gate: HEAD == pinned release $PIN_SHORT, complete build context clean ✅"
# <<< PIN GATE END

if [ -x deploy/preflight_runtime_permissions.sh ]; then
  RUNTIME_PREFLIGHT_REPO="$REPO_DIR" \
    bash deploy/preflight_runtime_permissions.sh \
    || die "non-root runtime bind preflight failed; no image/container was changed"
fi

# Release prompt provenance is calculated from tracked objects at the pin, never
# from filesystem bytes. This keeps the manifest immune to ignored/untracked files.
_prompt_tree="$($GIT_CMD ls-tree -r "$PIN_FULL" -- app/app.py app/core/loop_prompts.py app/core/context_assembler.py app/core/prompt_spec.py 2>/dev/null || true)"
[ -n "$_prompt_tree" ] || _prompt_tree="$PIN_FULL prompt-bundle-v1"
PROMPT_SCHEMA_SHA_VALUE="$(printf '%s' "$_prompt_tree" | sha256sum | awk '{print $1}')"
_prompt_source="$($GIT_CMD show "$PIN_FULL:app/core/loop_prompts.py" 2>/dev/null || true)"
_declared_prompt_version="$(sed -n 's/^FC_LOOP_SYSTEM_PROMPT_VERSION = "\([^"]*\)"/\1/p' \
  <<<"$_prompt_source" | head -1)"
# PROMPT_VERSION is the semantic PromptSpec contract version and is compared
# byte-for-byte with the runtime PromptSpec by /ready. Do not append the git
# revision here: APP_SOURCE_SHA already carries the full release commit, while
# PROMPT_SCHEMA_SHA and the runtime per-language hashes identify the exact
# prompt bytes. Mixing the commit into this field makes every correctly built
# production image fail readiness (for example, 2.1.0@138606d != 2.1.0).
PROMPT_VERSION_VALUE="${_declared_prompt_version:-bundle-v1}"

image_digest() {
  local image="$1" inspected digest
  if ! inspected="$($DOCKER_CMD image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' "$image" 2>/dev/null)"; then
    warn "cannot inspect the built image '$image'"
    return 1
  fi
  digest="${inspected##*@}"
  if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    warn "image '$image' returned an invalid digest '${inspected:-<empty>}'"
    return 1
  fi
  printf '%s\n' "$digest"
}

# ---------------------------------------------------------------------------
# Resolve the deploy target
# ---------------------------------------------------------------------------
if [ "$LIVE_POOL" = mixed ]; then
  die "both pools currently serve ${ROUTE_WEIGHT}% weighted public traffic; roll back to weight 0 before any image/container redeploy"
fi
if [ "$BOTH" -eq 1 ]; then
  [ -n "$LIVE_POOL" ] || die "cannot read the public upstream from '$CONF' (port='${LIVE_PORT:-}'); refusing to guess the safe two-pool order"
  standby="$(other_pool "$LIVE_POOL")"
  TARGETS=("$standby" "$LIVE_POOL")
  say "Two-pool order: refresh standby '$standby', then drain and refresh public '$LIVE_POOL'"
elif [ "$POOL" != "auto" ]; then
  TARGETS=("$POOL")
else
  [ -n "$LIVE_POOL" ] || die "cannot read the public upstream from '$CONF' (port='${LIVE_PORT:-}'). Pass --pool fc|legacy explicitly."
  TARGETS=("$LIVE_POOL")
  say "Public upstream is 127.0.0.1:$LIVE_PORT -> deploying the '$LIVE_POOL' pool"
fi

# ---------------------------------------------------------------------------
# Bootstrap the SearXNG live config (gitignored runtime file) from the example.
# ---------------------------------------------------------------------------
if [ ! -f searxng/settings.yml ]; then
  mkdir -p searxng
  cp deploy/searxng-settings.yml.example searxng/settings.yml
  say "Bootstrapped searxng/settings.yml from deploy/searxng-settings.yml.example"
fi

# ---------------------------------------------------------------------------
# fc: build the immutable image from an ISOLATED worktree at the pin
# ---------------------------------------------------------------------------
CANDIDATE_IMAGE_TAG="${CANDIDATE_ARCH//_/-}"
FC_IMAGE="uk-rent-agent:canary-${CANDIDATE_IMAGE_TAG}-${PIN_SHORT}"

build_fc_image() {
  if [ "$REBUILD_IMAGE" -eq 0 ] && $DOCKER_CMD image inspect "$FC_IMAGE" >/dev/null 2>&1; then
    # The tag encodes the commit, so an existing tag was built from this exact
    # source. The runbook's rule is "never rebuild a tag in place"; honour it.
    say "Image $FC_IMAGE already exists — reusing it (--rebuild-image to force)"
    return 0
  fi
  local tree rc=0; tree="$(mktemp -d -t fc-build-XXXXXX)"
  rmdir "$tree"      # `git worktree add` insists on a non-existent path
  say "Building $FC_IMAGE from an isolated worktree at $PIN_SHORT (never from the working tree)"
  $GIT_CMD worktree add --detach "$tree" "$PIN_FULL" >/dev/null \
    || die "could not check out $PIN_SHORT into $tree"
  # Cleanup is explicit rather than a RETURN trap: the worktree must go away on
  # a failed build too, and leaving one behind would poison the next run.
  $DOCKER_CMD build \
    --build-arg "PYTHON_IMAGE=$DEPLOY_PYTHON_IMAGE" \
    --build-arg "APP_SOURCE_SHA=$PIN_FULL" \
    --build-arg "PROMPT_VERSION=$PROMPT_VERSION_VALUE" \
    --build-arg "PROMPT_SCHEMA_SHA=$PROMPT_SCHEMA_SHA_VALUE" \
    -t "$FC_IMAGE" "$tree" || rc=$?
  $GIT_CMD worktree remove --force "$tree" >/dev/null 2>&1 || true
  [ "$rc" -eq 0 ] || die "docker build failed for $FC_IMAGE (exit $rc)"
}

deploy_fc() {
  local digest
  build_fc_image
  # Resolve the fallible image inspection before any .env write. An inline
  # command substitution can fail in a subshell without stopping set -e.
  digest="$(image_digest "$FC_IMAGE")" \
    || die "cannot resolve an immutable digest for '$FC_IMAGE'; .env was not changed"
  set_env_var FC_CANARY_IMAGE "$FC_IMAGE"
  set_env_var FC_CANARY_SHA   "$PIN_FULL"
  set_env_var FC_IMAGE_DIGEST "$digest"
  set_env_var PROMPT_VERSION "$PROMPT_VERSION_VALUE"
  set_env_var PROMPT_SCHEMA_SHA "$PROMPT_SCHEMA_SHA_VALUE"
  set_env_var RELEASE_METADATA_REQUIRED "1"
  say "Pinned FC_CANARY_IMAGE=$FC_IMAGE / FC_CANARY_SHA=$PIN_SHORT in $ENV_FILE"
  say "Recreating the fc pool (:${PORT[fc]})..."
  $COMPOSE_CMD --profile canary up -d app-fc || die "compose failed to bring up app-fc"
  verify_pool fc "$PIN_FULL"
}

build_legacy_image() {
  local image="uk-rent-agent:latest" tree rc=0
  tree="$(mktemp -d -t legacy-build-XXXXXX)"
  rmdir "$tree"
  say "Building $image from an isolated worktree at $PIN_SHORT"
  $GIT_CMD worktree add --detach "$tree" "$PIN_FULL" >/dev/null \
    || die "could not check out $PIN_SHORT into $tree"
  $DOCKER_CMD build \
    --build-arg "PYTHON_IMAGE=$DEPLOY_PYTHON_IMAGE" \
    --build-arg "APP_SOURCE_SHA=$PIN_FULL" \
    --build-arg "PROMPT_VERSION=$PROMPT_VERSION_VALUE" \
    --build-arg "PROMPT_SCHEMA_SHA=$PROMPT_SCHEMA_SHA_VALUE" \
    -t "$image" "$tree" || rc=$?
  $GIT_CMD worktree remove --force "$tree" >/dev/null 2>&1 || true
  [ "$rc" -eq 0 ] || die "docker build failed for $image (exit $rc)"
}

deploy_legacy() {
  local digest
  # Legacy reads its identity from LEGACY_APP_SHA (`:-` defaulted, so a missing
  # value can never block the rollback path). Setting it here is what lets the
  # escape hatch NAME its commit — without it, switch_pool.sh needs
  # --allow-unidentified-target to roll back onto it.
  # Build and inspect before mutating .env, for the same fail-closed contract as fc.
  build_legacy_image
  digest="$(image_digest uk-rent-agent:latest)" \
    || die "cannot resolve an immutable digest for 'uk-rent-agent:latest'; .env was not changed"
  set_env_var LEGACY_APP_SHA "$PIN_FULL"
  set_env_var PROMPT_VERSION "$PROMPT_VERSION_VALUE"
  set_env_var PROMPT_SCHEMA_SHA "$PROMPT_SCHEMA_SHA_VALUE"
  set_env_var RELEASE_METADATA_REQUIRED "1"
  set_env_var LEGACY_IMAGE_DIGEST "$digest"
  say "Pinned LEGACY_APP_SHA=$PIN_SHORT in $ENV_FILE"
  say "Recreating the legacy pool (:${PORT[legacy]}) with its inspected image digest..."
  # Compose interpolates every profile before selecting service app. Supply
  # parser-only fc values in the process environment so a missing/partial
  # canary pin can never disable the legacy rollback path; app-fc is not started.
  FC_CANARY_IMAGE="${FC_CANARY_IMAGE:-uk-rent-agent:inactive-fc-profile}" \
  FC_CANARY_SHA="${FC_CANARY_SHA:-$PIN_FULL}" \
  FC_IMAGE_DIGEST="${FC_IMAGE_DIGEST:-sha256:0000000000000000000000000000000000000000000000000000000000000000}" \
    $COMPOSE_CMD up -d app || die "compose failed to bring up app"
  verify_pool legacy "$PIN_FULL"
}

deploy_pool() { case "$1" in fc) deploy_fc ;; legacy) deploy_legacy ;; esac; }

# ---------------------------------------------------------------------------
# Maintenance drain: record the public route, restore it on success AND failure
# ---------------------------------------------------------------------------
# Draining onto the candidate means 100% candidate exposure for the length of a
# pool redeploy. That is legitimate ONLY because it is temporary, so the pre-drain
# weight/rollout-id/stage are read out of the routing include (the file that IS the
# rollout state) and put back by both the normal path and an EXIT trap.
DRAIN_ACTIVE=0
DRAINED_FROM=""
PRE_DRAIN_WEIGHT=""; PRE_DRAIN_ROLLOUT_ID=""; PRE_DRAIN_STAGE=""
PRE_DRAIN_FLIP_AUTHORISED=0

record_pre_drain_route() {
  PRE_DRAIN_WEIGHT="$(routing_weight || true)"
  PRE_DRAIN_ROLLOUT_ID="$(route_marker rollout-id || true)"
  PRE_DRAIN_STAGE="$(route_marker rollout-stage || true)"
  # NOTE (implicit invariant): a weighted host at 5/20/50 is `LIVE_POOL=mixed`,
  # which is refused before any deploy, so PRE_DRAIN_WEIGHT here is only ever
  # 0, 100 or empty. That is why the restore replays the stage/id but not a
  # partial weight — there is no partial weight to replay.
  #
  # Did the operator ALREADY authorise 100% candidate exposure before this run?
  # Only then may the restore re-grant CANARY_ALLOW_FLIP=1 (see below).  A
  # recorded `maintenance` at 100 is NOT authorisation: it is the debris of an
  # earlier interrupted drain, and replaying it would make a temporary stage
  # permanent — the precise outcome this stage exists to prevent.
  PRE_DRAIN_FLIP_AUTHORISED=0
  if [ -z "$PRE_DRAIN_WEIGHT" ]; then
    # Single-upstream host: the live upstream port IS the whole rollout state.
    [ "$LIVE_POOL" = fc ] && PRE_DRAIN_FLIP_AUTHORISED=1
  elif [ "$PRE_DRAIN_WEIGHT" = 100 ] && [ "${PRE_DRAIN_STAGE:-flip}" != maintenance ]; then
    PRE_DRAIN_FLIP_AUTHORISED=1
  fi
  return 0
}

restore_pre_drain_route() { # <rc of the work that just finished>
  local rc="${1:-0}" rrc=0
  [ "$DRAIN_ACTIVE" -eq 1 ] || return 0
  DRAIN_ACTIVE=0
  trap - EXIT
  local interrupted=0
  if [ "$rc" -ge 128 ]; then
    interrupted=1
    warn "INTERRUPTED by signal $((rc - 128)) — unwinding the maintenance drain before exiting. The redeploy did NOT complete."
  fi
  local restore_id="$PRE_DRAIN_ROLLOUT_ID" restore_stage="$PRE_DRAIN_STAGE"
  local -a args=(--to "$DRAINED_FROM") runner=(env "SWITCH_ROUTE_CONF=$ROUTE_CONF")
  # Only a SUCCESSFUL redeploy may demand the new pin; a restore after a failure
  # (or an interrupt, which never rebuilt anything) puts traffic back on code that
  # was already there and must not ask that pool to name the undeployed pin.
  if [ "$rc" -eq 0 ]; then args+=(--expect-sha "$PIN_FULL"); fi
  if [ "$DRAINED_FROM" = "fc" ] && [ "$PRE_DRAIN_FLIP_AUTHORISED" -eq 1 ]; then
    args+=(--allow-public-fc)
    if [ -z "$restore_id" ];    then restore_id="deploy-return-$PIN_SHORT"; fi
    if [ -z "$restore_stage" ]; then restore_stage="flip"; fi
    # Restoring an exposure the operator had ALREADY authorised is not a new flip
    # decision, so the allow-flip switch is scoped to this one call and is never
    # exported for anything else this script runs.
    warn "Restoring the pre-drain candidate exposure (weight=${PRE_DRAIN_WEIGHT:-100} stage=$restore_stage); CANARY_ALLOW_FLIP is scoped to this restore only"
    runner+=(CANARY_ALLOW_FLIP=1)
  elif [ "$DRAINED_FROM" = "fc" ]; then
    # The recorded pre-drain state was 100% candidate at stage `maintenance` — an
    # earlier drain that never unwound, not an authorised exposure. Replaying it
    # (with a self-granted CANARY_ALLOW_FLIP) would launder leftover debris into a
    # permanent cutover nobody approved. Public traffic therefore STAYS on the
    # drain target, which is the legacy rollback pool, and the operator decides.
    close_maintenance_window
    warn "NOT restoring the pre-drain route: it recorded weight=${PRE_DRAIN_WEIGHT:-<single-upstream>} at stage '${PRE_DRAIN_STAGE:-<none>}', which is an UNFINISHED drain, not an authorised 100% exposure."
    warn "Public traffic stays on '$(other_pool "$DRAINED_FROM")'. To put the candidate back on 100% deliberately:"
    warn "    sudo CANARY_ALLOW_FLIP=1 $SWITCH_CMD --to fc --allow-public-fc --rollout-id <id> --stage flip"
    return 0
  fi
  if [ -n "$restore_id" ];    then args+=(--rollout-id "$restore_id"); fi
  if [ -n "$restore_stage" ]; then args+=(--stage "$restore_stage"); fi
  say "Returning public traffic to '$DRAINED_FROM' (restoring weight=${PRE_DRAIN_WEIGHT:-<single-upstream>} stage=${restore_stage:-<default>})"
  "${runner[@]}" $SWITCH_CMD "${args[@]}" || rrc=$?
  # The drain's authorisation ends here, whether or not the switch back worked:
  # leaving the marker would let a later hand-run reuse this run's 100% licence.
  close_maintenance_window
  if [ "$rrc" -ne 0 ]; then
    if [ "$interrupted" -eq 1 ]; then
      die "INTERRUPTED AND THE DRAIN WAS NOT UNWOUND: public traffic is still on '$(other_pool "$DRAINED_FROM")' at stage maintenance. Fix by hand: $SWITCH_CMD --to $DRAINED_FROM"
    fi
    if [ "$rc" -eq 0 ]; then
      die "REDEPLOY SUCCEEDED BUT THE UPSTREAM IS STILL ON '$(other_pool "$DRAINED_FROM")'. Fix by hand: $SWITCH_CMD --to $DRAINED_FROM"
    fi
    warn "THE MAINTENANCE DRAIN WAS NOT RESTORED: public traffic is still on '$(other_pool "$DRAINED_FROM")' at stage maintenance. Fix by hand: $SWITCH_CMD --to $DRAINED_FROM"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Interrupts must reach the EXIT trap as FAILURES
# ---------------------------------------------------------------------------
# An EXIT trap does run on SIGINT/SIGTERM/SIGHUP, but `$?` inside it is 0, so a
# Ctrl-C during the drain used to take the SUCCESS branch of the restore: it
# demanded `--expect-sha <new pin>` from a pool that was never rebuilt, the switch
# failed, and the operator was told "REDEPLOY SUCCEEDED BUT THE UPSTREAM IS STILL
# ON ..." while production sat on the drain target (100% candidate at stage
# maintenance when the standby is fc). Each signal therefore records its own code
# and exits; the EXIT trap prefers that code over `$?`.
_INTERRUPT_RC=0
_on_signal() { _INTERRUPT_RC="$1"; exit "$1"; }
_on_exit() {
  local rc="${1:-0}"
  if [ "$_INTERRUPT_RC" -ne 0 ]; then rc="$_INTERRUPT_RC"; fi
  restore_pre_drain_route "$rc"
}
# The handlers MUST exit themselves: bash resumes the script otherwise.
trap '_on_signal 130' INT
trap '_on_signal 143' TERM
trap '_on_signal 129' HUP

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
for target in "${TARGETS[@]}"; do
  echo
  say "───── pool: $target ─────"

  # Skip only on a FULL identity match. Matching the sha alone would skip a pool
  # that is on the right commit but running the OTHER architecture's image —
  # exactly the state a half-finished pool switch leaves behind, and the one case
  # where "already current" is most confidently wrong.
  if [ "$FORCE" -eq 0 ]; then
    cur="$(identity_of "$target" || true)"
    if [ "$cur" = "${ARCH[$target]} $PIN_FULL ${SPECIALISTS[$target]}" ]; then
      say "$target already serves ${ARCH[$target]} $PIN_SHORT specialists=${SPECIALISTS[$target]} — nothing to do (--force to redeploy)"
      continue
    fi
  fi

  # Recreating the public container takes it down for its whole boot window and
  # can leave a live-but-not-ready candidate behind. Drain is therefore the
  # fail-closed default. A requested drain that cannot use the standby NEVER
  # degrades into an in-place production mutation.
  if [ "$DRAIN" -eq 1 ] && [ "$target" = "$LIVE_POOL" ]; then
    standby="$(other_pool "$target")"
    if identity_of "$standby" >/dev/null 2>&1; then
      record_pre_drain_route
      say "Draining public traffic to '$standby' for the redeploy (stage maintenance; needs sudo for nginx)"
      say "    pre-drain public route: weight=${PRE_DRAIN_WEIGHT:-<single-upstream>} rollout=${PRE_DRAIN_ROLLOUT_ID:-<none>}/${PRE_DRAIN_STAGE:-<none>} — restored when this pool finishes OR fails"
      # `--stage maintenance` is what authorises the drain's 100% exposure when
      # the standby is the candidate: set_canary_weight.sh refuses weight 100 for
      # every other stage unless CANARY_ALLOW_FLIP=1 is set deliberately, and it
      # requires this marker plus the machine rollout-id shape, so the stage
      # cannot be typed by hand into a permanent cutover.
      # SWITCH_ROUTE_CONF is passed explicitly: update.sh READ the pre-drain state
      # out of $ROUTE_CONF, and switch_pool.sh must WRITE the same file rather than
      # its own default, or a rehearsal silently records one file and edits another.
      open_maintenance_window "deploy-maintenance-$PIN_SHORT"
      env "SWITCH_ROUTE_CONF=$ROUTE_CONF" $SWITCH_CMD --to "$standby" \
        --rollout-id "deploy-maintenance-$PIN_SHORT" --stage maintenance \
        $([ "$standby" = "legacy" ] && echo "--allow-unidentified-target") \
        $([ "$standby" = "fc" ] && echo "--allow-public-fc") \
        || { close_maintenance_window
             die "could not drain to '$standby' — nothing was redeployed"; }
      DRAINED_FROM="$target"
      DRAIN_ACTIVE=1
      # The restore must survive a failed build, a failed compose, a failed
      # readiness gate and a Ctrl-C: leaving production parked on the drain
      # target is exactly the "routine release ends at 100% candidate" outcome
      # this stage exists to make temporary. The signal traps below make sure an
      # interrupt reaches this trap with a FAILURE code, not 0.
      trap '_on_exit "$?"' EXIT
    else
      die "cannot drain public '$target': standby '$standby' is not ready; no public container was redeployed. Refresh it first with: bash deploy/update.sh --pool $standby"
    fi
  elif [ "$target" = "$LIVE_POOL" ] && [ "$ALLOW_IN_PLACE" -eq 1 ]; then
    warn "EXPLICIT --allow-in-place: '$target' is PUBLIC and will 502 during boot; failed readiness may leave the candidate serving traffic."
  elif [ "$target" = "$LIVE_POOL" ]; then
    die "refusing an in-place public deploy without drain; use --drain or explicitly accept the risk with --allow-in-place"
  fi

  deploy_pool "$target"

  restore_pre_drain_route 0
done

# ---------------------------------------------------------------------------
# Drift warning: the rollback target is only useful if it holds the same code
# ---------------------------------------------------------------------------
echo
if [ "$BOTH" -eq 0 ] && [ -n "$LIVE_POOL" ]; then
  standby="$(other_pool "$LIVE_POOL")"
  standby_id="$(identity_of "$standby" 2>/dev/null || true)"
  read -r _standby_arch standby_sha _standby_specialists <<<"$standby_id"
  if [ "$standby_sha" != "$PIN_FULL" ]; then
    warn "Standby pool '$standby' is on '${standby_sha:-<unreachable>}', not the pin $PIN_SHORT."
    warn "It is your rollback target — rolling back would also roll back this fix."
    warn "Bring it level with:  bash deploy/update.sh --pool $standby"
  fi
fi

say "Done. Live at https://rentcompass.co.uk (pool '$LIVE_POOL', commit $PIN_SHORT)"
