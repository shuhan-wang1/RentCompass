#!/usr/bin/env bash
# Redeploy the LIVE pool after a code change. NO sudo needed (except --drain).
#
#   cd /home/shuhan/uk_rent_recommendation
#   git fetch && git checkout <the pinned sha>   # pull new code FIRST
#   bash deploy/update.sh                        # updates whatever the public site serves
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
#   --drain                 avoid the boot-window 502: move the public upstream
#                           to the standby pool, redeploy, move it back.
#                           REQUIRES sudo (nginx -t / reload) and a healthy
#                           standby. Off by default — see the note where it runs.
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
CONF="${UPDATE_CONF:-/etc/nginx/sites-available/rentcompass.co.uk.conf}"
HEALTH_FMT="${UPDATE_HEALTH_FMT:-http://127.0.0.1:%s/ready}"
HEALTH_RETRIES="${UPDATE_HEALTH_RETRIES:-30}"
HEALTH_DELAY="${UPDATE_HEALTH_DELAY:-3}"
UPSTREAM_BLOCK='upstream rentcompass_app'

declare -A PORT=( [legacy]=5001 [fc]=5002 )
declare -A ARCH=( [legacy]=legacy [fc]=fc_loop )
declare -A SERVICE=( [legacy]=app [fc]=app-fc )

say()  { printf '==> %s\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

POOL="auto"; BOTH=0; DRAIN=0; REBUILD_IMAGE=0; FORCE=0; STATUS_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pool)           POOL="${2:-}"; shift 2 ;;
    --both)           BOTH=1; shift ;;
    --drain)          DRAIN=1; shift ;;
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

other_pool() { case "$1" in legacy) echo fc ;; fc) echo legacy ;; esac; }

# "<arch> <sha>" for a pool, or "" when it is not answering /ready with a 200.
identity_of() {
  local url hdrs
  url=$(printf "$HEALTH_FMT" "${PORT[$1]}")
  hdrs=$($CURL_CMD -sS -D- -o /dev/null --max-time 10 "$url" 2>/dev/null) || return 1
  grep -qi '^HTTP/[0-9.]* 200' <<<"$hdrs" || return 1
  printf '%s %s\n' \
    "$(grep -i '^x-agent-arch:'    <<<"$hdrs" | tr -d '\r' | awk '{print $2}')" \
    "$(grep -i '^x-agent-version:' <<<"$hdrs" | tr -d '\r' | awk '{print $2}')"
}

# Block until the pool answers 200, then hold the caller to arch AND full sha.
verify_pool() {
  local pool="$1" want_sha="$2" i=0 got arch sha
  local url; url=$(printf "$HEALTH_FMT" "${PORT[$pool]}")
  say "Waiting for $pool health (the app reloads RAG/FAISS, ~30-40s)..."
  while [ "$i" -lt "$HEALTH_RETRIES" ]; do
    if got=$(identity_of "$pool"); then break; fi
    i=$((i + 1)); sleep "$HEALTH_DELAY"
  done
  [ -n "${got:-}" ] || die "$pool is not answering 200 at $url. Inspect: $COMPOSE_CMD logs --tail=60 ${SERVICE[$pool]}"
  read -r arch sha <<<"$got"
  [ "$arch" = "${ARCH[$pool]}" ] \
    || die "$pool answered as arch '$arch', expected '${ARCH[$pool]}' — the wrong image is on :${PORT[$pool]}"
  # The whole point of the deploy is that the NEW commit is live. A pool that
  # cannot name its commit, or names the old one, is a failed deploy however
  # healthy it looks.
  [ "$sha" = "$want_sha" ] \
    || die "$pool answered with commit '${sha:-<absent>}', expected the pin $want_sha — the new code is NOT live"
  say "$pool is live and self-identifies as $arch $sha ✅"
}

# ---------------------------------------------------------------------------
# Root .env pin rewriting (idempotent; keeps every other line byte-identical)
# ---------------------------------------------------------------------------
# One backup per RUN, taken before the first write — a per-write backup would
# overwrite the pre-run state with a half-rewritten file on the second call.
# `.env*` is gitignored (and tests/test_env_files_cannot_be_committed.py keeps it
# that way), so the copy cannot leak SEARXNG_SECRET into a commit.
_ENV_BACKED_UP=0
set_env_var() {
  local key="$1" value="$2" tmp
  [ -f "$ENV_FILE" ] || die "root env file '$ENV_FILE' is missing — compose needs it"
  if [ "$_ENV_BACKED_UP" -eq 0 ]; then
    cp "$ENV_FILE" "${ENV_FILE}.bak"; _ENV_BACKED_UP=1
  fi
  tmp=$(mktemp)
  if grep -q "^${key}=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" \
      '$0 ~ "^" k "=" { print k "=" v; next } { print }' "$ENV_FILE" > "$tmp"
  else
    cat "$ENV_FILE" > "$tmp"; printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
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
LIVE_PORT="$(upstream_port || true)"
LIVE_POOL="$(pool_of_port "${LIVE_PORT:-}")"
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
PROMPT_VERSION_VALUE="${_declared_prompt_version:-bundle-v1}@$PIN_SHORT"

image_digest() {
  local image="$1" digest
  digest="$($DOCKER_CMD image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' "$image" 2>/dev/null)" \
    || die "cannot inspect the built image '$image'"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "image '$image' returned an invalid digest '${digest:-<empty>}'"
  printf '%s\n' "$digest"
}

# ---------------------------------------------------------------------------
# Resolve the deploy target
# ---------------------------------------------------------------------------
if [ "$BOTH" -eq 1 ]; then
  TARGETS=(legacy fc)
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
FC_IMAGE="uk-rent-agent:canary-fc-loop-${PIN_SHORT}"

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
  build_fc_image
  # Both are `:?`-required by the app-fc service: the fc pool refuses to start on
  # an ambiguous image or an unpinned sha, so write them BEFORE any compose call.
  set_env_var FC_CANARY_IMAGE "$FC_IMAGE"
  set_env_var FC_CANARY_SHA   "$PIN_FULL"
  set_env_var FC_IMAGE_DIGEST "$(image_digest "$FC_IMAGE")"
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
  # Legacy reads its identity from LEGACY_APP_SHA (`:-` defaulted, so a missing
  # value can never block the rollback path). Setting it here is what lets the
  # escape hatch NAME its commit — without it, switch_pool.sh needs
  # --allow-unidentified-target to roll back onto it.
  set_env_var LEGACY_APP_SHA "$PIN_FULL"
  set_env_var PROMPT_VERSION "$PROMPT_VERSION_VALUE"
  set_env_var PROMPT_SCHEMA_SHA "$PROMPT_SCHEMA_SHA_VALUE"
  set_env_var RELEASE_METADATA_REQUIRED "1"
  say "Pinned LEGACY_APP_SHA=$PIN_SHORT in $ENV_FILE"
  # Build from the pin's isolated worktree as well: even gitignored files in the
  # operational checkout can never enter a production build context.
  build_legacy_image
  set_env_var LEGACY_IMAGE_DIGEST "$(image_digest uk-rent-agent:latest)"
  say "Recreating the legacy pool (:${PORT[legacy]}) with its inspected image digest..."
  $COMPOSE_CMD up -d app || die "compose failed to bring up app"
  verify_pool legacy "$PIN_FULL"
}

deploy_pool() { case "$1" in fc) deploy_fc ;; legacy) deploy_legacy ;; esac; }

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
    if [ "$cur" = "${ARCH[$target]} $PIN_FULL" ]; then
      say "$target already serves ${ARCH[$target]} $PIN_SHORT — nothing to do (--force to redeploy)"
      continue
    fi
  fi

  # Recreating a container takes it down for its whole boot window. When that
  # container IS the public upstream, that window is a public 502. --drain moves
  # the upstream to the standby pool first. It is OPT-IN and not the default,
  # because the standby runs the OTHER architecture and may sit on an older
  # commit: draining trades ~40s of downtime for ~40s of different answers, and
  # which of those is worse is an operator's call, not a script's.
  drained_from=""
  if [ "$DRAIN" -eq 1 ] && [ "$target" = "$LIVE_POOL" ]; then
    standby="$(other_pool "$target")"
    if identity_of "$standby" >/dev/null 2>&1; then
      say "Draining public traffic to '$standby' for the redeploy (needs sudo for nginx)"
      $SWITCH_CMD --to "$standby" --allow-unidentified-target \
        $([ "$standby" = "fc" ] && echo --allow-public-fc) \
        || die "could not drain to '$standby' — nothing was redeployed"
      drained_from="$target"
    else
      warn "--drain requested but the '$standby' pool is not healthy; redeploying in place."
      warn "The public site will 502 for the boot window (~30-40s)."
    fi
  elif [ "$target" = "$LIVE_POOL" ]; then
    warn "'$target' is the PUBLIC pool: it will 502 for its boot window (~30-40s). Use --drain to avoid this."
  fi

  deploy_pool "$target"

  if [ -n "$drained_from" ]; then
    say "Returning public traffic to '$drained_from'"
    $SWITCH_CMD --to "$drained_from" --expect-sha "$PIN_FULL" \
      $([ "$drained_from" = "fc" ] && echo --allow-public-fc) \
      || die "REDEPLOY SUCCEEDED BUT THE UPSTREAM IS STILL ON '$(other_pool "$drained_from")'. Fix by hand: $SWITCH_CMD --to $drained_from"
  fi
done

# ---------------------------------------------------------------------------
# Drift warning: the rollback target is only useful if it holds the same code
# ---------------------------------------------------------------------------
echo
if [ "$BOTH" -eq 0 ] && [ -n "$LIVE_POOL" ]; then
  standby="$(other_pool "$LIVE_POOL")"
  standby_sha="$(identity_of "$standby" 2>/dev/null || true)"; standby_sha="${standby_sha#* }"
  if [ "$standby_sha" != "$PIN_FULL" ]; then
    warn "Standby pool '$standby' is on '${standby_sha:-<unreachable>}', not the pin $PIN_SHORT."
    warn "It is your rollback target — rolling back would also roll back this fix."
    warn "Bring it level with:  bash deploy/update.sh --pool $standby"
  fi
fi

say "Done. Live at https://rentcompass.co.uk (pool '$LIVE_POOL', commit $PIN_SHORT)"
